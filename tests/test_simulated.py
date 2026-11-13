import json

import pytest

from incident_agents.connectors.simulated import FaultPlan, SimLogsConnector, SimMetricsConnector, SimRecordsConnector, run_log_query
from incident_agents.gateway import ConnectorError, QueryGateway, QueryRequest, Status, parse_sources
from incident_agents.secrets import CredentialBroker, EnvSecretStore


def ts(minute, second=0):
    return f"2026-11-10T08:{minute:02d}:{second:02d}Z"


@pytest.fixture
def data(tmp_path):
    logs = []
    for i in range(40):
        logs.append({"ts": ts(i % 30, i), "source_category": "app/api", "service": "api", "level": "info", "route": "/health", "status": 200, "message": "ok"})
    for i in range(12):
        logs.append({"ts": ts(10 + i), "source_category": "app/api", "service": "api", "level": "error", "route": "/transactions", "status": 500,
                     "message": "IntegrityError duplicate key idempotency_key", "trace_id": f"t{i}", "user_id": 100 + i % 3})
    logs.append({"ts": ts(12), "source_category": "worker/celery", "service": "worker", "level": "error", "route": "", "status": "", "message": "task retry: connection reset"})
    (tmp_path / "logs.jsonl").write_text("\n".join(json.dumps(r) for r in logs))
    (tmp_path / "metrics.json").write_text(json.dumps({"series": [
        {"service": "api", "metric": "error_rate", "unit": "ratio", "points": [[ts(m), 0.01 if m < 10 else 0.2] for m in range(30)]},
        {"service": "api", "metric": "p95_latency_ms", "unit": "ms", "points": [[ts(m), 120 + m] for m in range(30)]},
    ]}))
    (tmp_path / "deploys.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"ts": ts(5), "service": "api", "repo": "api", "sha": "abc123", "prev_sha": "000aaa", "by": "ci"},
        {"ts": "2026-11-09T15:00:00Z", "service": "worker", "repo": "worker", "sha": "fff999", "prev_sha": "eee888", "by": "ci"},
    ]))
    (tmp_path / "traces.jsonl").write_text(json.dumps({"ts": ts(10), "trace_id": "t0", "service": "api", "span": "INSERT transactions", "duration_ms": 14, "error": "duplicate key"}))
    return tmp_path


WINDOW = {"start": ts(0), "end": "2026-11-10T08:30:00Z", "scope": {"source_category": "app/api"}}


async def run(conn, **params):
    return await conn.execute(params, {}, timeout_s=0.05, max_rows=100)


def test_log_query_language():
    recs = [{"ts": ts(1), "level": "error", "route": "/a", "message": "Timeout calling db"}, {"ts": ts(7), "level": "info", "route": "/b", "message": "fine"}, {"ts": ts(8), "level": "error", "route": "/b", "message": "connection reset"}]
    assert len(run_log_query(recs, "timeout")[0]) == 1
    assert len(run_log_query(recs, "level=error")[0]) == 2
    assert len(run_log_query(recs, "level!=error")[0]) == 1
    assert len(run_log_query(recs, 'level=error "connection reset"')[0]) == 1
    assert len(run_log_query(recs, "timeout or reset")[0]) == 2
    assert len(run_log_query(recs, "route=/*")[0]) == 3
    rows, agg = run_log_query(recs, "level=error | count by route")
    assert agg and {r["route"]: r["_count"] for r in rows} == {"/a": 1, "/b": 1}
    assert run_log_query(recs, "* | count")[0] == [{"_count": 3}]
    slices, _ = run_log_query(recs, "* | timeslice 5m | count by _timeslice")
    assert [s["_count"] for s in slices] == [1, 2] and slices[0]["_timeslice"] == "2026-11-10T08:00:00Z"
    with pytest.raises(ConnectorError, match="Unsupported"):
        run_log_query(recs, "* | parse regex foo")


async def test_logs_aggregate_then_raw_with_scope_and_window(data):
    conn = SimLogsConnector({"data_dir": str(data)})
    agg = await run(conn, query="level=error | count by route", mode="aggregate", **WINDOW)
    assert agg.rows == [{"route": "/transactions", "_count": 12}]  # the worker error is out of scope
    raw = await run(conn, query="IntegrityError", mode="raw", limit=5, start=ts(10), end=ts(15), scope=WINDOW["scope"])
    assert len(raw.rows) == 5 and raw.rows[0]["trace_id"] == "t0" and not raw.truncated  # exactly 5 in window
    more = await run(conn, query="IntegrityError", mode="raw", limit=5, **WINDOW)
    assert more.truncated and more.meta["total"] == 12
    with pytest.raises(ConnectorError, match="count stage"):
        await run(conn, query="IntegrityError", mode="aggregate", **WINDOW)


async def test_metrics_window_step_and_discovery(data):
    conn = SimMetricsConnector({"data_dir": str(data)})
    listed = await run(conn, op="list", service="api")
    assert {r["metric"] for r in listed.rows} == {"error_rate", "p95_latency_ms"}
    series = await run(conn, service="api", metric="error_rate", start=ts(0), end=ts(20), step_s=600, agg="max")
    assert [r["value"] for r in series.rows] == [0.01, 0.2] and series.meta["unit"] == "ratio"
    with pytest.raises(ConnectorError, match="Metrics for api: error_rate, p95_latency_ms"):
        await run(conn, service="api", metric="cpu", start=ts(0), end=ts(20))


async def test_records_cover_deploys_traces_and_other_collections(data):
    conn = SimRecordsConnector({"data_dir": str(data)})
    assert {r["collection"] for r in (await run(conn, op="list")).rows} == {"deploys", "traces"}
    recent = await run(conn, collection="deploys", start=ts(0), end=ts(30))
    assert [d["sha"] for d in recent.rows] == ["abc123"]
    assert (await run(conn, collection="deploys", filters={"service": "worker"})).rows[0]["sha"] == "fff999"
    assert (await run(conn, collection="traces", filters={"trace_id": "t0"})).rows[0]["error"] == "duplicate key"
    assert (await run(conn, collection="deploys", text="ABC123")).meta["total"] == 1
    with pytest.raises(ConnectorError, match="Available: deploys, traces"):
        await run(conn, collection="secrets")


def test_fault_plan_is_deterministic():
    plan = FaultPlan([{"on_calls": [1], "fault": "timeout"}, {"match": "idempotency", "fault": "error", "times": 1}])
    assert plan.next({"q": "a"})["fault"] == "timeout"
    assert plan.next({"q": "a"}) is None
    assert plan.next({"q": "Idempotency_key"})["fault"] == "error"
    assert plan.next({"q": "idempotency_key"}) is None


async def test_injected_failures_surface_through_the_gateway(data):
    def gateway(faults, retries):
        sources = parse_sources({"sources": {"logs": {
            "type": "sim_logs", "kind": "log_search", "options": {"data_dir": str(data), "faults": faults},
            "policy": {"timeout_s": 0.05, "backoff_s": 0, "rate_per_min": 0, "max_retries": retries, "cache_ttl_s": 0,
                       "log_search": {"required_scope": ["source_category"]}},
        }}})
        return QueryGateway.from_config(sources, CredentialBroker(store=EnvSecretStore({})))

    req = QueryRequest("logs", {"query": "level=error | count by route", "mode": "aggregate", **WINDOW})

    recovered = await gateway([{"on_calls": [1], "fault": "timeout"}], retries=2).execute(req)
    assert recovered.status is Status.OK and recovered.attempts == 2

    gave_up = await gateway([{"fault": "timeout"}], retries=1).execute(req)
    assert gave_up.status is Status.TIMEOUT and gave_up.attempts == 2

    raw = QueryRequest("logs", {"query": "IntegrityError", "mode": "raw", **WINDOW, "start": ts(10), "end": ts(25)})
    gw = gateway([{"on_calls": [2], "fault": "partial", "keep": 0.25}], retries=0)
    assert (await gw.execute(req)).ok
    partial = await gw.execute(raw)
    assert partial.status is Status.PARTIAL and len(partial.rows) == 3 and partial.to_agent()["truncated"]

    locked = await gateway([{"fault": "auth"}], retries=0).execute(req)
    assert locked.status is Status.UNAVAILABLE
