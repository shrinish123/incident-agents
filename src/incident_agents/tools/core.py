"""Tool layer: what agents call.

Tools are named for what they find out (`logs_count`, `deploys_list`,
`code_diff`), not for the vendor behind them. A `capabilities` block in the
config binds each capability to a configured source, so the same agent runs
against simulated data, Sumo Logic, New Relic, Postgres or git unchanged:

    capabilities:
      logs:    {source: logs, dialect: sumo, scope: {index: prod, source_category: "app/{service}"}}
      metrics: {source: metrics}
      records: {source: records}
      db:      {source: appdb}
      code:    {source: code}

Every call goes through the query gateway, and every successful call is
turned into evidence records. A tool call never raises: bad arguments, refused
queries, timeouts and partial results all come back as a structured
`ToolResult` the agent can act on.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..evidence import Evidence, EvidenceStore, extract_pivots
from ..gateway import QueryGateway
from ..querytypes import QueryRequest, QueryResult, Status


@dataclass
class Binding:
    source: str
    options: dict[str, Any] = field(default_factory=dict)


def parse_bindings(data: dict[str, Any]) -> dict[str, Binding]:
    out: dict[str, Binding] = {}
    for capability, body in (data.get("capabilities") or {}).items():
        body = dict(body or {})
        out[capability] = Binding(source=body.pop("source"), options=body)
    return out


@dataclass
class ToolResult:
    tool: str
    status: Status
    evidence: list[Evidence] = field(default_factory=list)
    info: Any = None  # reference data that is not evidence (schemas, catalogues)
    note: str = ""
    truncated: bool = False
    violations: list[str] = field(default_factory=list)
    hint: str = ""
    error: str = ""
    query_id: str = ""
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status in (Status.OK, Status.PARTIAL)

    @property
    def evidence_ids(self) -> list[str]:
        return [e.id for e in self.evidence]

    def pivots(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for ev in self.evidence:
            for key, value in ev.pivots.items():
                if value not in out.setdefault(key, []) and len(out[key]) < 8:
                    out[key].append(value)
        return out

    def to_agent(self) -> dict[str, Any]:
        """What the LLM sees. Evidence comes with the IDs it must cite."""
        out: dict[str, Any] = {"status": self.status.value}
        if self.ok:
            if self.evidence:
                out["evidence"] = [e.full() for e in self.evidence]
            elif self.info is None:
                out["evidence"] = []
                out["note"] = self.note or "Nothing matched. An empty result is itself an observation, but it has no evidence ID."
            if self.info is not None:
                out["info"] = self.info
            if self.truncated:
                out["truncated"] = True
                out["hint"] = self.hint or "The result is incomplete. Narrow the query to see the rest, and treat counts as lower bounds."
            pivots = self.pivots()
            if pivots:
                out["pivots"] = pivots
        else:
            for key in ("violations", "hint", "error"):
                if getattr(self, key):
                    out[key] = getattr(self, key)
        if self.note and "note" not in out:
            out["note"] = self.note
        return out


@dataclass
class ToolCallRecord:
    tool: str
    agent: str
    step_id: str | None
    args: dict[str, Any]
    status: str
    evidence_ids: list[str]
    query_id: str
    latency_ms: int


class ToolContext:
    """Passed to a tool handler for one call."""

    def __init__(self, box: "Toolbox", tool: str, step_id: str | None, intent: str) -> None:
        self.box, self.tool, self.step_id, self.intent = box, tool, step_id, intent
        self.gateway, self.store, self.investigation_id = box.gateway, box.store, box.investigation_id

    def binding(self, capability: str) -> Binding:
        return self.box.bindings[capability]

    async def query(self, capability: str, params: dict[str, Any]) -> QueryResult:
        return await self.gateway.execute(
            QueryRequest(
                source=self.binding(capability).source,
                params=params,
                intent=self.intent or self.tool,
                investigation_id=self.investigation_id,
                step_id=self.step_id,
            )
        )

    def add(
        self,
        result: QueryResult,
        kind: str,
        subject: str,
        summary: str,
        data: dict[str, Any],
        *,
        ts: str | None = None,
        pivots: dict[str, str] | None = None,
    ) -> Evidence:
        return self.store.add(
            self.investigation_id, kind, subject, summary, data,
            ts=ts, pivots=pivots if pivots is not None else extract_pivots(data),
            # A capped sample is still made of whole rows. Only a source that returned less than it should have makes evidence partial.
            complete=not result.incomplete,
            source=result.source, tool=self.tool, query_id=result.query_id, step_id=self.step_id,
        )

    def done(self, result: QueryResult, evidence: list[Evidence], **kw: Any) -> ToolResult:
        return ToolResult(
            tool=self.tool, status=result.status, evidence=evidence, truncated=result.truncated,
            hint=result.hint, query_id=result.query_id, latency_ms=result.latency_ms, **kw,
        )

    def failed(self, result: QueryResult) -> ToolResult:
        return ToolResult(
            tool=self.tool, status=result.status, violations=result.violations, hint=result.hint,
            error=result.error, query_id=result.query_id, latency_ms=result.latency_ms,
        )

    def refuse(self, message: str, hint: str = "") -> ToolResult:
        return ToolResult(tool=self.tool, status=Status.REJECTED, violations=[message], hint=hint)


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass
class Tool:
    name: str
    capability: str  # logs, metrics, records, db, code, evidence
    description: str
    properties: dict[str, dict[str, Any]]
    required: list[str]
    handler: Handler

    def spec(self) -> dict[str, Any]:
        """Tool definition in the shape the Claude Messages API takes."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.properties,
                "required": self.required,
                "additionalProperties": False,
            },
        }


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
    "object": (dict,), "array": (list,),
}


def validate_args(tool: Tool, args: Any) -> list[str]:
    if not isinstance(args, dict):
        return ["Arguments must be an object"]
    problems = [f"Missing required argument {name!r}" for name in tool.required if args.get(name) in (None, "")]
    for name, value in args.items():
        prop = tool.properties.get(name)
        if prop is None:
            problems.append(f"Unknown argument {name!r}. Accepted: {', '.join(tool.properties)}")
            continue
        if value is None:
            continue
        expected = _TYPES.get(prop.get("type", ""), ())
        if expected and (not isinstance(value, expected) or (isinstance(value, bool) and bool not in expected)):
            problems.append(f"Argument {name!r} must be of type {prop['type']}")
        elif "enum" in prop and value not in prop["enum"]:
            problems.append(f"Argument {name!r} must be one of: {', '.join(map(str, prop['enum']))}")
    return problems


class Toolbox:
    """The tools available to one investigation."""

    def __init__(
        self,
        gateway: QueryGateway,
        store: EvidenceStore,
        bindings: dict[str, Binding],
        investigation_id: str,
        tools: list[Tool] | None = None,
    ) -> None:
        from .catalog import TOOLS  # late import: the catalogue imports this module

        self.gateway, self.store, self.bindings, self.investigation_id = gateway, store, bindings, investigation_id
        unknown = {b.source for b in bindings.values()} - set(gateway.sources)
        if unknown:
            raise ValueError(f"Capabilities point at unknown sources: {sorted(unknown)}")
        # A tool exists only when its capability is bound to a source.
        self.tools = {t.name: t for t in (tools or TOOLS) if t.capability == "evidence" or t.capability in bindings}
        self.calls: list[ToolCallRecord] = []

    def specs(self, capabilities: list[str] | None = None) -> list[dict[str, Any]]:
        return [t.spec() for t in self.tools.values() if capabilities is None or t.capability in capabilities]

    def names(self, capabilities: list[str] | None = None) -> list[str]:
        return [t.name for t in self.tools.values() if capabilities is None or t.capability in capabilities]

    async def call(
        self,
        name: str,
        args: dict[str, Any],
        *,
        agent: str = "",
        step_id: str | None = None,
        intent: str = "",
        allowed: list[str] | None = None,
    ) -> ToolResult:
        started = time.monotonic()
        tool = self.tools.get(name)
        if tool is None or (allowed is not None and name not in allowed):
            options = allowed if allowed is not None else list(self.tools)
            result = ToolResult(name, Status.REJECTED, violations=[f"Unknown tool {name!r}"], hint=f"Available tools: {', '.join(options)}.")
        else:
            problems = validate_args(tool, args)
            if problems:
                result = ToolResult(name, Status.REJECTED, violations=problems, hint="Fix the arguments and call the tool again.")
            else:
                try:
                    result = await tool.handler(ToolContext(self, name, step_id, intent), args)
                except Exception as exc:  # a tool bug must not take the investigation down
                    result = ToolResult(name, Status.ERROR, error=f"Tool failed internally: {type(exc).__name__}: {str(exc)[:200]}")
        if not result.latency_ms:
            result.latency_ms = int((time.monotonic() - started) * 1000)
        self.calls.append(
            ToolCallRecord(
                tool=name, agent=agent, step_id=step_id, args=args if isinstance(args, dict) else {},
                status=result.status.value, evidence_ids=result.evidence_ids, query_id=result.query_id,
                latency_ms=result.latency_ms,
            )
        )
        return result
