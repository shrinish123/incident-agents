"""Runtime controls: budget ledger, circuit breaker, rate limiter, cache, audit log."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# --- Budget ledger -------------------------------------------------------------


@dataclass
class _InvestigationSpend:
    queries: int = 0
    rejections: int = 0
    spent: dict[str, float] = field(default_factory=dict)
    reserved: dict[str, float] = field(default_factory=dict)


class BudgetLedger:
    """Per-investigation spend. Methods are synchronous, so check-and-reserve is
    atomic under asyncio and parallel threads cannot overspend together."""

    def __init__(self, max_queries: int = 80, max_rejections: int = 30) -> None:
        self.max_queries = max_queries
        self.max_rejections = max_rejections
        self._inv: dict[str, _InvestigationSpend] = {}

    def _get(self, inv: str) -> _InvestigationSpend:
        return self._inv.setdefault(inv, _InvestigationSpend())

    def exhausted(self, inv: str) -> str | None:
        s = self._get(inv)
        if s.queries >= self.max_queries:
            return f"Query limit reached ({self.max_queries} per investigation)"
        if s.rejections >= self.max_rejections:
            return f"Too many rejected queries ({self.max_rejections}); stop and report what is known"
        return None

    def remaining(self, inv: str, source: str, budget: float | None) -> float | None:
        if budget is None:
            return None
        s = self._get(inv)
        return budget - s.spent.get(source, 0.0) - s.reserved.get(source, 0.0)

    def reserve(self, inv: str, source: str, amount: float, budget: float | None) -> bool:
        s = self._get(inv)
        if budget is not None and amount > self.remaining(inv, source, budget) + 1e-9:
            return False
        s.reserved[source] = s.reserved.get(source, 0.0) + amount
        s.queries += 1
        return True

    def settle(self, inv: str, source: str, reserved: float, actual: float) -> None:
        s = self._get(inv)
        s.reserved[source] = max(0.0, s.reserved.get(source, 0.0) - reserved)
        s.spent[source] = s.spent.get(source, 0.0) + actual

    def note_rejection(self, inv: str) -> None:
        self._get(inv).rejections += 1

    def snapshot(self, inv: str) -> dict[str, Any]:
        s = self._get(inv)
        return {"queries": s.queries, "rejections": s.rejections, "spent": dict(s.spent)}


# --- Circuit breaker -----------------------------------------------------------


class CircuitBreaker:
    """Opens after consecutive failures; lets one probe through after cooldown.

    An auth failure opens it at once for a longer period: retrying bad
    credentials is how accounts get locked.
    """

    def __init__(self, threshold: int, cooldown_s: float, auth_cooldown_s: float, clock: Callable[[], float]) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.auth_cooldown_s = auth_cooldown_s
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0
        self.reason = ""

    def blocked(self) -> str | None:
        if self._clock() < self._open_until:
            return self.reason
        return None

    def success(self) -> None:
        self._failures = 0
        self._open_until = 0.0
        self.reason = ""

    def failure(self, reason: str) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._open_until = self._clock() + self.cooldown_s
            self.reason = f"{reason} ({self._failures} consecutive failures)"

    def trip_auth(self) -> None:
        self._open_until = self._clock() + self.auth_cooldown_s
        self.reason = "auth: credentials rejected after refresh"


# --- Rate and concurrency limiter -------------------------------------------------


class Limiter:
    def __init__(
        self,
        max_concurrency: int,
        rate_per_min: float,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._sem = asyncio.Semaphore(max(1, max_concurrency))
        self._interval = 60.0 / rate_per_min if rate_per_min > 0 else 0.0
        self._next = 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self):
        async with self._sem:
            if self._interval:
                async with self._lock:
                    now = self._clock()
                    wait = self._next - now
                    self._next = max(now, self._next) + self._interval
                if wait > 0:
                    await self._sleep(wait)
            yield


# --- Result cache ---------------------------------------------------------------


class TTLCache:
    def __init__(self, clock: Callable[[], float], max_entries: int = 512) -> None:
        self._clock = clock
        self._max = max_entries
        self._data: dict[str, tuple[float, Any]] = {}

    @staticmethod
    def key(source: str, params: dict[str, Any]) -> str:
        return source + "|" + json.dumps(params, sort_keys=True, default=str)

    def get(self, key: str) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        if hit[0] <= self._clock():
            del self._data[key]
            return None
        return hit[1]

    def put(self, key: str, value: Any, ttl_s: float) -> None:
        if ttl_s <= 0:
            return
        if len(self._data) >= self._max:
            self._data.pop(next(iter(self._data)))
        self._data[key] = (self._clock() + ttl_s, value)


# --- Audit log ------------------------------------------------------------------


@dataclass
class AuditRecord:
    ts: float
    query_id: str
    investigation_id: str
    step_id: str | None
    source: str
    kind: str
    intent: str
    params: dict[str, Any]
    status: str
    violations: list[str]
    est_cost: float | None
    cost: float | None
    cost_unit: str
    rows: int
    attempts: int
    latency_ms: int
    cached: bool
    error: str = ""


class AuditLog:
    """Every query decision, allowed or not. Appends JSON lines when given a path."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.records: list[AuditRecord] = []
        self._path = Path(path) if path else None
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: AuditRecord) -> None:
        self.records.append(record)
        if self._path:
            with self._path.open("a") as fh:
                fh.write(json.dumps(asdict(record), default=str) + "\n")


def now() -> float:
    return time.time()
