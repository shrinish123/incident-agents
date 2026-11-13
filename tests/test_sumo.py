"""Sumo connector against a scripted fake of the Search Job API (not a live account)."""

import asyncio
import json

import httpx
import pytest

from incident_agents.connectors.sumo import SumoConnector, build_query
from incident_agents.gateway import ConnectorError, ConnectorTimeout, QueryGateway, QueryRequest, Status, parse_sources
from incident_agents.secrets import AuthRejected, CredentialBroker, EnvSecretStore, Secret

CREDS = {"access_id": Secret("suABCDEFGH1234"), "access_key": Secret("k" * 40)}
PARAMS = {
    "query": "error | count by route",
    "scope": {"index": "prod", "source_category": "app/api"},
    "start": "2026-11-10T08:00:00Z",
    "end": "2026-11-10T08:30:00Z",
    "mode": "aggregate",
    "limit": 50,
}


class FakeSumo:
    def __init__(self, states=("GATHERING RESULTS", "DONE GATHERING RESULTS"), records=None, messages=None, status_extra=None):
        self.states = list(states)
        self.records = records if records is not None else [{"map": {"route": "/transactions", "_count": "42"}}]
        self.messages = messages or []
        self.status_extra = status_extra or {}
        self.log = []
        self.body = None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.log.append((req.method, path))
        assert req.headers["Authorization"].startswith("Basic ")
        if req.method == "POST" and path.endswith("/v1/search/jobs"):
            self.body = json.loads(req.content)
            return httpx.Response(202, json={"id": "JOB1"})
        if req.method == "DELETE":
            return httpx.Response(200, json={})
        if path.endswith("/JOB1"):
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return httpx.Response(200, json={"state": state, "recordCount": len(self.records), "messageCount": len(self.messages), **self.status_extra})
        if path.endswith("/records"):
            return httpx.Response(200, json={"records": self.records})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": self.messages})
        return httpx.Response(404)


async def nosleep(_):
    return None


def conn(fake, **options):
    return SumoConnector({"endpoint": "https://api.test/api", "poll_interval_s": 0.01, **options},
                         client=httpx.AsyncClient(transport=httpx.MockTransport(fake)), sleep=nosleep)


def test_query_is_always_scoped():
    assert build_query(PARAMS, {"index": "_index", "source_category": "_sourceCategory"}) == \
        '_index="prod" _sourceCategory="app/api" error | count by route'


async def test_aggregate_job_lifecycle_create_poll_fetch_delete():
    fake = FakeSumo()
    result = await conn(fake).execute(PARAMS, CREDS, timeout_s=10, max_rows=100)
    assert result.rows == [{"route": "/transactions", "_count": "42"}] and not result.truncated
    assert fake.body["query"].startswith('_index="prod" _sourceCategory="app/api"')
    assert fake.body["to"] - fake.body["from"] == 30 * 60 * 1000 and fake.body["timeZone"] == "UTC"
    assert [m for m, _ in fake.log] == ["POST", "GET", "GET", "GET", "DELETE"]


async def test_raw_mode_reads_messages_and_drops_noise_fields():
    fake = FakeSumo(messages=[{"map": {"_raw": "boom", "_messagetime": "1", "_blockid": "x", "_sourcecategory": "app/api"}}] * 3)
    result = await conn(fake).execute({**PARAMS, "mode": "raw", "query": "boom", "limit": 2}, CREDS, timeout_s=10, max_rows=100)
    assert ("GET", "/api/v1/search/jobs/JOB1/messages") in fake.log
    assert "_blockid" not in result.rows[0] and result.rows[0]["_raw"] == "boom"


async def test_more_results_than_fetched_is_marked_truncated():
    fake = FakeSumo(records=[{"map": {"i": str(i)}} for i in range(5)])
    fake2 = FakeSumo(records=[{"map": {"i": "0"}}], status_extra={"recordCount": 900})
    assert not (await conn(fake).execute(PARAMS, CREDS, timeout_s=10, max_rows=100)).truncated
    assert (await conn(fake2).execute(PARAMS, CREDS, timeout_s=10, max_rows=100)).truncated


async def test_job_is_deleted_when_it_never_finishes_or_fails():
    fake = FakeSumo(states=["GATHERING RESULTS"])
    with pytest.raises(ConnectorTimeout):
        await conn(fake).execute(PARAMS, CREDS, timeout_s=0.05, max_rows=10)
    assert fake.log[-1][0] == "DELETE"

    fake = FakeSumo(states=["CANCELLED"])
    with pytest.raises(ConnectorError, match="CANCELLED"):
        await conn(fake).execute(PARAMS, CREDS, timeout_s=5, max_rows=10)
    assert fake.log[-1][0] == "DELETE"

    fake = FakeSumo(states=["DONE GATHERING RESULTS"], status_extra={"pendingErrors": ["Field route not found"]})
    with pytest.raises(ConnectorError, match="route not found"):
        await conn(fake).execute(PARAMS, CREDS, timeout_s=5, max_rows=10)


async def test_job_is_deleted_when_the_gateway_cancels_the_call():
    fake = FakeSumo(states=["GATHERING RESULTS"])
    c = SumoConnector({"endpoint": "https://api.test/api", "poll_interval_s": 0.01}, client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(c.execute(PARAMS, CREDS, timeout_s=30, max_rows=10), timeout=0.05)
    await asyncio.sleep(0.02)
    assert ("DELETE", "/api/v1/search/jobs/JOB1") in fake.log


@pytest.mark.parametrize("status,exc,retryable", [(401, AuthRejected, None), (403, AuthRejected, None), (429, ConnectorError, True), (503, ConnectorError, True), (400, ConnectorError, False)])
async def test_http_error_mapping(status, exc, retryable):
    c = conn(lambda req: httpx.Response(status, json={"message": "Invalid query syntax"}))
    with pytest.raises(exc) as info:
        await c.execute(PARAMS, CREDS, timeout_s=5, max_rows=10)
    if retryable is not None:
        assert info.value.retryable is retryable


async def test_through_the_gateway_with_policy_and_cost():
    fake = FakeSumo()
    sources = parse_sources({"sources": {"logs": {
        "type": "sumo", "kind": "log_search",
        "auth": {"access_id": "secret://sumo/prod#access_id", "access_key": "secret://sumo/prod#access_key"},
        "policy": {"rate_per_min": 0, "cost_unit": "gb_scanned", "max_cost_per_query": 5,
                   "log_search": {"required_scope": ["index", "source_category"], "gb_per_hour": {"app/api": 8}}},
    }}})
    broker = CredentialBroker(store=EnvSecretStore({"SUMO_PROD_ACCESS_ID": "suABCDEFGH1234", "SUMO_PROD_ACCESS_KEY": "k" * 40}))
    gw = QueryGateway(sources, {"logs": conn(fake)}, broker)
    ok = await gw.execute(QueryRequest("logs", PARAMS))
    assert ok.status is Status.OK and ok.cost == 4 and ok.rows[0]["route"] == "/transactions"
    wide = await gw.execute(QueryRequest("logs", {**PARAMS, "end": "2026-11-10T09:00:00Z"}))
    assert wide.status is Status.REJECTED and len([m for m, _ in fake.log if m == "POST"]) == 1
