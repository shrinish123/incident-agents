"""Simulated sources for evaluation scenarios and local development.

They read a scenario's data directory and accept the same params as the real
connectors of the same kind, so agents, policies and cost limits behave the
same against simulated and real systems.

  sim_logs     kind log_search  data: logs.jsonl      (same params as Sumo)
  sim_metrics  kind generic     data: metrics.json    {series: [{service, metric, unit, points: [[ts, value]]}]}
  sim_records  kind generic     data: <collection>.jsonl for each collection
                                (deploys, config_changes, topology, traces, incidents...)

Each takes options.data_dir and an optional options.faults list that injects
tool failures deterministically:

  faults:
    - {on_calls: [1], fault: timeout}                 # first call hangs
    - {match: "idempotency", fault: error, times: 1}  # first matching call errors
    - {on_calls: [2, 3], fault: partial, keep: 0.4}   # returns 40% of rows, marked truncated
    - {fault: auth, times: 2}                         # credentials rejected twice
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..querytypes import ConnectorError, CostEstimate, RawResult
from ..secrets import AuthRejected
from ..timeutil import parse_ts
from .base import register_connector

# --- Fault injection ---------------------------------------------------------------


class FaultPlan:
    def __init__(self, faults: list[dict[str, Any]] | None) -> None:
        self._faults = [dict(f, _used=0) for f in (faults or [])]
        self.calls = 0

    def next(self, params: dict[str, Any]) -> dict[str, Any] | None:
        self.calls += 1
        text = json.dumps(params, default=str).lower()
        for f in self._faults:
            if "on_calls" in f and self.calls not in f["on_calls"]:
                continue
            if "match" in f and str(f["match"]).lower() not in text:
                continue
            if "times" in f and f["_used"] >= int(f["times"]):
                continue
            f["_used"] += 1
            return f
        return None


async def _apply_fault(fault: dict[str, Any] | None, timeout_s: float) -> None:
    if fault is None:
        return
    kind = fault.get("fault")
    if kind == "timeout":
        await asyncio.sleep(timeout_s + 5)  # the gateway's timeout fires first
    elif kind == "error":
        raise ConnectorError(fault.get("message", "simulated source error: 503 backend unavailable"), retryable=bool(fault.get("retryable", True)))
    elif kind == "auth":
        raise AuthRejected


def _partial(rows: list[dict[str, Any]], fault: dict[str, Any] | None) -> tuple[list[dict[str, Any]], bool]:
    if fault is None or fault.get("fault") != "partial" or not rows:
        return rows, False
    keep = max(1, int(len(rows) * float(fault.get("keep", 0.5))))
    return rows[:keep], keep < len(rows)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _in_window(ts: Any, start: datetime | None, end: datetime | None) -> bool:
    dt = parse_ts(ts)
    if dt is None:
        return start is None and end is None
    return (start is None or dt >= start) and (end is None or dt < end)


# --- Logs ------------------------------------------------------------------------

_STAGE_COUNT = re.compile(r"^count(?:\s+by\s+(?P<by>.+))?$", re.I)
_STAGE_SLICE = re.compile(r"^timeslice\s+(?P<n>\d+)\s*(?P<u>s|m|h)$", re.I)
_STAGE_LIMIT = re.compile(r"^(?:limit|top)\s+(?P<n>\d+)$", re.I)


def run_log_query(records: list[dict[str, Any]], query: str) -> tuple[list[dict[str, Any]], bool]:
    """A small search language, close to what log tools accept:

        timeout route=/transactions level!=debug "connection reset"
        error | count by route, status
        error | timeslice 5m | count by _timeslice

    Space-separated terms are ANDed; `or` between groups is supported.
    Returns (rows, is_aggregate).
    """
    head, *stages = [s.strip() for s in query.split("|")]
    groups: list[list[str]] = [[]]
    for token in shlex.split(head) if head else []:
        if token.lower() == "or":
            groups.append([])
        elif token.lower() != "and":
            groups[-1].append(token)
    groups = [g for g in groups if g] or [[]]

    def matches(rec: dict[str, Any], token: str) -> bool:
        m = re.match(r"^([A-Za-z_][\w.]*)(!=|=)(.*)$", token)
        if m:
            actual = str(rec.get(m[1], "")).lower()
            wanted = m[3].lower()
            hit = actual == wanted or (wanted.endswith("*") and actual.startswith(wanted[:-1]))
            return hit if m[2] == "=" else not hit
        if token == "*":
            return True
        blob = " ".join(str(v) for v in rec.values()).lower()
        return token.lower() in blob

    rows = [r for r in records if any(all(matches(r, t) for t in g) for g in groups)]
    aggregate = False
    for stage in stages:
        if m := _STAGE_SLICE.match(stage):
            seconds = int(m["n"]) * {"s": 1, "m": 60, "h": 3600}[m["u"].lower()]
            sliced = []
            for r in rows:
                dt = parse_ts(r.get("ts"))
                if dt is None:
                    continue
                bucket = int(dt.timestamp()) // seconds * seconds
                sliced.append({**r, "_timeslice": datetime.fromtimestamp(bucket, timezone.utc).isoformat().replace("+00:00", "Z")})
            rows = sliced
        elif m := _STAGE_COUNT.match(stage):
            aggregate = True
            by = [f.strip() for f in (m["by"] or "").split(",") if f.strip()]
            counts = Counter(tuple(str(r.get(f, "")) for f in by) for r in rows)
            rows = [{**dict(zip(by, key)), "_count": n} for key, n in counts.items()]
            if "_timeslice" in by:
                rows.sort(key=lambda r: r["_timeslice"])
            else:
                rows.sort(key=lambda r: -r["_count"])
        elif m := _STAGE_LIMIT.match(stage):
            rows = rows[: int(m["n"])]
        elif stage:
            raise ConnectorError(
                f"Unsupported search stage {stage!r}. Supported: count, count by <fields>, timeslice <n>m, limit <n>"
            )
    return rows, aggregate


class SimLogsConnector:
    type = "sim_logs"

    def __init__(self, options: dict[str, Any]) -> None:
        self._records = _load_jsonl(Path(options["data_dir"]) / options.get("file", "logs.jsonl"))
        self._faults = FaultPlan(options.get("faults"))

    async def estimate(self, params: dict[str, Any], creds: dict[str, Any]) -> CostEstimate | None:
        return None

    async def execute(
        self, params: dict[str, Any], creds: dict[str, Any], *, timeout_s: float, max_rows: int
    ) -> RawResult:
        fault = self._faults.next(params)
        await _apply_fault(fault, timeout_s)
        start, end = parse_ts(params.get("start")), parse_ts(params.get("end"))
        scope = params.get("scope") or {}
        in_scope = [
            r
            for r in self._records
            if _in_window(r.get("ts"), start, end) and all(str(r.get(k, v)) == str(v) for k, v in scope.items())
        ]
        rows, aggregate = run_log_query(in_scope, str(params.get("query") or ""))
        if params.get("mode", "aggregate") == "aggregate" and not aggregate:
            raise ConnectorError("mode is aggregate but the query has no count stage. Add '| count' or '| count by <field>'.")
        if not aggregate:
            rows.sort(key=lambda r: str(r.get("ts")))
        total = len(rows)
        limit = min(int(params.get("limit") or max_rows), max_rows)
        rows, cut = _partial(rows[:limit], fault)
        return RawResult(rows=rows, truncated=cut or total > limit, incomplete=cut, meta={"total": total})


# --- Metrics ---------------------------------------------------------------------

_AGGS = {"avg": lambda v: sum(v) / len(v), "max": max, "min": min, "sum": sum, "last": lambda v: v[-1]}


class SimMetricsConnector:
    """params: {service, metric, start, end, step_s?, agg?} or {op: list, service?}"""

    type = "sim_metrics"

    def __init__(self, options: dict[str, Any]) -> None:
        path = Path(options["data_dir"]) / options.get("file", "metrics.json")
        self._series = (json.loads(path.read_text()) if path.exists() else {}).get("series", [])
        self._faults = FaultPlan(options.get("faults"))

    async def estimate(self, params: dict[str, Any], creds: dict[str, Any]) -> CostEstimate | None:
        return None

    async def execute(
        self, params: dict[str, Any], creds: dict[str, Any], *, timeout_s: float, max_rows: int
    ) -> RawResult:
        fault = self._faults.next(params)
        await _apply_fault(fault, timeout_s)
        if params.get("op") == "list":
            rows = [
                {"service": s["service"], "metric": s["metric"], "unit": s.get("unit", "")}
                for s in self._series
                if not params.get("service") or s["service"] == params["service"]
            ]
            return RawResult(rows=rows[:max_rows], truncated=len(rows) > max_rows)

        service, metric = params.get("service"), params.get("metric")
        series = next((s for s in self._series if s["service"] == service and s["metric"] == metric), None)
        if series is None:
            known = sorted({s["metric"] for s in self._series if s["service"] == service})
            raise ConnectorError(
                f"No metric {metric!r} for service {service!r}. "
                + (f"Metrics for {service}: {', '.join(known)}." if known else "Use op: list to see what exists.")
            )
        start, end = parse_ts(params.get("start")), parse_ts(params.get("end"))
        agg_name = str(params.get("agg", "avg"))
        if agg_name not in _AGGS:
            raise ConnectorError(f"agg must be one of: {', '.join(_AGGS)}")
        step = max(1, int(params.get("step_s") or 60))
        buckets: dict[int, list[float]] = {}
        for ts, value in series["points"]:
            dt = parse_ts(ts)
            if dt is not None and _in_window(dt, start, end):
                buckets.setdefault(int(dt.timestamp()) // step * step, []).append(float(value))
        rows = [
            {
                "ts": datetime.fromtimestamp(b, timezone.utc).isoformat().replace("+00:00", "Z"),
                "value": round(_AGGS[agg_name](v), 4),
            }
            for b, v in sorted(buckets.items())
        ]
        total = len(rows)
        rows, cut = _partial(rows[:max_rows], fault)
        return RawResult(
            rows=rows,
            truncated=cut or total > max_rows,
            incomplete=cut,
            meta={"service": service, "metric": metric, "unit": series.get("unit", ""), "agg": agg_name, "step_s": step},
        )


# --- Records (deploys, config changes, topology, traces, past incidents) --------------


class SimRecordsConnector:
    """params: {collection, filters?: {field: value}, text?, start?, end?, limit?} or {op: list}"""

    type = "sim_records"

    def __init__(self, options: dict[str, Any]) -> None:
        self._dir = Path(options["data_dir"])
        self._collections: list[str] = options.get("collections") or sorted(p.stem for p in self._dir.glob("*.jsonl") if p.stem != "logs")
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._faults = FaultPlan(options.get("faults"))

    async def estimate(self, params: dict[str, Any], creds: dict[str, Any]) -> CostEstimate | None:
        return None

    async def execute(
        self, params: dict[str, Any], creds: dict[str, Any], *, timeout_s: float, max_rows: int
    ) -> RawResult:
        fault = self._faults.next(params)
        await _apply_fault(fault, timeout_s)
        if params.get("op") == "list":
            return RawResult(rows=[{"collection": c} for c in self._collections])
        name = params.get("collection")
        if name not in self._collections:
            raise ConnectorError(f"Unknown collection {name!r}. Available: {', '.join(self._collections)}")
        if name not in self._cache:
            self._cache[name] = _load_jsonl(self._dir / f"{name}.jsonl")
        start, end = parse_ts(params.get("start")), parse_ts(params.get("end"))
        filters = params.get("filters") or {}
        text = str(params.get("text") or "").lower()
        rows = []
        for rec in self._cache[name]:
            if (start or end) and "ts" in rec and not _in_window(rec["ts"], start, end):
                continue
            if any(str(rec.get(k)) != str(v) for k, v in filters.items()):
                continue
            if text and text not in json.dumps(rec, default=str).lower():
                continue
            rows.append(rec)
        total = len(rows)
        limit = min(int(params.get("limit") or max_rows), max_rows)
        rows, cut = _partial(rows[:limit], fault)
        return RawResult(rows=rows, truncated=cut or total > limit, incomplete=cut, meta={"collection": name, "total": total})


register_connector("sim_logs")(lambda options: SimLogsConnector(options))
register_connector("sim_metrics")(lambda options: SimMetricsConnector(options))
register_connector("sim_records")(lambda options: SimRecordsConnector(options))
