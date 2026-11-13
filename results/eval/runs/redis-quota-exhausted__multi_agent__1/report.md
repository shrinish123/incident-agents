# Incident report: fivo-api: 5xx rate above 5% for 10 minutes

**Status:** conclusive

At about 17:25Z on 2026-11-11 the Upstash Redis daily command counter reached its fixed 500,000 cap. Redis then rejected every command with 'ERR max daily request limit exceeded. Limit: 500000, Usage: 500000'. fivo-api could not enqueue work, so POST /transactions, /transactions/quick-add and /receipts returned 503 (120 in the window, none in baseline). fivo-worker could not connect to its broker and retried every ~32 s. No deploy, config or limit change caused it. The usage was a steady ~540 commands/min. Why that burn rate is so high, and why earlier days stayed under the cap, is not established.

## Root cause

- Service: upstash-redis
- Component: daily request quota (500,000 commands/day) on the fivo-broker instance
- Category: resource_exhaustion

A steady ~540 commands/min burn took the daily counter from 475,163 at 16:30 to the 500,000 cap by 17:25. Redis then rejected all commands, so the API enqueue raised QueueUnavailable (503) and the worker lost its broker. The source of the high burn is not identified.

## What changed (why now)

The cumulative Upstash daily command counter reached the fixed 500,000 plan cap. No deploy, config, plan or limit change was recorded (F4, F45). The reason for the ~540/min burn is not established. (at 2026-11-11T17:25:00Z)

- Before: At 16:30 usage was 475,163 and at 17:20 it was 499,513, with commands_per_min flat at about 523-556. The last OK POST /transactions span was at 17:24:08Z and the last worker task succeeded at 17:24:57. [metric:upstash-redis:0001, metric:upstash-redis:0002, trace:fivo-api:0001, log:fivo-worker:0036]
- After: Usage held at 500,000 and commands_per_min was 0 from 17:25 to 17:55. The first API enqueue failure was at 17:25:01 and the worker's first broker error at 17:25:06. [metric:upstash-redis:0001, metric:upstash-redis:0002, log:fivo-api:0001, log:fivo-worker:0037]
- Why it did not fail earlier: The condition is a fixed 500k cap against a steady ~540/min burn (about 780k/day if sustained). No change in rate preceded the cap. This was a limit reached, not a step change: the burn was flat from 14:00 (F33, F34). If the counter resets near 00:00 UTC (inferred only, F35, F43), the cap would be hit each day around 15:00-17:30 at this rate. Why earlier days did not fail cannot be determined, because the metrics source has nothing before 11-11 14:00Z (F32, F40, F44).

Changes before the incident, and what was concluded about each:

- **ruled out** — deploy on fivo-api: commit 8c66092 "categories: let clients cache the category list for five minutes" by github-actions (2026-11-11T16:54:00Z, 30 min before onset): dep-0414 changed only app/routes/categories.py, adding a Cache-Control header to GET /categories. It does not touch the queue or the write path. commands_per_min was flat at about 523-556 across the 16:54 deploy, and the 503s are Redis quota errors. [deploy:fivo-api:0001, changes:fivo-api:0001, diff:fivo-api:0001, metric:upstash-redis:0002, logagg:fivo-api:0004]

## Hypotheses, ranked

1. **The Upstash Redis daily command quota was exhausted, so the queue rejects commands and enqueue fails on the write routes.** (upstash-redis, resource_exhaustion) — most_likely, confidence 0.80
   - Supports: All 52 API 503s and 28 worker errors from 17:24 to 17:40 carry the same quota error text. [logagg:fivo-api:0004, logagg:fivo-worker:0005]
   - Supports: daily_commands_used hit 500,000 in the 17:25 bucket and commands_per_min fell from ~540 to 0, with the limit constant at 500,000. [metric:upstash-redis:0001, metric:upstash-redis:0002, metric:upstash-redis:0003]
   - Supports: Last OK POST /transactions span was 17:24:08Z; the first 503 was 17:25:01Z and the first worker broker error 17:25:06Z. Enqueue failures take 14-22 ms. [trace:fivo-api:0001, log:fivo-api:0001, log:fivo-worker:0037, trace:fivo-api:0002]
   - Supports: 120 503s on the three write routes, none in baseline, while reads and /health were unchanged. [logagg:fivo-api:0001, logagg:fivo-api:0002]
   - Contradicts: The counter implies exhaustion near 17:21, but writes and worker tasks succeeded until 17:24:57. The counter slope (~487/min) also differs about 10% from commands_per_min (~540). [metric:upstash-redis:0001, log:fivo-worker:0036]
   - Contradicts: Worker defaults and write volume do not explain the ~540/min burn. [code:fivo-worker:0001, codehit:fivo-worker:0002]
2. **A long-standing or runaway source of Redis commands (idle worker polling, result backend, retries) pushes usage to ~780k/day, above the 500k cap. This is the 'why now' behind H1.** (fivo-worker, config_change) — possible, confidence 0.15
   - Supports: The steady ~540/min is far above the ~100-150/min estimated from worker defaults (recalled, unverified) and above write volume. [metric:upstash-redis:0005, metric:upstash-redis:0008, code:fivo-worker:0001]
   - Contradicts: No config, plan or limit change was recorded from 10-28 to 11-11. [config:config_changes:0002, config:fivo-worker:0001]
   - Contradicts: Deploys in the 14 days touch no Celery, broker or enqueue code. The only worker deploy, dep-0407, changed groq_timeout_s. [deploy:fivo-worker:0001, diff:fivo-worker:0002, deploy:fivo-api:0002, diff:fivo-api:0002, changes:fivo-api:0002]
   - Contradicts: Quick-add polling and worker retries are bounded and too small to explain ~400/min. [code:fivo-api:0002, codehit:fivo-api:0003, codehit:fivo-worker:0004]
   - Contradicts: Earlier days' usage is unobservable, so this can be neither confirmed nor refuted. [metric:upstash-redis:0010, metric:upstash-redis:0006]
3. **Neon Postgres is suspended, cold or out of connections, so writes fail.** (neon-postgres, dependency_failure) — ruled_out, confidence 0.01
   - Contradicts: All 22 API 503s and 10 worker errors in 17:24-17:30 are Redis quota errors, with no database errors. The 503s return in 14-20 ms, not a pool-timeout pattern. [logagg:fivo-api:0003, logagg:fivo-worker:0004, log:fivo-api:0002, log:fivo-api:0006]
   - Contradicts: Read routes are healthy. [logagg:fivo-api:0001]
4. **fivo-worker is stuck, crash-looping or out of memory, so the queue backs up.** (fivo-worker, resource_exhaustion) — ruled_out, confidence 0.01
   - Contradicts: The worker ran healthily until its last task succeeded at 17:24:57. Its only errors are consumer connection failures to the quota-exhausted broker. No OOM, restart or 429 messages appear. [log:fivo-worker:0036, log:fivo-worker:0037, logagg:fivo-worker:0003]
5. **Deploy dep-0414 (8c66092) introduced a write-path regression.** (fivo-api, code_regression) — ruled_out, confidence 0.01
   - Supports: It is the only deploy on 11-11, at 16:54Z. [deploy:fivo-api:0001]
   - Contradicts: The diff changes only app/routes/categories.py, adding a Cache-Control header to GET /categories. [changes:fivo-api:0001, diff:fivo-api:0001]
   - Contradicts: commands_per_min was flat across the 16:54 deploy. [metric:upstash-redis:0002]

## Facts

- Between 17:08 and 17:58 fivo-api logged 120 503s (71 /transactions, 27 /receipts, 22 /transactions/quick-add), none in baseline. Reads were unchanged. [logagg:fivo-api:0001]
- Every API 503 from 17:24 to 17:40 (52) is 'QueueUnavailable ... ERR max daily request limit exceeded. Limit: 500000, Usage: 500000.' [logagg:fivo-api:0004, log:fivo-api:0001]
- All 28 worker error lines from 17:24 to 17:40 are 'Cannot connect to rediss://...: ERR max daily request limit exceeded', retried every 32 s. The worker logged 62 errors in the window against 0 in baseline. [logagg:fivo-worker:0005, logagg:fivo-worker:0001, log:fivo-worker:0037, log:fivo-worker:0038]
- daily_commands_used rose from 475,163 at 16:30 to 499,513 at 17:20 and reached 500,000 at 17:25. commands_per_min was ~523-556, then 0 from 17:25. [metric:upstash-redis:0001, metric:upstash-redis:0002]
- daily_command_limit was a constant 500,000. [metric:upstash-redis:0003, metric:upstash-redis:0009]
- The last OK POST /transactions span was 17:24:08Z and the last worker task succeeded at 17:24:57Z. [trace:fivo-api:0001, log:fivo-worker:0036]
- The only deploy on 11-11 was dep-0414 at 16:54Z, which changed only a Cache-Control header on GET /categories. No config changes were recorded from 12:00 to 17:58. [deploy:fivo-api:0001, changes:fivo-api:0001, diff:fivo-api:0001, config:config_changes:0001]
- Redis metrics exist only from 11-11 14:00Z. Counter rose ~29.2k/hour with commands_per_min flat at ~536-545 before the cap. [metric:upstash-redis:0004, metric:upstash-redis:0005, metric:upstash-redis:0006]
- No config or infrastructure change matching 'upstash' was recorded from 10-28 to 11-11, and no past incident matches 'daily request limit'. [config:config_changes:0002, incident:incidents:0001]

## Assumptions

- The daily counter resets at about 00:00 UTC. This is inferred from back-extrapolation only and the reset time was not observed.
- Reads stay healthy because they do not use Redis. This is inferred from topology and the fast-failing enqueue spans, since no Redis child span was returned.
- The remaining ~400/min burn is probably idle Celery/kombu broker traffic. This is not measured.
- Worker idle command estimate of 100-150/min is a recalled default and unverified.

## Recommended actions

- [immediate] Raise the Upstash plan or daily command limit for fivo-broker, or upgrade to a plan without a daily cap, so the broker accepts commands again. Then confirm POST /transactions, quick-add and /receipts return 201/202 and the worker consumer reconnects. — Redis rejects all commands at the 500,000 cap, which is the direct cause of the 503s. Without action, writes stay down until the counter resets, which is assumed to be about 00:00 UTC. [logagg:fivo-api:0004, metric:upstash-redis:0001, metric:upstash-redis:0002]
- [soon] Measure what drives ~540 commands/min using the Upstash console per-command breakdown and daily usage for the past 7 days. Check idle fivo-worker traffic (BRPOP polling, heartbeat, gossip, result backend) and reduce it if it is the main source. — At ~540/min usage is about 780k/day, above the cap. The source is unexplained and the cap will be hit again around 15:00-17:30 each day if the counter resets at midnight. [metric:upstash-redis:0005, code:fivo-worker:0001, metric:upstash-redis:0010]
- [later] Add alerts on upstash-redis daily_commands_used at 70% and 90% of daily_command_limit, and keep the metrics with longer retention than the current 11-11 14:00Z start. — Usage was at 95% by 16:30, about 55 minutes before the cap. Nothing alerted, and earlier days cannot be inspected. [metric:upstash-redis:0001, metric:upstash-redis:0006]
- [later] Do not roll back dep-0414. — It changes only a Cache-Control header on GET /categories and is unrelated to the failure. [diff:fivo-api:0001]

## Missing evidence

- Upstash daily usage before 11-11 14:00Z, the counter reset time and previous daily peaks. The metrics source has no data earlier and there is no Upstash console or API tool.
- Per-command breakdown of the ~540/min burn and a measured idle command rate for fivo-worker.
- Redis connected-clients, error and latency metrics do not exist, so a connection limit is excluded only by the error text.
- No Redis child spans. The trace search was partial, capped at 30 root spans, and spans after 17:35 were not viewed.
- Env var changes made outside deploys were not covered, and changes before 10-28 were not checked.
- Unreconciled: the counter implies exhaustion near 17:21 but successes continued to 17:24:57, and the counter slope differs ~10% from commands_per_min.

## Run

- system: multi_agent
- termination: accepted
- rounds: 3
- hypotheses: 5
- steps: 14
- max hops: 6
- tool calls: 58
- tool failures: 0
- guardrail rejections: 1
- llm calls: 56
- total tokens: 861899
- cost usd: 1.4573
- latency s: 1228.0
- citations: 86 checked, 0 invalid, 0 facts demoted to assumptions
