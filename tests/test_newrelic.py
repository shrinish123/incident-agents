import httpx
import pytest

from incident_agents.connectors.newrelic import NewRelicConnector
from incident_agents.gateway import ConnectorError, ConnectorTimeout
from incident_agents.secrets import AuthRejected, Secret

CREDS = {"api_key": Secret("NRAK-TESTKEYTESTKEYTESTKEY000")}
PARAMS = {"nrql": "SELECT count(*) FROM Transaction SINCE 5 minutes ago LIMIT 100"}


def conn(handler):
    return NewRelicConnector({"account_id": 42}, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def ok(results):
    return httpx.Response(200, json={"data": {"actor": {"account": {"nrql": {"results": results}}}}})


async def test_sends_nrql_to_the_configured_account_and_returns_rows():
    seen = {}

    def handler(req):
        import json

        seen.update(json.loads(req.content)["variables"], key=req.headers["API-Key"])
        return ok([{"count": 7}])

    result = await conn(handler).execute(PARAMS, CREDS, timeout_s=30, max_rows=100)
    assert result.rows == [{"count": 7}] and not result.truncated
    assert seen == {"a": 42, "q": PARAMS["nrql"], "t": 30, "key": "NRAK-TESTKEYTESTKEYTESTKEY000"}


async def test_rows_are_capped():
    result = await conn(lambda r: ok([{"i": i} for i in range(5)])).execute(PARAMS, CREDS, timeout_s=5, max_rows=3)
    assert len(result.rows) == 3 and result.truncated


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_rejection(status):
    with pytest.raises(AuthRejected):
        await conn(lambda r: httpx.Response(status, json={})).execute(PARAMS, CREDS, timeout_s=5, max_rows=10)


async def test_error_mapping():
    c = conn(lambda r: httpx.Response(200, json={"errors": [{"message": "NRQL Syntax Error: Error at line 1"}]}))
    with pytest.raises(ConnectorError) as exc:
        await c.execute(PARAMS, CREDS, timeout_s=5, max_rows=10)
    assert not exc.value.retryable and "Syntax Error" in str(exc.value)

    c = conn(lambda r: httpx.Response(200, json={"errors": [{"message": "query timed out", "extensions": {"errorClass": "TIMEOUT"}}]}))
    with pytest.raises(ConnectorTimeout):
        await c.execute(PARAMS, CREDS, timeout_s=5, max_rows=10)

    for status in (429, 503):
        with pytest.raises(ConnectorError) as exc:
            await conn(lambda r, s=status: httpx.Response(s)).execute(PARAMS, CREDS, timeout_s=5, max_rows=10)
        assert exc.value.retryable

    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(ConnectorTimeout):
        await conn(slow).execute(PARAMS, CREDS, timeout_s=5, max_rows=10)

    with pytest.raises(ConnectorError, match="unexpected response shape"):
        await conn(lambda r: httpx.Response(200, json={"data": {"actor": None}})).execute(PARAMS, CREDS, timeout_s=5, max_rows=10)


def test_account_id_is_required():
    with pytest.raises(ValueError):
        NewRelicConnector({})
