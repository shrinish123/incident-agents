# Incident report: fivo-api: 5xx rate above 5% for 10 minutes

**Status:** conclusive

Deploy dep-0414 (ef5484ec, 08:20Z) shortened the idempotency key from `user_id:device_id:client_seq` to `user_id:client_seq`. `client_seq` is only unique per device, so a user with two devices produces the same key from both. The second insert hits the unique constraint `uq_transactions_idempotency_key`. The IntegrityError is unhandled and returns 500 on POST /transactions and POST /transactions/quick-add. Roll back to 7bc71c84, or restore the device_id in the key. Check afterwards for rows saved under the short key format.

## Root cause

- Service: fivo-api
- Component: app/services/idempotency.py build_key (idempotency key format) with unique constraint uq_transactions_idempotency_key
- Category: code_regression

Commit ef5484ec removed device_id from the idempotency key, on the assumption that client_seq alone identifies an entry. client_seq is a per-device counter, so two devices of one user can send the same client_seq and get the same key. The insert then violates uq_transactions_idempotency_key, and the unhandled IntegrityError becomes a 500 on the transaction create routes.

## What changed (why now)

The idempotency key format dropped device_id, so it is no longer unique across a user's devices. (at 2026-11-10T08:20:00Z)

- Before: Keys were user_id:device_id:client_seq. Before the deploy there was 1 5xx in the 08:00 bucket and none in 08:10, and user 145's rows had unique keys such as 145:and-c065:395 and 145:ios-4072:398. [logagg:fivo-api:0001, db:transactions:0001]
- After: Keys are user_id:client_seq. Multi-device users collide, giving 87 UniqueViolation 500s between 08:20 and 09:02. There were 21 5xx in the 08:30 bucket and 22 in the 08:40 bucket. [logagg:fivo-api:0002, logagg:fivo-api:0001, diff:fivo-api:0001]

Changes before the incident, and what was concluded about each:

- **cause** — fivo-api deploy dep-0414, ef5484ec: idempotency keys shortened to user_id:client_seq (2026-11-10T08:20:00Z): The key change creates cross-device collisions on uq_transactions_idempotency_key. Errors start right after the deploy and match the constraint name. [deploy:fivo-api:0001, diff:fivo-api:0001, logagg:fivo-api:0002, db:transactions:0001]
- **ruled out** — Configuration, env var and feature flag changes, any service (2026-11-09T06:00:00Z to 2026-11-10T09:02:00Z): No config changes were recorded in the window. [config:config_changes:0001]

## Hypotheses, ranked

1. **The dep-0414 change to build_key drops device_id from the idempotency key. Multi-device users collide on the unique constraint, which causes the 500s.** (fivo-api, code_regression) — most_likely, confidence 0.95
   - Supports: The only change in the deploy is the build_key return value, from user:device:seq to user:seq. [diff:fivo-api:0001, deploy:fivo-api:0001]
   - Supports: Of the 89 5xx lines from 08:20 onward, 87 are the UniqueViolation on uq_transactions_idempotency_key (78 on /transactions, 9 on quick-add). The 5xx rate rises after the 08:20 deploy. [logagg:fivo-api:0002, log:fivo-api:0001]
   - Supports: User 145 has 2 devices. Rows before 08:20 carry the old key format. After 08:20 the keys are `145:397`, `145:398` and so on, and the same client_seq 398 was already used by the other device (ios-4072) under the old format. The failing requests come from device and-c065. [db:transactions:0001, db:users:0001, log:fivo-api:0001]
   - Supports: The route calls build_key(user.id, body.device_id, body.client_seq). find_replay takes device_id, but no handler catches IntegrityError. [code:fivo-api:0001]
2. **A dependency problem (Redis, worker or Postgres connectivity) caused the failures.** (neon-postgres, dependency_failure) — ruled_out, confidence 0.03
   - Supports: There are 2 ConnectionResetError 500s, one on /categories and one on /transactions. [logagg:fivo-api:0002]
   - Contradicts: The ConnectionResetError count is negligible next to the 87 UniqueViolation errors, and the errors line up with the deploy. [logagg:fivo-api:0002]
3. **A config or feature flag change caused the failures.** (fivo-api, config_change) — ruled_out, confidence 0.01
   - Contradicts: No config changes were recorded in the 24 hours before the incident. [config:config_changes:0001]

## Facts

- Deploy dep-0414 to fivo-api went live at 2026-11-10T08:20:00Z (ef5484ec, previous 7bc71c84). Its message was 'idempotency: shorter keys, client_seq already identifies the entry'. [deploy:fivo-api:0001]
- The diff changes build_key from f"{user_id}:{device_id}:{client_seq}" to f"{user_id}:{client_seq}". [diff:fivo-api:0001]
- 5xx responses start after 08:20 on /transactions and /transactions/quick-add. In the visible buckets there are 3 and 6 at 08:20, 21 at 08:30 and 22 at 08:40, up from 1 at 08:00. The counts are partial and so are lower bounds. [logagg:fivo-api:0001]
- From 08:20 to 09:02 there were 89 5xx lines. 87 were IntegrityError UniqueViolation on uq_transactions_idempotency_key (78 on /transactions, 9 on quick-add). [logagg:fivo-api:0002]
- User 145 (device_count=2) has both old-format and new-format keys. Client_seq 398 was saved by ios-4072 as '145:ios-4072:398' at 08:18 and by and-c065 as '145:398' at 08:24, which shows client_seq values overlap across devices. [db:transactions:0001, db:users:0001]
- The sampled failing requests for users 145 and 113 are repeated retries from a single device each. [log:fivo-api:0001, log:fivo-api:0002, log:fivo-api:0003, log:fivo-api:0004, log:fivo-api:0005]
- No config changes were found in the day before the incident. [config:config_changes:0001]

## Assumptions

- The collisions are between different devices of the same user. I did not query the row that held the colliding key for each failure.
- The mobile client keeps retrying failed saves, which explains the bursts of repeated 500s from one device. This is inferred from the repeated log lines.
- The failing share of POST traffic is assumed to be mostly multi-device users. I did not query the proportion.

## Recommended actions

- [immediate] Roll back fivo-api to 7bc71c84, or hotfix build_key to include device_id again. Deploy it. — This removes the key collisions and stops the 500s on /transactions and /transactions/quick-add. [diff:fivo-api:0001, logagg:fivo-api:0002]
- [soon] Find rows saved with the short key `user:seq` after 08:20 (for example 145:397, 145:398, 145:399, 145:400). Check how they interact with the old-format keys. Work out which entries the apps failed to save, and ask affected users to re-sync or retry. — If the rollback restores the old format, old and new key formats coexist. Rows written with the short key may collide with later entries, or may lead to duplicates. Support has reports of unsaved entries. [db:transactions:0001]
- [later] Handle IntegrityError on idempotency_key. Return the existing row or a 409 rather than a 500. Add a test with two devices that use the same client_seq. Add a unit test for the key format. — The current tests only cover a single device. Nothing catches the unique violation, so it surfaces as a 500. [code:fivo-api:0001, codehit:fivo-api:0001]

## Missing evidence

- I did not measure the overall 5xx percentage per minute. The log counts were partial.
- I did not fetch the row that held the conflicting key for each failure.
- I did not query metrics or traces. The logs and database were enough to settle the cause.

## Run

- system: single_agent
- termination: agent finished
- tool calls: 13
- tool failures: 0
- guardrail rejections: 1
- llm calls: 6
- total tokens: 104386
- cost usd: 0.1114
- latency s: 41.5
- citations: 38 checked, 0 invalid, 0 facts demoted to assumptions
