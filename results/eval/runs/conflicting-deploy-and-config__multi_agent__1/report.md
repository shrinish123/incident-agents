# Incident report: fivo-api: GET /insights 5xx above 30%

**Status:** conclusive

At 08:57:00Z cfg-0139 cut ANTHROPIC_TIMEOUT_S on fivo-api from 20 to 2 (dashboard:dev). GET /insights makes one Anthropic call with max_retries=0, and a timeout is raised as UpstreamTimeout and mapped to HTTP 504. Calls that used to take 2-9 s now fail at about 2.02 s: 29 of 101 /insights requests returned 504 in 08:42-09:32Z, the first at 08:58:22Z. Restore ANTHROPIC_TIMEOUT_S to 20 (or a value above the normal 2-9 s generation time). Deploy dep-0414 at 09:00Z is a no-op refactor and is ruled out.

## Root cause

- Service: fivo-api
- Component: ANTHROPIC_TIMEOUT_S env var (cfg-0139), used by the Anthropic client in app/services/anthropic_client.py
- Category: config_change

cfg-0139 lowered ANTHROPIC_TIMEOUT_S from 20 to 2 at 08:57:00Z. The client has max_retries=0 and the timeout is mapped to 504 with no fallback. Normal /insights generations take 2-9 s, so the slow ones now fail with 'UpstreamTimeout: anthropic request timed out after 2.0s'.

## What changed (why now)

ANTHROPIC_TIMEOUT_S on fivo-api was reduced from 20 to 2 (cfg-0139). (at 2026-11-11T08:57:00Z)

- Before: With a 20 s limit, slow Anthropic calls completed. In the 08:42–08:57Z sample 13 of 29 returned 200s were over 2 s (2.1–9.4 s), and the baseline had zero /insights 504s. [log:fivo-api:0036, log:fivo-api:0052, logagg:fivo-api:0001]
- After: From 08:57:50Z the sampled 200s are all at or under about 2.02 s, and 504s begin at 08:58:22Z with 2.02 s UpstreamTimeout errors. There were 29 504s in the window. [log:fivo-api:0069, log:fivo-api:0070, log:fivo-api:0001, logagg:fivo-api:0002]

Changes before the incident, and what was concluded about each:

- **cause** — config on fivo-api: ANTHROPIC_TIMEOUT_S: 20 -> 2 by dashboard:dev ("fail fast instead of holding a request for 20 s") (2026-11-11T08:57:00Z, 2 min after onset): ANTHROPIC_TIMEOUT_S went from 20 to 2 at 08:57:00Z. Every 504 is an anthropic timeout at about 2.0 s, the first at 08:58:22Z. Before the change, slow calls of 2–9 s succeeded. The code maps the timeout to 504 with no retry or fallback. [config:fivo-api:0001, log:fivo-api:0001, log:fivo-api:0036, log:fivo-api:0052, code:fivo-api:0001, code:fivo-api:0002]
- **ruled out** — deploy on fivo-api: commit 371cb6a "insights: restructure prompt building, tune request" by github-actions (2026-11-11T09:00:00Z, 5 min after onset): dep-0414 changes only app/routes/insights.py as a refactor with an identical prompt, max_tokens=400 and the same single LLM call. The first 504 at 08:58:22Z precedes the 09:00:00Z deploy, so it cannot be the trigger. [deploy:fivo-api:0001, diff:fivo-api:0001, changes:fivo-api:0001, log:fivo-api:0001]

## Hypotheses, ranked

1. **The ANTHROPIC_TIMEOUT_S cut from 20 to 2 (cfg-0139, 08:57Z) makes slower Anthropic calls in GET /insights time out and return 504.** (fivo-api, config_change) — most_likely, confidence 0.93
   - Supports: Config change from 20 to 2 at 08:57:00Z; the code default is 20, so the env value overrides it. [config:fivo-api:0001, codehit:fivo-api:0001]
   - Supports: First /insights 504 is at 08:58:22Z, after the change. Every sampled 504 is an anthropic timeout after 2.0 s, at 2021-2026 ms. [log:fivo-api:0001, log:fivo-api:0002, log:fivo-api:0003, log:fivo-api:0004, log:fivo-api:0005, log:fivo-api:0006]
   - Supports: Code path: client built with the timeout and max_retries=0; the timeout becomes UpstreamTimeout and is mapped to 504; /insights calls complete(). [code:fivo-api:0001, code:fivo-api:0002, codehit:fivo-api:0003]
   - Supports: Before the change, 13 of 29 sampled 200s took over 2 s, up to 9.4 s. After about 08:57:50Z sampled 200s are all at or under about 2.02 s. [log:fivo-api:0036, log:fivo-api:0052, log:fivo-api:0069, log:fivo-api:0070, log:fivo-api:0058, log:fivo-api:0061]
   - Supports: /insights 504 is new (29 in the window, 0 in the baseline). 200 volume fell, which fits slow requests becoming 504s. [logagg:fivo-api:0001, logagg:fivo-api:0006, logagg:fivo-api:0007]
   - Contradicts: The restart that applied the new env value was not directly observed. It is inferred from the 08:57:15Z to 08:57:50Z transition, since two 200s over 2 s (2095 and 4182 ms) came just after the change. [log:fivo-api:0067, log:fivo-api:0068]
2. **Deploy dep-0414 (371cb6a, 'insights: restructure prompt building, tune request') introduced a regression in /insights.** (fivo-api, code_regression) — ruled_out, confidence 0.02
   - Supports: The commit touches the insights route and its message says 'tune request'. [deploy:fivo-api:0001]
   - Contradicts: The diff is +7 -4 in app/routes/insights.py only. The prompt is identical and max_tokens stays 400. [changes:fivo-api:0001, diff:fivo-api:0001]
   - Contradicts: The first 504 (08:58:22Z) is before the 09:00:00Z deploy. [log:fivo-api:0001, deploy:fivo-api:0001]
3. **Anthropic API latency or errors rose independently from about 08:55Z.** (anthropic-llm, dependency_failure) — ruled_out, confidence 0.03
   - Contradicts: No 504 before 08:58 at minute resolution. Every failure ends at about 2.0 s, which matches our own timeout and not a stall. [logagg:fivo-api:0004, log:fivo-api:0001, log:fivo-api:0002, log:fivo-api:0003]
   - Contradicts: Before the change, calls of up to 9.4 s returned 200, so Anthropic was answering. [log:fivo-api:0036, log:fivo-api:0052]
4. **Database or cache slowness (neon-postgres, upstash-redis) or a resource limit on the 512 MB instance causes the 504s.** (neon-postgres, data_issue) — ruled_out, confidence 0.02
   - Contradicts: 504s are spread across about 25 users, and many of those users also got 200s. [logagg:fivo-api:0005]
   - Contradicts: All sampled 504 lines are the Anthropic UpstreamTimeout at 2.02-2.03 s, with no database or redis error. [log:fivo-api:0004, log:fivo-api:0005, log:fivo-api:0006, log:fivo-api:0011, log:fivo-api:0028]

## Facts

- cfg-0139 at 08:57:00Z changed ANTHROPIC_TIMEOUT_S on fivo-api from 20 to 2 (by dashboard:dev, note 'fail fast instead of holding a request for 20 s'). [config:fivo-api:0001]
- Deploy dep-0414 (371cb6a) went live at 09:00:00Z. [deploy:fivo-api:0001]
- In 08:42-09:32Z, /insights returned 29 x 504. The baseline had none, and other routes were healthy. [logagg:fivo-api:0001, logagg:fivo-api:0002]
- The first 504 is at 08:58:22Z with 'UpstreamTimeout: anthropic request timed out after 2.0s' at 2021 ms. Minute-level counts show nothing before 08:58. [log:fivo-api:0001, logagg:fivo-api:0004]
- The code reads the env var with a default of 20. The client uses max_retries=0 and the timeout maps to HTTP 504. [codehit:fivo-api:0001, code:fivo-api:0001, code:fivo-api:0002]
- The dep-0414 diff is a refactor of app/routes/insights.py with an identical prompt. [changes:fivo-api:0001, diff:fivo-api:0001]
- The 504s are spread across about 25 users (counts are lower bounds). [logagg:fivo-api:0005]
- No past incident matches 'insights 504'. [incident:incidents:0001]

## Assumptions

- The new env value reached the running process through a restart that was not directly observed. It is inferred from the import-time client and the 08:57:15Z to 08:57:50Z transition.
- The 200 durations after 09:18Z and before 08:42Z were not sampled, so the pattern is assumed to hold there.
- Changes in fivo-worker or other dependencies were not examined because the change list was not filtered by service.

## Recommended actions

- [immediate] Restore ANTHROPIC_TIMEOUT_S on fivo-api to 20 (revert cfg-0139) and make sure the process picks it up, restarting if needed. — The 2 s limit cuts off normal 2-9 s generations and returns 504. [config:fivo-api:0001, log:fivo-api:0036, log:fivo-api:0052]
- [immediate] After the revert, check that /insights 504s stop and that 200s over 2 s return. — This confirms the fix, given that the restart was not observed. [log:fivo-api:0001, logagg:fivo-api:0002]
- [soon] If fail-fast is wanted, pick a timeout above the observed latency (p95 of /insights generation), and add a fallback or cached insight instead of a 504. Consider retries. — The client has max_retries=0 and the timeout maps straight to 504 with no fallback. [code:fivo-api:0001, code:fivo-api:0002]
- [later] Review env var changes made from the dashboard, and alert on /insights 504 rate and on Anthropic timeouts. — The change was made without checking against real latency and was not caught for several minutes. [config:fivo-api:0001]

## Missing evidence

- Direct evidence of the process restart that applied the new env value.
- Samples of /insights 200 durations for 08:00-08:42Z and 09:20-09:30Z. logs_fetch is capped at 15 minutes, so these were only counted.
- Anthropic-side, neon-postgres, upstash-redis and fivo-worker metrics and logs were not queried.
- The change list was not filtered by service, so changes outside fivo-api were not examined.

## Run

- system: multi_agent
- termination: accepted
- rounds: 1
- hypotheses: 4
- steps: 8
- max hops: 3
- tool calls: 29
- tool failures: 0
- guardrail rejections: 3
- llm calls: 29
- total tokens: 335315
- cost usd: 0.6011
- latency s: 192.6
- citations: 77 checked, 0 invalid, 0 facts demoted to assumptions
