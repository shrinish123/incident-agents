# Incident report: fivo-api: 5xx rate above 5% and p95 above 5 s

**Status:** conclusive

At 13:51:00Z someone changed DB_POOL_SIZE on fivo-api from 10 to 2 in the dashboard. From 13:52 the SQLAlchemy pool (size 2, overflow 0, 5 s timeout) ran out of connections under normal load. Requests then waited 5 s for a connection and returned HTTP 500. That matches the 5xx and p95 alert on /transactions, /summary and quick-add, while /health, which needs no DB connection, still passed. Fix: restore DB_POOL_SIZE to 10 or another sensible value, and handle the database connection limit separately.

## Root cause

- Service: fivo-api
- Component: DB_POOL_SIZE env var (SQLAlchemy QueuePool), cfg-0139
- Category: config_change

cfg-0139 cut DB_POOL_SIZE from 10 to 2 at 13:51:00Z. With overflow 0 and a 5 s pool timeout, concurrent requests exhausted the 2-connection pool. They failed with sqlalchemy.exc.TimeoutError after about 5006 ms, which gave the 500s and the high p95.

## What changed (why now)

The fivo-api DB_POOL_SIZE env var was reduced from 10 to 2 through the dashboard. (at 2026-11-10T13:51:00Z)

- Before: Pool size was 10. There were no errors from 13:40 to 13:52, and db_connections ranged 2 to 8 and averaged 5.24 in the baseline window. [config:fivo-api:0001, log:fivo-api:0009, metric:fivo-api:0001]
- After: The pool is size 2 with overflow 0. Pool timeouts of about 5 s return 500s, and db_connections is pinned at 2. [log:fivo-api:0001, metric:fivo-api:0001, logagg:fivo-api:0001]

Changes before the incident, and what was concluded about each:

- **ruled out** — fivo-api deploy dep-0414, sha b139bb33 ('access log: record the request id') (2026-11-10T13:41:00Z): The diff adds one log field and touches only README.md and app/logging_setup.py. There were no errors for 11 minutes after it, until the config change. [deploy:fivo-api:0001, changes:fivo-api:0001, diff:fivo-api:0001, log:fivo-api:0009]
- **cause** — fivo-api env var DB_POOL_SIZE 10 to 2 (cfg-0139) (2026-11-10T13:51:00Z): The errors begin about one minute later and name the pool limit of size 2. db_connections is capped at 2 from 13:52. [config:fivo-api:0001, log:fivo-api:0001, metric:fivo-api:0001]

## Hypotheses, ranked

1. **The DB_POOL_SIZE reduction from 10 to 2 caused connection pool exhaustion, which caused 5xx errors and latency.** (fivo-api, config_change) — most_likely, confidence 0.95
   - Supports: Config change at 13:51:00Z, DB_POOL_SIZE 10 to 2, with the note 'stay under the database plan's connection limit'. [config:fivo-api:0001]
   - Supports: Errors are QueuePool limit of size 2 overflow 0 timeouts at about 5006 ms, across several routes. The first one is at 13:52:03Z. [log:fivo-api:0001, log:fivo-api:0004, log:fivo-api:0006]
   - Supports: There were no errors from 13:40 to 13:52, and errors start in the 13:52 minute. [log:fivo-api:0009, logagg:fivo-api:0002]
   - Supports: db_connections is pinned at exactly 2 from 13:52 onward, where before it ranged 3 to 8. [metric:fivo-api:0001]
2. **The 13:41 deploy (b139bb33, access log request_id) caused the errors.** (fivo-api, code_regression) — ruled_out, confidence 0.03
   - Supports: The deploy happened about 10 minutes before the errors began. [deploy:fivo-api:0001]
   - Contradicts: The diff only adds a request_id field to the access log, and only README.md and app/logging_setup.py changed. [changes:fivo-api:0001, diff:fivo-api:0001]
   - Contradicts: There were no errors between the deploy and the config change. [log:fivo-api:0009]
3. **A downstream dependency failure (Neon, Redis or an external API) caused the errors.** (neon-postgres, dependency_failure) — ruled_out, confidence 0.03
   - Contradicts: The errors are client-side pool timeouts (pool size 2, overflow 0), not database errors. I did not query dependency health directly. [log:fivo-api:0001]

## Facts

- A 5xx burst began at about 13:52Z and continued through 14:20Z at 9–21 error lines per 5-minute bucket. [logagg:fivo-api:0001]
- DB_POOL_SIZE was changed from 10 to 2 at 13:51:00Z by dashboard:dev. [config:fivo-api:0001]
- The errors are sqlalchemy QueuePool TimeoutErrors (size 2, overflow 0, timeout 5.00) with duration_ms of 5006, on /summary, /transactions, POST /transactions and quick-add. [log:fivo-api:0001, log:fivo-api:0003, log:fivo-api:0004, log:fivo-api:0006]
- The code reads the pool size from DB_POOL_SIZE, with a default of 10. [codehit:fivo-api:0001]
- db_connections has been a constant 2 since 13:52. [metric:fivo-api:0001]
- The 13:41 deploy only added request_id to the access log. [diff:fivo-api:0001, changes:fivo-api:0001]

## Assumptions

- The running instance picked up the new env var by restarting at or just after 13:51. I did not check the restarts metric.
- Normal concurrency is above 2 simultaneous DB sessions. This is inferred from the earlier db_connections range of 3 to 8.
- The log fetch was partial, so only some of the error lines were read.

## Recommended actions

- [immediate] Restore DB_POOL_SIZE to 10, or a value that fits the Neon plan limit, and restart or redeploy fivo-api. — This removes the pool exhaustion that is causing the 500s. [config:fivo-api:0001, log:fivo-api:0001]
- [soon] If the aim was to stay under the Neon connection limit, size the pool and max_overflow against the combined fivo-api and fivo-worker connections. Consider PgBouncer or the Neon pooled endpoint. — The change note shows the real concern was the connection cap. Setting the pool to 2 was too aggressive for the load. [config:fivo-api:0001, metric:fivo-api:0001]
- [later] Add an alert on pool timeouts, and review or gate dashboard env var edits in production. — The change was made by hand in the dashboard and caused an outage within a minute. [config:fivo-api:0001]

## Missing evidence

- I did not query the restarts metric to confirm when the env var took effect.
- I did not check the Neon connection limit or Neon-side metrics.
- I did not query the error_rate or p95_latency_ms metrics directly. Latency is inferred from duration_ms in the error logs.
- I did not check the database for load or connection counts.

## Run

- system: single_agent
- termination: agent finished
- tool calls: 12
- tool failures: 0
- guardrail rejections: 0
- llm calls: 5
- total tokens: 84977
- cost usd: 0.0884
- latency s: 39.2
- citations: 40 checked, 0 invalid, 0 facts demoted to assumptions
