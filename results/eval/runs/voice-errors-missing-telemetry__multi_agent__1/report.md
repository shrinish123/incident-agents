# Incident report: fivo-api: POST /voice/call failing above 25%

**Status:** inconclusive

From about 13:19:55 UTC on 2026-11-09, POST /voice/call on fivo-api began failing: 11 x 502 and 11 x 504 over 13:10-14:00, against 57 x 202 (partial counts are lower bounds). No deploys, config or flag changes were recorded (07:00-14:00, and none mentioning bolna 12:00-14:00). All failures happen inside the outbound bolna call: 502s take about 0.8-1.06 s with 'bolna upstream error: 502 Bad Gateway', and 504s hit the 10 s client timeout. Best candidate is the Bolna side or the path to it (H1, with H5 not separable), but Bolna's health was never observed (no logs, metrics or traces) and the cause is reached by elimination only. Memory, CPU, restarts, Redis/worker queue and per-user data issues were ruled out.

## Best candidate (not confirmed)

- Service: bolna-voice
- Component: outbound call from fivo-api bolna.start_call (POST /voice/call), or the network/gateway path to Bolna
- Category: dependency_failure

The failing requests spend their time in the single httpx.post to Bolna (10 s timeout, no retry), which maps 5xx responses to 502 and timeouts to 504. Whether Bolna itself or a gateway/egress path in front of it is at fault is not established. The trigger and why it began at about 13:15-13:20 are unevidenced.

## What changed (why now, not confirmed)

Not established: nothing was shown to differ from when the system last worked.

## Hypotheses, ranked

1. **bolna-voice is degraded, so fivo-api's outbound call returns 502 and times out (504) only on POST /voice/call. The rate/quota-limit variant fits poorly.** (bolna-voice, dependency_failure) — most_likely, confidence 0.40
   - Supports: Only /voice/call has error-level lines; the code can produce 502/504 only from the bolna call. [logagg:fivo-api:0003, code:fivo-api:0001, code:fivo-api:0002, code:fivo-api:0003]
   - Supports: 504s took about 10.01 s and 502s about 0.8-1.06 s, while 202s took 227-519 ms; failed voice_calls rows match. [log:fivo-api:0005, log:fivo-api:0006, log:fivo-api:0009, db:voice_calls:0001]
   - Supports: Failures occur at about 1 in-flight request, interleaved with fast 202s, so not fivo-api concurrency or a total outage. [logagg:fivo-api:0007, log:fivo-api:0014, log:fivo-api:0018, log:fivo-api:0019]
   - Supports: Error rate left baseline at 13:20 and p95 stepped up at 13:35. [metric:fivo-api:0007, metric:fivo-api:0006]
   - Supports: No deploys or config changes recorded, including for bolna. [deploy:deploys:0001, config:config_changes:0001, config:bolna-voice:0001, config:config_changes:0002]
   - Contradicts: Bolna has no logs, metrics or traces; its health was never observed. [logagg:bolna-voice:0001, topology:bolna-voice:0001]
   - Contradicts: A quota or rate limit would likely surface as 429 and then 500 via raise_for_status, and no 500s were logged. [code:fivo-api:0001, logagg:fivo-api:0003]
   - Contradicts: Request rate fell from about 13.4/min to about 9.4/min, so no load rise explains a Bolna-side limit. [metric:fivo-api:0005]
   - Contradicts: p95 latency stayed near normal until 13:30, which weakens a gradually worsening upstream story. [metric:fivo-api:0006]
2. **The fault is on the path between fivo-api and Bolna (egress, NAT, gateway/proxy), not in Bolna's service.** (fivo-api, infrastructure) — possible, confidence 0.12
   - Supports: The 502 text shows only that a 502 was received, and headers are discarded, so the sender is unknown. A non-timeout connection error would surface as 500, so the 502s were a 5xx status received from some hop. [code:fivo-api:0001, code:fivo-api:0003]
   - Supports: Env-level proxy settings cannot be excluded from code (httpx trust_env). [codehit:fivo-api:0003]
   - Contradicts: anthropic-llm and r2-storage show no errors or latency rise, against an instance-wide egress fault. [metric:anthropic-llm:0001, metric:anthropic-llm:0002, metric:r2-storage:0001, metric:r2-storage:0002]
   - Contradicts: The code default BOLNA_BASE_URL is a direct host; the deployed value was not seen. [code:fivo-api:0004]
3. **The single 512 MB fivo-api instance is exhausted.** (fivo-api, resource_exhaustion) — ruled_out, confidence 0.02
   - Contradicts: Memory was about 201-207 MB of 512 MB, CPU fell after 13:10, restarts were 0, and request rate fell. [metric:fivo-api:0001, metric:fivo-api:0002, metric:fivo-api:0003, metric:fivo-api:0005]
4. **The Redis queue handoff to fivo-worker is failing or backing up.** (upstash-redis, dependency_failure) — ruled_out, confidence 0.02
   - Contradicts: Queue depth and worker failure rate were 0; Redis was at about 46% of its daily limit with no rate change; the worker kept logging normally; no fivo-api log mentions redis. [metric:fivo-worker:0001, metric:fivo-worker:0002, metric:upstash-redis:0001, metric:upstash-redis:0002, logagg:fivo-worker:0001, logagg:fivo-api:0006]
5. **A data or input subset (particular users, reminders or phone numbers) makes some calls fail.** (fivo-api, data_issue) — ruled_out, confidence 0.02
   - Contradicts: 18 of 37 users failed and 13 had both outcomes; the same user succeeds and fails within minutes. [db:voice_calls:0002, log:fivo-api:0014, log:fivo-api:0001, log:fivo-api:0005, log:fivo-api:0019]
   - Contradicts: Onset was abrupt; only 1 failed call from 12:00 to 13:10. [db:voice_calls:0003]

## Facts

- POST /voice/call returned 57 x 202, 11 x 502 and 11 x 504 over 13:10-14:00. All 22 error-level lines are on /voice/call. [logagg:fivo-api:0004, logagg:fivo-api:0003]
- 22 failed voice_calls rows carry bolna error text and have no provider_call_id. The first failure in the window is at 13:19:55. [db:voice_calls:0001]
- No deploys or config/flag changes were recorded for any service 07:00-14:00; none mentioning bolna 12:00-14:00. [deploy:deploys:0001, config:config_changes:0001, config:config_changes:0002]
- bolna.start_call uses one httpx.post with a 10 s default timeout and no retry; timeouts map to 504 and 5xx responses to 502. [code:fivo-api:0001, code:fivo-api:0003]
- The 502 handling keeps only status and reason phrase; headers and body are not logged. [code:fivo-api:0001]
- p95 latency jumped at 13:35 to about 5-9 s, and error_rate left baseline at 13:20. [metric:fivo-api:0006, metric:fivo-api:0007]
- bolna-voice returned no log lines and no client-side metrics are collected for it. [logagg:bolna-voice:0001, topology:bolna-voice:0001]
- Memory, CPU, restarts, queue depth and other outbound host metrics show no problem. [metric:fivo-api:0001, metric:fivo-api:0003, metric:fivo-worker:0001, metric:anthropic-llm:0001, metric:r2-storage:0001]

## Assumptions

- The code at main is what runs; not verified.
- BOLNA_TIMEOUT_S and BOLNA_BASE_URL are not overridden in the environment; not verified.
- No platform, egress, Bolna-side or credential changes occurred; the change records only cover deploys and config/flags.
- The trigger (a Bolna-side or path fault starting about 13:15-13:20) is assumed by elimination.
- Several counts come from partial or capped results and are lower bounds.

## Recommended actions

- [immediate] Check Bolna's status page, incident feed, and account concurrency/quota usage, and ask Bolna support about 502s and timeouts on our API key since about 13:19 UTC. — Bolna's health is the unobserved core question; no tool reached it. [topology:bolna-voice:0001, logagg:bolna-voice:0001]
- [immediate] Confirm the runtime values of BOLNA_BASE_URL, BOLNA_TIMEOUT_S, HTTPS_PROXY and HTTP_PROXY on the instance, and that main is the running code. — Environment drift or a proxy would point to H5 and is unchecked. [code:fivo-api:0004, codehit:fivo-api:0003]
- [soon] Re-run trace searches for 6a239cc311824309 and 350d8a2d4446bcb5 once the ingest backlog clears and compare DNS, connect, TLS and TTFB for the bolna span between 202 and 502/504 requests. — This would separate Bolna from the path to it. [log:fivo-api:0005, log:fivo-api:0006]
- [soon] Log the response headers (Server, Via) and body of 5xx from Bolna; add a bounded retry for 5xx and timeouts. — Headers are currently discarded; no retry exists, so each transient fault becomes a user-visible failure. [code:fivo-api:0001]
- [later] Add client-side latency and error metrics for the Bolna call and alert on /voice/call 5xx share. — No Bolna metrics exist; error_rate rose from 13:20 without a dedicated signal. [topology:bolna-voice:0001, metric:fivo-api:0007]

## Missing evidence

- Bolna status page, incident feed, account concurrency and quota usage (no tool available).
- Span-level traces (trace store down with 'ingest backlog').
- Response headers of the 502s (not logged).
- Runtime values of BOLNA_BASE_URL, BOLNA_TIMEOUT_S and proxy variables; confirmation that main is the running code.
- Phone and reminder attributes (not in logs or schema); the users join was rejected.
- Full 502/504 log lines outside the sampled 13:30-13:45 window; several counts are partial.
- Explanation for the single failed call before 13:10 and the 1.4% error blip at 13:00.

## Run

- system: multi_agent
- termination: round limit reached
- rounds: 3
- hypotheses: 5
- steps: 16
- max hops: 6
- tool calls: 83
- tool failures: 8
- guardrail rejections: 4
- llm calls: 68
- total tokens: 940100
- cost usd: 1.5133
- latency s: 1181.0
- citations: 75 checked, 0 invalid, 0 facts demoted to assumptions
