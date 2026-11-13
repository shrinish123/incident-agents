# Incident report: fivo-api: POST /voice/call failing above 25%

**Status:** conclusive

POST /voice/call began failing because Bolna, the outbound-voice provider, degraded. It returned 502s from about 13:19Z and then stopped answering within the 10s client timeout from about 13:41Z. fivo-api calls Bolna synchronously with no retry, so each upstream failure becomes a 502 or 504 for the caller. No deploys or config changes were recorded in the prior day, and fivo-api CPU, memory and other routes are healthy. The Bolna-side cause is not visible to us: no client-side Bolna metrics exist and traces were unavailable. Check Bolna's status page or contact them, and consider retry or a queued fallback on our side.

## Root cause

- Service: bolna-voice
- Component: Bolna outbound call API (POST {bolna_base_url}/call) called synchronously from app/services/bolna.py start_call, with a 10s timeout and no retry
- Category: dependency_failure

Bolna started returning 502 Bad Gateway at about 13:19Z and then timing out at 10s from about 13:41Z. fivo-api's /voice/call handler calls Bolna inline with no retry or fallback, so each upstream failure becomes a 502 or 504 to the caller and a 'failed' voice_calls row. The 10s timeouts account for the p95 latency jump on fivo-api.

## What changed (why now)

Bolna's call API began failing: 502s first, then 10s timeouts. Nothing changed in fivo-api itself. (at 2026-11-09T13:19:00Z)

- Before: Before 13:10Z /voice/call returned 202 at roughly 200-460ms, with no 502 or 504 and no upstream errors. [logagg:fivo-api:0002, log:fivo-api:0001]
- After: From about 13:19Z Bolna calls return 502, and from about 13:41Z they time out at 10s and return 504. p95 latency rises to several seconds. [log:fivo-api:0011, log:fivo-api:0022, metric:fivo-api:0001]
- Why it did not fail earlier: The fault is in an external dependency. The sync, no-retry design has been in place all along and only fails when Bolna does. The exact start of the Bolna degradation cannot be established because we have no Bolna-side data. A first 502 appeared in the 13:10-13:20 bucket.

Changes before the incident, and what was concluded about each:

- **ruled out** — fivo-api deploys in the 24h before onset (2026-11-08T12:00:00Z): None recorded in 2026-11-08T12:00Z to 2026-11-09T14:00Z. [deploy:deploys:0001]
- **ruled out** — Config, env var and feature flag changes in the 24h before onset (2026-11-08T12:00:00Z): None recorded in the same window. [config:config_changes:0001]

## Hypotheses, ranked

1. **Bolna provider degradation (5xx, then timeouts) is causing the /voice/call failures.** (bolna-voice, dependency_failure) — most_likely, confidence 0.85
   - Supports: Error logs carry upstream-originated 'bolna upstream error: 502 Bad Gateway' from 13:19:56Z, and 'bolna request timed out after 10s' with duration of about 10.01s from 13:41:02Z. [log:fivo-api:0011, log:fivo-api:0012, log:fivo-api:0022, log:fivo-api:0023]
   - Supports: The only error groups new since baseline are /voice/call 502 and 504. The 12:00-12:30 baseline had a single /voice/call 500. [logagg:fivo-api:0003]
   - Supports: The voice_calls table records 11 failed calls with a 502 error and 11 with a timeout between 13:00 and 14:00. 23 failed and 248 queued in the 12:00-14:00 window. [db:voice_calls:0001, db:voice_calls:0002]
   - Supports: The code calls Bolna synchronously inside the request with a 10s timeout, and the handler has no retry. Upstream failures therefore surface directly as 502/504. [code:fivo-api:0001, code:fivo-api:0002]
   - Supports: p95 latency departs its baseline at 13:35Z and shifts to a mean of about 5.9s at 13:50Z, consistent with 10s timeouts. [metric:fivo-api:0001]
   - Contradicts: There is no Bolna-side metric or status evidence, and traces were unavailable, so the upstream fault is inferred from our client-side errors only. [topology:bolna-voice:0001]
2. **A code or config change in fivo-api caused the failures.** (fivo-api, code_regression) — ruled_out, confidence 0.04
   - Contradicts: No deploys in the 24h before the incident. [deploy:deploys:0001]
   - Contradicts: No config changes in the 24h before the incident. [config:config_changes:0001]
   - Contradicts: The failures are all upstream-generated messages, and no other route has errors. [logagg:fivo-api:0003]
3. **Local resource exhaustion on the 512MB fivo-api instance.** (fivo-api, resource_exhaustion) — ruled_out, confidence 0.03
   - Contradicts: Memory is flat at about 200-208 MB through the window. [metric:fivo-api:0002]
   - Contradicts: Other routes stay healthy during the incident. [logagg:fivo-api:0003]
4. **Bad request data (the 422 validation errors) is behind the failures.** (fivo-api, data_issue) — ruled_out, confidence 0.03
   - Contradicts: The 422s are fast (4ms) and are not 502/504. The 502/504 failures are upstream-generated. [log:fivo-api:0013, log:fivo-api:0011]

## Facts

- POST /voice/call returned 202 almost exclusively until 13:10Z. 502s appear from 13:10-13:20Z, and 502 plus 504 grow through 13:30-14:00Z. [logagg:fivo-api:0002]
- Bolna 502 errors are logged at 13:19:56Z and 13:21:55Z with durations of about 1s. [log:fivo-api:0011, log:fivo-api:0012]
- From 13:41Z Bolna calls time out at the 10s limit and return 504. [log:fivo-api:0022, log:fivo-api:0024]
- The Bolna timeout setting is 10.0s by default (BOLNA_TIMEOUT_S). [codehit:fivo-api:0001]
- Failed calls are persisted with the upstream error text. [db:voice_calls:0001]
- No deploys or config changes are recorded for the 24h before the incident. [deploy:deploys:0001, config:config_changes:0001]
- No past incident mentions bolna. [incident:incidents:0001]

## Assumptions

- The 502s are returned by Bolna or its gateway, not by a local proxy. The message text says upstream, but we have no Bolna-side confirmation.
- The Bolna degradation is external and not caused by anything we send, such as payload content or rate limiting. We have no evidence of request volume or rate-limit responses from Bolna.

## Recommended actions

- [immediate] Check Bolna's status page and open a support ticket with sample failing trace IDs (e.g. 1369f104b1b17402, e66a5918301db8bf). — The fault is on the provider side and we cannot see it ourselves. [log:fivo-api:0011, log:fivo-api:0022]
- [soon] Add bounded retry with backoff, and queue the call through fivo-worker so the request doesn't wait up to 10s. Consider a shorter timeout. — The synchronous call with no retry turns every transient upstream fault into a user-facing 502 or 504 and holds a request for 10s. [code:fivo-api:0002, code:fivo-api:0001]
- [soon] Re-attempt or notify for voice_calls rows with status='failed' once Bolna recovers. — Failed reminder calls were never placed. [db:voice_calls:0002]
- [later] Add client-side metrics and alerting for Bolna call outcomes and latency. — No Bolna client metrics exist, so we have no direct visibility into the dependency. [topology:bolna-voice:0001]

## Missing evidence

- Traces for the period are unavailable (trace store ingest backlog), so per-call spans for Bolna could not be inspected.
- No Bolna-side metrics or status data, so the cause of the provider degradation is unknown.
- The log counts were partial, so the exact failure ratio was not computed. The ratio exceeded 25% in the 13:40-14:00 buckets, where there were 10 successes against 7 failures, then 11 against 6.

## Run

- system: single_agent
- termination: agent finished
- tool calls: 21
- tool failures: 2
- guardrail rejections: 1
- llm calls: 8
- total tokens: 177063
- cost usd: 0.1344
- latency s: 333.0
- citations: 41 checked, 0 invalid, 0 facts demoted to assumptions
