import asyncio
import json

import pytest

from incident_agents.gateway import (
    AuditLog,
    BudgetLedger,
    ConnectorError,
    ConnectorTimeout,
    CostEstimate,
    LintContext,
    QueryGateway,
    QueryRequest,
    RawResult,
    Status,
    parse_sources,
)
from incident_agents.gateway.linters import lint_log_search, lint_nrql, lint_sql
from incident_agents.secrets import AuthRejected, CredentialBroker, EnvSecretStore

SOURCES = {
    "sources": {
        "appdb": {
            "type": "fake",
            "kind": "sql",
            "description": "Application database, read replica",
            "auth": {"user": "ro", "password": "secret://appdb/ro#password"},
            "policy": {
                "max_rows": 50,
                "timeout_s": 0.05,
                "backoff_s": 0,
                "rate_per_min": 0,
                "cost_unit": "planner_cost",
                "max_cost_per_query": 10000,
                "redact_fields": ["email", "phone"],
                "sql": {
                    "tables": {
                        "transactions": {"indexed": ["id", "user_id", "created_at"]},
                        "users": {"indexed": ["id"], "deny_columns": ["password_hash"]},
                        "categories": {"small": True},
                    }
                },
            },
        },
        "logs": {
            "type": "fake",
            "kind": "log_search",
            "auth": {"access_key": "secret://sumo/prod#access_key"},
            "policy": {
                "timeout_s": 0.05,
                "backoff_s": 0,
                "rate_per_min": 0,
                "cost_unit": "gb_scanned",
                "max_cost_per_query": 20,
                "approval_above": 10,
                "investigation_budget": 30,
                "log_search": {
                    "required_scope": ["index", "source_category"],
                    "max_window_s": 3600,
                    "raw_max_window_s": 900,
                    "gb_per_hour": {"app/api": 8, "gateway/nginx": 120},
                    "default_gb_per_hour": 2,
                },
            },
        },
        "apm": {
            "type": "fake",
            "kind": "nrql",
            "policy": {"timeout_s": 0.05, "backoff_s": 0, "rate_per_min": 0, "max_rows": 100, "max_concurrency": 2},
        },
    }
}
ENV = {"APPDB_RO_PASSWORD": "db-pass-93kd72", "SUMO_PROD_ACCESS_KEY": "sumo-key-55aa91"}


class FakeConnector:
    type = "fake"

    def __init__(self, rows=None, script=None, estimate=None):
        self.rows = rows if rows is not None else [{"n": 1}]
        self.script = list(script or [])  # exceptions or "hang" consumed one per call
        self.estimate_value = estimate
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def estimate(self, params, creds):
        if isinstance(self.estimate_value, Exception):
            raise self.estimate_value
        return self.estimate_value

    async def execute(self, params, creds, *, timeout_s, max_rows):
        self.calls.append((params, creds))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            if self.script:
                step = self.script.pop(0)
                if step == "hang":
                    await asyncio.sleep(10)
                elif isinstance(step, Exception):
                    raise step
            return RawResult(rows=list(self.rows))
        finally:
            self.active -= 1


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(connectors=None, env=None, **kw):
    sources = parse_sources(SOURCES)
    conns = {n: FakeConnector() for n in sources}
    conns.update(connectors or {})
    broker = CredentialBroker(store=EnvSecretStore(ENV if env is None else env))
    return QueryGateway(sources, conns, broker, **kw), conns


def sql(q, **kw):
    return QueryRequest(source="appdb", params={"sql": q}, investigation_id="inv-1", **kw)


def logs(mode="aggregate", minutes=30, cat="app/api", query="error | count by route", **kw):
    return QueryRequest(
        source="logs",
        investigation_id=kw.pop("inv", "inv-1"),
        params={
            "query": query,
            "scope": {"index": "prod", "source_category": cat},
            "start": "2026-11-10T08:00:00Z",
            "end": f"2026-11-10T{8 + minutes // 60:02d}:{minutes % 60:02d}:00Z",
            "mode": mode,
            **kw,
        },
    )


# --- SQL policy ---------------------------------------------------------------


@pytest.mark.parametrize(
    "query,needle",
    [
        ("DELETE FROM transactions WHERE id = 1", "Only SELECT"),
        ("UPDATE users SET name = 'x' WHERE id = 1", "Only SELECT"),
        ("SELECT 1; DROP TABLE users", "one statement"),
        ("WITH x AS (DELETE FROM transactions WHERE id = 1 RETURNING *) SELECT * FROM x", "DELETE"),
        ("SELECT id INTO backup FROM transactions WHERE id = 1", "INTO"),
        ("SELECT id FROM transactions WHERE id = 1 FOR UPDATE", "LOCK"),
        ("SELECT pg_sleep(30)", "pg_sleep"),
        ("SELECT * FROM generate_series(1, 100000000)", "not allowed"),
        ("SELECT id FROM payments WHERE id = 1", "payments is not queryable"),
        ("SELECT relname FROM pg_catalog.pg_class WHERE oid = 1", "not queryable"),
        ("SELECT id, amount FROM transactions", "no filter on an indexed column"),
        ("SELECT id FROM transactions WHERE note = 'x'", "no filter on an indexed column"),
        ("SELECT id FROM transactions WHERE user_id = 7 OR 1 = 1", "no filter on an indexed column"),
        ("SELECT id FROM transactions WHERE NOT user_id = 7", "no filter on an indexed column"),
        ("SELECT t.id FROM transactions t JOIN users u ON u.id = t.user_id", "no filter on an indexed column"),
        ("SELECT id FROM transactions WHERE id = 1 UNION ALL SELECT id FROM transactions", "no filter on an indexed column"),
        ("SELECT id FROM transactions WHERE id IN (SELECT id FROM transactions)", "no filter on an indexed column"),
        ("SELECT x.id FROM (SELECT id FROM transactions) x WHERE x.id = 1", "no filter on an indexed column"),
        ("WITH big AS (SELECT id FROM transactions) SELECT id FROM big WHERE id = 1", "no filter on an indexed column"),
        ("SELECT * FROM users WHERE id = 7", "SELECT *"),
        ("SELECT id, password_hash FROM users WHERE id = 7", "password_hash is restricted"),
        ("VACUUM transactions", "Only SELECT"),
        ("SELEC id FRM", "parsed"),
    ],
)
async def test_sql_policy_rejects_unsafe_queries_before_they_reach_the_database(query, needle):
    gw, conns = make()
    result = await gw.execute(sql(query))
    assert result.status is Status.REJECTED
    assert any(needle.lower() in v.lower() for v in result.violations), result.violations
    assert conns["appdb"].calls == []


async def test_sql_limit_is_added_and_clamped():
    gw, conns = make()
    r1 = await gw.execute(sql("SELECT id, amount FROM transactions WHERE user_id = 7"))
    r2 = await gw.execute(sql("SELECT id FROM transactions WHERE user_id = 8 LIMIT 100000"))
    r3 = await gw.execute(sql("SELECT id FROM transactions WHERE user_id = 9 LIMIT 5"))
    assert r1.ok and r1.executed_params["sql"].endswith("LIMIT 51")
    assert r2.executed_params["sql"].endswith("LIMIT 51")
    assert r3.executed_params["sql"].endswith("LIMIT 5")
    assert conns["appdb"].calls[0][0]["sql"].endswith("LIMIT 51")  # cap + 1, to detect truncation


async def test_sql_join_and_small_lookup_table_are_allowed():
    gw, _ = make()
    ok = await gw.execute(
        sql(
            "SELECT t.id, c.name, u.name FROM transactions t "
            "JOIN users u ON u.id = t.user_id JOIN categories c ON c.id = t.category_id "
            "WHERE t.user_id IN (7, 8) AND t.created_at >= '2026-11-10'"
        )
    )
    assert ok.status is Status.OK, ok.violations
    nested = await gw.execute(
        sql(
            "WITH recent AS (SELECT id, user_id FROM transactions WHERE created_at >= '2026-11-10') "
            "SELECT r.id FROM recent r WHERE r.user_id IN (SELECT id FROM users WHERE id = 7) "
            "UNION ALL SELECT id FROM transactions WHERE user_id = 9"
        )
    )
    assert nested.status is Status.OK, nested.violations


async def test_sql_cost_estimate_from_the_source_blocks_expensive_plans():
    gw, conns = make({"appdb": FakeConnector(estimate=CostEstimate(250000, "planner_cost", "Seq Scan"))})
    result = await gw.execute(sql("SELECT id FROM transactions WHERE created_at > '2020-01-01'"))
    assert result.status is Status.REJECTED and "per-query limit" in result.violations[0]
    assert conns["appdb"].calls == []


async def test_source_refusing_the_query_at_estimate_time_is_an_error_not_a_crash():
    gw, _ = make({"appdb": FakeConnector(estimate=ConnectorError('column "foo" does not exist'))})
    result = await gw.execute(sql("SELECT foo FROM transactions WHERE id = 1"))
    assert result.status is Status.ERROR and "foo" in result.error


# --- Log search policy -----------------------------------------------------------


async def test_log_search_requires_scope_and_bounded_window():
    gw, conns = make()
    no_scope = QueryRequest(source="logs", params={"query": "error", "start": "2026-11-10T08:00:00Z", "end": "2026-11-10T08:10:00Z"})
    wildcard = logs(cat="app/*")
    wide = logs(minutes=180)
    stacked = logs(query="timeout or refused or reset | count")
    for req, needle in [(no_scope, "scope.index is required"), (wildcard, "wildcard"), (wide, "window is 3h"), (stacked, "OR terms")]:
        result = await gw.execute(req)
        assert result.status is Status.REJECTED
        assert any(needle in v for v in result.violations), result.violations
        assert result.hint
    assert conns["logs"].calls == []


async def test_raw_lines_need_an_aggregate_on_the_same_scope_first():
    gw, _ = make()
    early = await gw.execute(logs(mode="raw", minutes=10))
    assert early.status is Status.REJECTED and "before any aggregate" in early.violations[0]
    assert (await gw.execute(logs(mode="aggregate", minutes=30))).ok
    assert (await gw.execute(logs(mode="raw", minutes=10))).ok
    other_scope = await gw.execute(logs(mode="raw", minutes=10, cat="worker/celery"))
    assert other_scope.status is Status.REJECTED
    other_investigation = await gw.execute(logs(mode="raw", minutes=10, inv="inv-2"))
    assert other_investigation.status is Status.REJECTED


async def test_log_search_cost_cap_approval_and_investigation_budget():
    approvals = []

    async def approver(req, est):
        approvals.append(est.amount)
        return True

    gw, conns = make(approver=approver)
    # 120 GB/h x 30m = 60 GB: over the 20 GB per-query cap
    too_big = await gw.execute(logs(cat="gateway/nginx", minutes=30))
    assert too_big.status is Status.REJECTED and "per-query limit" in too_big.violations[0]
    assert "Narrow" in too_big.hint

    # 8 GB/h x 30m = 4 GB: fine, no approval needed
    a = await gw.execute(logs(minutes=30))
    assert a.ok and a.cost == 4 and a.budget_remaining == 26 and approvals == []

    # 120 GB/h x 6m = 12 GB: above the 10 GB soft cap, approved by a human
    b = await gw.execute(logs(cat="gateway/nginx", minutes=6))
    assert b.ok and approvals == [12] and b.budget_remaining == 14

    # 12 GB more is fine, then the 30 GB investigation budget is spent
    c = await gw.execute(logs(cat="gateway/nginx", minutes=6, query="5xx | count by path"))
    assert c.ok and c.budget_remaining == 2
    d = await gw.execute(logs(minutes=30, query="timeout | count"))
    assert d.status is Status.BUDGET_EXHAUSTED and "2 left" in d.error
    assert len(conns["logs"].calls) == 3


async def test_above_soft_cap_without_an_approver_is_rejected():
    gw, conns = make()
    result = await gw.execute(logs(cat="gateway/nginx", minutes=6))
    assert result.status is Status.REJECTED and "not approved" in result.violations[0]
    assert conns["logs"].calls == []


# --- NRQL policy ---------------------------------------------------------------


def test_nrql_linter():
    policy = parse_sources(SOURCES)["apm"].policy
    ctx = LintContext()
    assert "SINCE" in lint_nrql({"nrql": "SELECT count(*) FROM Transaction"}, policy, ctx).violations[0]
    assert "limit is 6h" in lint_nrql({"nrql": "SELECT count(*) FROM Transaction SINCE 7 days ago"}, policy, ctx).violations[0]
    assert lint_nrql({"nrql": "DROP x; SELECT 1 SINCE 1 hour ago"}, policy, ctx).violations
    ok = lint_nrql({"nrql": "SELECT count(*) FROM Transaction FACET name SINCE 30 minutes ago LIMIT MAX"}, policy, ctx)
    assert not ok.violations and "LIMIT 100" in ok.params["nrql"] and "MAX" not in ok.params["nrql"]
    added = lint_nrql({"nrql": "FROM Transaction SELECT average(duration) SINCE 1 hour ago"}, policy, ctx)
    assert added.params["nrql"].endswith("LIMIT 100")


# --- Reliability ---------------------------------------------------------------


async def test_timeout_is_retried_and_then_succeeds():
    conn = FakeConnector(script=["hang"])
    gw, _ = make({"apm": conn})
    result = await gw.execute(QueryRequest("apm", {"nrql": "SELECT count(*) FROM Transaction SINCE 5 minutes ago"}))
    assert result.status is Status.OK and result.attempts == 2


async def test_retries_are_bounded_and_the_circuit_opens_then_recovers():
    clock = Clock()
    conn = FakeConnector(script=["hang"] * 3 + [ConnectorTimeout()] * 3)
    gw, _ = make({"apm": conn}, clock=clock)
    q = lambda n: QueryRequest("apm", {"nrql": f"SELECT count(*) FROM T{n} SINCE 5 minutes ago"})  # noqa: E731

    first = await gw.execute(q(1))
    assert first.status is Status.TIMEOUT and first.attempts == 3  # 1 try + 2 retries, never more
    assert len(conn.calls) == 3

    blocked = await gw.execute(q(2))
    assert blocked.status is Status.UNAVAILABLE and "consecutive failures" in blocked.error
    assert len(conn.calls) == 3  # not even attempted

    clock.t += 61  # cooldown over: one probe is allowed, it fails, circuit re-opens at once
    probe = await gw.execute(q(3))
    assert probe.status is Status.TIMEOUT and probe.attempts == 1
    clock.t += 61
    conn.script.clear()
    assert (await gw.execute(q(4))).status is Status.OK
    assert (await gw.execute(q(5))).status is Status.OK


async def test_transient_error_is_retried_but_a_permanent_one_is_not():
    conn = FakeConnector(script=[ConnectorError("502 bad gateway", retryable=True)])
    gw, _ = make({"apm": conn})
    assert (await gw.execute(QueryRequest("apm", {"nrql": "SELECT 1 FROM A SINCE 1 minute ago"}))).attempts == 2

    conn = FakeConnector(script=[ConnectorError("syntax error near FACET")])
    gw, _ = make({"apm": conn})
    result = await gw.execute(QueryRequest("apm", {"nrql": "SELECT 1 FROM A SINCE 1 minute ago"}))
    assert result.status is Status.ERROR and result.attempts == 1 and "syntax error" in result.error


async def test_connector_bug_becomes_a_structured_error():
    gw, _ = make({"apm": FakeConnector(script=[KeyError("results")])})
    result = await gw.execute(QueryRequest("apm", {"nrql": "SELECT 1 FROM A SINCE 1 minute ago"}))
    assert result.status is Status.ERROR and "KeyError" in result.error


async def test_rotated_credentials_are_picked_up_mid_investigation():
    env = dict(ENV)
    seen = []

    class Rotating(FakeConnector):
        async def execute(self, params, creds, *, timeout_s, max_rows):
            seen.append(creds["access_key"].reveal())
            if creds["access_key"].reveal() != "sumo-key-NEW777":
                raise AuthRejected
            return RawResult(rows=[{"count": 3}])

    gw, _ = make({"logs": Rotating()}, env=env)
    await gw.broker.resolve("secret://sumo/prod#access_key", source="logs")  # cache holds the old key
    env["SUMO_PROD_ACCESS_KEY"] = "sumo-key-NEW777"  # rotated at the source

    result = await gw.execute(logs())
    assert result.status is Status.OK
    assert seen == ["sumo-key-55aa91", "sumo-key-NEW777"]


async def test_revoked_credentials_stop_all_calls_to_that_source():
    class Revoked(FakeConnector):
        async def execute(self, params, creds, *, timeout_s, max_rows):
            self.calls.append(params)
            raise AuthRejected

    conn = Revoked()
    gw, _ = make({"logs": conn})
    first = await gw.execute(logs())
    assert first.status is Status.UNAVAILABLE and "auth" in first.error
    assert len(conn.calls) == 2  # original + one retry with refreshed credentials
    second = await gw.execute(logs(query="other | count"))
    assert second.status is Status.UNAVAILABLE and len(conn.calls) == 2
    assert "evidence gap" in second.hint


async def test_concurrency_limit_is_respected():
    conn = FakeConnector()
    gw, _ = make({"apm": conn})
    reqs = [QueryRequest("apm", {"nrql": f"SELECT {i} FROM A SINCE 1 minute ago"}) for i in range(8)]
    results = await asyncio.gather(*(gw.execute(r) for r in reqs))
    assert all(r.ok for r in results) and conn.max_active <= 2


async def test_investigation_stops_after_too_many_rejections_or_queries():
    gw, _ = make(ledger=BudgetLedger(max_queries=2, max_rejections=3))
    for _ in range(3):
        assert (await gw.execute(sql("SELECT id FROM transactions"))).status is Status.REJECTED
    stopped = await gw.execute(sql("SELECT id FROM transactions WHERE id = 1"))
    assert stopped.status is Status.BUDGET_EXHAUSTED and "rejected" in stopped.error

    gw, _ = make(ledger=BudgetLedger(max_queries=2))
    for i in range(2):
        assert (await gw.execute(sql(f"SELECT id FROM transactions WHERE id = {i}"))).ok
    assert (await gw.execute(sql("SELECT id FROM transactions WHERE id = 9"))).status is Status.BUDGET_EXHAUSTED


# --- Results: cache, caps, redaction, scrubbing, audit ----------------------------------


async def test_repeat_query_is_served_from_cache_and_costs_nothing():
    gw, conns = make()
    first = await gw.execute(logs())
    second = await gw.execute(logs())
    assert second.cached and second.rows == first.rows and second.cost == 0
    assert len(conns["logs"].calls) == 1
    assert second.budget_remaining == first.budget_remaining == 26


async def test_rows_are_capped_and_marked_partial():
    gw, _ = make({"appdb": FakeConnector(rows=[{"id": i} for i in range(80)])})
    result = await gw.execute(sql("SELECT id FROM transactions WHERE user_id = 7"))
    assert result.status is Status.PARTIAL and len(result.rows) == 50 and result.truncated
    assert result.to_agent()["truncated"] is True


async def test_personal_data_becomes_stable_tokens():
    rows = [
        {"id": 1, "email": "asha@example.com", "phone": "+91-9800000000", "note": "mail asha@example.com"},
        {"id": 2, "email": "asha@example.com", "phone": None, "note": "ok"},
    ]
    gw, _ = make({"appdb": FakeConnector(rows=rows)})
    out = (await gw.execute(sql("SELECT id, email, phone, note FROM users WHERE id IN (1, 2)"))).rows
    dumped = json.dumps(out)
    assert "asha" not in dumped and "9800000000" not in dumped
    assert out[0]["email"] == out[1]["email"] and out[0]["email"].startswith("pii_")  # still joinable
    assert out[1]["phone"] is None and out[0]["id"] == 1


async def test_secrets_never_reach_results_errors_or_the_audit_log(tmp_path):
    leaky_rows = [{"msg": "connected with password db-pass-93kd72"}]
    audit = AuditLog(tmp_path / "audit" / "queries.jsonl")
    gw, _ = make({"appdb": FakeConnector(rows=leaky_rows)}, audit=audit)
    ok = await gw.execute(sql("SELECT id FROM transactions WHERE id = 1", intent="check db-pass-93kd72"))
    assert "db-pass-93kd72" not in json.dumps(ok.rows)

    boom = ConnectorError('could not connect to "postgresql://ro:db-pass-93kd72@db.internal:5432/app"')
    gw2, _ = make({"appdb": FakeConnector(script=[boom])}, audit=audit)
    err = await gw2.execute(sql("SELECT id FROM transactions WHERE id = 2"))
    assert err.status is Status.ERROR and "db-pass-93kd72" not in err.error and "db.internal" in err.error

    text = (tmp_path / "audit" / "queries.jsonl").read_text()
    assert "db-pass-93kd72" not in text and "sumo-key-55aa91" not in text
    assert len(text.strip().splitlines()) == 2


async def test_every_decision_is_audited_including_refusals():
    gw, _ = make()
    await gw.execute(sql("SELECT id FROM transactions WHERE id = 1", intent="find the failing row", step_id="s1"))
    await gw.execute(sql("DELETE FROM transactions"))
    await gw.execute(QueryRequest("nope", {}))
    records = gw.audit.records
    assert [r.status for r in records] == ["ok", "rejected", "rejected"]
    assert records[0].intent == "find the failing row" and records[0].step_id == "s1"
    assert records[0].params["sql"].endswith("LIMIT 51") and records[1].violations
    assert "Available sources" in (await gw.execute(QueryRequest("nope", {}))).hint


def test_agents_only_see_source_names_and_descriptions():
    gw, _ = make()
    described = json.dumps(gw.describe_sources())
    assert "appdb" in described and "secret://" not in described and "password" not in described


def test_linters_are_pure_functions_usable_without_a_gateway():
    policy = parse_sources(SOURCES)["appdb"].policy
    assert lint_sql({"sql": "SELECT id FROM transactions WHERE id = 1"}, policy, LintContext()).violations == []
    lp = parse_sources(SOURCES)["logs"].policy
    res = lint_log_search(logs().params, lp, LintContext())
    assert res.estimate.amount == 4 and res.estimate.unit == "gb_scanned"
