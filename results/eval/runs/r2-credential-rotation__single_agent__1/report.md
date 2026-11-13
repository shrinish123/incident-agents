# Incident report: fivo-api: no receipt processed in the last 10 minutes

**Status:** conclusive

Receipts stopped reaching 'processed' because fivo-worker is still using the old R2 access key. The key was rotated at 09:23, and fivo-api was updated at 09:25. No config change for fivo-worker is recorded. The old key's 30-minute grace period ended at 09:53. From 09:54:22 every process_receipt task failed with 403 Forbidden on R2 GetObject. Fix: set the new R2_ACCESS_KEY_ID (****7c2e) in fivo-worker's environment and restart it. Then re-queue the receipts that failed. One gap: the worker's environment was not inspected directly, so the stale key is inferred.

## Root cause

- Service: fivo-worker
- Component: R2_ACCESS_KEY_ID in the fivo-worker environment (not updated after the R2 key rotation cfg-0139)
- Category: config_change

The quarterly R2 key rotation at 09:23 changed the key and updated fivo-api's copy at 09:25. The worker's separate copy was not updated, and no worker config change is recorded. When the old key was revoked at 09:53, the worker's GetObject calls began returning 403. process_receipt and embed_pending_receipts then failed, so no receipt reached 'processed' while uploads through the api still succeeded.

## What changed (why now)

The old R2 access key was revoked at the end of the rotation grace period. fivo-api had the new key, and fivo-worker apparently did not. (at 2026-11-10T09:53:00Z)

- Before: The worker authenticated to R2 successfully. process_receipt succeeded, for example in 8.18s at 09:51:48, and the old key was still valid. [log:fivo-worker:0017, config:r2-storage:0001]
- After: From 09:54:22 process_receipt and embed_pending_receipts fail with 403 Forbidden on GetObject. [log:fivo-worker:0001, log:fivo-worker:0006, logagg:fivo-worker:0002]
- Why it did not fail earlier: The rotation at 09:23 left the old key valid for a 30-minute grace period, so the stale key in the worker kept working until revocation at 09:53.

Changes before the incident, and what was concluded about each:

- **contributing** — r2-storage R2_ACCESS_KEY_ID rotation (cfg-0139) (2026-11-10T09:23:00Z): It started the rotation. The new key was not propagated to the worker, so the change set up the failure, but the revocation triggered it. [config:r2-storage:0001]
- **ruled out** — fivo-api env var R2_ACCESS_KEY_ID updated (cfg-0142) (2026-11-10T09:25:00Z): The api got the new key and continues to accept uploads. The api is not the failing component. [config:fivo-api:0001, logagg:fivo-api:0001]
- **cause** — r2-storage old key revoked, grace period ended (cfg-0145) (2026-11-10T09:53:00Z): The first 403 came at 09:54:22, a minute after revocation. Before it, the worker succeeded using the old key. [config:r2-storage:0002, log:fivo-worker:0001]
- **ruled out** — Code deploys in the prior 24h (2026-11-09T08:00:00Z): No deploys were found. [deploy:deploys:0001]
- **contributing** — fivo-worker config changes (2026-11-10T09:23:00Z): None were recorded, so the worker kept the old key. This is the omission that led to the failure. [config:fivo-worker:0001]

## Hypotheses, ranked

1. **fivo-worker still holds the revoked old R2 key, so its R2 GetObject calls return 403.** (fivo-worker, config_change) — most_likely, confidence 0.92
   - Supports: The rotation happened at 09:23 and the old key was revoked at 09:53. fivo-api's env var was updated at 09:25. [config:r2-storage:0001, config:fivo-api:0001, config:r2-storage:0002]
   - Supports: The first 403 on process_receipt came at 09:54:22, a minute after revocation. Successes were logged up to 09:51:48. [log:fivo-worker:0001, log:fivo-worker:0017]
   - Supports: embed_pending_receipts also fails with 403 GetObject, which fits a worker-wide credential problem. [log:fivo-worker:0006]
   - Supports: Errors continue every 5-minute bucket through 10:20 and are 1 to 6 per bucket. Each error bucket also has some info lines, which were not inspected. [logagg:fivo-worker:0002]
   - Supports: No fivo-worker config changes are recorded, which is consistent with the worker's key being left stale. [config:fivo-worker:0001]
   - Contradicts: The worker's actual env value was not observed. [config:fivo-worker:0001]
2. **Groq 429 rate limiting is blocking processing.** (groq-llm, dependency_failure) — ruled_out, confidence 0.04
   - Supports: Groq 429 retries appeared at 09:20-09:27. [log:fivo-worker:0009, log:fivo-worker:0011]
   - Contradicts: The retries affect categorize_transaction, not process_receipt. They are only 3 warnings, before the failure window, and they back off and retry after 1s. [log:fivo-worker:0009]
   - Contradicts: Receipts kept succeeding until 09:51:48, after the 429s. [log:fivo-worker:0017]
3. **A code deploy caused the failure.** (fivo-api, code_regression) — ruled_out, confidence 0.02
   - Contradicts: No deploys were found in the 24h before the incident. [deploy:deploys:0001]

## Facts

- The R2 access key was rotated at 09:23 and the old key stayed valid for 30 minutes. [config:r2-storage:0001]
- fivo-api's R2_ACCESS_KEY_ID was updated at 09:25 to ****7c2e. [config:fivo-api:0001]
- The old key was revoked at 09:53. [config:r2-storage:0002]
- The last successful process_receipt was at 09:51:48. [log:fivo-worker:0017]
- The first process_receipt 403 came at 09:54:22 (ClientError 403 on GetObject). [log:fivo-worker:0001]
- embed_pending_receipts also failed with 403 on GetObject at 09:58:00. [log:fivo-worker:0006]
- No fivo-worker config change is recorded from 11-01 to 11-10, and the topology says each service keeps its own copy of the R2 key. [config:fivo-worker:0001, topology:r2-storage:0001]
- Worker errors are continuous from the 09:50 bucket to 10:20. process_receipt error counts per 5-minute bucket are 1, 6, 5, 4, 1, 4 and 3. [logagg:fivo-worker:0001, logagg:fivo-worker:0002]
- POST /receipts returns 202 on the api side, so uploads succeed. [logagg:fivo-api:0001]

## Assumptions

- The worker's R2_ACCESS_KEY_ID still holds the old value ****a91f. This is inferred from the missing config change and the timing, not read from the worker's environment.
- The 403s come from the revoked credential rather than a bucket policy change. No other R2 change is recorded.
- Each process_receipt error bucket also has some info lines. The failure is assumed to cover all receipts, but those lines were not inspected.

## Recommended actions

- [immediate] Set R2_ACCESS_KEY_ID to the new key (****7c2e) in fivo-worker's environment, together with the matching secret if it was also rotated. Restart the worker. — Restores worker access to R2. [config:r2-storage:0001, log:fivo-worker:0001]
- [immediate] Re-queue or re-run process_receipt for receipts uploaded since about 09:53 that are not in 'processed'. Confirm that embed_pending_receipts recovers. — Tasks that failed with 403 were not completed. [logagg:fivo-worker:0002, log:fivo-worker:0006]
- [soon] Add a credential rotation checklist covering every service that holds the key (api and worker). Alert on 403 from R2 and on process_receipt failure rate. — The api copy was updated and the worker copy was missed. The failure showed up only when the grace period ended. [topology:r2-storage:0001, config:r2-storage:0002]

## Missing evidence

- The worker's actual environment variable value and the time of its last restart. No config record exists for it.
- Database state of receipts (pending vs processed), which was not queried.
- Full list of failed jobs: the raw log fetch was capped and partial.
- The info lines in the error buckets, to confirm that receipts fail consistently after revocation.

## Run

- system: single_agent
- termination: agent finished
- tool calls: 11
- tool failures: 0
- guardrail rejections: 1
- llm calls: 6
- total tokens: 104485
- cost usd: 0.098
- latency s: 44.9
- citations: 43 checked, 0 invalid, 0 facts demoted to assumptions
