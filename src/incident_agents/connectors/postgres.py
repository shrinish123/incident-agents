"""Postgres connector: read-only queries with an EXPLAIN-based cost estimate.

Point it at a read replica. Three independent safety layers apply on top of the
gateway's SQL linter:
  1. the database role should be read-only,
  2. every session sets default_transaction_read_only = on,
  3. every session sets statement_timeout, so no query can run long.

Source options: host, port, database, ssl (optional), estimate_timeout_s.
Auth: username, password.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from ..querytypes import ConnectorError, ConnectorTimeout, CostEstimate, RawResult
from ..secrets import AuthRejected, Secret
from .base import register_connector


def _plain(value: Any) -> Any:
    return value.reveal() if isinstance(value, Secret) else value


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (timedelta, UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


class PostgresConnector:
    type = "postgres"

    def __init__(self, options: dict[str, Any]) -> None:
        self._host = options.get("host", "localhost")
        self._port = int(options.get("port", 5432))
        self._database = options.get("database", "postgres")
        self._ssl = options.get("ssl")
        self._estimate_timeout_s = float(options.get("estimate_timeout_s", 3))

    async def _connect(self, creds: dict[str, Any], timeout_s: float) -> asyncpg.Connection:
        try:
            return await asyncpg.connect(
                host=self._host,
                port=self._port,
                database=self._database,
                user=_plain(creds.get("username")),
                password=_plain(creds.get("password")),
                ssl=self._ssl,
                timeout=timeout_s,
                server_settings={
                    "default_transaction_read_only": "on",
                    "statement_timeout": str(int(timeout_s * 1000)),
                    "application_name": "incident-agents",
                },
            )
        except (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError):
            raise AuthRejected from None
        except (asyncio.TimeoutError, TimeoutError):
            raise ConnectorTimeout("database connection timed out") from None
        except (OSError, asyncpg.PostgresConnectionError, asyncpg.TooManyConnectionsError, asyncpg.CannotConnectNowError) as exc:
            # Type name only: driver messages can include connection details.
            raise ConnectorError(f"database unreachable: {type(exc).__name__}", retryable=True) from None

    async def estimate(self, params: dict[str, Any], creds: dict[str, Any]) -> CostEstimate | None:
        conn = await self._connect(creds, self._estimate_timeout_s)
        try:
            raw = await conn.fetchval(f"EXPLAIN (FORMAT JSON) {params['sql']}", *params.get("args", []))
        except asyncpg.QueryCanceledError:
            raise ConnectorTimeout("EXPLAIN timed out") from None
        except asyncpg.PostgresError as exc:
            raise ConnectorError(f"{type(exc).__name__}: {exc}") from None
        finally:
            await conn.close()
        plan = (json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"]
        scans = _seq_scans(plan)
        detail = f"{plan.get('Node Type')}, ~{int(plan.get('Plan Rows', 0))} rows"
        if scans:
            detail += "; sequential scan on " + ", ".join(f"{t} (~{r} rows)" for t, r in scans)
        return CostEstimate(amount=float(plan.get("Total Cost", 0.0)), unit="planner_cost", detail=detail)

    async def execute(
        self, params: dict[str, Any], creds: dict[str, Any], *, timeout_s: float, max_rows: int
    ) -> RawResult:
        conn = await self._connect(creds, timeout_s)
        try:
            records = await conn.fetch(params["sql"], *params.get("args", []))
        except asyncpg.QueryCanceledError:
            raise ConnectorTimeout(f"query exceeded the {timeout_s:g}s statement timeout") from None
        except asyncpg.PostgresError as exc:
            raise ConnectorError(f"{type(exc).__name__}: {exc}") from None
        finally:
            await conn.close()
        rows = [{k: _jsonable(v) for k, v in dict(r).items()} for r in records]
        return RawResult(rows=rows[:max_rows], truncated=len(rows) > max_rows)


def _seq_scans(plan: dict[str, Any]) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    if plan.get("Node Type") == "Seq Scan":
        found.append((plan.get("Relation Name", "?"), int(plan.get("Plan Rows", 0))))
    for child in plan.get("Plans", []) or []:
        found.extend(_seq_scans(child))
    return found


@register_connector("postgres")
def _build(options: dict[str, Any]) -> PostgresConnector:
    return PostgresConnector(options)
