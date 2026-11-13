"""Integration tests against the docker-compose Postgres. Skipped when it is not running."""

import os
import socket

import asyncpg
import pytest

from incident_agents.connectors.postgres import PostgresConnector
from incident_agents.gateway import QueryGateway, QueryRequest, Status, parse_sources
from incident_agents.secrets import CredentialBroker, EnvSecretStore

PORT = int(os.environ.get("PG_PORT", 55432))
ADMIN_PW = os.environ.get("PG_ADMIN_PASSWORD", "localdev-admin")


def _up() -> bool:
    try:
        socket.create_connection(("localhost", PORT), timeout=0.5).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _up(), reason="local Postgres is not running (docker compose up -d)")

SETUP = """
DROP TABLE IF EXISTS transactions, users;
CREATE TABLE users (id int PRIMARY KEY, email text, password_hash text);
CREATE TABLE transactions (
    id bigserial PRIMARY KEY, user_id int NOT NULL, amount numeric(10,2), note text,
    created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX ON transactions (user_id);
INSERT INTO users SELECT g, 'user' || g || '@example.com', 'hash' FROM generate_series(1, 200) g;
INSERT INTO transactions (user_id, amount, note)
    SELECT (g % 200) + 1, (g % 5000) / 7.0, 'note ' || g FROM generate_series(1, 60000) g;
ANALYZE users; ANALYZE transactions;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investigator_ro') THEN
        CREATE ROLE investigator_ro LOGIN PASSWORD 'ro-pass-7731xz';
    END IF;
END $$;
GRANT SELECT ON users, transactions TO investigator_ro;
"""


@pytest.fixture
async def gw():
    admin = await asyncpg.connect(host="localhost", port=PORT, user="postgres", password=ADMIN_PW, database="app")
    await admin.execute(SETUP)
    await admin.close()
    sources = parse_sources(
        {
            "sources": {
                "appdb": {
                    "type": "postgres",
                    "kind": "sql",
                    "auth": {"username": "investigator_ro", "password": "secret://appdb/ro#password"},
                    "options": {"host": "localhost", "port": PORT, "database": "app"},
                    "policy": {
                        "timeout_s": 2,
                        "max_retries": 0,
                        "rate_per_min": 0,
                        "max_rows": 20,
                        "cost_unit": "planner_cost",
                        "max_cost_per_query": 500,
                        "require_estimate": True,
                        "redact_fields": ["email"],
                        "sql": {
                            "tables": {
                                "transactions": {"indexed": ["id", "user_id", "created_at"]},
                                "users": {"indexed": ["id"], "deny_columns": ["password_hash"]},
                            }
                        },
                    },
                }
            }
        }
    )
    broker = CredentialBroker(store=EnvSecretStore({"APPDB_RO_PASSWORD": "ro-pass-7731xz"}))
    return QueryGateway.from_config(sources, broker)


def q(sql):
    return QueryRequest("appdb", {"sql": sql}, investigation_id="inv-pg")


async def test_indexed_query_runs_and_types_are_json_safe(gw):
    result = await gw.execute(q("SELECT id, user_id, amount, created_at FROM transactions WHERE user_id = 7"))
    assert result.status is Status.PARTIAL and len(result.rows) == 20  # 300 rows match; the agent is told it was cut off
    row = result.rows[0]
    assert row["user_id"] == 7 and isinstance(row["amount"], float) and isinstance(row["created_at"], str)
    assert 0 < result.est_cost <= 500


async def test_explain_blocks_a_full_table_scan_that_passes_the_syntax_check(gw):
    # created_at is on the allowed list but has no index here: the linter passes it, EXPLAIN catches it.
    result = await gw.execute(q("SELECT id FROM transactions WHERE created_at > '2000-01-01' ORDER BY amount"))
    assert result.status is Status.REJECTED
    assert "per-query limit" in result.violations[0] and "sequential scan on transactions" in result.violations[0]


async def test_email_is_tokenised_and_restricted_column_is_blocked(gw):
    ok = await gw.execute(q("SELECT id, email FROM users WHERE id IN (1, 2)"))
    assert ok.status is Status.OK and all(r["email"].startswith("pii_") for r in ok.rows)
    blocked = await gw.execute(q("SELECT password_hash FROM users WHERE id = 1"))
    assert blocked.status is Status.REJECTED


async def test_unknown_column_is_a_structured_error(gw):
    result = await gw.execute(q("SELECT nope FROM transactions WHERE id = 1"))
    assert result.status is Status.ERROR and "nope" in result.error


async def test_session_is_read_only_even_if_the_linter_were_bypassed():
    conn = PostgresConnector({"host": "localhost", "port": PORT, "database": "app"})
    creds = {"username": "postgres", "password": ADMIN_PW}  # even a superuser session cannot write
    from incident_agents.gateway import ConnectorError

    with pytest.raises(ConnectorError, match="ReadOnlySQLTransaction"):
        await conn.execute({"sql": "DELETE FROM transactions WHERE id = 1"}, creds, timeout_s=2, max_rows=10)


async def test_statement_timeout_stops_a_slow_query():
    conn = PostgresConnector({"host": "localhost", "port": PORT, "database": "app"})
    creds = {"username": "postgres", "password": ADMIN_PW}
    from incident_agents.gateway import ConnectorTimeout

    with pytest.raises(ConnectorTimeout):
        await conn.execute({"sql": "SELECT pg_sleep(5)"}, creds, timeout_s=0.3, max_rows=10)


async def test_wrong_password_marks_the_source_unavailable(gw):
    gw.broker.store = EnvSecretStore({"APPDB_RO_PASSWORD": "wrong-password-000"})
    result = await gw.execute(q("SELECT id FROM transactions WHERE id = 1"))
    assert result.status is Status.UNAVAILABLE and "auth" in result.error
    assert "wrong-password-000" not in result.error
