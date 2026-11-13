# Incident report: fivo-worker: queue depth above 150 and rising

**Status:** conclusive

A poison-pill receipt crash-loops the worker. Receipt 4363 (user 154) is a 131-byte image/heic upload created at 04:30:10Z. `process_receipt` job 8f182553-20a6-d4ec decodes it through the native libheif decoder (`pillow_heif.read_heif`). That decode segfaults the process (exit 139). Because of `task_acks_late` and `task_reject_on_worker_lost`, the broker redelivers the task after every crash. The job has been received 219 times and restarted 219 times, and it blocks the two worker threads so the queue grows (about 309 at 06:00). Immediate fix: remove or ack the poison task, mark receipt 4363 failed, and guard the HEIC decode. The unconfirmed link is that I did not read the 131-byte object itself, so a corrupt or truncated file triggering the libheif crash is inferred.

## Root cause

- Service: fivo-worker
- Component: worker/parsing.py decode_image HEIC branch (pillow_heif.read_heif / libheif), combined with late-ack and reject-on-worker-lost redelivery of process_receipt
- Category: code_regression

Commit a5d51d17 (2026-10-26) added HEIC decoding through the native libheif decoder, with no validation or isolation. At 04:30:10Z a 131-byte HEIC receipt (id 4363) crashed that decoder with SIGSEGV. Late acks and reject-on-worker-lost redelivered the same task after every crash, which produced a crash loop (219 restarts) that starved the worker, so queue depth rose and receipts stayed in 'uploaded'.

## What changed (why now)

A malformed 131-byte HEIC receipt (4363) was uploaded and hit the native libheif decode path added in a5d51d17. The task was redelivered after every segfault by the late-ack configuration. (at 2026-11-10T04:30:10Z)

- Before: Before 04:30 the worker had a flat queue depth of 0, process_receipt tasks succeeded in about 6-9s, and no errors were logged. [metric:fivo-worker:0001, log:fivo-worker:0006, log:fivo-worker:0012, logagg:fivo-worker:0002]
- After: From 04:30:11 the worker segfaults (exit 139) about every 27s on the same job, with 219 restarts. The queue rose to 308.9 by 06:00 and receipts stayed in 'uploaded'. [log:fivo-worker:0031, logagg:fivo-worker:0001, metric:fivo-worker:0001, db:receipts:0002]
- Why it did not fail earlier: The HEIC decode path and late-ack redelivery had been live since 2026-10-26 and 2026-10-17. Nothing crashed until this tiny, apparently corrupt HEIC input arrived. The trigger is the input, not a recent deploy or config change.

Changes before the incident, and what was concluded about each:

- **ruled out** — fivo-worker deploy dep-0414 (sha b2f31f8a, 'summary: send the daily summary at 02:30 UTC') (2026-11-09T03:50:00Z): It only changes the daily summary schedule, about 25 hours before the onset. The queue stayed at 0 for hours afterwards, and the crash loop is in process_receipt/decode_image. [deploy:fivo-worker:0001, commit:fivo-worker:0001, metric:fivo-worker:0001]
- **ruled out** — Configuration, env var, feature flag and infrastructure changes for any service (2026-11-09T00:00:00Z): No config changes were recorded from 2026-11-09T00:00Z to 2026-11-10T06:10Z. [config:config_changes:0001]

## Hypotheses, ranked

1. **A malformed HEIC receipt (4363) segfaults libheif in decode_image. Redelivery via acks_late and reject_on_worker_lost creates an endless crash loop that blocks the queue.** (fivo-worker, code_regression) — most_likely, confidence 0.90
   - Supports: The first SIGSEGV restart came at 04:30:11Z. The same job 8f182553 was received at 04:30:10Z, 04:30:38Z, 04:31:05Z and 04:31:32Z, each time followed by a SIGSEGV restart. [log:fivo-worker:0031, log:fivo-worker:0035, log:fivo-worker:0036, log:fivo-worker:0037, log:fivo-worker:0038]
   - Supports: 219 SIGSEGV restarts and 219 receipts of the same job in the window. The jobs row shows status=started, attempts=219. [logagg:fivo-worker:0001, db:jobs:0001]
   - Supports: Receipt 4363 is a 131-byte image/heic file, created at 04:30:10Z and still 'uploaded'. HEIC receipts are tiny or corrupt only in this case: the minimum HEIC size is 131 bytes. [db:receipts:0001, db:receipts:0003]
   - Supports: The HEIC branch uses the native libheif decoder and has no guard. Late acks, reject_on_worker_lost and a 600s visibility timeout cause redelivery. [code:fivo-worker:0002, diff:fivo-worker:0002, codehit:fivo-worker:0003]
   - Supports: Queue depth was 0 until 04:20 and left its baseline at 04:30 (19.5), rising to 308.9 by 06:00. [metric:fivo-worker:0001]
   - Contradicts: Memory is flat at about 235-252 MB, so this is not an OOM or a leak. This fits a segfault instead. [metric:fivo-worker:0002]
   - Contradicts: The raw 131-byte object and the segfault stack were not inspected, so corruption as the trigger is inferred.
2. **The 2026-11-09 deploy dep-0414 (daily summary schedule) caused the failures.** (fivo-worker, code_regression) — ruled_out, confidence 0.03
   - Supports: It is the only deploy in the preceding day. [deploy:fivo-worker:0001]
   - Contradicts: It was deployed about 25 hours before onset and only changed the summary schedule. The queue was 0 for hours after it, and the crash loop is tied to process_receipt. [metric:fivo-worker:0001, logagg:fivo-worker:0001]
3. **Upstream dependency failure (Groq, Anthropic, Voyage, Redis quota or R2).** (upstash-redis, dependency_failure) — ruled_out, confidence 0.04
   - Contradicts: The platform logs show exit code 139 (SIGSEGV) rather than upstream errors. The error logs in the window are only these restarts. categorize_transaction tasks still succeed when the instance is up. [logagg:fivo-worker:0001, logagg:fivo-worker:0002, logagg:fivo-worker:0003]
4. **Resource exhaustion (memory or CPU) on the 512 MB instance.** (fivo-worker, resource_exhaustion) — ruled_out, confidence 0.03
   - Contradicts: Memory stayed flat at about 235-252 MB, well under 512 MB. [metric:fivo-worker:0002]

## Facts

- Queue depth was 0 through 04:20Z, began rising at 04:30Z, and reached 308.9 at 06:00Z. The alert level shift was at 05:50Z. [metric:fivo-worker:0001]
- The first SIGSEGV platform error was at 04:30:11Z, one second after process_receipt job 8f182553-20a6-d4ec (user 154) was received. Restarts then recurred roughly every 27s. [log:fivo-worker:0031, log:fivo-worker:0032, log:fivo-worker:0033, log:fivo-worker:0034, log:fivo-worker:0035, log:fivo-worker:0036]
- In the window there were 219 'Task process_receipt[8f182553-20a6-d4ec] received' lines, 219 SIGSEGV exits and 218 'celery ready' lines. None of these appeared in the baseline. [logagg:fivo-worker:0001]
- Before 04:30 process_receipt tasks succeeded in 6-9s. The previous crash-free baseline had no errors. [log:fivo-worker:0003, log:fivo-worker:0006, log:fivo-worker:0012, logagg:fivo-worker:0002]
- Receipt 4363 (user 154) is a 131-byte image/heic created at 04:30:10Z with status 'uploaded'. Jobs for it show attempts=219 and status=started. [db:receipts:0001, db:jobs:0001]
- Since 04:00Z, 103 receipts are 'uploaded' and 30 'processed'. HEIC receipts: 7 processed and 15 uploaded. [db:receipts:0002, db:receipts:0003]
- decode_image sends HEIC through pillow_heif.read_heif (native libheif) without validation. This was added in commit a5d51d17 on 2026-10-26. [code:fivo-worker:0002, diff:fivo-worker:0002, commit:fivo-worker:0003]
- The app config uses task_acks_late=True, task_reject_on_worker_lost=True and a 600s visibility_timeout. The late-ack commit 159d3d0f (2026-10-17) added the redelivery behaviour. [codehit:fivo-worker:0003, commit:fivo-worker:0004]
- No config changes were recorded in the day before the incident. The only deploy was dep-0414 (2026-11-09T03:50Z). [config:config_changes:0001, deploy:fivo-worker:0001]

## Assumptions

- The 131-byte HEIC object is truncated or corrupt and the libheif crash is triggered by its content. I did not read the R2 object, and no native stack trace is available.
- Receipt 4363's job is the only poison task. A crash loop on one job explains the whole backlog, but I did not check whether other malformed receipts are queued.
- The HEIC code path has been in production since Oct 26, and earlier HEIC receipts (7 processed since 04:00) decoded fine. So the bad input is the new element, not the code.

## Recommended actions

- [immediate] Stop the crash loop. Revoke or purge task 8f182553-20a6-d4ec from the Redis broker and mark receipt 4363 and the job as failed. Then restart the worker and confirm the queue drains. — This one task is redelivered after each crash, so the queue can't drain until it is removed. [db:jobs:0001, logagg:fivo-worker:0001]
- [immediate] Re-queue the receipts stuck in 'uploaded' (103 since 04:00Z, including user 154's later ones 4406 and 4431) once the worker is healthy. Check any other malformed HEIC objects first. — Receipts stay in 'uploaded' and are not processed until the worker recovers. [db:receipts:0002, db:receipts:0001]
- [soon] Harden decode_image. Validate the size and header of HEIC input, and decode untrusted images in a subprocess or sandbox so a native crash fails only that task. Reject or flag very small uploads at the API. — A native-library segfault must not kill the whole worker process. [code:fivo-worker:0002]
- [soon] Bound redelivery. Add a per-task delivery or attempt limit, for example by checking jobs.attempts before the decode and dead-lettering after a few tries. Alert on repeated SIGSEGV restarts and on attempts above 3. — With acks_late and reject_on_worker_lost, a crashing task is retried without limit (219 attempts here). [codehit:fivo-worker:0003, db:jobs:0001]
- [later] Review why the API accepted a 131-byte HEIC upload, and separate the queues so receipt processing cannot starve categorize_transaction. — This reduces the blast radius of a bad upload. [db:receipts:0001]

## Missing evidence

- The contents of the R2 object receipts/154/9557c58debd5cbb8ab8e and any native crash dump or stack trace from libheif.
- Per-task queue contents in Redis, so other poison tasks could not be ruled out.
- The deploy history for 2026-10-26 (HEIC release). I only have the code commit, not a deploy record, and deploys were listed for only the 2026-11-09 to 2026-11-10 window.
- The full list of platform log lines: log results were capped or partial.

## Run

- system: single_agent
- termination: agent finished
- tool calls: 26
- tool failures: 0
- guardrail rejections: 1
- llm calls: 11
- total tokens: 283215
- cost usd: 0.1772
- latency s: 63.8
- citations: 64 checked, 0 invalid, 0 facts demoted to assumptions
