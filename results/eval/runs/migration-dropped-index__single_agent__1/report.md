# Incident report: fivo-api: p95 latency above 2 s for 15 minutes

**Status:** conclusive

Deploy dep-0414 (sha 200f96f7, 18:54:00Z) added migration 0007, which dropped the composite index ix_transactions_user_created on transactions(user_id, created_at). Both slow routes filter by user_id and use created_at, so they lost their index. GET /transactions fell from about 13–17 ms to 1.7–3 s, and GET /summary from about 24–35 ms to 1.2–3.2 s. p95 jumped from about 750 ms to about 3150 ms at 18:55Z. Recreate the index (CREATE INDEX CONCURRENTLY) or revert the migration. I did not directly confirm the index is missing in the database, and I did not capture a query plan.

## Root cause

- Service: fivo-api
- Component: migrations/versions/0007_drop_unused_indexes.py: drop of ix_transactions_user_created on transactions(user_id, created_at)
- Category: code_regression

Deploy dep-0414 ran a migration that dropped ix_transactions_user_created, judged 'unused' from idx_scan=0 since the last stats reset. That index serves the per-user queries behind the transaction list (list_recent: WHERE user_id ORDER BY created_at DESC LIMIT) and the monthly summary (monthly_totals: WHERE user_id AND created_at >= month_start). Without it those queries slowed from tens of milliseconds to seconds, which pushed p95 above 2 s at about 18:55Z, one minute after the deploy.

## What changed (why now)

The deploy dropped the composite index ix_transactions_user_created (and ix_receipts_content_type) via migration 0007, and removed the Index from the Transaction model. (at 2026-11-10T18:54:00Z)

- Before: Before the deploy, p95 was about 750 ms (5-minute points of 480–950 ms). GET /transactions took 13–17 ms and GET /summary took 22–35 ms. [metric:fivo-api:0001, log:fivo-api:0014, log:fivo-api:0015, log:fivo-api:0010, log:fivo-api:0012]
- After: After the deploy, p95 was about 3150 ms (level shift at 18:55Z). GET /transactions took 1.7–3 s, and GET /summary took 1.2–3.2 s. POST /transactions stayed at 13–16 ms. CPU did not change. [metric:fivo-api:0001, log:fivo-api:0002, log:fivo-api:0003, log:fivo-api:0004, log:fivo-api:0006, log:fivo-api:0008, log:fivo-api:0005, metric:fivo-api:0002]

Changes before the incident, and what was concluded about each:

- **cause** — fivo-api deploy dep-0414, sha 200f96f7: migration 0007 drops ix_transactions_user_created and ix_receipts_content_type, and models.py removes the index (2026-11-10T18:54:00Z): This is the only change in the window. Latency shifted one minute later, and the dropped index matches the filters and ordering of the two slow queries. [deploy:fivo-api:0001, diff:fivo-api:0001, diff:fivo-api:0002, metric:fivo-api:0001, code:fivo-api:0001]
- **ruled out** — Configuration, environment variable and feature flag changes (any service) (2026-11-09T18:00:00Z): No configuration changes were recorded between 2026-11-09T18:00Z and 2026-11-10T19:35Z. [config:config_changes:0001]

## Hypotheses, ranked

1. **Dropping ix_transactions_user_created in migration 0007 (dep-0414) forced slow queries for the per-user transaction list and monthly summary.** (fivo-api, code_regression) — most_likely, confidence 0.88
   - Supports: The deploy landed at 18:54Z and the p95 level shift is at 18:55Z. [deploy:fivo-api:0001, metric:fivo-api:0001]
   - Supports: The diff removes the index on (user_id, created_at) from the model and drops it in migration 0007. [diff:fivo-api:0001, diff:fivo-api:0002]
   - Supports: The two slow routes run queries that filter on user_id and created_at, which this index would serve. [code:fivo-api:0001]
   - Supports: Latency was normal on both routes before the deploy and slow after. Writes (POST /transactions) were unaffected. [log:fivo-api:0014, log:fivo-api:0010, log:fivo-api:0002, log:fivo-api:0006, log:fivo-api:0005]
   - Contradicts: I did not directly confirm the index is gone in the database and have no query plan. No traces were available. [trace:traces:0001]
2. **Resource exhaustion on the single 512 MB web instance (CPU, memory, connections).** (fivo-api, resource_exhaustion) — ruled_out, confidence 0.04
   - Contradicts: CPU was flat at 11–18% with no shift. Only the routes that use the dropped index slowed, and request volume did not rise. [metric:fivo-api:0002, logagg:fivo-api:0001]
3. **A configuration change or traffic increase caused the slowdown.** (fivo-api, config_change) — ruled_out, confidence 0.02
   - Contradicts: No configuration changes were recorded in the 24 hours before the incident. [config:config_changes:0001]
   - Contradicts: Log volume fell slightly against baseline (x0.9) rather than rising. [logagg:fivo-api:0001]
4. **A downstream dependency (Redis, worker, or an LLM API) is slow.** (fivo-api, dependency_failure) — ruled_out, confidence 0.03
   - Contradicts: The slow routes are plain database reads. The routes that call other services (/insights, quick-add, /voice/call) are not where the slowdown shows. Error rate is unchanged. [logagg:fivo-api:0001, log:fivo-api:0002, log:fivo-api:0006]

## Facts

- dep-0414 (sha 200f96f7, previous 7bc71c84, message 'migrations: drop indexes with zero scans') went live at 18:54:00Z. [deploy:fivo-api:0001]
- The deploy changed only app/models.py and the new migration 0007_drop_unused_indexes.py. [changes:fivo-api:0001]
- Migration 0007 drops ix_receipts_content_type and ix_transactions_user_created. Its comment says idx_scan = 0 since the last statistics reset. The model change removes Index('ix_transactions_user_created', user_id, created_at). [diff:fivo-api:0001, diff:fivo-api:0002]
- p95 shifted from a mean of about 750 ms to about 3154 ms at 18:55Z and stayed high to 19:30Z (peak 3459 ms). [metric:fivo-api:0001]
- list_recent filters on user_id and orders by created_at desc with a limit. monthly_totals filters on user_id and created_at >= month_start. [code:fivo-api:0001]
- Before the deploy, GET /transactions took 13–17 ms and GET /summary took 22–35 ms. [log:fivo-api:0014, log:fivo-api:0015, log:fivo-api:0016, log:fivo-api:0010, log:fivo-api:0011, log:fivo-api:0012]
- After the deploy, GET /transactions took 1731–3000 ms and GET /summary took 1216–3158 ms. POST /transactions stayed at 13–16 ms. [log:fivo-api:0002, log:fivo-api:0003, log:fivo-api:0004, log:fivo-api:0006, log:fivo-api:0007, log:fivo-api:0008, log:fivo-api:0009, log:fivo-api:0005]
- CPU showed no level shift (11.5–18.4%). [metric:fivo-api:0002]
- No configuration changes were recorded in the window, and no past incidents mention indexes. [config:config_changes:0001, incident:incidents:0001]

## Assumptions

- The migration ran successfully at deploy time and the index is actually absent in Neon. This is inferred from the deploy and the matching latency shift, not queried directly.
- The slowdown is a sequential or poorly-indexed scan on transactions. No EXPLAIN output or trace spans were available.
- idx_scan=0 was probably misleading, for example because pg_stat counters had been reset recently or the stats window was short. This was not verified.
- The log lines I sampled are representative of the whole window. The log fetches were partial and capped.

## Recommended actions

- [immediate] Recreate the index now: CREATE INDEX CONCURRENTLY ix_transactions_user_created ON transactions (user_id, created_at). Also revert or supersede migration 0007 and the models.py change so the index is not dropped again. — This restores the access path used by the transaction list and the monthly summary. [diff:fivo-api:0001, diff:fivo-api:0002, code:fivo-api:0001]
- [immediate] Confirm in Neon that the index is missing (pg_indexes) and run EXPLAIN on the list_recent and monthly_totals queries before and after the fix. Watch p95 return to below 1 s. — This closes the gap that the drop was never confirmed directly and no query plan was captured. [metric:fivo-api:0001]
- [soon] Review ix_receipts_content_type as well. Check that no receipts query depends on it, or restore it if one does. — It was dropped in the same migration on the same unreliable idx_scan=0 reasoning. Its impact was not examined. [diff:fivo-api:0002]
- [later] Require evidence beyond idx_scan=0 for index-drop migrations. That means checking how long the stats have been collected, reading the code for queries that use the index, and testing on production-sized data. — The index looked unused only because of the stats window, but the code uses it. [diff:fivo-api:0002, code:fivo-api:0001]

## Missing evidence

- I did not query the database catalog (pg_indexes, pg_stat_user_indexes), so the absence of the index is not directly confirmed. The db_query tool exposes only application tables.
- No trace spans were found for the sampled slow request (trace 85d9da041d494513), so I have no per-span timing.
- Log fetches were partial and capped, so I could not compute per-route latency statistics.
- I did not check why idx_scan was 0, such as when the stats were last reset.
- I did not check the impact of dropping ix_receipts_content_type.

## Run

- system: single_agent
- termination: agent finished
- tool calls: 20
- tool failures: 0
- guardrail rejections: 2
- llm calls: 7
- total tokens: 123914
- cost usd: 0.1207
- latency s: 63.3
- citations: 67 checked, 0 invalid, 0 facts demoted to assumptions
