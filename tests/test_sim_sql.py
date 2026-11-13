"""Simulated SQL source: Postgres-dialect SQL against a SQLite file."""

import sqlite3

import pytest

from incident_agents.connectors.sim_sql import SimSqlConnector, to_sqlite
from incident_agents.gateway import ConnectorError, QueryGateway, QueryRequest, Status, parse_sources
from incident_agents.secrets import CredentialBroker, EnvSecretStore


@pytest.fixture
def data(tmp_path):
    db = sqlite3.connect(tmp_path / "app.db")
    db.executescript(
        """
        CREATE TABLE jobs (id TEXT PRIMARY KEY, user_id INTEGER, type TEXT, status TEXT, attempts INTEGER, last_error TEXT, created_at TEXT);
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, password_hash TEXT);
        CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT);
        """
    )
    for i in range(30):
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
            (f"job-{i:03d}", 100 + i % 4, "process_receipt", "failed" if i % 5 == 0 else "succeeded", 1 + i % 3,
             "Upstream TIMEOUT after 30s" if i % 5 == 0 else None, f"2026-11-10T08:{i:02d}:30Z"),
        )
    db.execute("INSERT INTO users VALUES (100, 'a@example.com', 'x')")
    db.executemany("INSERT INTO categories VALUES (?,?)", [(1, "food"), (2, "travel")])
    db.commit()
    db.close()
    return tmp_path


async def run(conn, sql, max_rows=100):
    return await conn.execute({"sql": sql}, {}, timeout_s=2, max_rows=max_rows)


async def test_plain_selects_and_row_cap(data):
    conn = SimSqlConnector({"data_dir": str(data)})
    res = await run(conn, "SELECT status, COUNT(*) AS n FROM jobs WHERE created_at >= '2026-11-10T08:00:00Z' GROUP BY status ORDER BY n DESC")
    assert res.rows == [{"status": "succeeded", "n": 24}, {"status": "failed", "n": 6}] and not res.truncated
    capped = await run(conn, "SELECT id FROM jobs WHERE user_id = 100 ORDER BY id LIMIT 6", max_rows=5)
    assert len(capped.rows) == 5 and capped.truncated
    exact = await run(conn, "SELECT id FROM jobs WHERE user_id = 100 ORDER BY id LIMIT 5", max_rows=5)
    assert len(exact.rows) == 5 and not exact.truncated


async def test_postgres_time_functions_work_on_iso_text(data):
    conn = SimSqlConnector({"data_dir": str(data), "now": "2026-11-10T08:30:00Z"})
    recent = await run(conn, "SELECT count(*) AS n FROM jobs WHERE created_at > now() - interval '10 minutes'")
    assert recent.rows == [{"n": 10}]  # 08:20:30 .. 08:29:30
    buckets = await run(conn, "SELECT date_trunc('hour', created_at) AS h, count(*) AS n FROM jobs WHERE created_at >= '2026-11-10T08:00:00Z'::timestamptz GROUP BY 1")
    assert buckets.rows == [{"h": "2026-11-10T08:00:00Z", "n": 30}]
    minute = await run(conn, "SELECT date_trunc('minute', created_at) AS m FROM jobs WHERE id = 'job-007'")
    assert minute.rows == [{"m": "2026-11-10T08:07:00Z"}]
    ilike = await run(conn, "SELECT count(*) AS n FROM jobs WHERE status = 'failed' AND last_error ILIKE '%timeout%'")
    assert ilike.rows == [{"n": 6}]
    part = await run(conn, "SELECT split_part(id, '-', 2) AS n, count(*) FILTER (WHERE attempts > 1) AS retried FROM jobs WHERE id = 'job-004' GROUP BY 1")
    assert part.rows == [{"n": "004", "retried": 1}]
    assert "TS_SHIFT('2026-11-10T08:30:00Z', 7200" in to_sqlite("SELECT 1 FROM jobs WHERE created_at < now() + INTERVAL '2 hours'", "2026-11-10T08:30:00Z")


async def test_sql_errors_name_the_problem_and_writes_cannot_run(data):
    conn = SimSqlConnector({"data_dir": str(data)})
    with pytest.raises(ConnectorError, match="no such column: retries"):
        await run(conn, "SELECT retries FROM jobs WHERE user_id = 100")
    with pytest.raises(ConnectorError, match="readonly|read-only|attempt to write"):
        await run(conn, "DELETE FROM jobs")
    with pytest.raises(ConnectorError, match="missing"):
        await run(SimSqlConnector({"data_dir": str(data), "file": "nope.db"}), "SELECT 1")


async def test_through_the_gateway_with_a_table_policy_and_faults(data):
    config = {"sources": {"appdb": {
        "type": "sim_sql", "kind": "sql",
        "options": {"data_dir": str(data), "faults": [{"on_calls": [1], "fault": "error", "retryable": False, "message": "replica is restarting"}]},
        "policy": {"rate_per_min": 0, "max_rows": 5, "timeout_s": 2, "redact_fields": ["email"], "sql": {"tables": {
            "jobs": {"indexed": ["id", "user_id", "created_at"]}, "users": {"indexed": ["id"], "deny_columns": ["password_hash"]}, "categories": {"small": True}}}},
    }}}
    gw = QueryGateway.from_config(parse_sources(config), CredentialBroker(store=EnvSecretStore({})))
    failed = await gw.execute(QueryRequest("appdb", {"sql": "SELECT name FROM categories"}))
    assert failed.status is Status.ERROR and "replica is restarting" in failed.error

    small = await gw.execute(QueryRequest("appdb", {"sql": "SELECT name FROM categories ORDER BY id"}))
    assert small.status is Status.OK and [r["name"] for r in small.rows] == ["food", "travel"]
    scan = await gw.execute(QueryRequest("appdb", {"sql": "SELECT count(*) FROM jobs"}))
    assert scan.status is Status.REJECTED and "user_id" in scan.hint
    capped = await gw.execute(QueryRequest("appdb", {"sql": "SELECT id, attempts FROM jobs WHERE created_at >= '2026-11-10T08:00:00Z' ORDER BY id"}))
    assert capped.status is Status.PARTIAL and len(capped.rows) == 5 and capped.truncated
    secret = await gw.execute(QueryRequest("appdb", {"sql": "SELECT password_hash FROM users WHERE id = 100"}))
    assert secret.status is Status.REJECTED
    masked = await gw.execute(QueryRequest("appdb", {"sql": "SELECT id, email FROM users WHERE id = 100"}))
    assert masked.rows[0]["email"].startswith("pii_")
    bad = await gw.execute(QueryRequest("appdb", {"sql": "SELECT nope FROM jobs WHERE user_id = 100"}))
    assert bad.status is Status.ERROR and "no such column: nope" in bad.error
