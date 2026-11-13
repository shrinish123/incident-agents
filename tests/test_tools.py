"""Tool layer tests: simulated logs/metrics/records, a real git repo, a fake SQL source."""

import json

import pytest

from incident_agents.connectors.base import build_connector
from incident_agents.evidence import EvidenceStore
from incident_agents.gateway import QueryGateway, RawResult, Status, parse_sources
from incident_agents.secrets import CredentialBroker, EnvSecretStore
from incident_agents.tools import Toolbox, build_log_query, parse_bindings, series_stats
from tests.test_git import V1, V2, make_repo


def ts(minute, second=0):
    return f"2026-11-10T08:{minute:02d}:{second:02d}Z"


W = {"start": ts(0), "end": ts(30)}


class FakeSql:
    def __init__(self):
        self.seen = []

    async def estimate(self, params, creds):
        return None

    async def execute(self, params, creds, *, timeout_s, max_rows):
        self.seen.append(params["sql"])
        return RawResult(rows=[{"user_id": 104, "idempotency_key": "104:1731225600", "n": 3}])


def build(tmp_path, faults=None, log_policy=None):
    data = tmp_path / "scenario"
    data.mkdir()
    logs = [{"ts": ts(i % 30, i), "service": "fivo-api", "level": "info", "route": "/health", "status": 200, "message": "ok"} for i in range(40)]
    logs += [{"ts": ts(10 + i), "service": "fivo-api", "level": "error", "route": "/transactions", "status": 500,
              "message": f"IntegrityError duplicate key idempotency_key for user {104 + i % 2}", "trace_id": f"t{i}", "user_id": 104 + i % 2} for i in range(12)]
    (data / "logs.jsonl").write_text("\n".join(json.dumps(r) for r in logs))
    (data / "metrics.json").write_text(json.dumps({"series": [
        {"service": "fivo-api", "metric": "error_rate", "unit": "ratio", "points": [[ts(m), 0.01 if m < 10 else 0.2] for m in range(30)]}]}))
    api1, api2 = make_repo(tmp_path / "src" / "api", {"app/keys.py": V1}, {"app/keys.py": V2})
    (data / "deploys.jsonl").write_text(json.dumps({"ts": ts(9), "service": "fivo-api", "repo": "api", "sha": api2, "prev_sha": api1, "by": "ci"}))
    (data / "traces.jsonl").write_text(json.dumps({"ts": ts(10), "trace_id": "t0", "service": "fivo-api", "span": "INSERT transactions", "duration_ms": 14, "error": "duplicate key"}))
    (data / "topology.jsonl").write_text(json.dumps({"service": "fivo-api", "depends_on": ["neon-postgres", "upstash-redis"]}))
    config = {
        "sources": {
            "logs": {"type": "sim_logs", "kind": "log_search", "options": {"data_dir": str(data), "faults": faults or []},
                     "policy": {"rate_per_min": 0, "timeout_s": 0.05, "max_retries": 0, "max_rows": 5, "log_search": {"required_scope": ["service"], **(log_policy or {})}}},
            "metrics": {"type": "sim_metrics", "options": {"data_dir": str(data)}, "policy": {"rate_per_min": 0}},
            "records": {"type": "sim_records", "options": {"data_dir": str(data)}, "policy": {"rate_per_min": 0}},
            "appdb": {"type": "fake_sql", "kind": "sql", "policy": {"rate_per_min": 0, "max_rows": 20, "sql": {"tables": {
                "transactions": {"indexed": ["user_id", "created_at"]}, "users": {"indexed": ["id"], "deny_columns": ["password_hash"]}}}}},
            "code": {"type": "git", "options": {"mirror_dir": str(tmp_path / "mirrors"), "repos": {"api": {"url": str(tmp_path / "src" / "api"), "branch": "main", "services": ["fivo-api"]}}},
                     "policy": {"rate_per_min": 0}},
        },
        "capabilities": {"logs": {"source": "logs"}, "metrics": {"source": "metrics"}, "records": {"source": "records"},
                         "db": {"source": "appdb"}, "code": {"source": "code"}},
    }
    sources = parse_sources(config)
    sql = FakeSql()
    connectors = {n: sql if s.type == "fake_sql" else build_connector(s.type, s.options) for n, s in sources.items()}
    gw = QueryGateway(sources, connectors, CredentialBroker(store=EnvSecretStore({})))
    box = Toolbox(gw, EvidenceStore(), parse_bindings(config), "inv-1")
    return box, sql, (api1, api2)


@pytest.fixture
def box(tmp_path):
    return build(tmp_path)[0]


def test_log_queries_are_rendered_per_dialect():
    kw = dict(text="duplicate key", filters={"level": "error"}, group_by=["route"], timeslice="5m", count=True)
    assert build_log_query("simple", **kw) == '"duplicate key" level="error" | timeslice 5m | count by _timeslice, route'
    assert build_log_query("sumo", **kw) == '"duplicate key" | json auto | where level = "error" | timeslice 5m | count by _timeslice, route'
    assert build_log_query("sumo", text="timeout", count=True) == '"timeout" | count'
    assert build_log_query("simple") == "*"
    assert "|" not in build_log_query("simple", text="a | delete")


def test_specs_are_valid_tool_definitions_and_follow_bindings(tmp_path):
    box = build(tmp_path)[0]
    specs = box.specs()
    assert len(specs) == 19 and all(s["input_schema"]["type"] == "object" and s["description"] for s in specs)
    assert box.names(["db"]) == ["db_schema", "db_query"]
    only_logs = Toolbox(box.gateway, box.store, {"logs": box.bindings["logs"]}, "inv-2")
    assert only_logs.names() == ["logs_count", "logs_fetch", "evidence_get", "evidence_find"]


async def test_multi_hop_from_logs_to_trace_to_deploy_to_diff(box):
    counts = await box.call("logs_count", {"service": "fivo-api", "filters": {"level": "error"}, "group_by": ["route", "status"], **W}, agent="observability", step_id="s1")
    assert counts.status is Status.OK and counts.evidence_ids == ["logagg:fivo-api:0001"]
    assert counts.evidence[0].data["total"] == 12 and "route=/transactions" in counts.evidence[0].summary

    lines = await box.call("logs_fetch", {"service": "fivo-api", "filters": {"level": "error"}, "start": ts(10), "end": ts(15)}, step_id="s2")
    assert lines.evidence_ids[0] == "log:fivo-api:0001" and len(lines.evidence) == 5
    pivots = lines.to_agent()["pivots"]
    assert pivots["trace_id"][0] == "t0" and pivots["user_id"] == ["104", "105"] and len(pivots["error_fingerprint"]) == 1

    spans = await box.call("traces_search", {"trace_id": pivots["trace_id"][0]})
    assert spans.evidence[0].id == "trace:fivo-api:0001" and spans.evidence[0].data["span"] == "INSERT transactions"

    deploy = (await box.call("deploys_list", {"service": "fivo-api", **W})).evidence[0]
    assert deploy.id == "deploy:fivo-api:0001" and deploy.ts == ts(9)
    files = await box.call("code_changed_files", {"repo": "api", "base": deploy.data["prev_sha"], "head": deploy.data["sha"]})
    assert "M app/keys.py" in files.evidence[0].summary
    diff = await box.call("code_diff", {"repo": "api", "base": deploy.data["prev_sha"], "head": deploy.data["sha"], "path": "app/keys.py"})
    assert diff.evidence[0].id == "diff:api:0001" and "+1 -1 lines" in diff.evidence[0].summary
    code = await box.call("code_read", {"repo": "api", "ref": deploy.data["sha"], "path": "app/keys.py"})
    assert "return f'{user_id}:{ts}'" in code.evidence[0].data["content"]
    hits = await box.call("code_search", {"repo": "api", "ref": deploy.data["sha"], "pattern": "idempotency_key"})
    assert "app/keys.py:1" in hits.evidence[0].summary

    # The deployed commit ties the deploy record to the code evidence.
    linked = await box.call("evidence_find", {"pivot_key": "sha", "pivot_value": deploy.data["sha"]})
    assert {d["id"] for d in linked.info} >= {deploy.id, diff.evidence[0].id, code.evidence[0].id}
    again = await box.call("evidence_get", {"ids": [deploy.id, "deploy:fivo-api:0042"]})
    assert again.evidence_ids == [deploy.id] and "deploy:fivo-api:0042" in again.note

    assert [c.tool for c in box.calls][:2] == ["logs_count", "logs_fetch"] and box.calls[0].agent == "observability"
    assert box.calls[1].step_id == "s2" and box.calls[1].evidence_ids == lines.evidence_ids
    assert box.store.find("inv-1", step_id="s2")[0].tool == "logs_fetch"


async def test_metric_evidence_names_the_level_shift(box):
    res = await box.call("metrics_query", {"service": "fivo-api", "metric": "error_rate", **W})
    ev = res.evidence[0]
    assert ev.id == "metric:fivo-api:0001" and ev.data["stats"]["shift"] == {"at": ts(10), "mean_before": 0.01, "mean_after": 0.2}
    assert f"level shift at {ts(10)}" in ev.summary
    assert "shift" not in series_stats([{"ts": ts(i), "value": 5 + (i % 2) * 0.1} for i in range(20)])
    listed = await box.call("metrics_list", {})
    assert listed.info == [{"service": "fivo-api", "metric": "error_rate", "unit": "ratio"}] and listed.evidence == []
    missing = await box.call("metrics_query", {"service": "fivo-api", "metric": "cpu", **W})
    assert missing.status is Status.ERROR and "error_rate" in missing.error


async def test_absence_is_recorded_as_evidence(box):
    none = await box.call("deploys_list", {"service": "fivo-worker", **W})
    assert none.status is Status.OK and none.evidence[0].id == "deploy:fivo-worker:0001"
    assert none.evidence[0].summary == f"No deploys found for service=fivo-worker, {ts(0)} to {ts(30)}"
    quiet = await box.call("logs_count", {"service": "fivo-api", "text": "OutOfMemory", **W})
    assert quiet.evidence[0].data["total"] == 0 and quiet.evidence[0].summary.startswith("0 log lines")


async def test_guardrail_refusals_reach_the_agent_with_a_way_forward(box):
    raw_first = await box.call("logs_fetch", {"service": "fivo-api", "start": ts(10), "end": ts(12)})
    assert raw_first.status is Status.REJECTED and "count" in raw_first.to_agent()["hint"].lower()
    wide = await box.call("logs_count", {"service": "fivo-api", "start": "2026-11-10T00:00:00Z", "end": "2026-11-10T08:00:00Z"})
    assert wide.status is Status.REJECTED and "limit is" in wide.violations[0]
    assert box.store.count("inv-1") == 0


async def test_bad_calls_come_back_as_structured_refusals(box):
    assert (await box.call("logs_count", {"service": "fivo-api"})).violations == ["Missing required argument 'start'", "Missing required argument 'end'"]
    assert "Unknown argument 'sevice'" in (await box.call("deploys_list", {"sevice": "x"})).violations[0]
    assert "must be of type integer" in (await box.call("deploys_list", {"limit": "ten"})).violations[0]
    assert "must be one of" in (await box.call("metrics_query", {"service": "a", "metric": "m", "agg": "p99", **W})).violations[0]
    assert "Invalid field name" in (await box.call("logs_count", {"service": "fivo-api", "group_by": ["route | delete"], **W})).violations[0]
    unknown = await box.call("shell_exec", {"cmd": "ls"})
    assert unknown.status is Status.REJECTED and "logs_count" in unknown.hint
    off_limits = await box.call("db_query", {"sql": "select 1"}, allowed=["logs_count"])
    assert off_limits.status is Status.REJECTED and off_limits.hint == "Available tools: logs_count."
    assert [c.status for c in box.calls] == ["rejected"] * 7


async def test_db_tools_explain_the_rules_and_enforce_them(tmp_path):
    box, sql, _ = build(tmp_path)
    schema = (await box.call("db_schema", {})).info
    assert schema["tables"]["transactions"] == {"filter_on_one_of": ["user_id", "created_at"]}
    assert schema["tables"]["users"]["restricted_columns"] == ["password_hash"]

    scan = await box.call("db_query", {"sql": "SELECT count(*) FROM transactions"})
    assert scan.status is Status.REJECTED and "user_id" in scan.hint and sql.seen == []
    write = await box.call("db_query", {"sql": "DELETE FROM transactions WHERE user_id = 104"})
    assert write.status is Status.REJECTED

    ok = await box.call("db_query", {"sql": "SELECT user_id, idempotency_key, count(*) AS n FROM transactions WHERE user_id = 104 GROUP BY 1, 2"})
    ev = ok.evidence[0]
    assert ev.id == "db:transactions:0001" and ev.summary.startswith("1 row from transactions; first: user_id=104")
    assert ev.pivots == {"user_id": "104", "idempotency_key": "104:1731225600"} and "LIMIT 21" in sql.seen[0]


async def test_tool_failures_are_reported_and_partial_results_are_marked(tmp_path):
    box, _, _ = build(tmp_path, faults=[{"on_calls": [1], "fault": "timeout"}, {"on_calls": [2], "fault": "partial", "keep": 0.5}])
    args = {"service": "fivo-api", "filters": {"level": "error"}, "group_by": ["trace_id"], **W}
    timed_out = await box.call("logs_count", args)
    assert timed_out.status is Status.TIMEOUT and timed_out.evidence == [] and timed_out.to_agent()["error"]
    partial = await box.call("logs_count", args)
    assert partial.status is Status.PARTIAL and partial.truncated
    ev = partial.evidence[0]
    assert not ev.complete and ev.summary.endswith("(partial result)") and ev.digest()["partial"] is True
    assert partial.to_agent()["truncated"] is True and partial.to_agent()["hint"]


async def test_metrics_can_be_served_by_nrql_templates():
    class FakeNrql:
        seen = []

        async def estimate(self, params, creds):
            return None

        async def execute(self, params, creds, *, timeout_s, max_rows):
            self.seen.append(params["nrql"])
            return RawResult(rows=[{"beginTimeSeconds": 1762761600 + 60 * i, "endTimeSeconds": 1762761660 + 60 * i,
                                    "percentile.duration": {"95": 120 if i < 6 else 900}} for i in range(12)])

    config = {
        "sources": {"apm": {"type": "fake", "kind": "nrql", "policy": {"rate_per_min": 0}}},
        "capabilities": {"metrics": {"source": "apm", "dialect": "nrql", "templates": {
            "p95_latency_ms": "SELECT percentile(duration, 95) FROM Transaction WHERE appName = '{service}' TIMESERIES {step_s} seconds SINCE '{start}' UNTIL '{end}'"}}},
    }
    fake = FakeNrql()
    gw = QueryGateway(parse_sources(config), {"apm": fake}, CredentialBroker(store=EnvSecretStore({})))
    box = Toolbox(gw, EvidenceStore(), parse_bindings(config), "inv")
    assert (await box.call("metrics_list", {})).info == [{"metric": "p95_latency_ms"}]
    res = await box.call("metrics_query", {"service": "fivo-api", "metric": "p95_latency_ms", **W})
    assert "appName = 'fivo-api' TIMESERIES 60 seconds SINCE '2026-11-10 08:00:00' UNTIL '2026-11-10 08:30:00'" in fake.seen[0]
    assert res.evidence[0].data["stats"]["shift"]["mean_after"] == 900 and res.evidence[0].data["points"][0]["ts"].endswith("Z")
    injected = await box.call("metrics_query", {"service": "x' OR 'a'='a", "metric": "p95_latency_ms", **W})
    assert injected.status is Status.REJECTED and len(fake.seen) == 1
    assert (await box.call("metrics_query", {"service": "fivo-api", "metric": "nope", **W})).status is Status.REJECTED


async def test_an_incomplete_result_is_not_replayed_from_cache(tmp_path):
    box, _, _ = build(tmp_path, faults=[{"on_calls": [1], "fault": "partial", "keep": 0.5}])
    args = {"service": "fivo-api", "group_by": ["level"], **W}
    first = await box.call("logs_count", args)
    assert first.status is Status.PARTIAL and "Retry the same query" in first.to_agent()["hint"]
    second = await box.call("logs_count", args)
    assert second.status is Status.OK and second.evidence[0].complete and second.evidence[0].id != first.evidence[0].id
    assert second.evidence[0].data["total"] > first.evidence[0].data["total"]


def test_a_ramp_reports_where_it_leaves_its_baseline():
    points = [{"ts": ts(i), "value": 1.0 if i < 12 else 1.0 + (i - 11) * 4} for i in range(30)]
    stats = series_stats(points)
    assert stats["departs_baseline"] == {"at": ts(13), "baseline": 1.0}
    assert "departs_baseline" not in series_stats([{"ts": ts(i), "value": 5.0} for i in range(20)])


async def test_counts_and_metrics_can_be_compared_with_a_window_from_before_the_incident(tmp_path):
    box, _, _ = build(tmp_path)
    now, before = {"start": ts(10), "end": ts(20)}, {"baseline_start": ts(0), "baseline_end": ts(10)}

    res = await box.call("logs_count", {"service": "fivo-api", "group_by": ["route", "status"], **now, **before})
    ev = res.evidence[0]
    base = ev.data["baseline"]
    assert res.status is Status.OK and base["baseline_total"] > 0 and base["complete"]
    new = [g for g in base["groups"] if g["change"] == "new"]
    assert [(g["route"], g["status"], g["count"], g["baseline_count"]) for g in new] == [("/transactions", "500", 10, 0)]
    health = next(g for g in base["groups"] if g["route"] == "/health")
    assert health["change"].startswith("x") and "new since baseline: route=/transactions status=500 (10)" in ev.summary

    res = await box.call("metrics_query", {"service": "fivo-api", "metric": "error_rate", **now, **before})
    ev = res.evidence[0]
    assert ev.data["baseline"]["stats"]["mean"] == 0.01 and ev.data["baseline"]["mean_change"] == "x20.0"
    assert "Baseline 2026-11-10T08:00:00Z to 2026-11-10T08:10:00Z" in ev.summary and "mean change x20.0" in ev.summary

    # The baseline must come from before the window, and a time series cannot be compared group by group.
    late = await box.call("logs_count", {"service": "fivo-api", **now, "baseline_start": ts(5), "baseline_end": ts(15)})
    assert late.status is Status.REJECTED and "before the start" in late.violations[0]
    sliced = await box.call("logs_count", {"service": "fivo-api", "timeslice": "1m", **now, **before})
    assert sliced.status is Status.REJECTED and "timeslice" in sliced.violations[0]
    half = await box.call("metrics_query", {"service": "fivo-api", "metric": "error_rate", **now, "baseline_start": ts(0)})
    assert half.status is Status.REJECTED


async def test_a_capped_sample_is_not_a_partial_result(tmp_path):
    box, _, _ = build(tmp_path)
    await box.call("logs_count", {"service": "fivo-api", "filters": {"level": "error"}, "group_by": ["route"], **W})
    lines = await box.call("logs_fetch", {"service": "fivo-api", "filters": {"level": "error"}, "start": ts(10), "end": ts(24)})
    # 12 lines match and the source's row cap is 5: the agent is told the sample was capped, but each line is whole evidence.
    assert lines.status is Status.PARTIAL and lines.truncated and len(lines.evidence) == 5
    assert all(ev.complete and "partial" not in ev.digest() for ev in lines.evidence)
