# Incident report: fivo-api: p95 latency above 2 s for 15 minutes

**Status:** conclusive

Deploy dep-0414 (commit 200f96f, 'migrations: drop indexes with zero scans') went live at 18:54 UTC. Migration 0007 dropped ix_transactions_user_created on transactions(user_id, created_at). One minute later, at 18:55, fivo-api p95 rose from about 750 ms to about 3.15 s. The list query (WHERE user_id ORDER BY created_at DESC LIMIT) and the monthly-totals query now run as Seq Scans on transactions, each 1.0-2.8 s, and Neon CPU went from about 11% to about 62%. The scan-from-dropped-index link is inferred from plan shape, query shape and timing. The production index list was not checked and no restore test was run. Recreate the index first.

## Root cause

- Service: neon-postgres
- Component: ix_transactions_user_created index on transactions(user_id, created_at), dropped by migration 0007 in deploy dep-0414 (200f96f)
- Category: code_regression

Migration 0007 dropped the composite index that served the per-user transaction list and monthly totals queries. Both queries now run as Seq Scans (about 418k rows removed by filter per scan) taking 1.0-2.8 s. That slows GET /transactions and GET /summary, raises Neon CPU and holds fivo-api connections longer.

## What changed (why now)

Deploy dep-0414 removed the composite index ix_transactions_user_created from transactions (migration 0007 plus models.py), so the per-user list and monthly totals queries became sequential scans. (at 2026-11-10T18:54:00Z)

- Before: Before the deploy, SELECT transactions by user took 7-11 ms, GET /transactions took 11-17 ms, SELECT monthly totals took 12.5 ms, and p95 was ~750 ms. Neon slow queries were ~0. [trace:fivo-api:0004, trace:fivo-api:0005, trace:fivo-api:0007, trace:fivo-api:0012, trace:fivo-api:0032, metric:fivo-api:0001, metric:neon-postgres:0002]
- After: After the deploy, the list query took 2.1-2.8 s and the monthly totals query 1.0-2.2 s. auto_explain shows Seq Scan on transactions with ~418k rows removed. Neon CPU was ~62%, slow queries 3-5 per minute, and p95 ~3.15 s. [trace:fivo-api:0054, trace:fivo-api:0064, trace:fivo-api:0083, trace:fivo-api:0057, trace:fivo-api:0089, log:neon-postgres:0001, log:neon-postgres:0006, metric:neon-postgres:0001, metric:neon-postgres:0002, metric:fivo-api:0001]

Changes before the incident, and what was concluded about each:

- **cause** — deploy on fivo-api: commit 200f96f "migrations: drop indexes with zero scans" by github-actions (2026-11-10T18:54:00Z, 1 min before onset): Deploy dep-0414 went live at 18:54, 1 min before the 18:55 onset. Migration 0007 dropped ix_transactions_user_created. Both slow queries filter on user_id and use created_at, and their plans are Seq Scans with ~418k rows removed. The query code did not change, no other change is recorded, and the worker, Redis and fivo-api resource alternatives are refuted. [deploy:fivo-api:0001, diff:fivo-api:0002, diff:fivo-api:0001, diff:fivo-api:0003, log:neon-postgres:0001, log:neon-postgres:0006, trace:fivo-api:0054, trace:fivo-api:0057, config:config_changes:0001]

## Hypotheses, ranked

1. **Deploy dep-0414 dropped ix_transactions_user_created, so the transaction list and monthly totals queries fall back to sequential scans and fivo-api p95 rises from about 750 ms to about 3.1 s.** (neon-postgres, code_regression) — most_likely, confidence 0.85
   - Supports: Deploy went live at 18:54, 1 min before the 18:55 p95 level shift. It is the only change on record. [deploy:fivo-api:0001, metric:fivo-api:0001, config:config_changes:0001]
   - Supports: Migration 0007 drops ix_transactions_user_created and models.py removes the same Index. [diff:fivo-api:0002, diff:fivo-api:0001]
   - Supports: Both handlers filter on user_id and use created_at, which is the shape the dropped index served. The repositories code did not change. [code:fivo-api:0001, code:fivo-api:0002, code:fivo-api:0003, diff:fivo-api:0003]
   - Supports: auto_explain shows Seq Scan on transactions with about 418k rows removed by filter for both queries. The list query's slow statement (2540.1 ms) and the summary plan (1815.8 ms) match the span durations. [log:neon-postgres:0001, log:neon-postgres:0006, log:neon-postgres:0005, trace:fivo-api:0054, trace:fivo-api:0057]
   - Supports: Before the deploy the list query took 7-11 ms and the monthly totals 12.5 ms. After it they took 2.1-2.8 s and 1.0-2.2 s. The DB span is over 99% of the slow GET /transactions requests. [trace:fivo-api:0004, trace:fivo-api:0007, trace:fivo-api:0032, trace:fivo-api:0054, trace:fivo-api:0055, trace:fivo-api:0064, trace:fivo-api:0083, trace:fivo-api:0074, trace:fivo-api:0086, trace:fivo-api:0089]
   - Supports: Neon CPU rose from about 11% to about 62% and slow_queries rose from about 0, both starting at the onset. [metric:neon-postgres:0001, metric:neon-postgres:0002]
   - Contradicts: The DB tool reports 1,238 transactions, which conflicts with the roughly 418k rows removed per scan in the production plans. This is unexplained. The tool may be reading a different database or branch. [db:transactions:0001, db:transactions:0002, log:neon-postgres:0001]
   - Contradicts: The migration comment says idx_scan = 0, yet pre-deploy queries ran in 7-11 ms. The reason is not shown. [diff:fivo-api:0002, trace:fivo-api:0004]
   - Contradicts: The production index list, pre-deploy plans and an index-recreation test were never obtained because the tools block them.
2. **fivo-worker or a background job is competing with fivo-api for the database and connections.** (fivo-worker, resource_exhaustion) — ruled_out, confidence 0.02
   - Supports: A past incident, INC-2026-030, had the same symptom from a summary job, but it happened at 03:00 UTC. [incident:fivo-api:0001]
   - Contradicts: No fivo-worker deploy occurred, queue depth stayed at 0, and task throughput, p95 and CPU had no level shift. [deploy:fivo-worker:0001, metric:fivo-worker:0001, metric:fivo-worker:0002, metric:fivo-worker:0003, metric:fivo-worker:0004]
   - Contradicts: Redis command rate was flat. [metric:upstash-redis:0001]
3. **A neon-postgres compute or infrastructure problem is slowing all queries.** (neon-postgres, infrastructure) — ruled_out, confidence 0.02
   - Supports: Neon CPU rose at 18:55 together with the latency, but this is consistent with scan load. [metric:neon-postgres:0001]
   - Contradicts: Trivial DB calls (INSERT transactions, INSERT and SELECT receipts, SELECT categories) stayed at 3-11 ms on the same instance. [trace:fivo-api:0052, trace:fivo-api:0058, trace:fivo-api:0062, trace:fivo-api:0066, trace:fivo-api:0085]
   - Contradicts: No config or infrastructure change was recorded for neon-postgres. [config:neon-postgres:0001]
4. **Resource pressure on the 512 MB fivo-api instance (memory or connection pool) is inflating p95.** (fivo-api, resource_exhaustion) — ruled_out, confidence 0.03
   - Supports: db_connections rose from about 4.9 to about 7.4 at the onset. It is more plausibly a result of slower queries holding connections. [metric:fivo-api:0002]
   - Contradicts: Memory stayed at 200-207 MB of 512 MB and CPU showed no level shift. [metric:fivo-api:0004, metric:fivo-api:0005]
   - Contradicts: No log lines mention 'pool'. The only error was an isolated 14 ms ConnectionResetError, not a pool or OOM failure. [logagg:fivo-api:0003, log:fivo-api:0001, logagg:fivo-api:0004]

## Facts

- Deploy dep-0414 (200f96f, previous 7bc71c8) went live at 2026-11-10T18:54:00Z. It was the only deploy found, and no config changes were recorded. [deploy:fivo-api:0001, config:config_changes:0001]
- Migration 0007 drops ix_transactions_user_created and ix_receipts_content_type. models.py removes the matching Index. [diff:fivo-api:0002, diff:fivo-api:0001, changes:fivo-api:0001]
- fivo-api p95 shifted at 18:55 from about 750 ms to about 3154 ms and peaked at 3458 ms at 19:30. [metric:fivo-api:0001]
- Neon plans for the list and summary queries are Seq Scans on transactions with 417,956 and 418,305 rows removed by filter. [log:neon-postgres:0001, log:neon-postgres:0006]
- The list query is WHERE user_id ORDER BY created_at DESC LIMIT. The summary query filters on user_id and a created_at range. The repositories code is unchanged between the two commits. [code:fivo-api:0001, code:fivo-api:0002, diff:fivo-api:0003]
- Neon CPU went from about 10.8% to about 62% at 18:55. slow_queries rose from about 0.4 to about 4.2 per minute at 19:00. [metric:neon-postgres:0001, metric:neon-postgres:0002]
- Error rate did not follow the latency. The only error in 19:20-19:35 was one 14 ms ConnectionResetError on GET /transactions at 19:30:35. [metric:fivo-api:0003, logagg:fivo-api:0004, log:fivo-api:0001]
- The worker, Redis and fivo-api memory and CPU show no change at onset. [metric:fivo-worker:0001, metric:upstash-redis:0001, metric:fivo-api:0004, metric:fivo-api:0005]

## Assumptions

- The Seq Scans are caused by the dropped index. This is inferred from plan shape, query shape and timing. No pre-deploy plan or pg_indexes listing exists.
- The summary query is tied to the dropped index through plan-to-span duration matching and query shape, not statement text from the same log line.
- The baseline p95 of about 750 ms is likely dominated by other routes, since the list and summary spans were 7-17 ms before the deploy. That the DB spans explain the 3.1 s p95 is inferred.
- The row-count mismatch (1,238 in the DB tool vs about 418k per scan in the plans) is assumed to come from the tool reading a different database or branch. This is not shown.
- The index was likely used before the deploy despite the idx_scan = 0 note, for example because of a stats reset. This is not shown.

## Recommended actions

- [immediate] Recreate ix_transactions_user_created on transactions(user_id, created_at), either by reverting commit 200f96f or with a new migration. Use CREATE INDEX CONCURRENTLY. — This restores the access path for the list and summary queries. Watch p95 and Neon CPU afterwards. If they do not return to about 750 ms and about 11%, the hypothesis is wrong. [deploy:fivo-api:0001, diff:fivo-api:0002, log:neon-postgres:0001, log:neon-postgres:0006]
- [soon] Check pg_indexes in production directly to confirm the index is absent. Compare EXPLAIN for the list and summary queries before and after restoring it. — This was never confirmed because the tools blocked it.
- [soon] Find out why the DB tool reports 1,238 rows while production plans scan about 418k rows. Check which database or branch it is connected to. — The conflict is unexplained and weakens the data-tool evidence. [db:transactions:0002, log:neon-postgres:0001]
- [later] Before dropping indexes, check that no route queries depend on them. Use a longer stats window that survives resets, and test the plans of the affected queries on production-sized data. Add a p95 alert per route. — The 'zero scans' check missed an index the main read paths relied on, and the per-route latency metric does not exist. [diff:fivo-api:0002, code:fivo-api:0001]

## Missing evidence

- Current pg_indexes listing for production (tool blocks it).
- Pre-deploy query plans and an index-recreation test showing latency returning to normal.
- EXPLAIN and pg_stat_statements baselines (tool blocks them).
- Explanation for the 1,238 vs about 418k row discrepancy.
- No per-route latency metric exists. The Neon log fetch was partial and capped, so only some slow-query warnings were read.
- The fivo-api connection pool limit is not exposed. Metrics are 5-minute buckets and trace sampling was partial.

## Run

- system: multi_agent
- termination: accepted
- rounds: 2
- hypotheses: 4
- steps: 13
- max hops: 6
- tool calls: 61
- tool failures: 0
- guardrail rejections: 3
- llm calls: 49
- total tokens: 697672
- cost usd: 1.1763
- latency s: 318.6
- citations: 105 checked, 0 invalid, 0 facts demoted to assumptions
