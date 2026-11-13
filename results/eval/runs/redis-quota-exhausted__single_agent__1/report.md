# Incident report: fivo-api: 5xx rate above 5% for 10 minutes

**Status:** conclusive

At about 17:25:01Z the Upstash Redis broker started refusing every command with `ERR max daily request limit exceeded. Limit: 500000, Usage: 500000`. fivo-api enqueues work through that broker on the three write routes (POST /transactions, /transactions/quick-add, /receipts), so they return 503 `QueueUnavailable`. fivo-worker also lost its broker connection and has completed 0 tasks since 17:25. Read routes don't use the queue and stayed healthy. Immediate fix: raise or upgrade the Upstash quota, or wait for the daily reset. I could not establish why the quota was exhausted at 17:25 today, because there are no Upstash usage metrics.

## Root cause

- Service: upstash-redis
- Component: Upstash daily command quota (500,000/day) on the Celery broker/result backend
- Category: resource_exhaustion

The Upstash Redis plan's 500,000-command daily quota was used up at about 17:25Z. Every Redis command from fivo-api (`celery.send_task` in `app/queue.py`) and from fivo-worker was then rejected. The API turned that into 503 `QueueUnavailable` on the write routes, and the worker could not consume tasks. The cause of the high consumption is not established.

## What changed (why now)

The Redis broker's daily command quota reached 500000/500000 at about 17:25Z. No deploy or config change that would explain it was found. The only deploy, dep-0414, was a Cache-Control header on GET /categories. (at 2026-11-11T17:25:01Z)

- Before: Write routes returned 201/202 and the worker completed about 3.4-6 tasks/min with queue depth 0 until 17:20. fivo-api had no 503s before 17:20 (only 3 isolated 500s). [logagg:fivo-api:0001, metric:fivo-worker:0002, metric:fivo-worker:0001]
- After: From 17:25:01Z, fivo-api write routes return 503 `QueueUnavailable: ... max daily request limit exceeded`. The worker logs `Cannot connect to rediss://...: ERR max daily request limit exceeded` and tasks_completed drops to 0. [log:fivo-api:0001, log:fivo-worker:0001, metric:fivo-worker:0002, logagg:fivo-api:0004]
- Why it did not fail earlier: This is a cumulative daily limit, so the failure arrives when usage crosses 500k, not when anything changes. I could not see per-hour Redis usage, the quota reset time or yesterday's peak, so I cannot say why it crossed at 17:25 today. Likely contributors are constant Celery broker polling by api and worker, 150 ms result polling in `wait_for_result`, and traffic about 1.4x the earlier baseline. All of these are unverified.

Changes before the incident, and what was concluded about each:

- **ruled out** — fivo-api deploy dep-0414, sha 8c660927 (prev 7bc71c84): categories: let clients cache the category list for five minutes (2026-11-11T16:54:00Z): It only adds a `Cache-Control` header to GET /categories. It does not touch the queue or write routes, and the 503s started 31 minutes later with an Upstash quota error. It could slightly reduce read traffic but cannot cause the failure. [deploy:fivo-api:0001, diff:fivo-api:0001, changes:fivo-api:0001, log:fivo-api:0001]
- **ruled out** — Configuration, env var, feature flag and infrastructure changes, 2026-11-10T16:00Z to 2026-11-11T18:00Z (2026-11-11T17:00:00Z): None were recorded. The `when` value is a placeholder within the searched window because there is no change to date. [config:config_changes:0001]

## Hypotheses, ranked

1. **Upstash Redis daily request quota (500k) was exhausted, breaking enqueue in the API and consumption in the worker.** (upstash-redis, resource_exhaustion) — most_likely, confidence 0.93
   - Supports: The exact quota error text appears in both fivo-api and fivo-worker logs, starting at 17:25. [log:fivo-api:0001, log:fivo-worker:0001]
   - Supports: Worker throughput went to 0 at the same minute. [metric:fivo-worker:0002]
   - Supports: Only queue-dependent POST routes fail; reads are healthy. [logagg:fivo-api:0002]
   - Contradicts: No Upstash usage metric is available, so the reason consumption reached 500k today is unexplained. [logagg:fivo-api:0003]
2. **The dep-0414 deploy (categories Cache-Control header) caused the 503s.** (fivo-api, code_regression) — ruled_out, confidence 0.03
   - Supports: It was the only deploy that day. [deploy:fivo-api:0001]
   - Contradicts: The diff only adds a response header to GET /categories, and the 503s began 31 minutes after the deploy with a Redis quota error. [diff:fivo-api:0001, log:fivo-api:0001]
3. **A config or env change (for example a Redis URL or credential change) broke the broker connection.** (fivo-api, config_change) — ruled_out, confidence 0.02
   - Contradicts: No config changes were recorded. The error is an explicit quota error from the server, not an auth or connection failure. [config:config_changes:0001, log:fivo-worker:0001]
4. **A traffic surge or retry storm consumed the quota unusually fast.** (fivo-api, traffic_change) — possible, confidence 0.10
   - Supports: Volume was about 1.4x the earlier baseline, and quick-add polls Redis every 150 ms. [logagg:fivo-api:0003, code:fivo-api:0001]
   - Contradicts: There is no sharp traffic spike, so usage was most likely cumulative. Command counts per source are not available to confirm. [logagg:fivo-api:0003]
5. **Worker, Postgres or Groq trouble caused the failures.** (fivo-worker, dependency_failure) — ruled_out, confidence 0.02
   - Supports: Groq 429s were seen before the incident. [logagg:fivo-worker:0002]
   - Contradicts: These were only 5 retried warnings, and the API errors are the Redis quota message. The worker's own errors are broker connection failures. [log:fivo-worker:0001, log:fivo-api:0001]

## Facts

- The first 503s in the window are at 17:25 (4 in the 17:25 minute, then 3-6 per minute). The messages are `QueueUnavailable: task queue unavailable: ERR max daily request limit exceeded. Limit: 500000, Usage: 500000.` on POST /receipts and POST /transactions. [log:fivo-api:0001, log:fivo-api:0002, logagg:fivo-api:0004]
- Errors from 16:45 to 17:30 are all on POST routes: /transactions 503 x10 and 500 x3, /receipts 503 x8, /transactions/quick-add 503 x4. No read route is in the error list. [logagg:fivo-api:0002]
- The worker consumer logs the same Upstash quota error from 17:25:06, retrying every 32s. Worker error lines run at 10-19 per 10 minutes through 17:50. [log:fivo-worker:0001, log:fivo-worker:0005, logagg:fivo-worker:0001]
- Worker tasks_completed fell from about 4.4/min to 0 at 17:25 and stayed 0 through 17:55. Queue depth stayed 0 because nothing could be enqueued. [metric:fivo-worker:0002, metric:fivo-worker:0001]
- fivo-api enqueues via `celery.send_task` and converts the broker `OperationalError` into `QueueUnavailable`, which surfaces as 503. quick-add also polls the result backend every 0.15s via `AsyncResult.ready()`. [code:fivo-api:0001]
- The topology notes say Upstash is the Celery broker and result backend, the plan has a daily command quota, and quick-add waits for the worker's result. [topology:upstash-redis:0001, topology:fivo-api:0001]
- The only deploy in the window, dep-0414 at 16:54Z (8c660927), changed one file, `app/routes/categories.py`, adding a `Cache-Control` header to GET /categories. It does not touch the queue. [deploy:fivo-api:0001, changes:fivo-api:0001, diff:fivo-api:0001]
- No configuration, env var or feature flag changes were recorded from 2026-11-10T16:00Z to 2026-11-11T18:00Z. [config:config_changes:0001]
- Request volume was only about 1.4x the earlier baseline (1274 lines in 16:00-17:58 vs 922 in a 14:00-15:58 window), with no new routes. There was no abrupt traffic spike. [logagg:fivo-api:0003]
- A few Groq 429 retries appeared in the worker before the incident. They are unrelated to the Redis error. [logagg:fivo-worker:0002]
- There are no past incidents matching this quota error. [incident:incidents:0001]

## Assumptions

- The 503s continue until the quota resets or is raised. The Upstash reset time was not checked.
- Cumulative Celery/Redis command usage (broker polling, result polling, task traffic) from both services consumed the 500k quota. I did not observe per-source command counts.
- The window's first 503 at 17:25:01 reflects the quota boundary. There may have been earlier unlogged rejections.

## Recommended actions

- [immediate] Raise or upgrade the Upstash plan or quota, or move to a pay-as-you-go tier, so Redis accepts commands again. Then confirm fivo-api 503s stop and worker tasks_completed recovers. — Redis is rejecting all commands, so writes and background work will stay down until the quota is lifted or resets. [log:fivo-api:0001, log:fivo-worker:0001]
- [immediate] After recovery, check the queue for tasks lost or stuck: transactions awaiting categorisation and receipts awaiting process_receipt. Re-enqueue as needed. Consider telling users whose writes were rejected between 17:25 and the fix. — Writes were refused for the whole outage, and worker tasks have not run since 17:25. [metric:fivo-worker:0002]
- [soon] Find the main consumer of Redis commands (Upstash command stats): idle Celery polling in the api and worker, the 150 ms `wait_for_result` polling, and result backend writes. Then reduce it, for example by lowering polling frequency or the worker/beat polling settings, or by using a connection-based plan. — The reason usage hit 500k is unexplained, so the same exhaustion will recur daily. [code:fivo-api:0001, code:fivo-worker:0001]
- [soon] Add an alert on Upstash quota usage (for example 70% and 90%). Make the write routes degrade gracefully when the queue is unavailable, for example by saving the transaction and enqueueing categorisation later. — This outage went undetected until the 5xx alert, and the queue is a hard dependency on the write path. [log:fivo-api:0001]

## Missing evidence

- No Upstash usage or command-rate metrics, so the consumption trend, per-source breakdown and quota reset time are unknown.
- Could not query logs beyond 4h windows, so there is no per-day comparison with yesterday's usage.
- The database was not queried, so the count of rejected or lost user writes is unquantified.
- fivo-worker error log fetch was partial/truncated, so only a sample of worker error lines was read.
- The deploys list showed only dep-0414 in the window. I did not confirm independently that no worker deploy occurred.

## Run

- system: single_agent
- termination: agent finished
- tool calls: 30
- tool failures: 0
- guardrail rejections: 2
- llm calls: 10
- total tokens: 233334
- cost usd: 0.1845
- latency s: 208.9
- citations: 53 checked, 0 invalid, 0 facts demoted to assumptions
