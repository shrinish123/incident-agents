"""Request and result types shared by the gateway, connectors and tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    OK = "ok"
    PARTIAL = "partial"  # rows returned but capped or incomplete
    REJECTED = "rejected"  # blocked before execution; see violations and hint
    TIMEOUT = "timeout"  # retries exhausted
    ERROR = "error"
    UNAVAILABLE = "unavailable"  # circuit open (repeated failures or auth)
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass
class QueryRequest:
    source: str
    params: dict[str, Any]
    intent: str = ""  # why the agent wants this; goes to the audit log
    investigation_id: str = "adhoc"
    step_id: str | None = None


@dataclass
class CostEstimate:
    amount: float
    unit: str  # e.g. gb_scanned, planner_cost, queries
    detail: str = ""


@dataclass
class RawResult:
    """What a connector returns."""

    rows: list[dict[str, Any]]
    truncated: bool = False
    incomplete: bool = False  # the source itself returned less than it should have (not a row cap); a retry may do better
    cost: float | None = None  # actual cost in the source's unit, when known
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class QueryResult:
    """What the gateway returns. Always structured, never raises to agents."""

    status: Status
    source: str
    query_id: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    incomplete: bool = False  # the source returned less than it should have; a row cap alone does not set this
    violations: list[str] = field(default_factory=list)
    hint: str = ""
    error: str = ""
    est_cost: float | None = None
    cost: float | None = None
    cost_unit: str = ""
    budget_remaining: float | None = None
    cached: bool = False
    attempts: int = 0
    latency_ms: int = 0
    executed_params: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (Status.OK, Status.PARTIAL)

    def to_agent(self) -> dict[str, Any]:
        """Compact view for an LLM: only the fields that affect its next move."""
        out: dict[str, Any] = {"status": self.status.value, "source": self.source}
        if self.ok:
            out["rows"] = self.rows
            out["row_count"] = len(self.rows)
            if self.truncated:
                out["truncated"] = True
                out["hint"] = self.hint or "Result was capped. Narrow the query to see the rest."
        else:
            if self.violations:
                out["violations"] = self.violations
            if self.hint:
                out["hint"] = self.hint
            if self.error:
                out["error"] = self.error
        if self.budget_remaining is not None:
            out["budget_remaining"] = round(self.budget_remaining, 3)
            out["cost_unit"] = self.cost_unit
        return out


class ConnectorError(Exception):
    """A source-side failure. `retryable` marks transient ones (5xx, resets)."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ConnectorTimeout(ConnectorError):
    def __init__(self, message: str = "source timed out") -> None:
        super().__init__(message, retryable=True)
