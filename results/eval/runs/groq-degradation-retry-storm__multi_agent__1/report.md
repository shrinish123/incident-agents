# Incident report: fivo-api: p95 latency above 3 s for 10 minutes

**Status:** conclusive

From about 11:00-11:05Z on 2026-11-11 fivo-worker's calls to groq-llm began timing out (30 s) or returning 503, with no deploy or config change on our side. categorize_transaction now takes 12-30 s per attempt, while POST /transactions/quick-add waits a fixed 8,000 ms for the worker result, so nearly every quick-add returns 504 from the 11:05 bucket. Other routes do not wait on the worker and stay healthy. Whether Groq itself or the worker's network path is at fault is not established.

## Root cause

- Service: groq-llm
- Component: fivo-worker categorize_transaction -> Groq chat completions call (30 s timeouts and 503s) against quick-add's fixed 8 s result wait
- Category: dependency_failure

Groq calls from fivo-worker degraded at about 11:00-11:05Z (client p95 ~480 ms to ~29 s, error rate 0 to ~0.62). categorize_transaction blocks on these calls, so the worker result arrives long after quick-add's 8 s wait ends, producing 504s. The queue also backs up (depth 0 to 30). The cause of the Groq degradation is not established.

## What changed (why now)

Groq responses to fivo-worker's categorize_transaction calls began timing out or returning 503 at about 11:00Z. This is upstream behaviour, since nothing of ours was deployed or reconfigured. (at 2026-11-11T11:00:00Z)

- Before: Groq client p95 was ~480 ms with 0 error rate at 10:50-10:55. Worker-to-Groq spans took 175-589 ms through 11:02. quick-add returned 201, including a 1.15 s success at 11:02:49. [metric:groq-llm:0001, metric:groq-llm:0002, trace:fivo-worker:0004, trace:fivo-worker:0024, trace:fivo-api:0001]
- After: From 11:00, Groq client p95 was ~29 s and the error rate ~0.62. Spans from 11:05:10 took 11.6-19.9 s. Worker task p95 sat at 30 s from 11:05. quick-add returned 504 from the 11:05 bucket. [metric:groq-llm:0001, metric:groq-llm:0002, trace:fivo-worker:0025, trace:fivo-worker:0042, metric:fivo-worker:0005, logagg:fivo-api:0002]

## Hypotheses, ranked

1. **fivo-worker's categorize_transaction fails or retries against groq-llm (timeouts, 503s), stalling the worker so quick-add's 8 s wait for the result expires and returns 504.** (groq-llm, dependency_failure) — most_likely, confidence 0.82
   - Supports: Groq client p95 went from ~480 ms to ~29 s and error rate from 0 to ~0.62, with a level shift at 11:00Z. [metric:groq-llm:0001, metric:groq-llm:0002]
   - Supports: Worker-to-Groq spans took 175-589 ms until 11:02 and 11.6-19.9 s from 11:05:10. [trace:fivo-worker:0004, trace:fivo-worker:0020, trace:fivo-worker:0024, trace:fivo-worker:0025, trace:fivo-worker:0026, trace:fivo-worker:0027, trace:fivo-worker:0042]
   - Supports: All 103 worker warnings in 11:00-11:40Z are categorize_transaction retries mentioning groq: 72 timeouts and 31 503s. The only errors are Groq timeouts. [logagg:fivo-worker:0005, logagg:fivo-worker:0006, logagg:fivo-worker:0008, logagg:fivo-worker:0009, log:fivo-worker:0001]
   - Supports: In the traced failing quick-add the API waited 8,000 ms and gave up; the worker task took 13,033 ms, of which 13,019 ms was the Groq POST. [trace:fivo-api:0033, trace:fivo-worker:0002, trace:fivo-worker:0003]
   - Supports: Worker task p95 pinned at 30,000 ms from 11:05 and queue depth rose from 0 to 30. [metric:fivo-worker:0005, metric:fivo-worker:0004]
   - Supports: No deploys or config changes in the window, consistent with an external trigger. [deploy:deploys:0001, config:config_changes:0001, deploy:fivo-worker:0001]
   - Contradicts: Trace status shows ok for slow Groq calls, so traces are not a usable error signal. [trace:fivo-worker:0025]
   - Contradicts: Only 2 warnings mention 429; they were not read, so rate limiting is not excluded as a sub-cause. [logagg:fivo-worker:0007]
   - Contradicts: Baseline task p95 was ~6 s while Groq p95 was ~480 ms, which is unexplained; the 8 s budget was already tight. [metric:fivo-worker:0005]
2. **The Celery queue on upstash-redis is stalled (quota, connection limit, broker problem).** (upstash-redis, resource_exhaustion) — ruled_out, confidence 0.03
   - Supports: quick-add depends on the Redis queue round trip and no local changes were recorded. [topology:fivo-api:0001, deploy:deploys:0001]
   - Contradicts: 0 worker log lines mention redis; tasks are being received from the queue. [logagg:fivo-worker:0010, logagg:fivo-worker:0004]
   - Contradicts: Enqueue took 1.8 ms in the traced request. [trace:fivo-api:0032]
   - Contradicts: quick-add 504 lines show only the 8000 ms result-not-ready error, with no Redis errors. [log:fivo-api:0001, log:fivo-api:0002, log:fivo-api:0010]
3. **fivo-worker is saturated by a local long-running job or memory pressure on its 512 MB, two-thread instance.** (fivo-worker, resource_exhaustion) — ruled_out, confidence 0.02
   - Supports: The worker has only two threads and beat runs in the same process. [topology:fivo-worker:0001]
   - Contradicts: Memory flat at 248-256 MB, CPU fell after 11:00, 0 restarts. [metric:fivo-worker:0001, metric:fivo-worker:0002, metric:fivo-worker:0003]
   - Contradicts: embed_pending_receipts finished in 2.62 s and only categorize_transaction produced warnings or errors. [log:fivo-worker:0012, logagg:fivo-worker:0003]
   - Contradicts: No receipt backlog: only 2 pending receipts. [db:receipts:0001]
4. **neon-postgres lock, slow query or data condition on the quick-add write path.** (neon-postgres, data_issue) — ruled_out, confidence 0.02
   - Supports: Nothing of ours changed and only one route is affected, which would also fit a path-specific DB issue. [deploy:deploys:0001, logagg:fivo-api:0001]
   - Contradicts: DB spans in the failing trace took 5.8 ms (insert) and 6.0 ms (select). [trace:fivo-api:0031, trace:fivo-worker:0001]
   - Contradicts: Writes to transactions continued at normal rates and job failures are Groq timeouts, not DB errors. [db:transactions:0001, db:jobs:0001]

## Facts

- quick-add returned 201 through the 11:00 bucket and 504 on every call from the 11:05 bucket; other routes showed no 5xx (counts partial). [logagg:fivo-api:0002, logagg:fivo-api:0001]
- Groq client p95 was ~480 ms and error rate 0 at 10:50-10:55; from 11:00 p95 was ~29 s and error rate ~0.62 through 11:35. [metric:groq-llm:0001, metric:groq-llm:0002]
- All 103 worker warnings in 11:00-11:40Z were categorize_transaction retries on Groq (72 timed out, 31 503); the first sampled warning is at 11:05:09Z. [logagg:fivo-worker:0005, logagg:fivo-worker:0008, logagg:fivo-worker:0009, log:fivo-worker:0003]
- Trace 654ee93acd37dccd (11:05:51Z): enqueue 1.8 ms, DB insert 5.8 ms, API waited 8,000 ms and errored, worker task took 13,033 ms with the Groq POST 13,019 ms. [trace:fivo-api:0031, trace:fivo-api:0032, trace:fivo-api:0033, trace:fivo-worker:0002, trace:fivo-worker:0003]
- Worker queue depth went from 0 to 5 at 11:05 and to 30 at 11:35; task p95 was 30,000 ms from 11:05. [metric:fivo-worker:0004, metric:fivo-worker:0005]
- No deploys or config changes for any service 06:00-11:40Z, and none for fivo-worker since 11-09. [deploy:deploys:0001, config:config_changes:0001, deploy:fivo-worker:0001]
- quick-add waits on the worker's result via the Redis queue; the worker runs Celery with two threads. [topology:fivo-api:0001, topology:fivo-worker:0001]
- Worker memory, CPU and restarts show no resource exhaustion. [metric:fivo-worker:0001, metric:fivo-worker:0002, metric:fivo-worker:0003]

## Assumptions

- Worker thread starvation is inferred from queue depth and task duration, not measured directly; a single slow Groq call already exceeds the 8 s wait.
- Queue wait of about 5 s is approximated from span timestamps.
- Onset is stated as ~11:00-11:05Z; fast spans at 11:02:50 suggest the true start is closer to 11:03-11:05.
- Whether the Groq degradation is provider-side or on the worker's network path is not established; the Groq metrics are our own client-side view.
- Only one full trace was examined.

## Recommended actions

- [immediate] Check Groq's status page and the worker host's network path to api.groq.com now, and confirm the key and account are not rate-limited or over quota. — Groq calls from the worker are the failing step; the provider-side versus path question is open. [metric:groq-llm:0001, metric:groq-llm:0002]
- [immediate] Mitigate quick-add: return 201 with categorisation deferred (async), or fail over to another categoriser, while Groq is degraded. — quick-add's fixed 8 s wait is exceeded by every Groq call at 12-30 s, so every call returns 504. [trace:fivo-api:0033, trace:fivo-worker:0002]
- [soon] Lower the Groq client timeout from 30 s and cap retries so a slow upstream cannot hold both worker threads; consider a separate queue or more concurrency for categorize_transaction (watch the broker connection limit from INC-2026-021). — 30 s timeouts pin task p95 at 30 s and the queue depth grew to 30. [metric:fivo-worker:0005, metric:fivo-worker:0004, incident:fivo-worker:0001]
- [soon] Read the 2 '429' warnings and check the other worker external dependencies (anthropic, voyage, r2) for the same 11:00 shift. — Distinguishes a Groq rate limit or a worker network problem from a provider outage. [logagg:fivo-worker:0007]
- [later] Add alerts on groq-llm client p95 and error rate, on worker queue_depth, and on the quick-add 504 rate. Investigate why baseline task p95 was ~6 s against an 8 s wait. — The step change at 11:00 was only caught at 11:20 via fivo-api p95, and the 8 s budget was already tight. [metric:fivo-worker:0005, metric:groq-llm:0001]

## Missing evidence

- Groq provider status or incident signal and the worker's other external dependencies (S13 was skipped).
- Direct measurement of worker thread occupancy.
- The content of the 2 '429' warnings.
- Upstash metrics were never queried (S7 skipped); pg_stat_activity and lock views were unavailable.
- Several log, trace and count results were partial or capped, so counts are lower bounds; only one full trace was examined.
- Changes to third-party dependencies and changes before 06:00Z on 11-11 were not checked.

## Run

- system: multi_agent
- termination: accepted
- rounds: 1
- hypotheses: 4
- steps: 11
- max hops: 3
- tool calls: 58
- tool failures: 0
- guardrail rejections: 7
- llm calls: 42
- total tokens: 595525
- cost usd: 0.9483
- latency s: 204.8
- citations: 90 checked, 0 invalid, 0 facts demoted to assumptions
