# Incident report: fivo-api: 5xx rate above 5% for 10 minutes

**Status:** conclusive

At 08:20:00Z deploy dep-0414 (commit ef5484e, 'idempotency: shorter keys') went live in fivo-api. It changed build_key from user_id:device_id:client_seq to user_id:client_seq. client_seq is a per-device counter, so a user's second device can now produce a key that already exists. That insert violates the global constraint uq_transactions_idempotency_key, and neither write route catches IntegrityError, so POST /transactions and POST /transactions/quick-add return 500. Roll back to 7bc71c8 now. The one inferred link is that the colliding rows come from another device; no failing request's key has been paired with an existing row.

## Root cause

- Service: fivo-api
- Component: app/services/idempotency.py build_key (dep-0414, ef5484e), with unique constraint uq_transactions_idempotency_key and unhandled IntegrityError on the write routes
- Category: code_regression

The deploy dropped device_id from the idempotency key, while the replay check find_replay is still device-scoped and the DB constraint is global. Cross-device seq overlap for the same user now collides and surfaces as an unhandled UniqueViolation, returned as a 500.

## What changed (why now)

The idempotency key construction dropped device_id, so keys are now unique per user and seq instead of per user, device and seq. (at 2026-11-10T08:20:00Z)

- Before: Rows saved before 08:20 carry three-part keys such as 113:and-4cf0:351 and 132:ios-7ed8:254. The baseline hour had 1 500 on /transactions and none on quick-add. [db:transactions:0001, logagg:fivo-api:0001]
- After: All 111 rows saved from 08:20:20 to 09:01:52 use the short user:seq key, and none use the old format. The first sampled UniqueViolation is at 08:22:31. There are 79 500s on /transactions and 9 on quick-add, which was 0 in the baseline. [db:transactions:0002, db:transactions:0004, log:fivo-api:0007, logagg:fivo-api:0001]

Changes before the incident, and what was concluded about each:

- **cause** — deploy on fivo-api: commit ef5484e "idempotency: shorter keys, client_seq already identifies the entry" by github-actions (2026-11-10T08:20:00Z, 0 min before onset): Deploy dep-0414 (ef5484e) went live at 08:20:00Z, the same time as error onset. Its one-line diff drops device_id from the key. All post-deploy rows use the new key. The UniqueViolations on uq_transactions_idempotency_key began at 08:22 and there were none before. No other deploy or config change is recorded. [deploy:fivo-api:0001, diff:fivo-api:0001, db:transactions:0002, logagg:fivo-api:0002, config:config_changes:0001]

## Hypotheses, ranked

1. **Deploy dep-0414 (ef5484e) dropped device_id from the idempotency key, so keys collide across a user's devices and writes fail with an unhandled UniqueViolation (500).** (fivo-api, code_regression) — most_likely, confidence 0.90
   - Supports: Deploy went live at 08:20:00Z and is the only deploy; no config changes were recorded. [deploy:fivo-api:0001, config:config_changes:0001]
   - Supports: The diff touches one file and one line: build_key lost device_id. [changes:fivo-api:0001, diff:fivo-api:0001]
   - Supports: The global unique constraint exists, find_replay is device-scoped, and neither route handles IntegrityError. [codehit:fivo-api:0001, code:fivo-api:0001, code:fivo-api:0002]
   - Supports: Sampled error lines are UniqueViolation on uq_transactions_idempotency_key, first at 08:22:31; 75 of 77 errors in 08:20-09:00 are of this type. [log:fivo-api:0007, log:fivo-api:0008, log:fivo-api:0009, logagg:fivo-api:0012]
   - Supports: All 111 rows saved from 08:20:20 use the short key and none use the old format; pre-deploy rows use user:device:seq. [db:transactions:0002, db:transactions:0004, db:transactions:0001]
   - Supports: 10 post-08:20 rows across 9 users share user_id and client_seq with an earlier row from a different device, showing cross-device seq overlap is common. [db:transactions:0003]
   - Contradicts: No failing request's key was paired with an existing row from another device; logs carry neither client_seq nor the key. [log:fivo-api:0007]
   - Contradicts: Only 77 error lines were classified (08:20-09:00) against 89 error lines in the wider window; the type of the remainder is unchecked. [logagg:fivo-api:0002, logagg:fivo-api:0007]
   - Contradicts: 2 ConnectionResetError lines (one is GET /categories at 08:20:58) are unexplained but are not on the main failure path. [log:fivo-api:0006]
2. **A neon-postgres problem (suspended compute, pool exhaustion, slow or locked writes) is causing write timeouts, as in INC-2026-024.** (neon-postgres, infrastructure) — ruled_out, confidence 0.02
   - Supports: A past incident had a 5xx spike on POST /transactions from a suspended DB compute. [incident:fivo-api:0001]
   - Contradicts: 0 log lines matching QueuePool, OperationalError or connection refused from 08:20 to 09:00. [logagg:fivo-api:0008, logagg:fivo-api:0009, logagg:fivo-api:0010]
   - Contradicts: The 500s are fast (12-17 ms) UniqueViolations, so the database is up and rejecting duplicate keys. [log:fivo-api:0007, log:fivo-api:0008, log:fivo-api:0009]
3. **The Redis queue or fivo-worker path is failing, causing 500s on quick-add and transaction writes.** (upstash-redis, dependency_failure) — ruled_out, confidence 0.02
   - Supports: quick-add waits on the worker through the Redis queue. [topology:fivo-api:0001]
   - Contradicts: 0 lines matching 'redis' or 'timeout' in fivo-api from 08:20 to 09:00. [logagg:fivo-api:0004, logagg:fivo-api:0006]
   - Contradicts: fivo-worker logged no errors and had no deploys. [logagg:fivo-worker:0001, deploy:fivo-worker:0001]
   - Contradicts: The observed errors are Postgres unique violations. [logagg:fivo-api:0012]
4. **A client-side change (new app release, bad or missing client_seq) hits a latent write-path bug, independent of the deploy.** (fivo-api, traffic_change) — ruled_out, confidence 0.03
   - Supports: After 08:20 a user's saved rows show one device's seq run advancing with the other device's seqs missing. This fits rejected writes, and H1 explains it equally well. [db:transactions:0001]
   - Contradicts: Failures are spread across many iOS and Android devices, and several devices both succeed and fail in the same window. [logagg:fivo-api:0011]
   - Contradicts: client_seq runs consecutively per device across 08:20, with no resets or duplicates. [db:transactions:0001]
   - Contradicts: The key format switched exactly at the deploy time. [db:transactions:0002, db:transactions:0004]
   - Contradicts: A single server-side constraint conflict dominates the errors. [logagg:fivo-api:0012]

## Facts

- dep-0414 (ef5484ec, previous 7bc71c84) went live in fivo-api at 2026-11-10T08:20:00Z; it is the only deploy found, and no config changes were recorded 04:00-09:02. [deploy:fivo-api:0001, config:config_changes:0001]
- The deploy changes only app/services/idempotency.py, one line: the key lost device_id. [changes:fivo-api:0001, diff:fivo-api:0001]
- Neither POST /transactions nor quick-add catches IntegrityError, and the unique constraint on idempotency_key is global. [code:fivo-api:0001, codehit:fivo-api:0001]
- In 08:12-09:02, /transactions returned 79 500s (1 in baseline) and quick-add returned 9 500s with 17 201s (0 500s in baseline). [logagg:fivo-api:0001]
- Error logs begin in the 08:20 bucket and stay elevated through 09:00. [logagg:fivo-api:0002]
- Of 77 error lines in 08:20-09:00, 67 were on /transactions, 9 on quick-add and 1 on /categories; 75 are UniqueViolation on uq_transactions_idempotency_key and 2 are ConnectionResetError. [logagg:fivo-api:0007, logagg:fivo-api:0012]
- The first sampled UniqueViolation is at 08:22:31. The failures are fast and repeat from the same device, which looks like client retries. [log:fivo-api:0007, log:fivo-api:0008, log:fivo-api:0009]
- All 111 rows saved 08:20:20-09:01:52 (46 users) use the user:seq key; none use the old format. [db:transactions:0002, db:transactions:0004]
- There are 0 matches for QueuePool, OperationalError, timeout, connection refused or redis in fivo-api errors, and fivo-worker logged no errors. [logagg:fivo-api:0008, logagg:fivo-api:0009, logagg:fivo-api:0006, logagg:fivo-api:0010, logagg:fivo-api:0004, logagg:fivo-worker:0001]

## Assumptions

- The colliding existing rows come from a different device of the same user. This is inferred from the code and data; no failing request's key was paired with an existing row.
- The roughly 12 error lines outside the classified 08:20-09:00 window (between 09:00 and 09:02) are assumed to be the same failure.
- The 2 ConnectionResetError lines and the 1 baseline /transactions 500 are assumed to be unrelated noise.
- Neon, Redis and the external dependencies were not checked directly; they are assumed healthy because no log lines point to them.
- Exact onset is bounded by the 5-minute log buckets.

## Recommended actions

- [immediate] Roll back fivo-api to 7bc71c84 (revert ef5484e / dep-0414). — Restores the device-scoped key user:device:seq, which stops the new collisions. The 5xx started with the deploy at 08:20 and was still occurring at 09:00. [deploy:fivo-api:0001, diff:fivo-api:0001, logagg:fivo-api:0002]
- [soon] Ask support to tell the two affected mobile users to retry once the rollback is live. — Rejected inserts are rolled back, so those entries were not saved. [db:transactions:0002]
- [soon] Before re-shipping shorter keys, keep device_id in the key or make the replay check and constraint consistent. Catch IntegrityError on both write routes and return the existing entry or a 409, not a 500. — The replay check is device-scoped while the key is not, and the routes only flush and commit with no duplicate handling. [code:fivo-api:0001, code:fivo-api:0002, codehit:fivo-api:0001]
- [later] Add a test with one user on two devices sending the same client_seq, and an alert on UniqueViolation counts on uq_transactions_idempotency_key. — This would have caught the regression before or right after deploy; the first collision logged was 08:22. [log:fivo-api:0007, db:transactions:0003]
- [soon] Confirm the cause after rollback by checking that the UniqueViolation count drops to zero. — This closes the one inferred link, since no failing key has been paired with an existing row. [logagg:fivo-api:0012]

## Missing evidence

- No failing request's key was paired with an existing row from a different device_id; the last data step (S12) was skipped because the investigation was accepted. Logs carry neither client_seq nor the key, and rejected inserts are not stored.
- The exception type of about 12 error lines outside the classified 08:20-09:00 window was not checked.
- No metrics were queried for neon-postgres, upstash-redis or the external APIs, and no deploys were checked for them.
- App version is not in the logs (user_agent is empty).
- Several samples were partial or capped: device_id counts are lower bounds, transaction rows were capped at 50, and error line samples were limited.
- The 2 ConnectionResetError lines are unexplained.

## Run

- system: multi_agent
- termination: accepted
- rounds: 1
- hypotheses: 4
- steps: 9
- max hops: 3
- tool calls: 36
- tool failures: 0
- guardrail rejections: 0
- llm calls: 32
- total tokens: 363417
- cost usd: 0.7526
- latency s: 193.4
- citations: 80 checked, 0 invalid, 0 facts demoted to assumptions
