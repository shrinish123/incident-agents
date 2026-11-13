# Incident report: fivo-api: 429 responses above 20% of requests

**Status:** conclusive

One client, user 137 on device cli-7f3a, started posting about 320 transactions a minute to POST /transactions at about 07:10Z. fivo-api's rate limiter is a single fixed one-minute window of 240 requests per minute (RATE_LIMIT_RPM) shared by every user on the instance. That traffic filled the window, so the limiter returned 429 to everyone else too. The 07:06 deploy only changed an insights prompt string and no config changed, so neither is the cause. Immediate action is to block or throttle user 137 and the cli-7f3a client. Longer term, the limiter needs to be per-user.

## Root cause

- Service: fivo-api
- Component: RateLimitMiddleware (app/middleware/ratelimit.py), a global fixed-window limiter with RATE_LIMIT_RPM=240, triggered by user 137's cli-7f3a bulk POST /transactions traffic
- Category: traffic_change

User 137's device cli-7f3a (source=api) began a sustained burst of POST /transactions at about 07:10Z, around 1,600 requests per 5 minutes, which is about 320 per minute. The middleware counts all requests from all users in one per-instance window of 240 per minute and returns 429 once it is exceeded. The burst therefore consumed the whole budget, and about 25% of all requests, including other users' requests on other routes, got 429.

## What changed (why now)

A new high-volume client (user 137, device cli-7f3a, source=api) began a sustained burst of POST /transactions at about 07:10Z. It exhausted the instance-wide 240 rpm limit. (at 2026-11-11T07:10:00Z)

- Before: Traffic was about 45 to 80 requests per 5 minutes with no 429s, well under the 240 rpm global limit. [logagg:fivo-api:0001]
- After: User 137 sent about 1,600 requests per 5 minutes, and 429s ran at roughly 400 to 460 per 5 minutes for all users. [logagg:fivo-api:0004, logagg:fivo-api:0001, db:transactions:0001]
- Why it did not fail earlier: The global-limit design and the 240 rpm value were already in place and only matter once one client exceeds the budget. No earlier client sent that volume. I did not look at the limiter's history, so I cannot say how long the design has been this way.

Changes before the incident, and what was concluded about each:

- **ruled out** — fivo-api deploy dep-0414, sha e951293b (prev 7bc71c84): insights prompt now also asks for a saving tip (2026-11-11T07:06:00Z): Only app/routes/insights.py changed, by one prompt string. It does not touch the rate limiter or /transactions, and the 429 volume comes from one user posting to /transactions. [deploy:fivo-api:0001, changes:fivo-api:0001, diff:fivo-api:0001, logagg:fivo-api:0003]
- **ruled out** — Configuration, env var, feature flag and infrastructure changes (all services) (2026-11-10T06:00:00Z): No config changes were recorded between 2026-11-10T06:00Z and 2026-11-11T08:00Z. [config:config_changes:0001]

## Hypotheses, ranked

1. **A single user's high-rate scripted POST /transactions traffic exhausted the global (not per-user) rate limit, so all users received 429.** (fivo-api, traffic_change) — most_likely, confidence 0.93
   - Supports: User 137 accounts for 3736 of the 201s and 1347 of the 429s on POST /transactions between 07:08 and 07:30. Volume for this user jumps from 1 request in the 07:05 bucket to about 1,600 per 5 minutes from 07:15. [logagg:fivo-api:0003, logagg:fivo-api:0004]
   - Supports: The DB shows 7216 transactions from device cli-7f3a with source=api. All have unique idempotency keys and client_seq values, so this is not a retry loop. The merchants are synthetic-looking, spread evenly over 22 values. [db:transactions:0001, db:transactions:0002]
   - Supports: The limiter is one fixed 60-second window over all requests to the instance, at a default of 240 rpm, and it returns 429 with Retry-After: 30. Only /health is exempt. [code:fivo-api:0001, codehit:fivo-api:0002]
   - Supports: Other users and other routes (/summary, GET /transactions, /receipts) also received 429s, which fits a shared global counter. [logagg:fivo-api:0002, logagg:fivo-api:0003]
   - Supports: Normal traffic before 07:10 was about 45 to 80 requests per 5 minutes with no 429s. From 07:10, 429s were 92, then 443, 406, 445 and 458 per 5-minute bucket. [logagg:fivo-api:0001]
   - Contradicts: Both the 5-minute counts and the 07:10 to 07:20 route counts are partial or capped, so they are lower bounds. [logagg:fivo-api:0001]
2. **The 07:06 deploy (e951293) caused the 429s.** (fivo-api, code_regression) — ruled_out, confidence 0.03
   - Supports: The deploy was 4 minutes before onset. [deploy:fivo-api:0001]
   - Contradicts: The deploy changed one string in the /insights prompt and nothing else. It does not touch the rate limiter or /transactions. [changes:fivo-api:0001, diff:fivo-api:0001]
   - Contradicts: The excess load comes from one client on /transactions, not /insights. [logagg:fivo-api:0002, logagg:fivo-api:0003]
3. **A config change, such as a lowered RATE_LIMIT_RPM, caused the 429s.** (fivo-api, config_change) — ruled_out, confidence 0.03
   - Contradicts: No config changes were recorded in the 26 hours before the incident. [config:config_changes:0001]
4. **A downstream dependency failure (Redis, worker or DB) caused the 429s.** (fivo-api, dependency_failure) — ruled_out, confidence 0.02
   - Contradicts: The 429s are produced by the in-process middleware. Almost all requests that got through returned 201, with only one 500 seen. I did not check dependency metrics directly. [codehit:fivo-api:0001, logagg:fivo-api:0001]

## Facts

- 429s on fivo-api began in the 07:10 bucket (92) and stayed at roughly 400 to 460 per 5 minutes. 201s rose from about 10 to about 1,170 per 5 minutes in the same period. [logagg:fivo-api:0001]
- Between 07:10 and 07:20, POST /transactions had 1416 201s and 527 429s. Other routes got a few 429s. [logagg:fivo-api:0002]
- User 137 is a free-plan user with 2 devices. [db:users:0001]
- User 137's device cli-7f3a created 7216 transactions with source=api, from 07:14:00 to 07:44:47. The same user's normal ios-393d activity was 5 transactions. [db:transactions:0001]
- The rate limiter is a global fixed window of 240 requests per minute for the whole instance. [code:fivo-api:0001, codehit:fivo-api:0002]
- The only deploy in the window was dep-0414 at 07:06, an insights prompt change. No config changes were found. [deploy:fivo-api:0001, changes:fivo-api:0001, diff:fivo-api:0001, config:config_changes:0001]

## Assumptions

- cli-7f3a is a script or bulk-import client (inferred from unique keys, evenly spread synthetic merchants and a steady rate). I did not confirm who runs it or whether it is authorized.
- The 429s on other users' requests come from the shared counter, since the limiter has no per-user key. This is read from the code, not measured per user.
- The roughly 25% 429 share is calculated from the partial 5-minute counts, so it is approximate.
- The burst is still running as of 07:44:47, so I assume the incident is ongoing.

## Recommended actions

- [immediate] Block or throttle user 137 and device cli-7f3a, for example revoke its API token, and contact the owner to find out what the client is doing. — This single client is consuming the whole instance-wide rate budget. [logagg:fivo-api:0003, db:transactions:0001]
- [soon] Replace the global fixed-window limiter with per-user or per-IP limits. Keep a separate, higher global ceiling for overload protection only. — One noisy tenant should not cause 429s for everyone. Today any client sending more than 240 requests a minute locks out all users. [code:fivo-api:0001]
- [soon] Review the roughly 7,200 transactions from cli-7f3a and delete or flag them if they are junk. — The rows are in user 137's data and will skew that user's summaries and insights. [db:transactions:0001, db:transactions:0002]
- [later] Add alerting on per-user request share and on the 429 rate. Consider a limiter with a shared store, since the current counter is per process. — This would have shown the source of the burst earlier. [logagg:fivo-api:0001]

## Missing evidence

- fivo-api metrics were not queried, so I did not check CPU, memory or latency on the 512 MB instance.
- The identity and purpose of the cli-7f3a client (for example a user-agent or token) is not in the data I looked at.
- The 429 log lines could not be fetched for 07:10 to 07:12 (log:fivo-api:0001 returned no matches), and the 5-minute counts are partial. The exact first-request time of the burst is therefore approximate, and the first transaction for cli-7f3a is at 07:14.
- I did not check upstash-redis, neon-postgres or fivo-worker health directly.

## Run

- system: single_agent
- termination: agent finished
- tool calls: 18
- tool failures: 0
- guardrail rejections: 0
- llm calls: 7
- total tokens: 124812
- cost usd: 0.1318
- latency s: 58.9
- citations: 43 checked, 0 invalid, 0 facts demoted to assumptions
