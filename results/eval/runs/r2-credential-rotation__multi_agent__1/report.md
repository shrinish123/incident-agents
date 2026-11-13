# Incident report: fivo-api: no receipt processed in the last 10 minutes

**Status:** conclusive

The R2 access key was rotated at 09:23Z (****a91f to ****7c2e, cfg-0139) and applied to fivo-api at 09:25Z (cfg-0142), but no change was recorded for fivo-worker. The old key ****a91f was revoked at 09:53Z when its 30-minute grace period ended (cfg-0145). The worker builds its boto3 client once at import, so it most likely kept the old key. From 09:54:22Z every process_receipt failed with R2 GetObject 403, and 27 such errors ran through 10:25Z, so no receipt reached 'processed'. Uploads still return 202 because fivo-api holds the new key. The one unobserved link is the worker's actual key value, so that step is inferred.

## Root cause

- Service: fivo-worker
- Component: R2_ACCESS_KEY_ID in the worker environment (boto3 client built at import in worker/clients/storage.py)
- Category: config_change

The R2 key rotation was applied to r2-storage and fivo-api but not to fivo-worker. The worker's cached client kept the old key ****a91f, which was revoked at 09:53Z, so its GetObject calls started returning 403 at 09:54:22Z and process_receipt never reaches mark_receipt('processed').

## What changed (why now)

The old R2 key a91f was revoked by the system at the end of its 30-minute grace period. The rotation had been applied to r2-storage and fivo-api but not to fivo-worker, so the worker's credential became invalid. (at 2026-11-10T09:53:00Z)

- Before: The old key a91f was still valid through its grace period. process_receipt tasks succeeded in 6.5-9.6s, the last at 09:51:48Z, and there were no worker errors before 09:54. [config:r2-storage:0001, log:fivo-worker:0010, logagg:fivo-worker:0008]
- After: a91f was revoked at 09:53Z. From 09:54:22Z every process_receipt failed with GetObject 403, with 27 such errors through 10:25Z. Tasks that do not use R2 kept succeeding. [config:r2-storage:0002, log:fivo-worker:0012, logagg:fivo-worker:0006, logagg:fivo-worker:0008]
- Why it did not fail earlier: The worker's stale credential was already in place from 09:23Z. It stayed hidden because the old key remained valid during the 30-minute grace period, and it only failed once the key was revoked.

Changes before the incident, and what was concluded about each:

- **contributing** — config on r2-storage: R2_ACCESS_KEY_ID: ****a91f -> ****7c2e by dashboard:maya ("quarterly rotation; the previous key stays valid for 30 minutes") (2026-11-10T09:23:00Z, 32 min before onset): The 09:23Z rotation started the 30-minute grace clock that ended in the revocation. Because the worker was not updated, the rotation set up the failure. On its own it caused no errors, since the old key stayed valid. [config:r2-storage:0001, config:fivo-worker:0001]
- **contributing** — config on fivo-api: R2_ACCESS_KEY_ID: ****a91f -> ****7c2e by dashboard:maya ("rotated storage key") (2026-11-10T09:25:00Z, 30 min before onset): fivo-api received 7c2e at 09:25Z while the worker did not, making the rotation partial. This is why uploads kept succeeding while worker reads failed. The change itself did not fail: fivo-api returned only 2xx afterwards. [config:fivo-api:0001, logagg:fivo-api:0004, code:fivo-api:0001]
- **cause** — config on r2-storage: R2_ACCESS_KEY_ID: ****a91f -> (revoked) by system ("grace period for the previous key ended") (2026-11-10T09:53:00Z, 2 min before onset): The old key a91f was revoked at 09:53Z. The last worker success was at 09:51:48Z and the first 403 at 09:54:22Z, and no errors came earlier. This is the event that turned the stale worker credential into failures. [config:r2-storage:0002, log:fivo-worker:0010, log:fivo-worker:0012]

## Hypotheses, ranked

1. **fivo-worker still uses the old R2 key ****a91f, revoked at 09:53Z, so every R2 GetObject from process_receipt and embed_pending_receipts returns 403.** (fivo-worker, config_change) — most_likely, confidence 0.85
   - Supports: The rotation hit r2-storage at 09:23Z and fivo-api at 09:25Z; no config change exists for fivo-worker. The old key was revoked at 09:53Z. [config:r2-storage:0001, config:fivo-api:0001, config:fivo-worker:0001, config:r2-storage:0002]
   - Supports: The last process_receipt success was at 09:51:48Z, the first 403 at 09:54:22Z, with no interleaving. Every later process_receipt in the fetched window failed. [log:fivo-worker:0010, log:fivo-worker:0012, log:fivo-worker:0013, log:fivo-worker:0016, logagg:fivo-worker:0008]
   - Supports: The worker reads its R2 credentials once at import into a module-level boto3 client, with no refresh path. process_receipt calls get_object first. [code:fivo-worker:0001, code:fivo-worker:0002, codehit:fivo-worker:0003, code:fivo-worker:0003]
   - Supports: All 27 errors are GetObject 403. Tasks that do not use R2 kept succeeding, and POST /receipts kept returning 202. [logagg:fivo-worker:0006, logagg:fivo-worker:0005, logagg:fivo-api:0001]
   - Contradicts: The worker's actual key value and start time were not readable. The log lines carry no key ID, so the match to the revocation rests on timing and the absence of a config change. [config:fivo-worker:0001, log:fivo-worker:0012]
2. **The new key ****7c2e is under-scoped for reads, so GetObject returns 403 for every service once the old key was revoked.** (r2-storage, config_change) — ruled_out, confidence 0.04
   - Supports: fivo-api shows no R2 reads, so the new key's read permission was never tested directly. [logagg:fivo-api:0002, trace:fivo-api:0001]
   - Contradicts: The rotation record shows only a key ID change, with no scope change. The worker succeeded until 09:51:48Z, so it was not on 7c2e, and no worker config change exists. [config:r2-storage:0001, config:fivo-worker:0002, log:fivo-worker:0010]
   - Contradicts: fivo-api, on 7c2e since 09:25Z, returned only 2xx after the revocation. [logagg:fivo-api:0004, config:fivo-api:0001]
3. **An independent R2 bucket policy, token or outage change starting around 09:50 caused the failures, with the revocation coincidental.** (r2-storage, infrastructure) — ruled_out, confidence 0.02
   - Contradicts: The apparent 09:50 error is the 09:54:22Z 403, which falls in the 09:50-09:55 bucket. There were no errors from 09:40 to 09:54, and successes ran cleanly until 09:51:48Z. [log:fivo-worker:0012, logagg:fivo-worker:0007, logagg:fivo-worker:0008, log:fivo-worker:0010]

## Facts

- r2-storage R2_ACCESS_KEY_ID was rotated at 09:23Z (cfg-0139, ****a91f to ****7c2e, by dashboard:maya). The note says the previous key stays valid for 30 minutes. [config:r2-storage:0001]
- fivo-api R2_ACCESS_KEY_ID was changed to ****7c2e at 09:25Z (cfg-0142). [config:fivo-api:0001]
- The old key ****a91f was revoked by the system at 09:53Z (cfg-0145) when its grace period ended. [config:r2-storage:0002]
- No config changes were recorded for fivo-worker between 06:00Z and 10:25Z, and there were no deploys for any service in that period. [config:fivo-worker:0001, deploy:deploys:0001]
- The last process_receipt success was at 09:51:48Z (8.18s). The first GetObject 403 was at 09:54:22Z. [log:fivo-worker:0010, log:fivo-worker:0012]
- fivo-worker logged 27 errors from 09:50 to 10:25Z, all GetObject 403 Forbidden: 23 from process_receipt and 4 from embed_pending_receipts. The result may be partial, so the counts are lower bounds. [logagg:fivo-worker:0004, logagg:fivo-worker:0006]
- The worker is alive and consuming the queue. categorize_transaction tasks, which do not read R2, kept succeeding. [logagg:fivo-worker:0002, logagg:fivo-worker:0005, logagg:fivo-worker:0008]
- fivo-api kept returning 202 on POST /receipts and 2xx on all routes. This result is partial, so counts are lower bounds. [logagg:fivo-api:0001, logagg:fivo-api:0004]
- The worker builds a module-level boto3 client at import from env vars, with no refresh or per-task reload. process_receipt calls get_object before mark_receipt('processed'). [code:fivo-worker:0001, code:fivo-worker:0002, codehit:fivo-worker:0003, code:fivo-worker:0003]

## Assumptions

- The worker's runtime R2_ACCESS_KEY_ID is still ****a91f. This is inferred from the absence of a config change plus the code, not read directly.
- The worker process has not restarted since before 09:25Z.
- Code read at main matches what is running, since there were no deploys.
- The match between the 403s and the 09:53Z revocation rests on timing, because log lines do not carry the key ID.
- fivo-api holds a valid key because it keeps returning 2xx. No fivo-api R2 read was observed.

## Recommended actions

- [immediate] Set fivo-worker R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY to the new key (****7c2e) and restart the worker so its boto3 client reloads. — The worker caches credentials at import and needs a restart to pick up the rotated key. A restart is the only way to recover. [code:fivo-worker:0001, code:fivo-worker:0002, config:r2-storage:0002]
- [immediate] After the restart, confirm a process_receipt succeeds and a GetObject returns 200. Then re-queue or re-run the receipts that failed since 09:54Z, including embed_pending_receipts. — This confirms the mechanism, rules out an under-scoped 7c2e (H2), and clears the backlog of unprocessed receipts. [logagg:fivo-worker:0006, log:fivo-worker:0012]
- [soon] Make the worker pick up rotated credentials without a restart, for example by reading the secret per task or rebuilding the client on a 403. — The client is built once at import with no refresh path. [code:fivo-worker:0001, codehit:fivo-worker:0003]
- [later] Add a rotation checklist that lists every consumer (fivo-api and fivo-worker) and require confirmation before the grace period ends. Alert on a spike of R2 403s in the worker. — The rotation was applied to only two of the three places. The first signal was the alert at 10:05Z, about 12 minutes after the revocation. [config:r2-storage:0001, config:fivo-api:0001, config:fivo-worker:0001]

## Missing evidence

- The worker's actual runtime R2_ACCESS_KEY_ID (last 4 characters) and process start time could not be read.
- Token permission and bucket scope of ****7c2e and ****a91f are not exposed by the config tool.
- The fivo-api and early fivo-worker log queries were partial, so their counts are lower bounds.
- No fivo-api R2 read was observed (no R2 log lines, no GetObject trace spans).
- Metrics (queue depth, dependency latency) were not checked.
- Code was read at main, not at a deployed SHA.
- Only one past-incident search phrase was tried.

## Run

- system: multi_agent
- termination: accepted
- rounds: 1
- hypotheses: 3
- steps: 8
- max hops: 3
- tool calls: 36
- tool failures: 0
- guardrail rejections: 0
- llm calls: 34
- total tokens: 461642
- cost usd: 0.5429
- latency s: 925.2
- citations: 72 checked, 0 invalid, 0 facts demoted to assumptions
