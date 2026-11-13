# Incident report: fivo-api: 429 responses above 20% of requests

**Status:** conclusive

At 07:14 UTC on 2026-11-11, user 137 (python-requests/2.32, device cli-7f3a, source=api) began a scripted write flood on POST /transactions, about 235 accepted requests a minute and 7216 rows by 07:44:47. fivo-api's RateLimitMiddleware is a single in-memory fixed 60-second counter for the whole instance, with a code default of 240 requests a minute and no per-user key. The flood fills that shared budget, so excess requests get 429 'rate limit exceeded' in about 1 ms, and other users' requests are also rejected once the cap is hit. That gave 2707 429s out of 10342 requests (about 26%), 2673 of them on /transactions, with no recovery by 07:45. The deploy at 07:06 (e951293, one insights prompt line) is not involved. The deployed RATE_LIMIT_RPM value and the reason user 137 started are unverified.

## Root cause

- Service: fivo-api
- Component: RateLimitMiddleware (app/middleware/ratelimit.py), global in-memory 240 rpm counter, hit by user 137's POST /transactions flood
- Category: traffic_change

User 137 began a scripted bulk write at 07:14, offering about 330 requests a minute against a global per-instance limit of 240 a minute (code default). The limiter has no per-user key, so once the shared budget is spent, every request is rejected with 429, including other users' requests. The limit itself did not change; normal traffic of about 9 requests a minute never approached it.

## What changed (why now)

User 137 began a sustained scripted write flood on POST /transactions at about 235 accepted requests a minute. This pushed the instance's total request rate from about 9 a minute to about 330, above the global 240 rpm cap. The limiter code and config did not change. (at 2026-11-11T07:14:00Z)

- Before: In the 06:00-06:50 baseline there were no 429s and no source=api rows. The request rate was about 9 a minute, far below 240, and 135 /transactions 201s were logged. User 137 had only one ordinary card row, at 07:04, before the flood. [logagg:fivo-api:0001, db:transactions:0001, metric:fivo-api:0005, logagg:fivo-api:0004]
- After: From 07:14 to 07:44:47 user 137 got about 235 201s and 77-94 429s a minute. The request rate was about 328 a minute and 429s ran at 406-458 per 5 minutes. [logagg:fivo-api:0004, db:transactions:0003, metric:fivo-api:0005, logagg:fivo-api:0002]
- Why it did not fail earlier: The global 240 rpm limiter was already in place. The only deploy (07:06) touched a single prompt string in /insights and not the limiter (F11, F12), and no config change is recorded (F5). Normal traffic of about 9 requests a minute never approached the cap. Only the 07:14 flood did. The 07:10 onset in triage comes from 5-minute bucketing: the 07:10 bucket's 72.8 requests a minute and 92 429s are consistent with one flood minute at 07:14 (inferred).

Changes before the incident, and what was concluded about each:

- **ruled out** — deploy on fivo-api: commit e951293 "insights: ask for one saving tip as well" by github-actions (2026-11-11T07:06:00Z, 4 min before onset): dep-0414 changes one prompt string in the GET /insights handler and does not touch /transactions, the limiter, middleware or Celery. The only restart was at 07:05, before any 429s. The flood began at 07:14 from one external client, with a global limiter that was already in place. [deploy:fivo-api:0001, changes:fivo-api:0001, diff:fivo-api:0001, metric:fivo-api:0003, logagg:fivo-api:0004]

## Hypotheses, ranked

1. **A single scripted client (user 137) flooding POST /transactions exhausts fivo-api's global in-memory rate limit, so 429s are returned to it and to other users.** (fivo-api, traffic_change) — most_likely, confidence 0.85
   - Supports: User 137 accounts for 7216 of about 7300 POST /transactions 201s and 2630 of about 2673 429s (lower bounds, partial result). [logagg:fivo-api:0003]
   - Supports: The flood runs at about 235 201s a minute from 07:14, with 7216 rows in the DB from 07:14:00 to 07:44:47. All have distinct idempotency keys and come from one device, so this is a scripted bulk write, not a retry loop. [logagg:fivo-api:0004, db:transactions:0003, db:transactions:0002]
   - Supports: The limiter is a global per-instance fixed-window counter in memory. The code default is 240 rpm, with no key function and no per-route limit. The 429 body is 'rate limit exceeded'. [code:fivo-api:0001, codehit:fivo-api:0001]
   - Supports: All 2707 429s come from host fivo-api-1 with the message 'rate limit exceeded' and a duration of 1 ms, so no downstream call was involved. [logagg:fivo-api:0006, log:fivo-api:0002, log:fivo-api:0003, log:fivo-api:0004]
   - Supports: Request rate rose from about 9 a minute to about 328 a minute. In the 07:05-07:20 window, 522 of 535 429s belong to user 137 and the rest are single 429s for other users. [metric:fivo-api:0005, logagg:fivo-api:0005]
   - Supports: /transactions 201s rose from 135 to 7296, about 54x, with no 429s in the baseline. [logagg:fivo-api:0001]
   - Contradicts: Earlier per-user framing of the limiter is contradicted by the code: it is global, not per-user. This changes how the impact on other users is explained, not the cause. [code:fivo-api:0001]
   - Contradicts: No 429 lines appear in the 07:10-07:11 window, so the 07:10 triage onset is a bucketing artefact. The 07:10 bucket (92 429s) is consistent with the 07:14 flood minute (inferred). [log:fivo-api:0001]
2. **Deploy dep-0414 (e951293, 07:06) introduced a regression that caused the 429s.** (fivo-api, code_regression) — ruled_out, confidence 0.01
   - Supports: It is the only deploy, at 07:06, shortly before the incident. [deploy:fivo-api:0001]
   - Contradicts: The diff changes one file (app/routes/insights.py), one line of the GET /insights prompt text. It does not touch /transactions, the limiter, middleware or Celery. [changes:fivo-api:0001, diff:fivo-api:0001]
   - Contradicts: The flood began at 07:14, 8 minutes after the deploy, from one external client. [logagg:fivo-api:0004, logagg:fivo-api:0003]
3. **A fivo-worker/Celery retry loop or an Upstash Redis problem (such as the daily quota) caused the 429s.** (upstash-redis, dependency_failure) — ruled_out, confidence 0.02
   - Supports: Redis commands and worker log volume rose from about 07:10, and the worker logged 78 groq 429 retry warnings. These are downstream of the write flood or a separate provider (inferred). [metric:upstash-redis:0001, logagg:fivo-worker:0002, logagg:fivo-worker:0003]
   - Contradicts: Redis used 217597 of 500000 daily commands by 07:40, about 44%. [metric:upstash-redis:0002, metric:upstash-redis:0003]
   - Contradicts: Worker queue depth and task failure rate were 0 throughout. [metric:fivo-worker:0001, metric:fivo-worker:0002]
   - Contradicts: The 429s come from fivo-api itself at about 1 ms, and the limiter is in-process, not Redis-backed. [logagg:fivo-api:0006, code:fivo-api:0001]
4. **The single 512 MB fivo-api instance is degraded (memory pressure or restart) and is shedding load with 429s.** (fivo-api, resource_exhaustion) — ruled_out, confidence 0.01
   - Supports: CPU rose to about 97% from 07:10, but this follows the request-rate surge (a consequence). [metric:fivo-api:0002, metric:fivo-api:0005]
   - Contradicts: Memory peaked at 273 MB of 512 MB. [metric:fivo-api:0001]
   - Contradicts: The only restart was in the 07:05 bucket, before any 429s. [metric:fivo-api:0003]
   - Contradicts: p95 latency was flat at about 830-983 ms across the window. [metric:fivo-api:0004]

## Facts

- fivo-api returned 429 on 2707 of 10342 requests in 06:55-07:45. 2673 of them were on /transactions. There were no 429s in the 06:00-06:50 baseline. [logagg:fivo-api:0001, logagg:fivo-api:0002]
- 429s ran at a sustained 406-458 per 5 minutes from 07:15 to 07:40, with no recovery by the end of the window. [logagg:fivo-api:0002]
- User 137 (python-requests/2.32) sent 7216 201s and 2630 429s on POST /transactions (lower bounds, partial result). [logagg:fivo-api:0003]
- The flood is about 235 201s a minute from 07:14. [logagg:fivo-api:0004]
- User 137 wrote 7216 source=api rows from one device between 07:14:00 and 07:44:47. The rows have distinct idempotency keys and distinct merchant/amount/created_at values. [db:transactions:0003]
- Every other user and source group wrote 7 rows or fewer. [db:transactions:0003, db:transactions:0001]
- RateLimitMiddleware is a custom global fixed 60-second in-memory counter per instance, with default RATE_LIMIT_RPM 240 and no key function. It applies to all routes except /health and returns 429 'rate limit exceeded'. [code:fivo-api:0001, codehit:fivo-api:0001]
- The 429s are logged by fivo-api-1 with the message 'rate limit exceeded' and a duration of 1 ms. [logagg:fivo-api:0006, log:fivo-api:0002]
- Request rate rose from about 9 a minute to about 328 a minute at about 07:15, and CPU rose from about 17% to about 97% at 07:10. [metric:fivo-api:0005, metric:fivo-api:0002]
- The only deploy, dep-0414 at 07:06, changed one line in app/routes/insights.py. No config changes were recorded. [deploy:fivo-api:0001, changes:fivo-api:0001, diff:fivo-api:0001, config:config_changes:0001]
- Redis was at about 44% of its daily quota, worker queue depth and task failure rate were 0, memory peaked at 273 MB, and p95 latency was flat. [metric:upstash-redis:0002, metric:fivo-worker:0001, metric:fivo-worker:0002, metric:fivo-api:0001, metric:fivo-api:0004]
- Non-137 429s are few: single 429s for about 12 other users on /transactions, /summary and /receipts in 07:05-07:20. [logagg:fivo-api:0005]

## Assumptions

- The deployed RATE_LIMIT_RPM is 240 (the code default); the env value was not checked. The 235 accepted writes a minute plus a few other requests are consistent with it.
- The 07:10 triage onset is a 5-minute bucketing artefact, with the 92 429s in that bucket coming from the 07:14 flood minute (inferred).
- The Redis and worker volume rises are downstream of the write flood (inferred).
- A client-side release was not examined and is not visible from this repo.
- The Retry-After value of 30 in the limiter code was not seen in the logs.

## Recommended actions

- [immediate] Identify user 137 (device cli-7f3a, python-requests/2.32) and stop or throttle the script. If it is a legitimate import or integration, have it slow down to well under 240 requests a minute. If it is abuse, block the account or key. — The flood of about 235 writes a minute fills the instance-wide budget and causes the 429s for everyone. [logagg:fivo-api:0003, logagg:fivo-api:0004, db:transactions:0003]
- [immediate] Check the deployed RATE_LIMIT_RPM value on fivo-api. Raise it only as a stopgap if capacity allows, since CPU is already at about 97%. — 240 is only the code default, and the limit is global per instance. [codehit:fivo-api:0001, metric:fivo-api:0002]
- [soon] Replace the global counter with a per-user or per-key limiter (preferably shared across instances, for example Redis-backed), with a stricter limit on POST /transactions. — One client can currently use the whole instance budget and starve other users. The state is in-process and resets on restart. [code:fivo-api:0001]
- [soon] Find out why user 137 started at 07:14 (import job, integration, bot or client release) and give bulk writers a proper batch endpoint. — The origin of the flood is unknown. [logagg:fivo-api:0003]
- [later] Add alerts on 429 rate and on per-user request concentration. — This would have caught the single-client flood sooner. [logagg:fivo-api:0002, logagg:fivo-api:0005]

## Missing evidence

- The deployed RATE_LIMIT_RPM env value was not verified.
- Who or what user 137 is, and why it started at 07:14, is unknown (S13, the users table lookup, was skipped).
- Per-user counts are lower bounds because the log aggregation was partial and capped.
- No trace spans exist for the 429 requests, so the 1 ms duration in the logs is the only sign that they involved no downstream call.
- The worker warning grouping was partial (50 of 78 seen).
- A client-side release that might have started the flood was not checked.
- The baseline 'no source=api rows' claim rests on a top-10 query, so small api groups are not strictly excluded.

## Run

- system: multi_agent
- termination: accepted
- rounds: 2
- hypotheses: 4
- steps: 10
- max hops: 4
- tool calls: 42
- tool failures: 0
- guardrail rejections: 1
- llm calls: 38
- total tokens: 497058
- cost usd: 1.0127
- latency s: 298.9
- citations: 80 checked, 0 invalid, 0 facts demoted to assumptions
