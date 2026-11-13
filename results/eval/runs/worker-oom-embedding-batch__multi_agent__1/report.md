# Incident report: fivo-api: receipts stuck in 'uploaded' for more than 30 minutes

**Status:** conclusive

At 15:44Z, fivo-worker deploy dep-0414 (fdbd6e96, 'embeddings: clear the legacy import backlog in one pass') removed the LIMIT and batching from embed_pending_receipts. At the first beat run on the new code (15:53:00Z), the task loaded about 2,600 unembedded processed receipts and worker memory jumped from ~244 MB to ~490 MB. The 512 MB instance was killed at 15:53:05Z (exit 137), and the same embed job is redelivered on every boot, so the worker restarts about 1.7 times a minute. No receipt has finished since 15:52:29Z, and 37 are stuck in 'uploaded'. Quick-add 504s fit the same pattern. Roll back dep-0414. The link from the crash loop to process_receipt starvation is inferred, not directly shown.

## Root cause

- Service: fivo-worker
- Component: embed_pending_receipts task and repo.list_pending_receipts() (deploy dep-0414, fdbd6e96)
- Category: code_regression

dep-0414 replaced the capped, batched fetch (limit=64, save per batch) with an unbounded list_pending_receipts() that holds all rows, texts and vectors in memory before a single save. The first run on the new code at 15:53 hit the 512 MB limit and was OOM-killed. The job is redelivered on every boot, so the worker is stuck in a crash loop and process_receipt does not complete.

## What changed (why now)

Deploy dep-0414 made embed_pending_receipts unbounded in memory. It first ran on the new code at the 15:53 beat tick. (at 2026-11-09T15:44:00Z)

- Before: On the old capped code, the 15:43 embed_pending_receipts run caused no error. Worker memory was flat at about 238-255 MB and there were no errors before 15:50. process_receipt succeeded at 15:49:02, 15:50:40 and 15:52:29. [logagg:fivo-worker:0012, metric:fivo-worker:0001, logagg:fivo-worker:0001, log:fivo-worker:0038]
- After: At 15:53 memory stepped to 501 MB (mean 490.9) and stayed at 458-510 MB. A kill followed 5s after embed_pending_receipts was received, and restarts rose from about 0 to 8-9 per 5 minutes. No receipt was processed after 15:52:29. [metric:fivo-worker:0001, log:fivo-worker:0043, log:fivo-worker:0044, metric:fivo-worker:0002, db:receipts:0001]
- Why it did not fail earlier: The deploy landed at 15:44, just after the 15:43 beat tick. The first run on the new code was therefore the 15:53 tick, which explains the roughly 9-minute gap. The 2,600-row backlog of unembedded processed receipts is the data that makes the unbounded load exceed 512 MB.

Changes before the incident, and what was concluded about each:

- **cause** — deploy on fivo-worker: commit fdbd6e9 "embeddings: clear the legacy import backlog in one pass" by github-actions (2026-11-09T15:44:00Z, 6 min before onset): C1 is the only change in the window (F5). It removed the LIMIT and batching from embed_pending_receipts (F16-F18). The first run on the new code at 15:53 produced the memory step and the first kill (F31, F27). The 15:43 run on the old code caused none (F34). Only embeddings.py and repo.py changed (F19). [deploy:fivo-worker:0001, diff:fivo-worker:0001, diff:fivo-worker:0002, changes:fivo-worker:0001, metric:fivo-worker:0001, logagg:fivo-worker:0012, log:fivo-worker:0044]

## Hypotheses, ranked

1. **Deploy dep-0414 made embed_pending_receipts load the whole backlog into memory in one pass, so the 512 MB worker is OOM-killed in a loop and process_receipt never completes.** (fivo-worker, code_regression) — most_likely, confidence 0.88
   - Supports: The only deploy in the window was dep-0414 at 15:44Z. Only repo.py and embeddings.py changed. [deploy:fivo-worker:0001, changes:fivo-worker:0001, config:config_changes:0001]
   - Supports: The diff removed the cap: the old code was limited to 64 and saved per batch. The new code has no LIMIT and holds all rows, texts and vectors, saving once. [code:fivo-worker:0001, diff:fivo-worker:0001, diff:fivo-worker:0002]
   - Supports: Memory was flat at ~244 MB, then stepped to ~491 MB at 15:53 and stayed near the limit (peak 509.9 MB). Restarts rose from ~0 to 8-9 per 5 minutes. [metric:fivo-worker:0001, metric:fivo-worker:0002]
   - Supports: embed_pending_receipts was received at 15:53:00 and the kill came at 15:53:05. The same job id was redelivered every ~35s. [log:fivo-worker:0043, log:fivo-worker:0044, log:fivo-worker:0046, log:fivo-worker:0053, log:fivo-worker:0056]
   - Supports: 2,600 processed receipts have no embedding, which is the set the unbounded query loads. No receipt processed after 15:52:29Z, and 37 are stuck in 'uploaded'. [db:receipts:0001, db:receipts:0002]
   - Contradicts: Per-receipt size is unknown, so it is not shown that 2,600 rows reach ~490 MB within 5s. The memory step and kill timing are observed regardless. [db:receipts:0002, metric:fivo-worker:0001]
   - Contradicts: A second embed task id (~72 received lines) and per-minute embed lines doubling to 4 at 16:08-16:09 are not explained by the single-job chain. [logagg:fivo-worker:0009, logagg:fivo-worker:0012]
2. **Redis (Upstash) quota or connection limit is blocking task delivery and acknowledgement.** (upstash-redis, dependency_failure) — ruled_out, confidence 0.02
   - Supports: Redis is the shared broker with a daily quota, and INC-2026-021 had a similar symptom. [topology:upstash-redis:0001, incident:fivo-worker:0001]
   - Contradicts: Daily commands peaked at ~274k against a 500k limit. [metric:upstash-redis:0001, metric:upstash-redis:0002]
   - Contradicts: Commands per minute dipped slightly after 15:55, which fits a down worker rather than a retry storm. [metric:upstash-redis:0003]
   - Contradicts: No Redis lines in either service's logs, and the only worker error is the OOM kill. [logagg:fivo-worker:0004, logagg:fivo-api:0004, logagg:fivo-worker:0011]
3. **A surge or oversized uploads made process_receipt itself exceed 512 MB.** (fivo-worker, traffic_change) — ruled_out, confidence 0.02
   - Contradicts: POST /receipts volume was flat at 4-11 per 10 minutes. [logagg:fivo-api:0003]
   - Contradicts: process_receipt durations were normal up to 15:52:29 (6.68s, 8.45s, 8.88s). [log:fivo-worker:0038, log:fivo-worker:0022, log:fivo-worker:0006]
   - Contradicts: The first kill followed embed_pending_receipts being received, with no process_receipt in flight, and the same embed job was then redelivered repeatedly. [log:fivo-worker:0043, log:fivo-worker:0044, log:fivo-worker:0046, log:fivo-worker:0053]
4. **An R2, Voyage or Anthropic failure made process_receipt retry and hold memory.** (r2-storage, dependency_failure) — ruled_out, confidence 0.03
   - Contradicts: R2 and Voyage client error rate was 0.0 with no latency step at onset. [metric:r2-storage:0001, metric:r2-storage:0002, metric:voyage-embeddings:0001, metric:voyage-embeddings:0002]
   - Contradicts: Zero worker log lines mention R2, Voyage, Anthropic or timeout. [logagg:fivo-worker:0005, logagg:fivo-worker:0006, logagg:fivo-worker:0007, logagg:fivo-worker:0008]
   - Contradicts: The kills follow embed_pending_receipts, not retried upstream calls. Earlier Anthropic calls were healthy. [log:fivo-worker:0043, log:fivo-worker:0044, trace:fivo-worker:0008, trace:fivo-worker:0031]

## Facts

- The only deploy from 10:00 to 16:50 was fivo-worker dep-0414 at 15:44:00Z (fdbd6e96 from 994ebf05). No config changes were recorded. [deploy:fivo-worker:0001, config:config_changes:0001]
- The diff removed the LIMIT and per-batch saves. embed_pending_receipts now loads all pending rows, texts and vectors and saves once. [code:fivo-worker:0001, diff:fivo-worker:0001, diff:fivo-worker:0002]
- Worker memory stepped from ~244 MB to ~491 MB at 15:53 and peaked at 509.9 MB, against the 512 MB limit. [metric:fivo-worker:0001]
- The first kill seen in the fetched logs was at 15:53:05Z, 5s after embed_pending_receipts was received. The fetch was partial. [log:fivo-worker:0043, log:fivo-worker:0044]
- The same embed job id was redelivered at 15:53:35, 15:54:10, 15:54:45, 15:55:20 and 15:55:55. [log:fivo-worker:0046, log:fivo-worker:0053, log:fivo-worker:0054, log:fivo-worker:0055, log:fivo-worker:0056]
- All 98 worker errors from 15:45 to 16:50 are the 512 MB memory-limit kill (exit 137). Restarts run at 8-9 per 5 minutes. [logagg:fivo-worker:0003, metric:fivo-worker:0002]
- 2,600 of 2,817 processed receipts have embedded_at NULL. 37 receipts are stuck in 'uploaded' (created 15:54:23Z to 16:49:22Z), and the last processing was at 15:52:29Z. [db:receipts:0001, db:receipts:0002]
- fivo-api errors are mostly 504s on /transactions/quick-add (44 in a partial count). [logagg:fivo-api:0002]
- POST /receipts volume was flat and Redis, R2 and Voyage showed no limits or errors. [logagg:fivo-api:0003, metric:upstash-redis:0001, metric:r2-storage:0001, metric:voyage-embeddings:0001]

## Assumptions

- The crash loop is what stops process_receipt completing between kills (inferred from the shared 2-thread process and timing).
- Quick-add 504s come from the worker being down, since that route waits on the worker (inferred from topology; api counts are partial).
- The unembedded backlog at 15:44Z was similar to the current 2,600 (no point-in-time count).
- 2,600 rows with texts and vectors plausibly reach ~490 MB (per-receipt size unknown).

## Recommended actions

- [immediate] Roll back fivo-worker to 994ebf05 (revert dep-0414 / fdbd6e96) and confirm memory returns to ~245 MB and restarts stop. — The unbounded embed_pending_receipts is the only change and is crash-looping the worker. [deploy:fivo-worker:0001, metric:fivo-worker:0001, metric:fivo-worker:0002]
- [immediate] After rollback, confirm the 37 stuck 'uploaded' receipts get processed, and re-queue any that do not. Also check that quick-add recovers. — No receipt has processed since 15:52:29Z. [db:receipts:0002, logagg:fivo-api:0002]
- [soon] Re-implement the backlog clear with a LIMIT and per-batch save (restore the limit=64 pattern), or run it as a bounded one-off job, then drain the ~2,600 unembedded receipts in batches. — The one-pass load cannot fit in the 512 MB instance. [diff:fivo-worker:0001, diff:fivo-worker:0002, db:receipts:0002]
- [later] Add an alert on worker restarts and memory near the limit. Add a pre-deploy check on memory use for tasks that read unbounded row sets. — Memory hit the limit at 15:53 and restarts began about 10 minutes before detection at 16:30. [metric:fivo-worker:0001, metric:fivo-worker:0002]

## Missing evidence

- Why process_receipt cannot finish between kills (acks_late, prefetch and thread scheduling not examined).
- Redis queue depth and connection count, not exposed by the metrics.
- Per-receipt text and vector size, so the 2,600-row memory arithmetic is unproven.
- An explanation for the second embed_pending_receipts task id (~72 received lines) and the doubling of embed lines at 16:08-16:09.
- Anthropic metrics were not queried. 403 and 429 were not searched in worker logs.
- fivo-api log counts are partial and capped, so the quick-add count is a lower bound.
- No point-in-time backlog count at 15:44Z.

## Run

- system: multi_agent
- termination: accepted
- rounds: 1
- hypotheses: 4
- steps: 9
- max hops: 3
- tool calls: 50
- tool failures: 0
- guardrail rejections: 0
- llm calls: 38
- total tokens: 508383
- cost usd: 0.7526
- latency s: 928.5
- citations: 95 checked, 0 invalid, 0 facts demoted to assumptions
