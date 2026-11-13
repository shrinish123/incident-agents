# Incident report: fivo-api: receipts stuck in 'uploaded' for more than 30 minutes

**Status:** conclusive

Deploy dep-0414 of fivo-worker (sha fdbd6e96, 15:44Z) rewrote `embed_pending_receipts` to load every pending receipt in one pass. About 2,600 legacy receipts are waiting for embeddings, so the task reads all their text into memory before embedding anything. The 512 MB instance hits its limit and is OOM-killed (exit 137) about every 35 seconds. Because the task is redelivered after each kill, the worker never gets to run `process_receipt`. Receipts therefore stay in 'uploaded' while the queue grows. Roll back to 994ebf05, or restore the batched loop, and then drain the backlog in bounded batches.

## Root cause

- Service: fivo-worker
- Component: worker/tasks/embeddings.py embed_pending_receipts + worker/repo.py list_pending_receipts (commit fdbd6e96, dep-0414)
- Category: code_regression

dep-0414 replaced the batched `iter_pending_receipt_ids(limit=64)` loop with `list_pending_receipts()`. It now reads the text of every pending receipt into memory and holds all the vectors before saving. With about 2,600 receipts pending, the first beat run at 15:53 pushed the 512 MB worker past its limit. The worker is OOM-killed and restarts roughly every 35 seconds, and the redelivered embed task re-triggers the kill, so `process_receipt` tasks starve and receipts stay 'uploaded'.

## What changed (why now)

fivo-worker dep-0414 changed `embed_pending_receipts` from a bounded batch loop (limit 64) to an unbounded load of every pending receipt, with all vectors accumulated in memory. (at 2026-11-09T15:44:00Z)

- Before: Worker memory was steady at about 243 MB, with no errors. Receipts processed in about 9s and queue depth was 0. [metric:fivo-worker:0001, log:fivo-worker:0007, metric:fivo-worker:0004]
- After: Memory sat at about 487–499 MB, with OOM kills every ~35s. Throughput fell to 0 tasks/min from 16:10, and queue depth reached 170 and kept growing. [metric:fivo-worker:0001, log:fivo-worker:0041, metric:fivo-worker:0003, metric:fivo-worker:0004]
- Why it did not fail earlier: The deploy landed at 15:44. The memory spike began at 15:52 and the first OOM kill was logged at 15:53:05, with the first embed task received at 15:53:00. That is the first beat run after the deploy that I saw. The old code only held 64 receipts at a time, so the 2,600-receipt backlog was harmless.

Changes before the incident, and what was concluded about each:

- **cause** — fivo-worker deploy dep-0414, sha fdbd6e96 (prev 994ebf05): 'embeddings: clear the legacy import backlog in one pass' (2026-11-09T15:44:00Z): The diff introduces the unbounded in-memory load. Memory, OOM kills and the stall all begin within about 10 minutes of the deploy, on the first embed run. [deploy:fivo-worker:0001, diff:fivo-worker:0001, diff:fivo-worker:0002, metric:fivo-worker:0001, log:fivo-worker:0057]
- **ruled out** — Configuration, environment variable, feature flag and infrastructure changes, 2026-11-08T12:00Z to 2026-11-09T17:00Z (2026-11-09T15:00:00Z): None recorded in the window. [config:config_changes:0001]

## Hypotheses, ranked

1. **The embeddings rewrite in dep-0414 loads the whole backlog into memory. This causes an OOM crash loop on the worker that starves process_receipt.** (fivo-worker, code_regression) — most_likely, confidence 0.93
   - Supports: The diff removes the batched fetch and save loop, and adds an unbounded `list_pending_receipts()` that reads all texts and accumulates all vectors before a single save. [diff:fivo-worker:0001, diff:fivo-worker:0002]
   - Supports: Memory jumped from about 243 MB to about 487 MB at 15:52 and stayed pinned near the 512 MB limit. Restarts rose from 0 to 2, and the queue grew from 0 to 170. [metric:fivo-worker:0001, metric:fivo-worker:0002, metric:fivo-worker:0004]
   - Supports: The same embed task job_id (864952ab) was received repeatedly, 35 seconds apart, starting at 15:53:00. Platform OOM kills (exit 137) were logged at 15:53:05, 15:53:40 and so on, and were still occurring at 16:30–16:35. [log:fivo-worker:0051, log:fivo-worker:0052, log:fivo-worker:0057, log:fivo-worker:0058, log:fivo-worker:0041, log:fivo-worker:0050]
   - Supports: About 2,600 processed receipts still have no embedding (the backlog), and 37 receipts are stuck in 'uploaded'. [db:receipts:0002, db:receipts:0001]
   - Supports: tasks_completed fell from about 4.6 to about 0.9 per minute after the deploy. It reached 0 from 16:10. [metric:fivo-worker:0003]
   - Contradicts: I did not see the heap profile or the Python code for storage.read_text. The per-receipt memory size is inferred from the memory plateau. [metric:fivo-worker:0001]
2. **An external dependency failure (Anthropic, R2 or Redis quota) is stalling process_receipt.** (anthropic-llm, dependency_failure) — ruled_out, confidence 0.04
   - Contradicts: The only errors in the window are platform OOM kills (exit 137, fingerprint fp_974247fe0f). Before the deploy, process_receipt succeeded in about 9s. [log:fivo-worker:0041, log:fivo-worker:0007]
3. **A configuration or infrastructure change caused the stall.** (fivo-worker, config_change) — ruled_out, confidence 0.02
   - Contradicts: No config changes were recorded in the 29 hours before the incident. [config:config_changes:0001]

## Facts

- fivo-worker was deployed at 15:44:00Z (dep-0414, 'embeddings: clear the legacy import backlog in one pass'). It was the only deploy in the window, and no config changes were recorded. [deploy:fivo-worker:0001, config:config_changes:0001]
- The change in embed_pending_receipts replaces chunked fetching and saving with a load-all and accumulate-all approach. [diff:fivo-worker:0001, diff:fivo-worker:0002]
- Worker memory moved from about 243 MB to about 487–499 MB at 15:52. Platform OOM kills (exit 137) repeat roughly every 35 seconds. [metric:fivo-worker:0001, log:fivo-worker:0057, log:fivo-worker:0041]
- The embed task with the same job_id was redelivered about every 35 seconds starting 15:53:00. [log:fivo-worker:0051, log:fivo-worker:0056]
- Worker throughput fell to 0 tasks/min from about 16:10, and queue depth rose to 170 by 16:48. [metric:fivo-worker:0003, metric:fivo-worker:0004]
- The backlog is 2,600 processed receipts without embeddings. 37 receipts are in 'uploaded'. [db:receipts:0002, db:receipts:0001]
- Before the deploy, process_receipt succeeded in about 9s and the worker had no errors. [log:fivo-worker:0007, logagg:fivo-worker:0001]

## Assumptions

- Memory use scales with the number of pending receipts (about 2,600), because the new code reads all their text and vectors before saving. I did not see a heap profile.
- Celery redelivers the unacknowledged embed task after each kill (late ack or visibility timeout). This is inferred from the repeated receipt of the same job_id.
- The 15:44 SIGTERM and the 15:44 restart were the deploy rollout, not an OOM kill. The first OOM kill I saw was at 15:53:05.

## Recommended actions

- [immediate] Roll back fivo-worker to 994ebf05, or hotfix `embed_pending_receipts` to a bounded batch loop (`iter_pending_receipt_ids(limit=64)` with save per batch). — This removes the unbounded memory use that is crash-looping the worker. [diff:fivo-worker:0002, metric:fivo-worker:0001]
- [immediate] After the rollback, purge or revoke the redelivered embed_pending_receipts task from the Redis queue. Confirm that process_receipt drains the queue and that the 37 'uploaded' receipts reach 'processed'. — A poison-pill redelivery can keep crashing the worker even after the code is fixed. The queue depth is 170 and growing. [log:fivo-worker:0056, metric:fivo-worker:0004]
- [soon] Drain the 2,600-receipt embedding backlog in small batches with a memory-safe job, off the main worker or with a hard cap per run. — The backlog is the trigger for the memory spike and still has to be processed. [db:receipts:0002]
- [later] Add a memory alert for the worker at 80% of the limit and an OOM-restart alert. Review the change for unbounded loads before it deploys. Give embed tasks a time and memory budget and separate queues from process_receipt. — The crash loop ran for over 30 minutes before detection. Embedding work should not be able to starve user-facing receipt processing. [metric:fivo-worker:0002]

## Missing evidence

- No heap or per-task memory profile, so the exact allocation that exceeds the limit was not observed.
- Celery configuration (acks_late, visibility timeout) was not inspected.
- The log fetch at 15:44–15:58 was partial and truncated, so some lines were not read.
- I did not check the API's own logs. Uploads succeed per the incident description, and the fault is on the worker side.

## Run

- system: single_agent
- termination: agent finished
- tool calls: 18
- tool failures: 0
- guardrail rejections: 1
- llm calls: 7
- total tokens: 160704
- cost usd: 0.1633
- latency s: 48.1
- citations: 52 checked, 0 invalid, 0 facts demoted to assumptions
