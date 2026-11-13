"""Sumo Logic connector, using the Search Job API.

Flow per query: create a search job, poll until it has gathered results,
fetch records (aggregate mode) or messages (raw mode), then delete the job.
The job is deleted even when the gateway cancels the call on timeout, so
abandoned searches do not keep scanning data.

Source options:
  endpoint         API base for your deployment, e.g. https://api.us2.sumologic.com/api
  scope_fields     maps scope keys to Sumo metadata fields
                   (default: index -> _index, source_category -> _sourceCategory)
  poll_interval_s  default 1
Auth: access_id, access_key.

Params (already checked by the log_search linter):
  query, scope{...}, start, end (ISO 8601), mode (aggregate|raw), limit
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..querytypes import ConnectorError, ConnectorTimeout, CostEstimate, RawResult
from ..secrets import AuthRejected, Secret
from ..timeutil import parse_ts
from .base import register_connector

_DEFAULT_SCOPE_FIELDS = {"index": "_index", "source_category": "_sourceCategory"}
_NOISY_FIELDS = {"_blockid", "_messageid", "_size", "_collectorid", "_sourceid", "_format", "_messagecount", "_receipttime"}
_DONE = "DONE GATHERING RESULTS"


def build_query(params: dict[str, Any], scope_fields: dict[str, str]) -> str:
    parts = []
    for key, value in (params.get("scope") or {}).items():
        field = scope_fields.get(key, key)
        escaped = str(value).replace('"', '\\"')
        parts.append(f'{field}="{escaped}"')
    parts.append(str(params.get("query") or "").strip())
    return " ".join(p for p in parts if p)


class SumoConnector:
    type = "sumo"

    def __init__(
        self,
        options: dict[str, Any],
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._endpoint = str(options.get("endpoint", "https://api.sumologic.com/api")).rstrip("/")
        self._scope_fields = {**_DEFAULT_SCOPE_FIELDS, **(options.get("scope_fields") or {})}
        self._poll_s = float(options.get("poll_interval_s", 1))
        self._client = client or httpx.AsyncClient()  # keeps the cookies the API expects across calls
        self._sleep = sleep

    async def estimate(self, params: dict[str, Any], creds: dict[str, Any]) -> CostEstimate | None:
        return None  # priced by policy: measured GB per hour x window

    async def _request(self, method: str, path: str, auth: httpx.BasicAuth, timeout_s: float, **kw: Any) -> httpx.Response:
        try:
            resp = await self._client.request(method, f"{self._endpoint}{path}", auth=auth, timeout=timeout_s, **kw)
        except httpx.TimeoutException:
            raise ConnectorTimeout("Sumo Logic did not answer in time") from None
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Sumo Logic unreachable: {type(exc).__name__}", retryable=True) from None
        if resp.status_code in (401, 403):
            raise AuthRejected
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ConnectorError(f"Sumo Logic returned {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", "")
            except Exception:
                message = ""
            raise ConnectorError(f"Sumo Logic rejected the search ({resp.status_code}): {str(message)[:200]}")
        return resp

    async def execute(
        self, params: dict[str, Any], creds: dict[str, Any], *, timeout_s: float, max_rows: int
    ) -> RawResult:
        access_id, access_key = creds.get("access_id"), creds.get("access_key")
        if access_id is None or access_key is None:
            raise ConnectorError("sumo source needs auth.access_id and auth.access_key")
        auth = httpx.BasicAuth(
            access_id.reveal() if isinstance(access_id, Secret) else str(access_id),
            access_key.reveal() if isinstance(access_key, Secret) else str(access_key),
        )
        start, end = parse_ts(params.get("start")), parse_ts(params.get("end"))
        if start is None or end is None:
            raise ConnectorError("start and end are required")
        mode = params.get("mode", "aggregate")
        limit = min(int(params.get("limit") or max_rows), max_rows)
        body = {
            "query": build_query(params, self._scope_fields),
            "from": int(start.timestamp() * 1000),
            "to": int(end.timestamp() * 1000),
            "timeZone": "UTC",
        }

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        created = await self._request("POST", "/v1/search/jobs", auth, timeout_s, json=body)
        job_id = created.json().get("id")
        if not job_id:
            raise ConnectorError("Sumo Logic did not return a search job id")
        try:
            status: dict[str, Any] = {}
            while True:
                status = (await self._request("GET", f"/v1/search/jobs/{job_id}", auth, timeout_s)).json()
                state = str(status.get("state", ""))
                if state == _DONE:
                    break
                if state in ("CANCELLED", "FORCE PAUSED"):
                    raise ConnectorError(f"Sumo search job ended in state {state}")
                if loop.time() + self._poll_s > deadline:
                    raise ConnectorTimeout("Sumo search job did not finish in time")
                await self._sleep(self._poll_s)

            errors = status.get("pendingErrors") or []
            if errors:
                raise ConnectorError(f"Sumo search failed: {str(errors[0])[:200]}")

            kind = "records" if mode == "aggregate" else "messages"
            total = int(status.get("recordCount" if mode == "aggregate" else "messageCount") or 0)
            rows: list[dict[str, Any]] = []
            if total:
                page = await self._request(
                    "GET", f"/v1/search/jobs/{job_id}/{kind}", auth, timeout_s, params={"offset": 0, "limit": limit}
                )
                for item in page.json().get(kind) or []:
                    rows.append({k: v for k, v in (item.get("map") or {}).items() if k not in _NOISY_FIELDS})
            warnings = [str(w)[:160] for w in (status.get("pendingWarnings") or [])][:3]
            return RawResult(
                rows=rows,
                truncated=total > len(rows),
                meta={"total": total, "query": body["query"], **({"warnings": warnings} if warnings else {})},
            )
        finally:
            # Shielded so a cancelled call (gateway timeout) still cleans up its job.
            await asyncio.shield(self._delete(job_id, auth))

    async def _delete(self, job_id: str, auth: httpx.BasicAuth) -> None:
        try:
            await self._client.request("DELETE", f"{self._endpoint}/v1/search/jobs/{job_id}", auth=auth, timeout=5)
        except Exception:
            pass  # best effort: Sumo also expires idle jobs on its own

    async def aclose(self) -> None:
        await self._client.aclose()


@register_connector("sumo")
def _build(options: dict[str, Any]) -> SumoConnector:
    return SumoConnector(options)
