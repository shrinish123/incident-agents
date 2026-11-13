"""Simulated SQL source: a SQLite file queried with Postgres-dialect SQL.

Stands in for the application database in evaluation scenarios, so they run
with no database server. The gateway lints SQL as Postgres (same policy as the
real `postgres` connector); this connector rewrites the linted statement for
SQLite and runs it on a read-only connection.

Timestamps in the data are ISO 8601 UTC text (`2026-11-10T08:00:00Z`), which
sorts and compares correctly as text. The Postgres time functions agents
commonly reach for are mapped onto that representation:

  now()                     -> the scenario clock (options.now), not wall time
  ts +/- interval '1 hour'  -> ts_shift(ts, +/-3600)
  date_trunc('minute', ts)  -> truncated ISO text
  x::timestamptz            -> x (casts to time types are dropped)
  extract(epoch from ts)    -> seconds since 1970

Options: data_dir, file (default app.db), now (ISO 8601), faults (see simulated.py).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from ..querytypes import ConnectorError, ConnectorTimeout, CostEstimate, RawResult
from ..timeutil import parse_ts
from .base import register_connector
from .simulated import FaultPlan, _apply_fault, _partial

_TIME_TYPES = {"TIMESTAMP", "TIMESTAMPTZ", "TIMESTAMPLTZ", "TIMESTAMPNTZ", "DATETIME"}
_UNIT_S = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}
_TRUNC = {"minute": 16, "hour": 13, "day": 10, "month": 7, "year": 4}
_PAD = {16: ":00Z", 13: ":00:00Z", 10: "T00:00:00Z", 7: "-01T00:00:00Z", 4: "-01-01T00:00:00Z"}


def _iso(dt: Any) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_trunc(unit: Any, value: Any) -> Any:
    if value is None:
        return None
    width = _TRUNC.get(str(unit).lower().rstrip("s"))
    text = str(value)
    if width is None:
        return text
    if width == 16:  # minute
        return text[:16] + ":00Z"
    return text[:width] + _PAD[width]


def _ts_shift(value: Any, seconds: Any) -> Any:
    dt = parse_ts(value) if value is not None else None
    return _iso(dt + timedelta(seconds=float(seconds))) if dt else None


def _epoch(value: Any) -> Any:
    dt = parse_ts(value) if value is not None else None
    return dt.timestamp() if dt else None


def _split_part(value: Any, sep: Any, n: Any) -> Any:
    if value is None:
        return None
    parts = str(value).split(str(sep))
    return parts[int(n) - 1] if 0 < int(n) <= len(parts) else ""


def _interval_seconds(node: exp.Interval) -> float | None:
    text = str(node.this.name if isinstance(node.this, exp.Literal) else node.this.sql()).strip().lower()
    unit = str(node.args["unit"].name if node.args.get("unit") is not None else "").lower()
    if not unit:  # INTERVAL '90 minutes'
        amount, _, unit = text.partition(" ")
    else:
        amount = text
    unit = unit.strip().rstrip("s")
    try:
        return float(amount) * _UNIT_S[unit]
    except (ValueError, KeyError):
        return None


def to_sqlite(sql: str, now: str | None = None) -> str:
    """Rewrite one Postgres SELECT so SQLite can run it against ISO-text timestamps."""
    tree = sqlglot.parse_one(sql, read="postgres")

    def inner(node: exp.Expression) -> exp.Expression:
        # transform() does not descend into a node it has just replaced
        return node.transform(rewrite)

    def rewrite(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.CurrentTimestamp) and now:
            return exp.Literal.string(now)
        if isinstance(node, exp.Cast):
            kind = node.to.this.name if hasattr(node.to.this, "name") else str(node.to.this)
            if kind in _TIME_TYPES:
                return inner(node.this)
            if kind == "DATE":
                return exp.func("substr", inner(node.this), exp.Literal.number(1), exp.Literal.number(10))
        if isinstance(node, (exp.Add, exp.Sub)) and isinstance(node.expression, exp.Interval):
            seconds = _interval_seconds(node.expression)
            if seconds is not None:
                signed = seconds if isinstance(node, exp.Add) else -seconds
                return exp.func("ts_shift", inner(node.this), exp.Literal.number(signed))
        if isinstance(node, (exp.TimestampTrunc, exp.DateTrunc)):
            unit = node.args.get("unit")
            return exp.func("iso_trunc", exp.Literal.string(unit.name if unit is not None else "minute"), inner(node.this))
        if isinstance(node, exp.Extract) and node.this.name.upper() == "EPOCH":
            return exp.func("ts_epoch", inner(node.expression))
        return node

    return tree.transform(rewrite).sql(dialect="sqlite")


class SimSqlConnector:
    type = "sim_sql"

    def __init__(self, options: dict[str, Any]) -> None:
        self._path = Path(options["data_dir"]) / options.get("file", "app.db")
        self._now = options.get("now")
        self._faults = FaultPlan(options.get("faults"))

    async def estimate(self, params: dict[str, Any], creds: dict[str, Any]) -> CostEstimate | None:
        return None

    def _run(self, sql: str, timeout_s: float, fetch: int) -> list[dict[str, Any]]:
        if not self._path.exists():
            raise ConnectorError(f"database file {self._path.name} is missing")
        conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True, timeout=timeout_s)
        try:
            conn.row_factory = sqlite3.Row
            conn.create_function("iso_trunc", 2, _date_trunc, deterministic=True)
            conn.create_function("ts_shift", 2, _ts_shift, deterministic=True)
            conn.create_function("ts_epoch", 1, _epoch, deterministic=True)
            conn.create_function("split_part", 3, _split_part, deterministic=True)
            # Abort statements that run past the timeout (checked every N VM steps).
            stop_at = time.monotonic() + timeout_s
            conn.set_progress_handler(lambda: 1 if time.monotonic() > stop_at else 0, 20_000)
            cursor = conn.execute(sql)
            return [dict(r) for r in cursor.fetchmany(fetch)]
        finally:
            conn.close()

    async def execute(
        self, params: dict[str, Any], creds: dict[str, Any], *, timeout_s: float, max_rows: int
    ) -> RawResult:
        fault = self._faults.next(params)
        await _apply_fault(fault, timeout_s)
        try:
            sql = to_sqlite(params["sql"], self._now)
        except SqlglotError as exc:
            raise ConnectorError(f"SQL could not be translated: {str(exc).splitlines()[0][:200]}") from None
        try:
            rows = await asyncio.to_thread(self._run, sql, timeout_s, max_rows + 1)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise ConnectorTimeout(f"query exceeded the {timeout_s:g}s statement timeout") from None
            raise ConnectorError(f"SQL error: {exc}") from None
        except sqlite3.Error as exc:
            raise ConnectorError(f"SQL error: {exc}") from None
        total = len(rows)
        rows, cut = _partial(rows[:max_rows], fault)
        return RawResult(rows=rows, truncated=cut or total > max_rows, incomplete=cut)


@register_connector("sim_sql")
def _build(options: dict[str, Any]) -> SimSqlConnector:
    return SimSqlConnector(options)
