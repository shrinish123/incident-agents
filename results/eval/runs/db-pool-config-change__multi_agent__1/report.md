# Incident report: fivo-api: 5xx rate above 5% and p95 above 5 s

**Status:** conclusive

At 13:51:00Z config change cfg-0139 cut fivo-api's DB_POOL_SIZE from 10 to 2 (dashboard, dev). The engine has max_overflow=0 and pool_timeout=5.0 s, so requests that cannot get one of 2 connections wait 5 s and fail with sqlalchemy.exc.TimeoutError. The first such error is at 13:52:03Z, db_connections is a flat 2 from 13:52Z, and p95 is pinned at 5006 ms. The 5xx errors are partial and span many DB routes, while /health stays 200. Restoring DB_POOL_SIZE to 10 is the immediate mitigation. This has not yet been confirmed by reverting.

## Root cause

- Service: fivo-api
- Component: DB_POOL_SIZE env var (cfg-0139), SQLAlchemy engine in app/db.py
- Category: config_change

cfg-0139 reduced DB_POOL_SIZE from 10 to 2 at 13:51:00Z. With max_overflow=0 and a 5 s pool timeout, concurrent requests that each hold a session exhaust the 2-connection pool and fail with QueuePool timeouts. This produces 500s and the 5006 ms latency ceiling on every DB-backed route.

## What changed (why now)

DB_POOL_SIZE was reduced from 10 to 2 on fivo-api via the dashboard (cfg-0139). Pre-change demand reached 8 connections, so a cap of 2 is too small. (at 2026-11-10T13:51:00Z)

- Before: Pool cap was 10. db_connections ranged 2-8 (mean about 5.1) and error_rate was 0.0 every minute from 13:45Z to 13:50Z. There were no QueuePool errors before 13:52:03Z. [config:fivo-api:0001, metric:fivo-api:0007, metric:fivo-api:0008, log:fivo-api:0001]
- After: Pool cap is 2. db_connections is a constant 2.0 from 13:52Z to 14:24Z. QueuePool 'size 2 overflow 0 ... timeout 5.00' errors start at 13:52:03Z, and error_rate reaches 0.2143 at 13:52Z and 0.6071 by 13:59Z. [metric:fivo-api:0007, metric:fivo-api:0008, log:fivo-api:0001, logagg:fivo-api:0003]

Changes before the incident, and what was concluded about each:

- **ruled out** — deploy on fivo-api: commit b139bb3 "access log: record the request id" by github-actions (2026-11-10T13:41:00Z, 9 min before onset): The diff only adds a request_id key to the access log and 2 README lines, with no I/O, DB or session code. No errors appeared between the 13:41Z deploy and 13:52:03Z. All errors are pool timeouts that name the new pool size of 2. [deploy:fivo-api:0001, diff:fivo-api:0001, diff:fivo-api:0002, changes:fivo-api:0001, log:fivo-api:0001, metric:fivo-api:0008]
- **cause** — config on fivo-api: DB_POOL_SIZE: 10 -> 2 by dashboard:dev ("stay under the database plan's connection limit") (2026-11-10T13:51:00Z, 1 min after onset): DB_POOL_SIZE was cut from 10 to 2 at 13:51:00Z. The first QueuePool 'size 2' error is at 13:52:03Z, db_connections is a flat 2 from 13:52Z, and errors are 5006 ms pool timeouts on every DB route. Before the change there were no errors, with demand up to 8 connections. [config:fivo-api:0001, log:fivo-api:0001, metric:fivo-api:0007, metric:fivo-api:0008, code:fivo-api:0001]

## Hypotheses, ranked

1. **Config change cfg-0139 (DB_POOL_SIZE 10 -> 2 at 13:51Z) starved fivo-api's connection pool, so DB-backed requests hit the 5 s pool timeout and return 500.** (fivo-api, config_change) — most_likely, confidence 0.90
   - Supports: Config change set DB_POOL_SIZE 10 -> 2 at 13:51:00Z. [config:fivo-api:0001]
   - Supports: All 28 error lines in 13:48-14:00Z are 'QueuePool limit of size 2 overflow 0 reached ... timeout 5.00'. The first is at 13:52:03Z, after the change. [logagg:fivo-api:0003, log:fivo-api:0001, log:fivo-api:0028]
   - Supports: db_connections is a flat 2.0 from 13:52Z. error_rate was 0.0 through 13:50Z and rose from 13:51Z to 0.6071 by 13:59Z. [metric:fivo-api:0007, metric:fivo-api:0008]
   - Supports: Code sets pool_size from DB_POOL_SIZE, max_overflow=0 and pool_timeout=5.0. This matches the 5006 ms p95 ceiling. [code:fivo-api:0001, codehit:fivo-api:0002, metric:fivo-api:0001]
   - Supports: Errors span many routes while /health stays 200. Successful requests continue, which fits partial pool starvation. [logagg:fivo-api:0001]
   - Contradicts: error_rate 0.0333 at 13:51Z precedes the first error log by about a minute. This is unexplained but small. The 5-minute metrics show onset in the 13:50 bucket, which is bucket granularity. [metric:fivo-api:0008, metric:fivo-api:0002]
2. **Deploy dep-0414 (b139bb3, 'access log: record the request id') introduced a regression causing 500s and stalls.** (fivo-api, code_regression) — ruled_out, confidence 0.02
   - Supports: It was deployed 9 minutes before the 13:50 onset on code that runs on every request. [deploy:fivo-api:0001]
   - Contradicts: The diff only adds one request_id key to the access log, plus README lines. It has no I/O, DB or session code. [changes:fivo-api:0001, diff:fivo-api:0001, diff:fivo-api:0002]
   - Contradicts: The first error is at 13:52:03Z, and errors are pool timeouts naming size 2. [log:fivo-api:0001, logagg:fivo-api:0003]
3. **neon-postgres is slow, suspended or at its connection limit, as in INC-2026-024.** (neon-postgres, dependency_failure) — ruled_out, confidence 0.01
   - Supports: A past incident had QueuePool timeouts caused by a Neon cold start. This is a lead only. [incident:fivo-api:0001]
   - Contradicts: No Neon deploys or config changes were found. [deploy:neon-postgres:0001, config:neon-postgres:0001]
   - Contradicts: Neon logs show only routine checkpoint lines. [logagg:neon-postgres:0002]
   - Contradicts: slow_queries was 0 throughout. Active connections fell at 13:50Z instead of saturating. [metric:neon-postgres:0002, metric:neon-postgres:0001]
   - Contradicts: The errors are client-side pool timeouts of size 2. [logagg:fivo-api:0003]
4. **The single 512 MB fivo-api instance is out of memory or CPU, or thrashing.** (fivo-api, resource_exhaustion) — ruled_out, confidence 0.01
   - Supports: fivo-api runs on one 512 MB instance shared by all routes. [topology:fivo-api:0001]
   - Contradicts: Memory stayed at 198.7-208.0 MB and CPU at 6.6-22.5% with no shift. [metric:fivo-api:0003, metric:fivo-api:0004]
   - Contradicts: Only two restarts occurred, at 13:41Z and 13:51Z, each matching a change. None followed. [metric:fivo-api:0005]

## Facts

- cfg-0139 at 13:51:00Z changed fivo-api DB_POOL_SIZE from 10 to 2, by dashboard:dev, with the note 'stay under the database plan's connection limit'. [config:fivo-api:0001]
- Deploy dep-0414 (b139bb3) went live at 13:41:00Z. Its diff only adds a request_id key to the access log, plus README lines. [deploy:fivo-api:0001, changes:fivo-api:0001, diff:fivo-api:0002]
- All 28 error lines in 13:48-14:00Z are QueuePool size 2 timeout errors. The first is at 13:52:03Z. [logagg:fivo-api:0003, log:fivo-api:0001]
- db_connections is a constant 2.0 from 13:52Z to 14:24Z. Before that it ranged 2-8. [metric:fivo-api:0007]
- error_rate was 0.0 from 13:45Z to 13:50Z and reached 0.6071 by 13:59Z. [metric:fivo-api:0008]
- p95 latency sat at 5006 ms from 13:55Z to 14:20Z. Baseline mean was about 932 ms. [metric:fivo-api:0001]
- 500s span /transactions, /summary, quick-add, /receipts and /categories. The health check returned 200 throughout. [logagg:fivo-api:0001]
- The engine uses pool_size=settings.db_pool_size, max_overflow=0 and pool_timeout=5.0 s. Sessions are request-scoped. [code:fivo-api:0001, codehit:fivo-api:0002, codehit:fivo-api:0003]
- Neon had no deploys or config changes and slow_queries was 0. fivo-api memory and CPU were flat. [deploy:neon-postgres:0001, config:neon-postgres:0001, metric:neon-postgres:0002, metric:fivo-api:0003, metric:fivo-api:0004]

## Assumptions

- The 13:51Z restart is probably the instance restarting to apply the env var. This is inferred from timing.
- Sessions may stay open during the quick-add worker wait and the insights LLM call. This is inferred, not proven, and does not change the conclusion.
- No evidence quantifies concurrent connection hold times. Saturation of a pool of 2 at the observed request rate is inferred.
- The 13:50 onset in the 5-minute metrics is assumed to be bucket granularity, because 1-minute data shows errors starting after 13:51Z.

## Recommended actions

- [immediate] Restore fivo-api DB_POOL_SIZE to 10 (revert cfg-0139) and watch error_rate, p95 and db_connections. — This restores the pre-change pool cap, under which there were no errors. It also confirms the cause. [config:fivo-api:0001, log:fivo-api:0001, metric:fivo-api:0007]
- [soon] If the database plan's connection limit is a real concern, pick a pool size from measured concurrent use (db_connections reached 8 before the change). Shorten session lifetime in quick-add and insights so they do not hold a session across the worker wait or the LLM call. — The change was meant to stay under a plan limit, but 2 is too small for the demand. Long-held sessions make a small pool saturate. [codehit:fivo-api:0003, code:fivo-api:0002, metric:fivo-api:0007]
- [later] Alert on QueuePool TimeoutError counts and on db_connections pinned at the pool cap. Add review or a staged rollout for pool-size env var changes. Add a health check that touches the DB. — /health stayed 200 while many routes failed for over 30 minutes, and detection came about 15 minutes after onset. [logagg:fivo-api:0001, logagg:fivo-api:0002]

## Missing evidence

- No revert of DB_POOL_SIZE has been tried yet, so recovery is unconfirmed.
- There is no separate pool checked-out metric, so in-use connections cannot be told apart from the cap.
- Error lines 0017-0028 were seen only as summaries. Their route and duration fields were not read.
- The single fivo-worker warning line (logagg:fivo-worker:0001) was never read.
- Neon's external status page could not be checked.

## Run

- system: multi_agent
- termination: accepted
- rounds: 1
- hypotheses: 4
- steps: 8
- max hops: 3
- tool calls: 40
- tool failures: 0
- guardrail rejections: 1
- llm calls: 34
- total tokens: 422335
- cost usd: 0.7249
- latency s: 196.8
- citations: 74 checked, 0 invalid, 0 facts demoted to assumptions
