# Incident report: fivo-api: GET /insights 5xx above 30%

**Status:** conclusive

At 08:57:00Z the env var ANTHROPIC_TIMEOUT_S on fivo-api was lowered from 20 to 2 seconds (cfg-0139). Normal Anthropic insight calls take about 1–3.5 s, with occasional outliers near 8 s, so a large share of calls now exceed 2 s and are cut off as UpstreamTimeout, which returns 504 on GET /insights. The first 504 was at 08:58:22Z, with none before 08:58Z. The 09:00Z deploy (371cb6a) is a behaviour-neutral refactor and is not the cause. Immediate action: revert ANTHROPIC_TIMEOUT_S to 20 or another value comfortably above p99 Anthropic latency.

## Root cause

- Service: fivo-api
- Component: ANTHROPIC_TIMEOUT_S env var (cfg-0139), used as anthropic_timeout_s in app/config.py
- Category: config_change

ANTHROPIC_TIMEOUT_S was cut from 20 s to 2 s at 08:57Z. Insight generation via Anthropic normally takes about 1–3.5 s and sometimes more, so many calls now hit the 2 s client timeout and raise UpstreamTimeout. GET /insights then returns 504 for roughly half of requests while other routes stay healthy.

## What changed (why now)

The ANTHROPIC_TIMEOUT_S env var on fivo-api was lowered from 20 s to 2 s (cfg-0139). (at 2026-11-11T08:57:00Z)

- Before: With a 20 s timeout, /insights returned 200 in about 0.9–3.5 s (one at 7.9 s), with zero 504s between 08:00 and 08:58Z. [logagg:fivo-api:0003, log:fivo-api:0020, log:fivo-api:0025, log:fivo-api:0001]
- After: Anthropic calls over 2 s fail with 'UpstreamTimeout ... after 2.0s' and /insights returns 504 for about half of requests from 08:58:22Z. [log:fivo-api:0011, logagg:fivo-api:0002]

Changes before the incident, and what was concluded about each:

- **cause** — fivo-api env var ANTHROPIC_TIMEOUT_S 20 -> 2 (cfg-0139, by dashboard:dev) (2026-11-11T08:57:00Z): The timeout error message shows the 2.0 s limit, and 504s begin about 80 s after the change, with none before. Normal call latency of 1–3.5 s straddles the new limit. [config:fivo-api:0001, log:fivo-api:0011, logagg:fivo-api:0003]
- **ruled out** — fivo-api deploy dep-0414 sha 371cb6a (prev 7bc71c8): insights prompt restructure (2026-11-11T09:00:00Z): The diff is a behaviour-neutral refactor with max_tokens unchanged at 400. The 504s started at 08:58:22Z, before the deploy, and the error is the 2.0 s timeout from the env var. [diff:fivo-api:0001, deploy:fivo-api:0001, log:fivo-api:0011]

## Hypotheses, ranked

1. **The ANTHROPIC_TIMEOUT_S reduction from 20 to 2 s truncates normal-latency Anthropic calls and produces 504s.** (fivo-api, config_change) — most_likely, confidence 0.93
   - Supports: The env var change was made at 08:57:00Z, 20 → 2, note 'fail fast'. [config:fivo-api:0001]
   - Supports: Errors read 'anthropic request timed out after 2.0s', with duration of about 2021–2025 ms, and the first is at 08:58:22Z. [log:fivo-api:0011, log:fivo-api:0014, log:fivo-api:0015, log:fivo-api:0017, log:fivo-api:0018]
   - Supports: There were zero 504s on /insights from 08:00 to 08:58Z. [logagg:fivo-api:0003]
   - Supports: Before the change, successful /insights calls routinely took 1–3.5 s, and one took 7.9 s. Many of these would exceed a 2 s limit. [log:fivo-api:0020, log:fivo-api:0024, log:fivo-api:0025, log:fivo-api:0027, log:fivo-api:0037, log:fivo-api:0001]
   - Supports: After the change, 504s and 200s are mixed in every 5-minute bucket (about 3–6 of 7–11 requests are 504). [logagg:fivo-api:0002]
   - Supports: The code default is 20.0 s, so the 2 s value comes only from the env override. [codehit:fivo-api:0001]
   - Contradicts: Successful requests continued after the change, but they were all under 2 s, which is consistent with a threshold effect rather than contradicting it. [log:fivo-api:0012, log:fivo-api:0013, log:fivo-api:0016]
2. **Deploy dep-0414 (371cb6a) changed prompt building or the request and caused the failures.** (fivo-api, code_regression) — ruled_out, confidence 0.05
   - Supports: The deploy landed at 09:00Z, close to the onset, and its message says 'tune request'. [deploy:fivo-api:0001]
   - Contradicts: The diff only moves prompt construction into _build_prompt and makes MAX_TOKENS = 400, the same value as before. Prompt content and max_tokens are unchanged. [diff:fivo-api:0001]
   - Contradicts: Timeouts began at 08:58:22Z, before the deploy. [log:fivo-api:0011, log:fivo-api:0014]
3. **Anthropic upstream degradation or slowness caused the timeouts.** (anthropic-llm, dependency_failure) — ruled_out, confidence 0.05
   - Contradicts: Successful calls before the change had similar 1–3.5 s latency and no 504s, and the failures begin exactly after the timeout was lowered. Latency did not visibly shift, though I did not query Anthropic-side metrics. [logagg:fivo-api:0003, log:fivo-api:0020, log:fivo-api:0025]
4. **Resource exhaustion or DB or network problems on fivo-api.** (fivo-api, resource_exhaustion) — ruled_out, confidence 0.03
   - Supports: There were two 500 ConnectionResetError events on /insights at 08:09 and 08:11Z. [logagg:fivo-api:0004, log:fivo-api:0031]
   - Contradicts: Those two 500s were fast (about 21–25 ms), occurred before the incident, and are a different error from the 504 timeouts. Other routes are healthy. [log:fivo-api:0031, log:fivo-api:0035]

## Facts

- ANTHROPIC_TIMEOUT_S on fivo-api was changed from 20 to 2 at 08:57:00Z by dashboard:dev. [config:fivo-api:0001]
- No /insights 504s occurred between 08:00 and 08:58Z. [logagg:fivo-api:0003]
- 504s on /insights began at 08:58:22Z with 'UpstreamTimeout: anthropic request timed out after 2.0s' and recurred in every 5-minute bucket through 09:30Z. [log:fivo-api:0011, logagg:fivo-api:0002]
- Baseline successful /insights requests took roughly 0.9–3.5 s, with one at 7.9 s. [log:fivo-api:0020, log:fivo-api:0025, log:fivo-api:0001]
- Deploy dep-0414 at 09:00Z only refactored prompt construction. MAX_TOKENS stays 400 and the prompt text is unchanged. [diff:fivo-api:0001, deploy:fivo-api:0001]
- The code default for the timeout is 20.0 s. [codehit:fivo-api:0001]

## Assumptions

- Anthropic call latency is about 1–3.5 s (the request duration is inferred from the /insights request duration, which includes DB work), so a 2 s timeout will truncate a large share of calls.
- The env var change took effect on the running process at or shortly after 08:57Z (the first timeout is 08:58:22Z); the restart or reload was not directly observed.
- Anthropic's own latency did not change; its provider-side metrics were not checked.

## Recommended actions

- [immediate] Revert ANTHROPIC_TIMEOUT_S on fivo-api to 20, or to a value above p99 Anthropic latency (for example 10–15 s), and restart or reload the service. — This removes the trigger. The error is a client-side timeout at 2.0 s on calls that normally take 1–3.5 s. [config:fivo-api:0001, log:fivo-api:0011]
- [immediate] Confirm recovery: /insights 504 count should drop to zero and p95 latency should return to the 08:00–08:55Z baseline. — Verifies the mechanism. [logagg:fivo-api:0002]
- [soon] Review timeout changes before applying them: derive them from measured upstream latency percentiles, and consider a separate, shorter timeout with a cached or fallback insight response if fail-fast behaviour is wanted. — The change was made from the dashboard without a latency check, and the error returned to users is a hard 504. [config:fivo-api:0001]
- [later] Alert on the 504 rate for /insights and add Anthropic call latency metrics to fivo-api. — No client-side latency metric for the Anthropic dependency was used in this investigation.

## Missing evidence

- No Anthropic-call-level latency histogram was available, so the share of calls over 2 s was inferred from request durations.
- I did not check platform logs confirming when the env var change took effect (restart or reload).
- The log counts are partial or capped in places, so counts are lower bounds. The 5-minute /insights counts were complete (status ok).

## Run

- system: single_agent
- termination: agent finished
- tool calls: 12
- tool failures: 0
- guardrail rejections: 0
- llm calls: 5
- total tokens: 92995
- cost usd: 0.1349
- latency s: 43.7
- citations: 55 checked, 0 invalid, 0 facts demoted to assumptions
