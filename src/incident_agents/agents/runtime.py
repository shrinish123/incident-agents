"""Agent runtime: one bounded tool-calling loop, shared by every agent.

An agent is a system prompt, an allow-list of tools, an output schema and
limits. The loop:

  model turn -> run requested tools in parallel -> return results -> ...
  -> final answer as JSON matching the schema

Bounds and recovery, all local to the loop:
  - a tool-call budget: past it, tool calls are answered with "budget used
    up, answer now" instead of being run
  - a turn limit
  - one retry on a transient model error, one repair round when the final
    answer is not valid JSON or is cut off
  - tool failures never raise: the model sees the structured failure and
    chooses another route

A run always returns an `AgentRun`; `ok=False` carries the reason so the
orchestrator can retry the step, route around it or record the gap.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..llm import LLM, BudgetExceeded, LLMError, LLMRequest
from ..tools import Toolbox, ToolResult

MAX_RESULT_CHARS = 14_000


@dataclass
class AgentSpec:
    name: str
    role: str  # key into the LLM gateway's role table
    system: str
    schema: dict[str, Any]
    tools: list[str] = field(default_factory=list)  # tool names; empty means no tools
    max_tool_calls: int = 10
    max_turns: int = 14


@dataclass
class AgentRun:
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    tool_calls: int = 0
    turns: int = 0
    evidence_ids: list[str] = field(default_factory=list)  # evidence this run produced or re-read
    tool_failures: list[str] = field(default_factory=list)


def render_result(result: ToolResult, limit: int = MAX_RESULT_CHARS) -> str:
    """Tool result as JSON for the model. Oversized results keep every evidence
    ID but fall back to digests for the tail, and say so."""
    payload = result.to_agent()
    text = json.dumps(payload, default=str)
    if len(text) <= limit or not payload.get("evidence"):
        return text
    records = payload["evidence"]
    keep = len(records)
    while keep > 0:
        keep -= max(1, keep // 4)
        shown = records[:keep] + [{k: v for k, v in r.items() if k != "data"} for r in records[keep:]]
        trimmed = {**payload, "evidence": shown, "note": f"Result was large: the last {len(records) - keep} records are shown as summaries. Call evidence_get with an ID for the full record."}
        text = json.dumps(trimmed, default=str)
        if len(text) <= limit:
            return text
    if len(records) == 1:
        # One oversized record (a long table or file): keep its head and say what was cut.
        record = dict(records[0])
        body = json.dumps(record.get("data"), default=str)
        record["data"] = body[: limit - 600] + "...[cut]"
        note = "This record is large and was cut for display. Narrow the query (fewer groups, a shorter window, a line range) to see the rest."
        return json.dumps({**payload, "evidence": [record], "note": note}, default=str)
    return text


def parse_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        try:
            out = json.loads(match[0])
            return out if isinstance(out, dict) else None
        except ValueError:
            return None
    return None


def missing_keys(schema: dict[str, Any], output: dict[str, Any]) -> list[str]:
    return [k for k in schema.get("required", []) if k not in output]


async def run_agent(
    spec: AgentSpec,
    llm: LLM,
    toolbox: Toolbox | None,
    prompt: str,
    *,
    step_id: str | None = None,
    intent: str = "",
) -> AgentRun:
    run = AgentRun(ok=False)
    allowed = [t for t in spec.tools if toolbox is not None and t in toolbox.tools]
    tool_specs = [toolbox.tools[t].spec() for t in allowed] if toolbox is not None else []
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    repaired = False
    retried = False

    while run.turns < spec.max_turns:
        run.turns += 1
        try:
            resp = await llm.complete(
                LLMRequest(agent=spec.name, role=spec.role, system=spec.system, messages=messages, tools=tool_specs or None, schema=spec.schema)
            )
        except BudgetExceeded as exc:
            run.error = str(exc)
            return run
        except LLMError as exc:
            if exc.retryable and not retried:
                retried = True
                run.turns -= 1
                await asyncio.sleep(0)
                continue
            run.error = f"Model call failed: {exc}"
            return run

        if resp.tool_calls:
            messages.append({"role": "assistant", "content": resp.content})
            budget_left = spec.max_tool_calls - run.tool_calls
            runnable, refused = resp.tool_calls[: max(0, budget_left)], resp.tool_calls[max(0, budget_left):]
            results = await asyncio.gather(
                *(toolbox.call(c.name, c.input, agent=spec.name, step_id=step_id, intent=intent, allowed=allowed) for c in runnable)
            ) if toolbox is not None and runnable else []
            run.tool_calls += len(runnable)
            blocks: list[dict[str, Any]] = []
            for call, result in zip(runnable, results):
                run.evidence_ids += result.evidence_ids
                if not result.ok:
                    run.tool_failures.append(f"{call.name}: {result.status.value}")
                blocks.append({"type": "tool_result", "tool_use_id": call.id, "content": render_result(result), **({} if result.ok else {"is_error": True})})
            for call in refused:
                blocks.append({"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                               "content": json.dumps({"status": "budget_exhausted", "hint": "Your tool-call budget for this task is used up."})})
            if run.tool_calls >= spec.max_tool_calls:
                blocks.append({"type": "text", "text": "Tool-call budget used up. Give your final answer now from the evidence you have, and say what you could not check."})
            messages.append({"role": "user", "content": blocks})
            continue

        output = parse_json(resp.text) if resp.stop_reason != "max_tokens" else None
        problem = ""
        if output is None:
            problem = "Your answer was cut off. Reply again, shorter." if resp.stop_reason == "max_tokens" else "Your answer was not a JSON object."
        elif missing_keys(spec.schema, output):
            problem = f"Your answer is missing required fields: {', '.join(missing_keys(spec.schema, output))}."
        if not problem:
            run.ok, run.output = True, output
            run.evidence_ids = list(dict.fromkeys(run.evidence_ids))
            return run
        if repaired:
            run.error = f"No usable answer after a repair round: {problem}"
            return run
        repaired = True
        if resp.content:
            messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": f"{problem} Reply with only the JSON object described in your instructions."})

    run.error = f"Turn limit reached ({spec.max_turns}) without a final answer"
    return run
