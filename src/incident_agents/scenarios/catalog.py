"""The incident scenarios.

Each function perturbs a `World` and returns `(incident, answer)`:
`incident` is the page the investigating system receives (symptoms only);
`answer` is the hidden key with the root cause, the evidence a correct
investigation would reach, the red herrings, and a `golden_path` of tool
calls used by the tests to prove the scenario is solvable.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from typing import Any

from .codebase import DEV, ISHAAN, MAYA
from .world import AFTER, World


def win(w: World, a: float, b: float) -> dict[str, str]:
    return {"start": w.ts(a), "end": w.ts(b)}


def page(w: World, title: str, service: str, severity: str, description: str) -> dict[str, Any]:
    return {"title": title, "service": service, "severity": severity, "environment": "production",
            "detected_at": w.ts(0), "window": win(w, -30, AFTER), "description": description}


def step(tool: str, **args: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args}


def key(
    w: World, *, tags: list[str], service: str, component: str, category: str, summary: str, trigger: tuple[str, str],
    evidence: list[tuple[str, str, str]], red_herrings: list[tuple[str, str]], min_hops: int, path: list[dict[str, Any]],
    acceptable: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "tags": tags,
        "root_cause": {"service": service, "component": component, "category": category, "summary": summary,
                       "trigger": {"type": trigger[0], "ref": trigger[1]}},
        "acceptable": acceptable or [],
        "key_evidence": [{"kind": k, "must_contain": m, "why": why} for k, m, why in evidence],
        "red_herrings": [{"description": d, "why_innocent": why} for d, why in red_herrings],
        "min_hops": min_hops,
        "golden_path": path,
    }


# 1 -----------------------------------------------------------------------------------


def idempotency_key_regression(w: World):
    sha = w.commit(
        "fivo-api", -51, MAYA, "idempotency: shorter keys, client_seq already identifies the entry",
        patch=[("app/services/idempotency.py", 'return f"{user_id}:{device_id}:{client_seq}"', 'return f"{user_id}:{client_seq}"')],
    )
    dep = w.deploy("fivo-api", -22)
    live = -22 + 20 / 60
    w.key_fn = lambda r: f"{r.user_id}:{r.client_seq}" if r.t >= live else f"{r.user_id}:{r.device_id}:{r.client_seq}"
    message = ('sqlalchemy.exc.IntegrityError: (psycopg.errors.UniqueViolation) duplicate key value violates unique constraint '
               '"uq_transactions_idempotency_key"')
    seen: dict[str, Any] = {}
    failures = []
    for r in sorted(w.reqs("POST", start=live), key=lambda r: r.t):
        if r.client_seq is None or r.status >= 400:
            continue
        k = f"{r.user_id}:{r.client_seq}"
        if k in seen and seen[k].device_id != r.device_id:
            r.fail(500, message, peer="neon-postgres", peer_error='duplicate key value violates unique constraint "uq_transactions_idempotency_key"')
            failures.append((r, seen[k]))
            for delay in (0.03, 0.12):  # the app retries a failed save twice
                w.add_request(w.clone_request(r, r.t + delay))
        else:
            seen.setdefault(k, r)
    if len(failures) < 8:
        raise ValueError("idempotency scenario produced too few collisions")
    failed, first = failures[0]
    user = failed.user_id
    incident = page(
        w, "fivo-api: 5xx rate above 5% for 10 minutes", "fivo-api", "high",
        "The share of 5xx responses from fivo-api has been above 5% for ten minutes and is still rising. "
        "Failures are concentrated on POST /transactions and POST /transactions/quick-add. "
        "Support has two reports of entries that will not save from the mobile app.",
    )
    answer = key(
        w, tags=["multi_hop"], service="fivo-api", component="idempotency key (app/services/idempotency.py)", category="code_regression",
        summary=(
            f"The fivo-api deploy {dep['id']} shipped a commit that removed device_id from the transaction idempotency key. "
            "client_seq is a per-device counter, so two devices of the same user now produce the same key and the second insert "
            "violates uq_transactions_idempotency_key, which surfaces as 500s for users with more than one device. The app retries each failed save, multiplying the errors."
        ),
        trigger=("deploy", sha),
        evidence=[
            ("log", "uq_transactions_idempotency_key", "the failing requests are unique-constraint violations on the idempotency key"),
            ("deploy", sha, "a fivo-api deploy went live shortly before the first failure"),
            ("diff", 'return f"{user_id}:{client_seq}"', "the deployed commit drops device_id from the key"),
            ("db", f'"idempotency_key": "{user}:{first.client_seq}"', "rows written after the deploy carry two-part keys; the colliding entry came from the user's other device"),
        ],
        red_herrings=[("Past incident INC-2026-024 had 5xx on POST /transactions", "that was a database cold start with pool timeouts on every route; here only inserts fail and the error is a constraint violation")],
        min_hops=3,
        path=[
            step("logs_count", service="fivo-api", filters={"level": "error"}, group_by=["route", "status"], **win(w, -45, 15)),
            step("logs_fetch", service="fivo-api", filters={"level": "error", "route": "/transactions"}, limit=20, **win(w, -12, 0)),
            step("deploys_list", service="fivo-api", **win(w, -180, AFTER)),
            step("code_diff", repo="fivo-api", base=dep["prev_sha"], head=sha),
            step("db_query", sql=f"SELECT id, device_id, client_seq, idempotency_key, created_at FROM transactions WHERE user_id = {user} AND created_at >= '{w.ts(-120)}' ORDER BY created_at"),
        ],
    )
    return incident, answer


# 2 -----------------------------------------------------------------------------------


def db_pool_config_change(w: World):
    innocent = w.commit(
        "fivo-api", -47, DEV, "access log: record the request id",
        patch=[
            ("app/logging_setup.py", '                    "trace_id": request.headers.get("x-trace-id", ""),\n',
             '                    "trace_id": request.headers.get("x-trace-id", ""),\n                    "request_id": request.headers.get("x-request-id", ""),\n'),
            ("README.md", "Settings come from environment variables, see `app/config.py`.\n",
             "Settings come from environment variables, see `app/config.py`.\n\nAccess logs are JSON lines; each one carries the trace id and the request id.\n"),
        ],
    )
    dep = w.deploy("fivo-api", -24)
    cfg = w.config_change("fivo-api", -14, "DB_POOL_SIZE", "10", "2", by="dashboard:dev", note="stay under the database plan's connection limit")
    live = -14 + 0.3 + 18 / 60
    rng = random.Random(w.seed + 5)
    for r in w.reqs(start=live):
        if r.span("neon-postgres") is None or r.status >= 400:
            continue
        if rng.random() < 0.3:
            r.spans = [{"kind": "db", "peer": "neon-postgres", "name": "acquire pool connection", "ms": 5000.0}]
            r.fail(500, "sqlalchemy.exc.TimeoutError: QueuePool limit of size 2 overflow 0 reached, connection timed out, timeout 5.00",
                   peer="neon-postgres", peer_error="no pooled connection became free within 5.00s", duration_ms=5006.0)
        else:
            wait = round(rng.uniform(80, 3200), 1)
            r.spans.insert(0, {"kind": "db", "peer": "neon-postgres", "name": "acquire pool connection", "ms": wait})
            r.duration_ms = round(r.duration_ms + wait, 1)
    w.shape("fivo-api", "db_connections", lambda m, v: 2.0 if m >= -13 else v)
    w.shape("neon-postgres", "active_connections", lambda m, v: max(3.0, v - 3) if m >= -13 else v)
    incident = page(
        w, "fivo-api: 5xx rate above 5% and p95 above 5 s", "fivo-api", "high",
        "5xx responses and latency on fivo-api rose sharply about ten minutes ago. Several routes are affected "
        "(GET /transactions, POST /transactions, GET /summary). The health check still passes.",
    )
    answer = key(
        w, tags=["misleading", "multi_hop"], service="fivo-api", component="database connection pool (DB_POOL_SIZE)", category="config_change",
        summary=(
            f"Configuration change {cfg['id']} lowered DB_POOL_SIZE on fivo-api from 10 to 2. The engine is created with max_overflow=0 and a 5 s pool timeout, "
            "so under ordinary traffic requests queue for one of two connections and those that wait longer than 5 s fail with a QueuePool timeout. "
            "The fivo-api deploy ten minutes earlier only changed access logging."
        ),
        trigger=("config_change", cfg["id"]),
        evidence=[
            ("log", "QueuePool limit of size 2", "the errors are pool-acquire timeouts and name the pool size in effect"),
            ("config", "DB_POOL_SIZE", "the pool size was changed from 10 to 2 at the time the errors began"),
            ("metric", '"metric": "db_connections"', "fivo-api's open connections drop to a flat 2 at the change"),
            ("code", "max_overflow=0", "the pool cannot grow beyond DB_POOL_SIZE, so the setting is a hard cap"),
            ("diff", "request_id", "the earlier deploy only touched access logging"),
        ],
        red_herrings=[
            (f"fivo-api deploy {dep['id']} ten minutes before the errors", "its diff adds a field to the access log and a README line; errors start at the config change, not at the deploy"),
            ("Past incident INC-2026-024 shows the same QueuePool error", "that one was a database cold start; here the database is healthy and the pool size itself changed"),
        ],
        min_hops=3,
        path=[
            step("logs_count", service="fivo-api", filters={"level": "error"}, timeslice="5m", **win(w, -60, 15)),
            step("logs_fetch", service="fivo-api", filters={"level": "error"}, limit=20, **win(w, -10, 0)),
            step("deploys_list", service="fivo-api", **win(w, -60, AFTER)),
            step("code_diff", repo="fivo-api", base=dep["prev_sha"], head=innocent),
            step("config_changes_list", service="fivo-api", **win(w, -60, AFTER)),
            step("metrics_query", service="fivo-api", metric="db_connections", **win(w, -60, AFTER)),
            step("code_read", repo="fivo-api", ref=innocent, path="app/db.py"),
        ],
    )
    return incident, answer


# 3 -----------------------------------------------------------------------------------


def groq_degradation_retry_storm(w: World):
    onset = -16
    rng = random.Random(w.seed + 9)
    for t in w.tasks_of("categorize_transaction", start=onset):
        plan: list[tuple[float, str, Any]] = []
        for i in range(6):
            if rng.random() < 0.6:
                if rng.random() < 0.7:
                    ms, exc = 30000.0, "groq request timed out after 30s"
                else:
                    ms, exc = rng.uniform(4000, 9000), "groq returned 503 Service Unavailable"
                plan.append((ms, "fail", f"UpstreamError('{exc}')") if i == 5 else (ms, "retry", (2**i, exc)))
            else:
                plan.append((rng.uniform(10000, 22000), "ok", None))
                break
        t.plan = plan
    w.shape("groq-llm", "client_latency_p95_ms", lambda m, v: v if m < onset - 1 else (9400.0 if m < onset else 29300 + (v - 470) * 6))
    w.shape("groq-llm", "client_error_rate", lambda m, v: v if m < onset else 0.55 + 0.07 * abs(math.sin(m * 1.9)))
    incident = page(
        w, "fivo-api: p95 latency above 3 s for 10 minutes", "fivo-api", "high",
        "p95 latency on fivo-api has been above 3 s for ten minutes. POST /transactions/quick-add is returning 504 for most callers. "
        "Other routes look normal. Users report that one-tap entries spin and then fail.",
    )
    answer = key(
        w, tags=["multi_hop"], service="groq-llm", component="categorisation provider (Groq API)", category="dependency_failure",
        summary=(
            "The categorisation provider (Groq) became slow and started failing: most calls from fivo-worker time out at 30 s or return 503. "
            "categorize_transaction tasks hold both worker slots and retry, so the queue backs up, and POST /transactions/quick-add on fivo-api, "
            "which waits 8 s for the worker's result, returns 504. Nothing was deployed or reconfigured."
        ),
        trigger=("external", ""),
        acceptable=[{"service": "fivo-worker", "category": "dependency_failure"}],
        evidence=[
            ("log", "categorisation result not ready after 8000ms", "the API failure is a wait on the worker, not an API fault"),
            ("metric", '"metric": "queue_depth"', "the worker queue grows from the same minute"),
            ("log", "groq request timed out after 30s", "worker tasks are failing on the provider call"),
            ("metric", '"metric": "client_latency_p95_ms"', "client-side latency to the provider jumps from under 0.5 s to about 30 s"),
            ("deploy", "No deploys found", "no deploy of either service in the window"),
        ],
        red_herrings=[
            ("Past incident INC-2026-029 (categorisation backlog)", "that was a 429 rate limit after a bulk card sync; here there is no traffic spike and the provider returns timeouts and 503"),
        ],
        min_hops=4,
        path=[
            step("metrics_query", service="fivo-api", metric="p95_latency_ms", **win(w, -60, AFTER)),
            step("logs_count", service="fivo-api", filters={"level": "error"}, group_by=["route", "status"], **win(w, -30, 10)),
            step("logs_fetch", service="fivo-api", filters={"level": "error", "route": "/transactions/quick-add"}, limit=10, **win(w, -10, 0)),
            step("topology_get", service="fivo-api"),
            step("metrics_query", service="fivo-worker", metric="queue_depth", **win(w, -60, AFTER)),
            step("logs_count", service="fivo-worker", filters={"level": "warning"}, group_by=["task"], **win(w, -30, 10)),
            step("logs_fetch", service="fivo-worker", filters={"level": "warning"}, limit=20, **win(w, -10, 0)),
            step("metrics_query", service="groq-llm", metric="client_latency_p95_ms", **win(w, -60, AFTER)),
            step("deploys_list", service="fivo-worker", **win(w, -180, AFTER)),
            step("config_changes_list", **win(w, -180, AFTER)),
        ],
    )
    return incident, answer


# 4 -----------------------------------------------------------------------------------


def worker_oom_embedding_batch(w: World):
    sha = w.commit(
        "fivo-worker", -75, DEV, "embeddings: clear the legacy import backlog in one pass",
        patch=[
            ("worker/tasks/embeddings.py",
             "    done = 0\n    for batch in chunked(repo.iter_pending_receipt_ids(), BATCH_SIZE):\n"
             "        texts = [storage.read_text(receipt_id) for receipt_id in batch]\n"
             "        repo.save_embeddings(batch, voyage.embed(texts))\n        done += len(batch)\n",
             "    pending = repo.list_pending_receipts()\n"
             "    texts = [storage.read_text(receipt.id) for receipt in pending]\n"
             "    vectors: list[list[float]] = []\n    for batch in chunked(texts, BATCH_SIZE):\n        vectors.extend(voyage.embed(batch))\n"
             "    repo.save_embeddings([receipt.id for receipt in pending], vectors)\n    done = len(pending)\n"),
            ("worker/repo.py", "def mark_receipt(receipt_id: int, status: str) -> None:",
             "def list_pending_receipts() -> list[ReceiptRow]:\n    with session_scope() as s:\n        rows = s.execute(\n"
             "            text(\"SELECT id, user_id, object_key, content_type FROM receipts WHERE status = 'processed' AND embedded_at IS NULL ORDER BY id\")\n"
             "        )\n        return [ReceiptRow(*r) for r in rows]\n\n\ndef mark_receipt(receipt_id: int, status: str) -> None:"),
        ],
    )
    dep = w.deploy("fivo-worker", -46)
    live = -46 + 20 / 60
    first = None
    for t in w.tasks_of("embed_pending_receipts", start=live):
        first = first if first is not None else t.queued
        t.crash_ms, t.restart_s = 5000.0, 30.0
        t.crash_text = "Instance exceeded its 512 MB memory limit and was restarted (exit code 137)"
    assert first is not None
    w.shape("fivo-worker", "memory_mb", lambda m, v: v if m < math.floor(first) else min(511.0, 458 + 52 * abs(math.sin(m * 1.3))))
    w.simulate()
    stuck = sum(1 for j in w.db["jobs"] if j["status"] == "started" and j["created_at"] >= w.ts(live))
    pending = sum(1 for r in w.db["receipts"] if r["status"] == "processed" and r["embedded_at"] is None)
    incident = page(
        w, "fivo-api: receipts stuck in 'uploaded' for more than 30 minutes", "fivo-api", "high",
        "No receipt has finished processing for over 30 minutes. Uploads through POST /receipts still succeed, "
        "but GET /receipts/{id} keeps returning status 'uploaded'. Transaction entry is unaffected.",
    )
    answer = key(
        w, tags=["multi_hop"], service="fivo-worker", component="embed_pending_receipts task (worker/tasks/embeddings.py)", category="code_regression",
        summary=(
            f"The fivo-worker deploy {dep['id']} changed embed_pending_receipts to load every pending receipt and its text into memory at once instead of 64 per run. "
            f"With {pending} legacy receipts waiting, the task exceeds the 512 MB limit a few seconds after it starts and the instance is killed. "
            "Because tasks are acknowledged late and requeued when the process dies, the task starts again after every restart, so the worker is in a restart loop and receipt jobs never finish."
        ),
        trigger=("deploy", sha),
        evidence=[
            ("log", "512 MB memory limit", "the worker instance is being killed for memory, repeatedly"),
            ("metric", '"metric": "memory_mb"', "worker memory jumps to the limit from the first beat run after the deploy"),
            ("deploy", sha, "a fivo-worker deploy precedes the first restart"),
            ("diff", "repo.list_pending_receipts()", "the deployed commit loads the whole backlog instead of a capped batch"),
            ("db", f'"pending": {pending}', "the backlog the new code loads is thousands of receipts"),
        ],
        red_herrings=[
            ("Past incident INC-2026-021 (receipt queue stalled)", "that was the broker's connection limit; here the broker is healthy and the instance is restarting on memory"),
            (f"{stuck} jobs sit in 'started'", "they are victims: in flight when the instance was killed, not the cause"),
        ],
        min_hops=4,
        path=[
            step("metrics_query", service="fivo-worker", metric="queue_depth", **win(w, -90, AFTER)),
            step("logs_count", service="fivo-worker", filters={"level": "error"}, timeslice="10m", **win(w, -90, 10)),
            step("logs_fetch", service="fivo-worker", filters={"level": "error"}, limit=10, **win(w, -10, 0)),
            step("metrics_query", service="fivo-worker", metric="memory_mb", **win(w, -90, AFTER)),
            step("deploys_list", service="fivo-worker", **win(w, -180, AFTER)),
            step("code_diff", repo="fivo-worker", base=dep["prev_sha"], head=sha),
            step("db_query", sql="SELECT count(*) AS pending FROM receipts WHERE status = 'processed' AND embedded_at IS NULL"),
            step("db_query", sql=f"SELECT type, status, count(*) AS n FROM jobs WHERE created_at >= '{w.ts(-60)}' GROUP BY type, status ORDER BY n DESC"),
        ],
    )
    return incident, answer


# 5 -----------------------------------------------------------------------------------


def migration_dropped_index(w: World):
    migration = '''"""drop indexes that pg_stat_user_indexes reports as unused"""

from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade() -> None:
    # idx_scan = 0 for both since the last statistics reset
    op.drop_index("ix_receipts_content_type", table_name="receipts")
    op.drop_index("ix_transactions_user_created", table_name="transactions")
'''
    sha = w.commit(
        "fivo-api", -66, ISHAAN, "migrations: drop indexes with zero scans",
        add={"migrations/versions/0007_drop_unused_indexes.py": migration},
        patch=[("app/models.py", '        Index("ix_transactions_user_created", "user_id", "created_at"),\n', "")],
    )
    dep = w.deploy("fivo-api", -21)
    live = -21 + 20 / 60
    rng = random.Random(w.seed + 3)
    slow: Counter[int] = Counter()
    list_stmt = ("SELECT transactions.id, transactions.user_id, transactions.amount_minor, transactions.merchant, transactions.created_at "
                 "FROM transactions WHERE transactions.user_id = $1 ORDER BY transactions.created_at DESC LIMIT $2")
    sum_stmt = ("SELECT categories.name AS category, sum(transactions.amount_minor) AS total_minor FROM transactions "
                "LEFT OUTER JOIN categories ON categories.id = transactions.category_id "
                "WHERE transactions.user_id = $1 AND transactions.created_at >= date_trunc('month', now()) GROUP BY categories.name")
    for r in w.reqs("GET", start=live):
        if r.status >= 400 or r.route not in ("/transactions", "/summary"):
            continue
        ms = round(rng.lognormvariate(math.log(2300 if r.route == "/transactions" else 1700), 0.33), 1)
        r.slow("neon-postgres", ms)
        slow[math.floor(r.t)] += 1
        done = r.t + ms / 60000
        w.log(done, "neon-postgres", "warning", f"duration: {ms:.3f} ms  statement: {list_stmt if r.route == '/transactions' else sum_stmt}", host="neon-compute", source="log_min_duration_statement")
        if rng.random() < 0.3:
            w.log(done, "neon-postgres", "warning",
                  f"duration: {ms:.3f} ms  plan: {'Limit  ->  Sort' if r.route == '/transactions' else 'HashAggregate  ->  Hash Left Join'}  ->  "
                  f"Seq Scan on transactions  Filter: (user_id = $1)  Rows Removed by Filter: {rng.randint(417900, 418400)}",
                  host="neon-compute", source="auto_explain")
    w.shape("neon-postgres", "cpu_pct", lambda m, v: v if m < -20 else min(94.0, v + 52 + 7 * math.sin(m)))
    w.shape("neon-postgres", "slow_queries", lambda m, v: float(slow.get(m, 0)))
    w.shape("fivo-api", "db_connections", lambda m, v: v if m < -20 else min(10.0, v + 3))
    w.sql_tables["transactions"]["indexed"] = ["id", "created_at", "idempotency_key"]
    incident = page(
        w, "fivo-api: p95 latency above 2 s for 15 minutes", "fivo-api", "medium",
        "p95 latency on fivo-api has been above 2 s for fifteen minutes. The transaction list and the monthly summary load slowly in the app. "
        "The error rate has not changed.",
    )
    answer = key(
        w, tags=["multi_hop"], service="fivo-api", component="migration 0007 (dropped ix_transactions_user_created)", category="code_regression",
        summary=(
            f"The fivo-api deploy {dep['id']} ran migration 0007, which dropped ix_transactions_user_created as an apparently unused index. "
            "GET /transactions and GET /summary filter transactions by user_id, so every such query became a sequential scan of the table; "
            "database CPU rose and the two routes went from milliseconds to seconds."
        ),
        trigger=("deploy", sha),
        acceptable=[{"service": "neon-postgres", "category": "code_regression"}],
        evidence=[
            ("metric", '"metric": "p95_latency_ms"', "latency steps up at the deploy"),
            ("log", "Seq Scan on transactions", "the database log shows the per-user queries scanning the whole table"),
            ("deploy", sha, "a fivo-api deploy went live at the step"),
            ("diff", 'op.drop_index("ix_transactions_user_created"', "the deploy's migration drops the index those queries used"),
            ("code", "Transaction.user_id == user_id", "the list query still filters by user_id"),
        ],
        red_herrings=[
            ("Past incident INC-2026-030 (slow GET /transactions)", "that was a nightly job at 03:00 UTC and resolved itself; this starts at a deploy and does not recover"),
        ],
        min_hops=4,
        path=[
            step("metrics_query", service="fivo-api", metric="p95_latency_ms", **win(w, -90, AFTER)),
            step("metrics_query", service="neon-postgres", metric="cpu_pct", **win(w, -90, AFTER)),
            step("logs_count", service="neon-postgres", filters={"level": "warning"}, timeslice="10m", **win(w, -90, 10)),
            step("logs_fetch", service="neon-postgres", filters={"level": "warning"}, limit=30, **win(w, -6, 0)),
            step("deploys_list", service="fivo-api", **win(w, -180, AFTER)),
            step("code_diff", repo="fivo-api", base=dep["prev_sha"], head=sha),
            step("code_read", repo="fivo-api", ref=sha, path="app/repositories/transactions.py"),
        ],
    )
    return incident, answer


# 6 -----------------------------------------------------------------------------------


def poison_receipt_requeue_loop(w: World):
    innocent = w.commit(
        "fivo-worker", -1700, ISHAAN, "summary: send the daily summary at 02:30 UTC",
        patch=[
            ("worker/app.py", "crontab(hour=3, minute=0)", "crontab(hour=2, minute=30)"),
            ("README.md", "| `daily_summary` | 03:00 UTC (beat) | Anthropic |", "| `daily_summary` | 02:30 UTC (beat) | Anthropic |"),
        ],
    )
    dep = w.deploy("fivo-worker", -1560)
    w.incidents.append({
        "id": "INC-2026-033", "ts": "2026-11-06T09:20:00Z", "service": "fivo-worker", "title": "Receipts stuck in 'uploaded', worker restarting in a loop",
        "symptoms": "queue_depth climbing, worker instance restarting every few seconds, receipts not processed",
        "cause": "A deploy shipped an image library wheel built for the wrong architecture; the worker crashed on import at startup.",
        "resolution": "Rolled back the deploy and pinned the dependency.", "duration_min": 35,
    })
    poison = next(r for r in w.reqs("POST", "/receipts", start=-80) if r.status == 202)
    poison.payload = {"content_type": "image/heic", "size_bytes": 131}
    task = poison.task
    task.crash_ms, task.restart_s = 400.0, 27.0
    task.crash_text = "Instance exited with code 139 (SIGSEGV) and was restarted"
    w.simulate()
    receipt_id = task.ref_id
    incident = page(
        w, "fivo-worker: queue depth above 150 and rising", "fivo-worker", "high",
        "The worker queue has been growing for over an hour and is above 150 tasks. Receipts stay in 'uploaded' and new transactions are categorised late or not at all. "
        "The worker instance has restarted many times.",
    )
    answer = key(
        w, tags=["misleading", "multi_hop"], service="fivo-worker", component=f"process_receipt on receipt {receipt_id} (truncated HEIC upload)", category="data_issue",
        summary=(
            f"Receipt {receipt_id} is a 131-byte image/heic upload. Decoding it in process_receipt crashes the worker process natively (exit 139) about 0.4 s after the task starts. "
            "Tasks are acknowledged late and requeued when the process dies, and nothing limits redelivery, so the same job is handed back after every restart: "
            f"it has started {task.attempts} times. The instance spends nearly all its time restarting and the rest of the queue starves. "
            "The worker deploy a day earlier only moved the daily summary schedule."
        ),
        trigger=("data", str(receipt_id)),
        evidence=[
            ("log", "code 139 (SIGSEGV)", "the worker is crashing natively, not on memory and not at startup"),
            ("logagg", task.job_id, "one job id is received again after every restart"),
            ("db", f'"attempts": {task.attempts}', "that job has been started hundreds of times and never finished"),
            ("db", '"size_bytes": 131', "its receipt is a 131-byte HEIC file, far too small to be a real image"),
            ("code", "task_reject_on_worker_lost=True", "a task whose process dies is requeued, with no cap on redelivery"),
            ("code", "pillow_heif.read_heif", "HEIC uploads go through a native decoder"),
        ],
        red_herrings=[
            (f"fivo-worker deploy {dep['id']} the previous day", "its diff only changes the daily summary schedule, and the worker ran normally for a day after it"),
            ("Past incident INC-2026-033 (worker restart loop after a deploy)", "then the worker crashed on import at startup; now it starts cleanly and crashes only when one specific job runs"),
            ("Other receipt jobs also show several attempts", "they were in flight when the instance died; their attempts are in single digits"),
        ],
        min_hops=4,
        path=[
            step("metrics_query", service="fivo-worker", metric="queue_depth", **win(w, -150, AFTER)),
            step("metrics_query", service="fivo-worker", metric="restarts", **win(w, -150, AFTER)),
            step("logs_count", service="fivo-worker", filters={"level": "error"}, timeslice="10m", **win(w, -150, 10)),
            step("logs_fetch", service="fivo-worker", filters={"level": "error"}, limit=10, **win(w, -5, 0)),
            step("logs_count", service="fivo-worker", text="received", group_by=["job_id"], **win(w, -15, 0)),
            step("db_query", sql=f"SELECT id, type, ref_id, status, attempts, created_at FROM jobs WHERE status = 'started' AND created_at >= '{w.ts(-180)}' ORDER BY attempts DESC LIMIT 5"),
            step("db_query", sql=f"SELECT id, user_id, content_type, size_bytes, status, created_at FROM receipts WHERE id = {receipt_id}"),
            step("deploys_list", service="fivo-worker", start=w.ts(-3000), end=w.ts(AFTER)),
            step("code_diff", repo="fivo-worker", base=dep["prev_sha"], head=innocent),
            step("code_search", repo="fivo-worker", ref=innocent, pattern="reject_on_worker_lost"),
            step("code_read", repo="fivo-worker", ref=innocent, path="worker/parsing.py"),
            step("incidents_search", text="restart"),
        ],
    )
    return incident, answer


# 7 -----------------------------------------------------------------------------------


def redis_quota_exhausted(w: World):
    innocent = w.commit(
        "fivo-api", -70, DEV, "categories: let clients cache the category list for five minutes",
        patch=[
            ("app/routes/categories.py", "from fastapi import APIRouter, Depends\n", "from fastapi import APIRouter, Depends, Response\n"),
            ("app/routes/categories.py", "def list_categories(session: Session = Depends(get_session)):\n",
             "def list_categories(response: Response, session: Session = Depends(get_session)):\n    response.headers[\"Cache-Control\"] = \"private, max-age=300\"\n"),
        ],
    )
    dep = w.deploy("fivo-api", -44)
    quota = -13
    err = "ERR max daily request limit exceeded. Limit: 500000, Usage: 500000."
    for r in w.reqs("POST", start=quota):
        if r.status < 400 and r.span("upstash-redis") is not None:
            r.fail(503, f"QueueUnavailable: task queue unavailable: {err}", peer="upstash-redis", peer_error=err)
    for t in w.tasks:
        if t.queued >= quota:
            t.dropped = True
    tick = quota + 0.1
    while tick < AFTER:
        w.log(tick, "fivo-worker", "error", f"consumer: Cannot connect to rediss://default:**@fivo-broker.upstash.example:6379//: {err} Trying again in 32.00 seconds...", host="fivo-worker-1")
        tick += 32 / 60
    w.shape("upstash-redis", "commands_per_min", lambda m, v: v + 215 if m < quota else 0.0)
    w.shape("upstash-redis", "daily_commands_used", lambda m, v: 500000.0 if m >= quota else 500000.0 - (quota - m) * 487)
    incident = page(
        w, "fivo-api: 5xx rate above 5% for 10 minutes", "fivo-api", "high",
        "fivo-api is returning 503 for POST /transactions, POST /transactions/quick-add and POST /receipts. Read routes are healthy. "
        "No transaction or receipt has been accepted for about ten minutes.",
    )
    answer = key(
        w, tags=["misleading", "multi_hop"], service="upstash-redis", component="broker daily command quota", category="resource_exhaustion",
        summary=(
            "The Redis broker reached its plan's daily quota of 500,000 commands. From that minute the broker rejects every command, so fivo-api cannot enqueue work "
            "and returns 503 on the routes that enqueue, and fivo-worker cannot connect to consume. Usage had been climbing steadily all day; "
            "the fivo-api deploy half an hour earlier only added a cache header to GET /categories."
        ),
        trigger=("none", ""),
        acceptable=[{"service": "fivo-api", "category": "resource_exhaustion"}, {"service": "fivo-worker", "category": "resource_exhaustion"}],
        evidence=[
            ("log", "max daily request limit exceeded", "the broker itself states that the daily limit is used up"),
            ("metric", '"metric": "daily_commands_used"', "usage climbs steadily and flattens at exactly the limit when the errors start"),
            ("log", "Cannot connect to rediss", "the worker is locked out by the same error, so the API is not the origin"),
            ("diff", "Cache-Control", "the same-hour deploy touches only GET /categories"),
        ],
        red_herrings=[
            (f"fivo-api deploy {dep['id']} in the same hour", "its diff adds a response header to GET /categories; the 503s start 31 minutes later, on other routes, and the worker fails too"),
            ("Past incident INC-2026-021 (broker refused connections)", "that was a connection-count limit after a concurrency change; here the daily command quota is exhausted and nothing changed"),
        ],
        min_hops=3,
        path=[
            step("logs_count", service="fivo-api", filters={"level": "error"}, group_by=["route", "status"], **win(w, -30, 10)),
            step("logs_fetch", service="fivo-api", filters={"level": "error"}, limit=10, **win(w, -10, 0)),
            step("deploys_list", service="fivo-api", **win(w, -180, AFTER)),
            step("code_diff", repo="fivo-api", base=dep["prev_sha"], head=innocent),
            step("metrics_list", service="upstash-redis"),
            step("metrics_query", service="upstash-redis", metric="daily_commands_used", **win(w, -180, AFTER)),
            step("logs_count", service="fivo-worker", filters={"level": "error"}, timeslice="5m", **win(w, -30, 10)),
            step("logs_fetch", service="fivo-worker", filters={"level": "error"}, limit=5, **win(w, -10, 0)),
        ],
    )
    return incident, answer


# 8 -----------------------------------------------------------------------------------


def r2_credential_rotation(w: World):
    rotated = w.config_change("r2-storage", -42, "R2_ACCESS_KEY_ID", "****a91f", "****7c2e", by="dashboard:maya", kind="credential_rotation",
                              note="quarterly rotation; the previous key stays valid for 30 minutes", restart=False)
    w.config_change("fivo-api", -40, "R2_ACCESS_KEY_ID", "****a91f", "****7c2e", by="dashboard:maya", note="rotated storage key")
    revoked = -12
    w.config_change("r2-storage", revoked, "R2_ACCESS_KEY_ID", "****a91f", "(revoked)", by="system", kind="credential_expired",
                    note="grace period for the previous key ended", restart=False)
    err = "ClientError('An error occurred (403) when calling the GetObject operation: Forbidden')"
    for t in w.tasks:
        if t.queued >= revoked and t.name in ("process_receipt", "embed_pending_receipts"):
            t.plan = [(random.Random(f"{w.seed}:{t.job_id}").uniform(140, 260), "fail", err)]
    w.shape("r2-storage", "client_error_rate", lambda m, v: v if m < revoked else 0.46 + 0.1 * abs(math.sin(m * 2.3)))
    incident = page(
        w, "fivo-api: no receipt processed in the last 10 minutes", "fivo-api", "high",
        "Receipts uploaded through POST /receipts are accepted but none has reached 'processed' for ten minutes. "
        "Uploads themselves succeed and the files are present in storage.",
    )
    answer = key(
        w, tags=["conflicting", "multi_hop"], service="fivo-worker", component="R2 access key in the worker's environment", category="config_change",
        summary=(
            f"The R2 access key was rotated ({rotated['id']}). The new key was applied to fivo-api's environment but not to fivo-worker's. "
            "When the previous key's 30-minute grace period ended, every storage read from the worker started failing with 403, so process_receipt and "
            "embed_pending_receipts fail on their first call, while uploads from fivo-api, which has the new key, keep working."
        ),
        trigger=("config_change", rotated["id"]),
        acceptable=[{"service": "r2-storage", "category": "config_change"}],
        evidence=[
            ("log", "(403) when calling the GetObject operation", "the worker is refused by storage"),
            ("logagg", '"status": "202"', "uploads from fivo-api to the same bucket still succeed"),
            ("config", "credential_rotation", "the storage key was rotated, with a 30-minute grace period that ends when the failures start"),
            ("config", "No config changes found for service=fivo-worker", "the worker's environment was never given the new key"),
            ("code", "R2_ACCESS_KEY_ID", "the worker reads its own copy of the key from its environment"),
        ],
        red_herrings=[
            ("r2-storage client_error_rate rises to about 50%", "it is an average over both callers: all worker reads fail and all API writes succeed, which rules out a storage outage"),
        ],
        min_hops=4,
        path=[
            step("logs_count", service="fivo-worker", filters={"level": "error"}, group_by=["task"], **win(w, -30, 10)),
            step("logs_fetch", service="fivo-worker", filters={"level": "error"}, limit=10, **win(w, -10, 0)),
            step("logs_count", service="fivo-api", filters={"route": "/receipts"}, group_by=["status"], **win(w, -12, 10)),
            step("metrics_query", service="r2-storage", metric="client_error_rate", **win(w, -60, AFTER)),
            step("config_changes_list", **win(w, -120, AFTER)),
            step("config_changes_list", service="fivo-worker", **win(w, -120, AFTER)),
            step("deploys_list", service="fivo-worker", **win(w, -180, AFTER)),
            step("code_search", repo="fivo-worker", pattern="R2_ACCESS_KEY_ID"),
        ],
    )
    return incident, answer


# 9 -----------------------------------------------------------------------------------


def bulk_import_traffic_spike(w: World):
    innocent = w.commit(
        "fivo-api", -50, ISHAAN, "insights: ask for one saving tip as well",
        patch=[("app/routes/insights.py", '"Give three short, specific observations about this spending:\\n"',
                '"Give three short, specific observations about this spending and one saving tip:\\n"')],
    )
    dep = w.deploy("fivo-api", -19)
    start = -11
    user = 137
    device = "cli-7f3a"
    w.devices[user].append(device)
    w.seq[device] = 0
    rng = random.Random(w.seed + 1)
    for m in range(start, AFTER):
        for _ in range(rng.randint(305, 335)):
            r = w.make_request(m + rng.random(), "POST", "/transactions", user, device=device)
            r.payload["source"] = "api"
            r.traced = rng.random() < 0.01
            r.extra["user_agent"] = "python-requests/2.32"
            w.add_request(r)
    limit = 240
    seen: Counter[int] = Counter()
    for r in sorted(w.reqs(start=start), key=lambda r: r.t):
        seen[math.floor(r.t)] += 1
        if seen[math.floor(r.t)] > limit:
            r.spans = []
            r.fail(429, "rate limit exceeded", level="warning", duration_ms=1.0)
            r.traced = False
        elif r.status < 400 and r.span("neon-postgres") is not None:
            r.slow("neon-postgres", round(r.span("neon-postgres")["ms"] * rng.uniform(30, 120), 1))
    w.shape("fivo-api", "db_connections", lambda m, v: 10.0 if m >= start else v)
    w.simulate()
    created = sum(1 for t in w.db["transactions"] if t["user_id"] == user and t["created_at"] >= w.ts(start))
    incident = page(
        w, "fivo-api: 429 responses above 20% of requests", "fivo-api", "high",
        "More than a fifth of requests to fivo-api are being answered with 429. "
        "Users across the app report 'too many requests' errors when saving or loading transactions.",
    )
    answer = key(
        w, tags=["misleading", "multi_hop"], service="fivo-api", component="POST /transactions traffic from one API client", category="traffic_change",
        summary=(
            f"A single client (user {user}, device {device}, a script) began posting about 320 transactions a minute to POST /transactions. "
            "The rate limiter counts all requests to the instance in one shared 240-per-minute window, so this one caller exhausts it within each minute and "
            f"every other user gets 429 for the rest of the minute; the accepted writes ({created} rows so far) also load the database. "
            "The fivo-api deploy shortly before only changed the insights prompt."
        ),
        trigger=("traffic", f"user:{user}"),
        evidence=[
            ("metric", '"metric": "request_rate"', "request rate jumps roughly thirtyfold in one minute"),
            ("logagg", f'"user_id": "{user}"', "almost all of the new requests come from one user"),
            ("db", f'"user_id": {user}', "that user has created thousands of transactions since the spike began"),
            ("code", "self._count > self._rpm", "the rate limit is one counter for the whole instance, so one caller can starve everyone"),
            ("diff", "one saving tip", "the deploy in the window changes only a prompt string"),
        ],
        red_herrings=[
            (f"fivo-api deploy {dep['id']} eight minutes before the spike", "its diff changes a prompt string on GET /insights; the 429s track request volume from one user, not the deploy"),
            ("Past incident INC-2026-029 (bulk card sync)", "similar shape but that one hit the categorisation provider's limit; here our own rate limiter is saturated"),
        ],
        min_hops=3,
        path=[
            step("metrics_query", service="fivo-api", metric="request_rate", **win(w, -60, AFTER)),
            step("logs_count", service="fivo-api", group_by=["status"], **win(w, -10, 5)),
            step("logs_count", service="fivo-api", group_by=["user_id"], **win(w, -10, 5)),
            step("logs_fetch", service="fivo-api", filters={"user_id": str(user)}, limit=5, **win(w, -3, 0)),
            step("db_query", sql=f"SELECT user_id, count(*) AS n FROM transactions WHERE created_at >= '{w.ts(start)}' GROUP BY user_id ORDER BY n DESC LIMIT 5"),
            step("deploys_list", service="fivo-api", **win(w, -180, AFTER)),
            step("code_diff", repo="fivo-api", base=dep["prev_sha"], head=innocent),
            step("code_read", repo="fivo-api", ref=innocent, path="app/middleware/ratelimit.py"),
        ],
    )
    return incident, answer


# 10 ----------------------------------------------------------------------------------


def worker_regression_with_log_failures(w: World):
    sha = w.commit(
        "fivo-worker", -95, ISHAAN, "features: use the merchant's local time of day",
        patch=[
            ("worker/features.py", '"""Signals passed to the categorisation model alongside the merchant name."""\n\nfrom .repo import Txn',
             '"""Signals passed to the categorisation model alongside the merchant name."""\n\nfrom zoneinfo import ZoneInfo\n\nfrom .repo import Txn'),
            ("worker/features.py",
             "    hour = txn.created_at.hour\n    return {\"hour\": hour, \"is_weekend\": txn.created_at.weekday() >= 5, \"is_night\": hour < 6 or hour >= 22}",
             "    local = txn.created_at.astimezone(ZoneInfo(txn.merchant_tz))\n    hour = local.hour\n"
             "    return {\"hour\": hour, \"is_weekend\": local.weekday() >= 5, \"is_night\": hour < 6 or hour >= 22}"),
        ],
    )
    dep = w.deploy("fivo-worker", -27)
    live = -27 + 20 / 60
    err = "TypeError('expected str, bytes or os.PathLike object, not NoneType')"
    for t in w.tasks_of("categorize_transaction", start=live):
        if t.req is not None and t.req.payload.get("merchant_tz") is None:
            t.plan = [(random.Random(f"{w.seed}:{t.job_id}").uniform(25, 60), "fail", err)]
    w.faults["logs"] = [{"on_calls": [1, 2], "fault": "timeout"}, {"on_calls": [4, 5], "fault": "partial", "keep": 0.4}]
    w.policy_overrides["logs"] = {"timeout_s": 1, "max_retries": 1}
    incident = page(
        w, "fivo-api: POST /transactions/quick-add 5xx above 20%", "fivo-api", "high",
        "About a third of POST /transactions/quick-add calls are returning 500. Plain POST /transactions succeeds, "
        "but the share of transactions without a category is growing.",
    )
    answer = key(
        w, tags=["tool_failure", "multi_hop"], service="fivo-worker", component="time_features (worker/features.py)", category="code_regression",
        summary=(
            f"The fivo-worker deploy {dep['id']} changed time_features to convert created_at into the merchant's time zone with ZoneInfo(txn.merchant_tz). "
            "merchant_tz is only filled for card-synced transactions; for manual entries it is NULL and the task raises TypeError. Those transactions are never categorised, "
            "and POST /transactions/quick-add, which returns the worker's result, turns the failed result into a 500."
        ),
        trigger=("deploy", sha),
        evidence=[
            ("db", "not NoneType", "failed categorisation jobs all carry the same TypeError"),
            ("db", '"source": "manual"', "since the deploy, manual entries (no merchant time zone) are never categorised while card entries are"),
            ("deploy", sha, "a fivo-worker deploy went live when the failures began"),
            ("diff", "ZoneInfo(txn.merchant_tz)", "the deployed commit passes a nullable column to ZoneInfo"),
            ("log", "raised unexpected: TypeError", "the worker log shows the task raising"),
        ],
        red_herrings=[
            ("The 500s are raised in fivo-api (a pydantic ValidationError)", "fivo-api was not deployed or reconfigured; it is failing on the worker's failed result"),
        ],
        min_hops=4,
        path=[
            {**step("logs_count", service="fivo-api", filters={"level": "error"}, group_by=["route", "status"], **win(w, -40, 10)), "retry": True},
            step("logs_count", service="fivo-worker", filters={"level": "error"}, group_by=["task"], **win(w, -40, 10)),
            step("logs_fetch", service="fivo-worker", filters={"level": "error"}, limit=20, **win(w, -10, 0)),
            step("db_query", sql=f"SELECT last_error, count(*) AS n FROM jobs WHERE status = 'failed' AND created_at >= '{w.ts(-60)}' GROUP BY last_error"),
            step("db_query", sql=f"SELECT source, count(*) AS total, count(category_id) AS categorised FROM transactions WHERE created_at >= '{w.ts(live)}' GROUP BY source"),
            step("deploys_list", service="fivo-worker", **win(w, -180, AFTER)),
            step("code_diff", repo="fivo-worker", base=dep["prev_sha"], head=sha),
        ],
    )
    return incident, answer


# 11 ----------------------------------------------------------------------------------


def conflicting_deploy_and_config(w: World):
    sha = w.commit(
        "fivo-api", -30, ISHAAN, "insights: restructure prompt building, tune request",
        patch=[
            ("app/routes/insights.py", "router = APIRouter()\n",
             "router = APIRouter()\nMAX_TOKENS = 400\n\n\ndef _build_prompt(rows) -> str:\n"
             "    lines = [f\"{r.category}: {r.total_minor / 100:.2f}\" for r in rows]\n"
             "    return \"Give three short, specific observations about this spending:\\n\" + \"\\n\".join(lines)\n"),
            ("app/routes/insights.py",
             "    prompt = \"Give three short, specific observations about this spending:\\n\" + \"\\n\".join(\n"
             "        f\"{r.category}: {r.total_minor / 100:.2f}\" for r in rows\n    )\n"
             "    text = anthropic_client.complete(prompt, max_tokens=400)\n",
             "    text = anthropic_client.complete(_build_prompt(rows), max_tokens=MAX_TOKENS)\n"),
        ],
    )
    cfg = w.config_change("fivo-api", -15, "ANTHROPIC_TIMEOUT_S", "20", "2", by="dashboard:dev", note="fail fast instead of holding a request for 20 s")
    dep = w.deploy("fivo-api", -12)
    live = -15 + 0.3 + 18 / 60
    first_end = None
    for r in sorted(w.reqs("GET", "/insights", start=live), key=lambda r: r.t):
        call = r.span("anthropic-llm")
        if r.status >= 400 or call is None or call["ms"] <= 2000:
            continue
        r.fail(504, "UpstreamTimeout: anthropic request timed out after 2.0s", peer="anthropic-llm", peer_ms=2000.0, peer_error="client timeout after 2.0s")
        first_end = first_end if first_end is not None else r.t + r.duration_ms / 60000
    assert first_end is not None and first_end < -12, "errors must begin before the deploy"
    w.shape("anthropic-llm", "client_latency_p95_ms", lambda m, v: v if m < -14 else 2000.0)
    w.shape("anthropic-llm", "client_error_rate", lambda m, v: v if m < -14 else 0.42 + 0.1 * abs(math.sin(m * 1.7)))
    incident = page(
        w, "fivo-api: GET /insights 5xx above 30%", "fivo-api", "medium",
        "Nearly half of GET /insights requests are returning 504. Other routes are healthy. The insights card in the app shows an error for many users.",
    )
    answer = key(
        w, tags=["conflicting", "misleading"], service="fivo-api", component="Anthropic client timeout (ANTHROPIC_TIMEOUT_S)", category="config_change",
        summary=(
            f"Configuration change {cfg['id']} lowered ANTHROPIC_TIMEOUT_S on fivo-api from 20 to 2. An insights completion normally takes 1 to 4 seconds, "
            "so about half the calls now exceed the client timeout and the route returns 504. The first failure is in the minute of the config change, "
            f"about three minutes before deploy {dep['id']}, whose diff only restructures how the prompt is built."
        ),
        trigger=("config_change", cfg["id"]),
        evidence=[
            ("log", "timed out after 2.0s", "the client gives up at exactly 2.0 s"),
            ("config", "ANTHROPIC_TIMEOUT_S", "the timeout was lowered from 20 to 2"),
            ("logagg", w.ts(math.floor(first_end)), "the first timeout precedes the deploy"),
            ("deploy", sha, "the deploy landed after the errors had started"),
            ("diff", "MAX_TOKENS = 400", "the deployed change keeps the same prompt and the same max_tokens"),
            ("code", "timeout=settings.anthropic_timeout_s", "the client timeout comes straight from the setting"),
        ],
        red_herrings=[
            (f"fivo-api deploy {dep['id']} touches the failing endpoint", "it landed about three minutes after the first failure and does not change the request that is sent"),
            ("anthropic-llm client_error_rate rises", "the provider is not slower: client latency is pinned at 2000 ms, our own timeout, and the worker's Anthropic calls are unaffected"),
            ("Past incident INC-2026-027 (insights failing)", "that was a retired model id returning 502 for every call; here only calls longer than 2 s fail"),
        ],
        min_hops=3,
        path=[
            step("logs_count", service="fivo-api", filters={"level": "error"}, group_by=["route", "status"], **win(w, -30, 10)),
            step("logs_fetch", service="fivo-api", filters={"level": "error", "route": "/insights"}, limit=10, **win(w, -10, 0)),
            step("logs_count", service="fivo-api", text="timed out after", timeslice="1m", **win(w, -30, 0)),
            step("deploys_list", service="fivo-api", **win(w, -60, AFTER)),
            step("code_diff", repo="fivo-api", base=dep["prev_sha"], head=sha),
            step("config_changes_list", service="fivo-api", **win(w, -60, AFTER)),
            step("code_read", repo="fivo-api", ref=sha, path="app/services/anthropic_client.py"),
            step("metrics_query", service="anthropic-llm", metric="client_latency_p95_ms", **win(w, -60, AFTER)),
        ],
    )
    return incident, answer


# 12 ----------------------------------------------------------------------------------


def voice_errors_missing_telemetry(w: World):
    onset = -21
    rng = random.Random(w.seed + 4)
    for r in w.reqs("POST", "/voice/call", start=onset):
        if r.status >= 400 or rng.random() >= 0.38:
            continue
        if rng.random() < 0.5:
            r.fail(502, "UpstreamError: bolna upstream error: 502 Bad Gateway", peer="bolna-voice", peer_ms=round(rng.uniform(600, 1400), 1), peer_error="502 Bad Gateway", inserted=True)
        else:
            r.fail(504, "UpstreamTimeout: bolna request timed out after 10s", peer="bolna-voice", peer_ms=10000.0, peer_error="client timeout after 10s", inserted=True)
    w.faults["records"] = [{"match": '"collection": "traces"', "fault": "error", "retryable": False,
                            "message": "trace store query failed: spans for this period are not available (ingest backlog)"}]
    incident = page(
        w, "fivo-api: POST /voice/call failing above 25%", "fivo-api", "medium",
        "More than a quarter of reminder calls requested through POST /voice/call are failing with 502 or 504. Other routes are healthy.",
    )
    answer = key(
        w, tags=["tool_failure", "inconclusive_ok"], service="bolna-voice", component="voice provider API", category="dependency_failure",
        summary=(
            "About 40% of calls from fivo-api to the voice provider fail: the provider answers 502 or does not answer within the 10 s client timeout. "
            "Nothing was deployed or reconfigured, and fivo-api's other outbound calls are healthy, which points at the provider. "
            "It cannot be confirmed from our side: there are no client-side metrics for this provider and the trace store cannot be queried for the period, "
            "so a network path problem specific to that provider is not excluded."
        ),
        trigger=("external", ""),
        acceptable=[{"service": "", "category": "unknown"}, {"service": "fivo-api", "category": "unknown"}, {"service": "bolna-voice", "category": "unknown"}],
        evidence=[
            ("log", "bolna upstream error: 502 Bad Gateway", "the provider itself returns 502 for part of the calls"),
            ("log", "bolna request timed out after 10s", "the rest time out at the client timeout"),
            ("db", '"status": "failed"', "failed calls are recorded for many different users"),
            ("deploy", "No deploys found", "no deploy in the window"),
            ("config", "No config changes found", "no configuration change in the window"),
        ],
        red_herrings=[
            ("Trace queries fail", "the trace store is unavailable for the period; that is a gap in evidence, not a cause"),
        ],
        min_hops=3,
        path=[
            step("logs_count", service="fivo-api", filters={"level": "error"}, group_by=["route", "status"], **win(w, -40, 10)),
            step("logs_fetch", service="fivo-api", filters={"level": "error", "route": "/voice/call"}, limit=20, **win(w, -12, 0)),
            {**step("traces_search", service="fivo-api", text="bolna", **win(w, -30, 0)), "expect": "error"},
            step("deploys_list", **win(w, -180, AFTER)),
            step("config_changes_list", **win(w, -180, AFTER)),
            step("db_query", sql=f"SELECT status, count(*) AS n, count(DISTINCT user_id) AS users FROM voice_calls WHERE created_at >= '{w.ts(onset)}' GROUP BY status"),
            step("metrics_list", service="bolna-voice"),
        ],
    )
    return incident, answer


SCENARIOS: dict[str, dict[str, Any]] = {
    "idempotency-key-regression": {
        "build": idempotency_key_regression, "detected_at": "2026-11-10T08:42:00Z", "seed": 101,
        "world": {"weights": {("POST", "/transactions"): 32}, "multi_bias": 3.0},
    },
    "db-pool-config-change": {"build": db_pool_config_change, "detected_at": "2026-11-10T14:05:00Z", "seed": 202},
    "groq-degradation-retry-storm": {
        "build": groq_degradation_retry_storm, "detected_at": "2026-11-11T11:20:00Z", "seed": 303,
        "world": {"weights": {("POST", "/transactions/quick-add"): 12}},
    },
    "worker-oom-embedding-batch": {
        "build": worker_oom_embedding_batch, "detected_at": "2026-11-09T16:30:00Z", "seed": 404, "world": {"legacy_receipts": 2600},
    },
    "migration-dropped-index": {"build": migration_dropped_index, "detected_at": "2026-11-10T19:15:00Z", "seed": 505},
    "poison-receipt-requeue-loop": {"build": poison_receipt_requeue_loop, "detected_at": "2026-11-10T05:50:00Z", "seed": 606},
    "redis-quota-exhausted": {"build": redis_quota_exhausted, "detected_at": "2026-11-11T17:38:00Z", "seed": 707},
    "r2-credential-rotation": {"build": r2_credential_rotation, "detected_at": "2026-11-10T10:05:00Z", "seed": 808},
    "bulk-import-traffic-spike": {"build": bulk_import_traffic_spike, "detected_at": "2026-11-11T07:25:00Z", "seed": 909},
    "worker-regression-with-log-failures": {
        "build": worker_regression_with_log_failures, "detected_at": "2026-11-10T12:48:00Z", "seed": 1010,
        "world": {"weights": {("POST", "/transactions/quick-add"): 12}},
    },
    "conflicting-deploy-and-config": {
        "build": conflicting_deploy_and_config, "detected_at": "2026-11-11T09:12:00Z", "seed": 1111,
        "world": {"weights": {("GET", "/insights"): 28}},
    },
    "voice-errors-missing-telemetry": {
        "build": voice_errors_missing_telemetry, "detected_at": "2026-11-09T13:40:00Z", "seed": 1212,
        "world": {"weights": {("POST", "/voice/call"): 30}},
    },
}
