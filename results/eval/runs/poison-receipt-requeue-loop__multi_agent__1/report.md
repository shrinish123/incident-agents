# Incident report: fivo-worker: queue depth above 150 and rising

**Status:** conclusive

At 04:30:10Z on 2026-11-10 a 131-byte image/heic receipt (id 4363, user 154) was uploaded, and process_receipt[8f182553-20a6-d4ec] was received. One second later fivo-worker exited with code 139 (SIGSEGV). The task is redelivered after every restart (about every 27s) and crashes the worker again, so process_receipt completions fell to zero, categorize_transaction slowed to 4-10 per 10 min, and queue_depth grew to 317 by 06:05Z. No deploy or config change preceded the onset. Two links are inferred: the task-to-receipt match rests on user and timestamp, and the native library that segfaults is not identified.

## Root cause

- Service: fivo-worker
- Component: process_receipt task 8f182553-20a6-d4ec (likely decoding corrupt 131-byte HEIC receipt 4363), redelivered after each crash
- Category: data_issue

A malformed receipt upload at 04:30:10Z leads to a process_receipt task that segfaults the worker (exit 139). The task is redelivered after every restart and crashes it again, so the single worker never drains the queue and receipts stay in 'uploaded'.

## What changed (why now)

New input data arrived: a corrupt 131-byte HEIC receipt from user 154 whose process_receipt task crashes the worker and is redelivered indefinitely. No code or config change is involved. (at 2026-11-10T04:30:10Z)

- Before: Until 04:30:06Z process_receipt (6-9s) and categorize_transaction tasks completed normally. 30 process_receipt successes were logged in 04:00-04:30Z. No receipt created from 04:00:00Z to 04:30:09Z is stuck. [log:fivo-worker:0040, logagg:fivo-worker:0009, db:receipts:0003]
- After: At 04:30:10Z the 131-byte HEIC receipt 4363 was created and process_receipt 8f182553 was received. Exit 139 followed at 04:30:11Z and repeated about every 27s. No process_receipt completed from 04:30Z to 06:10Z. [db:receipts:0002, log:fivo-worker:0041, log:fivo-worker:0042, logagg:fivo-worker:0009]
- Why it did not fail earlier: The crash-prone code path was probably already present, since no deploy or config change landed (F2, F3, F5). It only failed when the first malformed input reached it. That the code path has not crashed before is an assumption, because the library and code were not read.

## Hypotheses, ranked

1. **A poison process_receipt task (corrupt 131-byte HEIC receipt) triggers a native-library SIGSEGV in fivo-worker and is redelivered after each restart, causing a crash loop.** (fivo-worker, data_issue) — most_likely, confidence 0.85
   - Supports: First crash at 04:30:11Z, one second after process_receipt[8f182553-20a6-d4ec] was received. The same task was received again after every restart (about every 27s) and never logged 'succeeded'. [log:fivo-worker:0041, log:fivo-worker:0042, log:fivo-worker:0044, log:fivo-worker:0048, log:fivo-worker:0078]
   - Supports: In 04:00-04:40Z there were 22 receipts of that task and 22 exit-139 errors (partial counts, lower bounds). [logagg:fivo-worker:0004]
   - Supports: Receipt 4363 (user 154, image/heic, 131 bytes, status uploaded) was created at 04:30:10Z. It is the first stuck receipt and none earlier are stuck. [db:receipts:0002, db:receipts:0001, db:receipts:0003]
   - Supports: process_receipt completions fell from 30 in 04:00-04:30Z to zero afterwards, while categorize_transaction continued at a reduced 4-10 per 10 min. Restarts jumped 0 to 11 at 04:30Z. [logagg:fivo-worker:0009, logagg:fivo-worker:0010, metric:fivo-worker:0002, metric:fivo-worker:0001]
   - Supports: No deploy or config change in the hours before onset, which points to new input rather than new code. [deploy:deploys:0001, config:config_changes:0001, config:fivo-worker:0001]
   - Contradicts: The link between job 8f182553 and receipt 4363 rests on user and timestamp, not an identifier. The crashing library is unidentified, and later crashes up to 06:10Z are assumed to be the same task. [db:receipts:0002, logagg:fivo-worker:0004]
   - Contradicts: No trace spans exist for the crashing task's trace, so there is no corroboration from traces. [trace:traces:0001]
2. **A platform, image or runtime fault (for example a wrong-architecture binary) makes the worker crash at boot or independent of any task.** (fivo-worker, infrastructure) — ruled_out, confidence 0.02
   - Supports: Prior incident INC-2026-033 was a restart loop after a wrong-architecture wheel, and SIGSEGV with flat memory fits a native fault. [incident:fivo-worker:0003, metric:fivo-worker:0003]
   - Contradicts: The worker ran normally until 04:30:10Z, and every restart reached 'celery ready' and received tasks before crashing, so this is not a boot failure. [log:fivo-worker:0001, log:fivo-worker:0040, log:fivo-worker:0045, log:fivo-worker:0049]
   - Contradicts: Crashes track one task id (33 receipts, 33 SIGSEGVs over 04:20-04:45Z, partial). No deploy or config change is recorded. [logagg:fivo-worker:0003, deploy:deploys:0001, config:config_changes:0001]
3. **A scheduled job in the same process (daily_summary or embed_pending_receipts), possibly changed by dep-0414, crashes the worker.** (fivo-worker, code_regression) — ruled_out, confidence 0.02
   - Supports: Beat runs in the same process, and dep-0414 is the only recent change. [topology:fivo-worker:0001, deploy:fivo-worker:0001]
   - Contradicts: dep-0414 only changed README.md and the daily_summary cron from 03:00 to 02:30; neither time is near 04:30Z. daily_summary is plain Python with no native call. [changes:fivo-worker:0001, diff:fivo-worker:0002, diff:fivo-worker:0001, code:fivo-worker:0001, code:fivo-worker:0002]
   - Contradicts: Crashes follow redelivery of process_receipt, not any scheduled task. [log:fivo-worker:0044, log:fivo-worker:0048]
4. **An upstream dependency (Redis, Groq, Anthropic, Voyage, R2) degraded at about 04:30Z and caused a client library crash.** (upstash-redis, dependency_failure) — ruled_out, confidence 0.03
   - Supports: Earlier incidents involved broker limits and provider 429s, and no change was on record. [incident:fivo-worker:0001, incident:fivo-worker:0002]
   - Contradicts: Error rates for groq, anthropic, voyage and r2 were 0.0 throughout with no shift at 04:30Z. Redis commands/min dipped only about 7% (315 to 292), consistent with a crashing consumer. [metric:groq-llm:0001, metric:anthropic-llm:0001, metric:voyage-embeddings:0001, metric:r2-storage:0001, metric:upstash-redis:0001]
   - Contradicts: The crash tracks a single task id, which general dependency degradation would not produce. [logagg:fivo-worker:0004]

## Facts

- queue_depth left 0 at about 04:30Z and rose to 317 at 06:05Z. Restarts jumped from 0 to 11 at 04:30Z. [metric:fivo-worker:0001, metric:fivo-worker:0002]
- Between 05:20Z and 06:10Z there were 109 error lines, all 'Instance exited with code 139 (SIGSEGV) and was restarted'. [logagg:fivo-worker:0002]
- Memory stayed flat at about 236-255 MB and task_failure_rate was 0.0 throughout. [metric:fivo-worker:0003, metric:fivo-worker:0004]
- No deploys or config changes were recorded from 01:00Z to 06:10Z. The last fivo-worker deploy, dep-0414, was at 2026-11-09T03:50Z. [deploy:deploys:0001, config:config_changes:0001, deploy:fivo-worker:0001]
- process_receipt[8f182553-20a6-d4ec] was received at 04:30:10Z and exit 139 followed at 04:30:11Z. The task was received again after each restart and never logged 'succeeded'. [log:fivo-worker:0041, log:fivo-worker:0042, log:fivo-worker:0044, log:fivo-worker:0048, logagg:fivo-worker:0004]
- Receipt 4363 (user 154) is image/heic, 131 bytes, status uploaded, created 04:30:10Z, and is the first stuck receipt. [db:receipts:0002, db:receipts:0003]
- process_receipt succeeded 30 times in 04:00-04:30Z and zero times from 04:30Z to 06:10Z. categorize_transaction continued at 4-10 per 10 min. [logagg:fivo-worker:0009, logagg:fivo-worker:0010]
- dep-0414 changed only README.md and the daily_summary cron (03:00 to 02:30 UTC). [changes:fivo-worker:0001, diff:fivo-worker:0002]
- No trace spans exist for the crashing task's trace or for 04:20-04:45Z. [trace:traces:0001, trace:fivo-worker:0001]

## Assumptions

- Job 8f182553 processes receipt 4363; the link rests on user 154 and the timestamp only.
- The segfault is a native decode of the corrupt HEIC file; no code or library was read.
- Crashes after 04:34Z (up to 06:10Z) are the same task; raw lines were read only to 04:33:49Z and counts are lower bounds.
- Redelivery is unbounded because of Celery settings (acks_late, reject_on_worker_lost, no retry limit); not checked.
- The code path had not crashed before because no malformed input had reached it.

## Recommended actions

- [immediate] Purge or dead-letter process_receipt[8f182553-20a6-d4ec] from the broker, then confirm restarts stop and queue_depth drains. — This task is received before every SIGSEGV and never succeeds. Removing it should end the crash loop. [log:fivo-worker:0041, log:fivo-worker:0044, logagg:fivo-worker:0004]
- [immediate] Mark receipt 4363 (user 154, 131-byte HEIC) as failed so it is not requeued. Then re-enqueue the stuck receipts 4364-4373 and check they process. — Receipt 4363 is the likely poison input. The other stuck receipts have ordinary sizes and are probably only blocked by the loop. [db:receipts:0002]
- [soon] Find the native library that crashes on this file, and read the Celery acks_late, task_reject_on_worker_lost and max-redelivery settings. Fix the decoder handling, or run decoding in a subprocess. — The crash site and the cause of unbounded redelivery are unverified. [logagg:fivo-worker:0004]
- [soon] Validate uploads (minimum size and a valid header or decode check) before enqueueing process_receipt. Cap redeliveries so a crashing task goes to a dead-letter queue. — One bad file halted receipt processing for over 1.5 hours. [db:receipts:0002, log:fivo-worker:0048]
- [later] Alert on restarts above 0 for a sustained period, and on the same task id being received repeatedly. Count a worker crash as a task failure. — task_failure_rate stayed 0.0 during the crash loop, so failure-rate alerts could not fire. [metric:fivo-worker:0004, metric:fivo-worker:0002]

## Missing evidence

- No job or task table check linking job 8f182553 to receipt 4363 (S13 skipped).
- The code path, native library and Celery redelivery settings were not read (S14 skipped).
- Raw log lines after 04:33:49Z were not read, and the log aggregates were partial.
- No trace spans exist for 04:20-04:45Z or for the crashing task.
- Dependency latency and Redis daily quota were not checked, and neon-postgres was not queried.
- Platform and image events for the instance were not checked (S7, S8 skipped).

## Run

- system: multi_agent
- termination: accepted
- rounds: 1
- hypotheses: 4
- steps: 9
- max hops: 3
- tool calls: 51
- tool failures: 0
- guardrail rejections: 1
- llm calls: 39
- total tokens: 576956
- cost usd: 0.8064
- latency s: 939.8
- citations: 82 checked, 0 invalid, 0 facts demoted to assumptions
