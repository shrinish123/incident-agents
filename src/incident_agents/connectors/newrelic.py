"""New Relic connector: runs NRQL through the NerdGraph API.

Source options:
  account_id      New Relic account id, or
  account_id_env  name of an env var holding it (keeps it out of committed config)
  endpoint        NerdGraph URL (default US; EU is https://api.eu.newrelic.com/graphql)
Auth:
  api_key         a user API key
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..querytypes import ConnectorError, ConnectorTimeout, CostEstimate, RawResult
from ..secrets import AuthRejected, Secret
from .base import register_connector

_QUERY = "query($a:Int!,$q:Nrql!,$t:Seconds){actor{account(id:$a){nrql(query:$q,timeout:$t){results}}}}"
_TIMEOUT_CLASSES = {"TIMEOUT", "NRDB_TIMEOUT"}
_TRANSIENT_CLASSES = {"SERVER_ERROR", "SERVICE_UNAVAILABLE", "TOO_MANY_REQUESTS", "INTERNAL_SERVER_ERROR"}


class NewRelicConnector:
    type = "newrelic"

    def __init__(self, options: dict[str, Any], client: httpx.AsyncClient | None = None) -> None:
        self._endpoint = options.get("endpoint", "https://api.newrelic.com/graphql")
        account = options.get("account_id") or os.environ.get(options.get("account_id_env", ""), "")
        if not account:
            raise ValueError("newrelic source needs options.account_id or options.account_id_env")
        self._account = int(account)
        self._client = client or httpx.AsyncClient()

    async def estimate(self, params: dict[str, Any], creds: dict[str, Any]) -> CostEstimate | None:
        return None  # the NRQL linter prices every query at one unit

    async def execute(
        self, params: dict[str, Any], creds: dict[str, Any], *, timeout_s: float, max_rows: int
    ) -> RawResult:
        key = creds.get("api_key")
        if not isinstance(key, Secret):
            raise ConnectorError("newrelic source has no api_key configured")
        body = {
            "query": _QUERY,
            "variables": {"a": self._account, "q": params["nrql"], "t": max(1, min(int(timeout_s), 120))},
        }
        try:
            resp = await self._client.post(
                self._endpoint,
                json=body,
                headers={"API-Key": key.reveal(), "Content-Type": "application/json"},
                timeout=timeout_s,
            )
        except httpx.TimeoutException:
            raise ConnectorTimeout("New Relic did not answer in time") from None
        except httpx.HTTPError as exc:
            raise ConnectorError(f"New Relic unreachable: {type(exc).__name__}", retryable=True) from None

        if resp.status_code in (401, 403):
            raise AuthRejected
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ConnectorError(f"New Relic returned {resp.status_code}", retryable=True)
        if resp.status_code != 200:
            raise ConnectorError(f"New Relic returned {resp.status_code}")

        payload = resp.json()
        errors = payload.get("errors") or []
        if errors:
            first = errors[0]
            message = str(first.get("message", "unknown error"))[:240]
            klass = str((first.get("extensions") or {}).get("errorClass", "")).upper()
            if klass in _TIMEOUT_CLASSES or "timed out" in message.lower() or "timeout" in message.lower():
                raise ConnectorTimeout(f"NRQL timed out: {message}")
            if "api key" in message.lower() or klass in {"UNAUTHORIZED", "FORBIDDEN"}:
                raise AuthRejected
            raise ConnectorError(f"NRQL error: {message}", retryable=klass in _TRANSIENT_CLASSES)

        try:
            results = payload["data"]["actor"]["account"]["nrql"]["results"]
        except (KeyError, TypeError):
            raise ConnectorError("New Relic returned an unexpected response shape") from None
        rows = [r if isinstance(r, dict) else {"value": r} for r in (results or [])]
        return RawResult(rows=rows[:max_rows], truncated=len(rows) > max_rows)

    async def aclose(self) -> None:
        await self._client.aclose()


@register_connector("newrelic")
def _build(options: dict[str, Any]) -> NewRelicConnector:
    return NewRelicConnector(options)
