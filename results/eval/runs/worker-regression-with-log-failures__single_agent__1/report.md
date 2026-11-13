# Incident report: fivo-api: POST /transactions/quick-add 5xx above 20%

**Status:** conclusive

The fivo-worker deploy dep-0414 (sha 732a2a8a, 12:21Z) made `time_features()` call `ZoneInfo(txn.merchant_tz)`. Every manual transaction, including all quick-add ones, has `merchant_tz` NULL, so `categorize_transaction` now raises TypeError. Quick-add waits for the worker result, so the API fails validating the response and returns 500. Plain card transactions carry a timezone and are unaffected. Roll back fivo-worker to 994ebf05, or make `time_features` fall back to UTC when `merchant_tz` is NULL. Then re-queue categorisation for the failed jobs.

## Root cause

- Service: fivo-worker
- Component: worker/features.py time_features() (ZoneInfo(txn.merchant_tz)) in deploy dep-0414, sha 732a2a8a
- Category: code_regression

Deploy dep-0414 changed `time_features` to convert `created_at` with `ZoneInfo(txn.merchant_tz)` and has no handling for NULL. Manual/quick-add transactions always have a NULL `merchant_tz`, so `categorize_transaction` fails with TypeError and leaves them uncategorised. The API's quick-add waits on that result, hits a pydantic validation error because `category` is not a string, and returns 500.

## What changed (why now)

fivo-worker began converting transaction time to the merchant's local timezone via ZoneInfo(txn.merchant_tz). That value is NULL for every manual/quick-add transaction. (at 2026-11-10T12:21:00Z)

- Before: `time_features` used `created_at.hour` and never read `merchant_tz`. Manual transactions with a NULL tz were categorised normally: 189 jobs succeeded with no failures before the deploy. [diff:fivo-worker:0001, db:transactions:0003, db:jobs:0001]
- After: From 12:21:20Z, categorize_transaction raises TypeError for NULL-tz transactions. 57 jobs failed, and 57 of 58 NULL-tz transactions are uncategorised. Quick-add returns 500 on the pydantic validation error. [log:fivo-worker:0001, db:jobs:0001, db:transactions:0001, log:fivo-api:0001]
- Why it did not fail earlier: The NULL merchant_tz data for manual transactions already existed, but the old code never read the timezone. The new code first dereferenced it at the 12:21Z deploy.

Changes before the incident, and what was concluded about each:

- **cause** — fivo-worker deploy dep-0414, sha 732a2a8a, 'features: use the merchant's local time of day' (2026-11-10T12:21:00Z): It introduces ZoneInfo(txn.merchant_tz) with no NULL handling. Errors start 20 seconds after the deploy, and the failures map exactly to NULL-tz transactions. [deploy:fivo-worker:0001, diff:fivo-worker:0001, log:fivo-worker:0001, db:jobs:0001]
- **ruled out** — Configuration, environment variable and feature-flag changes (all services) (2026-11-09T12:00:00Z): The tool returned no config changes in the 24h window before the incident. [config:config_changes:0001]

## Hypotheses, ranked

1. **Worker deploy dep-0414 introduced a NULL-timezone crash in categorisation. Manual/quick-add transactions have a NULL merchant_tz, so categorisation fails and quick-add returns 500.** (fivo-worker, code_regression) — most_likely, confidence 0.95
   - Supports: The diff replaces `txn.created_at.hour` with `created_at.astimezone(ZoneInfo(txn.merchant_tz))`. `ZoneInfo(None)` raises exactly this TypeError. [diff:fivo-worker:0001]
   - Supports: Worker errors begin at 12:21:20Z, right after the 12:21Z deploy. The message is TypeError('expected str, bytes or os.PathLike object, not NoneType'). [deploy:fivo-worker:0001, log:fivo-worker:0001, logagg:fivo-worker:0001]
   - Supports: 57 jobs have failed with this TypeError and none have failed otherwise. 189 jobs succeeded. [db:jobs:0001]
   - Supports: Since 12:21Z, 57 of 58 NULL-tz transactions are uncategorised, while all transactions with a timezone are categorised. [db:transactions:0001]
   - Supports: All 58 manual-source transactions after the deploy have NULL tz. All 126 manual transactions before the deploy also had NULL tz, and card transactions always had a tz. [db:transactions:0002, db:transactions:0003]
   - Supports: The API quick-add 500 at 12:21:20Z has the same trace_id (802cc1a662e685cb) as the first worker failure. The error is a pydantic ValidationError because `category` is not a string. [log:fivo-api:0001, log:fivo-worker:0001]
   - Contradicts: The API also logged unrelated ConnectionResetError 500s on GET /summary and /transactions. These look like background noise (a connection reset) and do not explain the quick-add failures. I did not investigate them. [log:fivo-api:0002, log:fivo-api:0003]
2. **A dependency failure (Groq, Redis or Neon) is breaking categorisation.** (groq-llm, dependency_failure) — ruled_out, confidence 0.03
   - Contradicts: The failure is a local TypeError, not a timeout or an HTTP error. Jobs for transactions with a timezone succeed, which a dependency outage would not allow. [db:jobs:0001, db:transactions:0001]
3. **A configuration change caused the failures.** (fivo-api, config_change) — ruled_out, confidence 0.02
   - Contradicts: No config changes were recorded in the 24h before the incident. [config:config_changes:0001]

## Facts

- The only deploy in the prior day was fivo-worker dep-0414 at 12:21Z, 'features: use the merchant's local time of day'. [deploy:fivo-worker:0001]
- The diff touches only worker/features.py and calls ZoneInfo(txn.merchant_tz) with no NULL guard. [diff:fivo-worker:0001]
- Worker categorize_transaction errors start at 12:21:20Z. The worker logged 11 errors in the 12:20 bucket and 5 to 16 in each later 10-minute bucket. These counts are lower bounds because the fetch was partial. [logagg:fivo-worker:0001, log:fivo-worker:0001]
- 57 jobs failed with the TypeError and 189 succeeded since 12:00Z. [db:jobs:0001]
- Transactions with a NULL merchant_tz are uncategorised (57 of 58), while those with a tz are fine. [db:transactions:0001]
- Manual transactions always had a NULL tz, both before and after the deploy. The old code never used the timezone, so they were categorised fine. [db:transactions:0002, db:transactions:0003]
- The quick-add 500 shares trace 802cc1a662e685cb with the worker failure. [log:fivo-api:0001, log:fivo-worker:0001]

## Assumptions

- quick-add is the main path that creates manual transactions with a NULL tz and waits for the worker. This comes from the topology notes and the correlation between manual source and NULL tz. I did not read the API code.
- ZoneInfo(None) is what produces the TypeError message. This is inferred from the diff and the message text. I did not run it.
- About a third of quick-add calls fail rather than all of them. I did not establish why, for example whether some requests supply a tz or use another source.

## Recommended actions

- [immediate] Roll back fivo-worker to 994ebf056196cf1b944f9f8e4cdc0c7419d7b0bc, or hotfix `time_features` to fall back to UTC when `merchant_tz` is None or invalid. — This stops the TypeError for all manual and quick-add transactions. [diff:fivo-worker:0001, db:jobs:0001]
- [immediate] Re-run categorisation for the failed jobs and the transactions with category_id NULL since 12:21Z. — At least 57 transactions are uncategorised and their jobs are in status failed. [db:jobs:0001, db:transactions:0001]
- [soon] Make the API return a clear fallback, such as an uncategorised result, when the worker fails. Make `TransactionOut.category` tolerate None. — A worker categorisation failure should not turn quick-add into a 500. [log:fivo-api:0001]
- [later] Add a test with merchant_tz=None to the worker's feature tests. Consider populating or defaulting merchant_tz for manual transactions. — No test or review caught that the column is nullable and always NULL for manual entries. [db:transactions:0003]
- [later] Look at the ConnectionResetError 500s on GET /summary and /transactions separately. — They appear unrelated to this incident but were not explained. [log:fivo-api:0002, log:fivo-api:0003]

## Missing evidence

- The first fivo-api logs_count by route and status timed out. I did not get a per-route 5xx rate, so the 'about a third' figure is not independently confirmed.
- The worker and API error log fetches were partial and truncated. Error counts are lower bounds.
- The API code that handles quick-add and the worker's categorize_transaction task were not read. The path from the TypeError to the 500 is shown by the shared trace and the pydantic error, not by code.
- The ConnectionResetError 500s on GET /summary and /transactions were not investigated.

## Run

- system: single_agent
- termination: agent finished
- tool calls: 15
- tool failures: 1
- guardrail rejections: 1
- llm calls: 7
- total tokens: 103894
- cost usd: 0.0932
- latency s: 289.3
- citations: 45 checked, 0 invalid, 0 facts demoted to assumptions
