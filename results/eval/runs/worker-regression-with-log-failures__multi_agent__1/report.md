# Incident report: fivo-api: POST /transactions/quick-add 5xx above 20%

**Status:** conclusive

At 12:21:00Z fivo-worker deploy dep-0414 (732a2a8, "features: use the merchant's local time of day") added an unguarded ZoneInfo(txn.merchant_tz) to time_features. categorize_transaction calls it, so it now raises TypeError for rows with a NULL merchant_tz. The first worker error was at 12:21:20Z and the first quick-add 500 at 12:21:20Z. quick-add passes the failed task's exception object on as the category, pydantic rejects it, and the API returns 500: 27 500s against 21 201s from 12:21 to 13:08. Plain POST /transactions does not wait on the worker, so it stays healthy but leaves rows uncategorized. Roll back the worker to 994ebf0 or guard the None tz. Two links are inferred: the TypeError is not tied to the ZoneInfo line by a traceback, and the tz of the 201 rows was not joined per row.

## Root cause

- Service: fivo-worker
- Component: worker/features.py time_features (unguarded ZoneInfo(txn.merchant_tz)), called from categorize_transaction; deploy dep-0414 / 732a2a8
- Category: code_regression

Deploy dep-0414 at 12:21:00Z made time_features call ZoneInfo(merchant_tz) with no None guard. For NULL-tz manual rows categorize_transaction raises TypeError, so the row stays uncategorized. quick-add gets the exception object back from get(propagate=False) and passes it as category, TransactionOut validation fails, and the API returns 500.

## What changed (why now)

fivo-worker deploy dep-0414 (732a2a8) made `time_features` use the merchant's local time via an unguarded `ZoneInfo(merchant_tz)`. NULL merchant_tz rows were harmless before and now raise. (at 2026-11-10T12:21:00Z)

- Before: Before the deploy there were no worker errors from 12:00 to 12:19, no null-category rows from 11:30 to about 12:20, and a task failure rate of about 0.0. NULL-tz manual rows already existed (17, 20, 24, 14 and 13 per bucket), so the data trait was unchanged. [logagg:fivo-worker:0004, db:transactions:0002, metric:fivo-worker:0005]
- After: The first categorize_transaction TypeError is at 12:21:20Z, with 57 errors in total to 13:08. The task failure rate is 0.24-0.50 from 12:20. 57 of 58 NULL-tz manual rows are uncategorized, against 0 of 61 rows with a valid tz. The first quick-add ValidationError 500 is at 12:21:20Z, with 27 500s against 21 201s to 13:08. [log:fivo-worker:0001, logagg:fivo-worker:0004, metric:fivo-worker:0005, db:transactions:0001, log:fivo-api:0001, logagg:fivo-api:0004]

Changes before the incident, and what was concluded about each:

- **cause** — deploy on fivo-worker: commit 732a2a8 "features: use the merchant's local time of day" by github-actions (2026-11-10T12:21:00Z, 1 min after onset): dep-0414 (732a2a8) changes only `worker/features.py`, adding an unguarded `ZoneInfo(txn.merchant_tz)` on the categorize path. Worker errors, the task failure rate step, null categories and the quick-add ValidationError 500s all start after 12:21:00Z, with none before it. The NULL-tz data existed earlier and was harmless. No other deploys or config changes were recorded. [deploy:fivo-worker:0001, diff:fivo-worker:0001, log:fivo-worker:0001, logagg:fivo-worker:0004, db:transactions:0002, metric:fivo-worker:0005, config:config_changes:0001]

## Hypotheses, ranked

1. **Worker deploy dep-0414 (732a2a8) introduced an unguarded ZoneInfo(merchant_tz) in time_features. categorize_transaction fails for NULL-tz rows, and quick-add turns the failed result into a 500 while uncategorized rows accumulate.** (fivo-worker, code_regression) — most_likely, confidence 0.88
   - Supports: Only worker/features.py changed. It now calls ZoneInfo(txn.merchant_tz), which is typed str | None and has no None guard, on the categorize path. [changes:fivo-worker:0001, diff:fivo-worker:0001, codehit:fivo-worker:0001, codehit:fivo-worker:0003]
   - Supports: No worker errors before the deploy. The first error is at 12:21:20Z, 20 s after it. All fetched errors are the same categorize_transaction TypeError (not NoneType). 57 errors in total. [logagg:fivo-worker:0004, log:fivo-worker:0001, log:fivo-worker:0002, log:fivo-worker:0008]
   - Supports: Task failure rate stepped from about 0.0 to 0.24-0.50 at 12:20. [metric:fivo-worker:0005]
   - Supports: 57 of 58 NULL-tz manual rows are uncategorized, against 0 of 61 rows with a valid tz. NULL-tz rows existed before the deploy with 0 null categories. [db:transactions:0001, db:transactions:0002]
   - Supports: 26 of 27 quick-add 500s are the ValidationError 'category: Input should be a valid string', fast at 454-752 ms. The code returns the failed result's exception object (propagate=False) unchecked as category. [logagg:fivo-api:0003, log:fivo-api:0001, code:fivo-api:0001, code:fivo-api:0004]
   - Supports: Four sampled quick-add 201 traces show the worker 'succeeded' with no error. [log:fivo-worker:0010, logagg:fivo-worker:0009]
   - Supports: No other deploy or config change was recorded in the window. [config:config_changes:0001, deploy:fivo-worker:0001]
   - Contradicts: TransactionOut.category is str | None, so a None category would not cause the 500. The mechanism is a non-string exception object instead. [code:fivo-api:0002]
   - Contradicts: No traceback ties the TypeError to the ZoneInfo line. The 201 rows' tz and the ~30 other null-category rows were not joined per row. About 1 quick-add 500, and single /transactions and /summary 500s, are unexplained. Counts are partial lower bounds. [logagg:fivo-api:0001, logagg:fivo-api:0004]
2. **Groq (used by categorize_transaction) is rate-limited, slow or failing.** (groq-llm, dependency_failure) — ruled_out, confidence 0.02
   - Contradicts: All 20 fetched error lines are the same application TypeError, with no 429, timeout or connection errors. A search for 'groq' returned 0 lines. [logagg:fivo-worker:0002, logagg:fivo-worker:0003]
3. **fivo-worker is resource-starved (512 MB, two threads), through OOM, restarts or a queue backlog.** (fivo-worker, resource_exhaustion) — ruled_out, confidence 0.01
   - Contradicts: Memory was flat at 244-249 MB and CPU low. Queue depth was 0 throughout. The only restart was at 12:20, matching the deploy. p95 duration did not shift. [metric:fivo-worker:0001, metric:fivo-worker:0002, metric:fivo-worker:0004, metric:fivo-worker:0003, metric:fivo-worker:0006]
4. **An Upstash Redis queue or result-backend problem stops quick-add tasks or results.** (upstash-redis, infrastructure) — ruled_out, confidence 0.01
   - Contradicts: 26 of 27 quick-add 500s are fast pydantic ValidationErrors, so the result came back with a bad category. No Redis or timeout errors appear. [logagg:fivo-api:0003, log:fivo-api:0001, log:fivo-api:0005]

## Facts

- fivo-worker deploy dep-0414 (sha 732a2a8, prev 994ebf0) went live at 2026-11-10T12:21:00Z. It was the only deploy found in the window, and no config changes were recorded. [deploy:fivo-worker:0001, config:config_changes:0001]
- The diff changes only worker/features.py. time_features now calls ZoneInfo(txn.merchant_tz), and merchant_tz is str | None. [diff:fivo-worker:0001, changes:fivo-worker:0001, codehit:fivo-worker:0001]
- The first worker error is at 12:21:20Z, with none before. There are 57 errors in total, all categorize_transaction TypeError('expected str, bytes or os.PathLike object, not NoneType') in the lines read. [log:fivo-worker:0001, logagg:fivo-worker:0004, log:fivo-worker:0008]
- Worker task failure rate rose from about 0.0 to 0.24-0.50 at 12:20. Memory, CPU, queue depth and p95 duration showed no resource problem. [metric:fivo-worker:0005, metric:fivo-worker:0001, metric:fivo-worker:0004, metric:fivo-worker:0006]
- From 12:21 to 13:08, 57 of 58 NULL-tz manual rows have a null category, against 0 for rows with a tz. Before 12:20 there were 0 null-category rows even though NULL-tz manual rows existed. [db:transactions:0001, db:transactions:0002]
- From 12:21 to 13:08, quick-add returned 27 500s and 21 201s. 26 of the 27 500s are a ValidationError on TransactionOut category, the first at 12:21:20Z, with durations of 454-752 ms in the lines read. [logagg:fivo-api:0004, logagg:fivo-api:0003, log:fivo-api:0001]
- quick-add inserts a row defaulting to source='manual' and merchant_tz=None, waits on the worker, and uses the returned value as category. A None result gives 504. wait_for_result uses get(propagate=False). [code:fivo-api:0001, code:fivo-api:0002, code:fivo-api:0003, code:fivo-api:0004]
- TransactionOut.category is str | None = None. [code:fivo-api:0002]
- Plain POST /transactions stayed 200/201, apart from a single 500 in the 12:20 bucket (partial, lower-bound counts). [logagg:fivo-api:0001]
- Four sampled quick-add 201 traces had worker 'received' and 'succeeded' lines and no error lines. [log:fivo-worker:0009, log:fivo-worker:0010, logagg:fivo-worker:0009]
- A search for 'groq' in worker logs returned 0 lines, and no past incidents match 'quick-add'. [logagg:fivo-worker:0003, incident:incidents:0001]

## Assumptions

- The TypeError message matches ZoneInfo(None) in time_features. No traceback was seen.
- The 21 quick-add 201s are rows with a valid tz. This is not joined per row, and the 201 rows are presumably not NULL-tz manual rows.
- The 57 worker errors split into 27 quick-add 500s and about 30 plain POST /transactions rows. This is arithmetic, not a per-row join.
- fivo-api code read at main matches production, since no fivo-api deploy was recorded.
- The 12:20 and 12:21 restart is the deploy itself.

## Recommended actions

- [immediate] Roll fivo-worker back to 994ebf0, or hotfix time_features to guard a None or invalid merchant_tz, for example by falling back to UTC. — The unguarded ZoneInfo(merchant_tz) raises for NULL-tz rows and fails categorize_transaction. [deploy:fivo-worker:0001, diff:fivo-worker:0001, codehit:fivo-worker:0001]
- [soon] Re-run categorize_transaction for the null-category rows created since 12:21Z (57 NULL-tz manual rows by 13:08) after the fix. — These rows stayed uncategorized, and the share is still growing. [db:transactions:0001, db:transactions:0002]
- [soon] In fivo-api wait_for_result or quick-add, check whether the result is an exception and return a controlled error or fallback instead of passing it to TransactionOut. — get(propagate=False) hands a failed task's exception object to the response model, causing a 500 with a ValidationError. [code:fivo-api:0004, code:fivo-api:0001, log:fivo-api:0001]
- [later] Add a worker test for NULL and invalid merchant_tz in time_features, and alert on categorize_transaction failure rate and on a rising count of null-category rows. — Task failure rate went from 0 to 0.24-0.50 at the deploy and was not caught. NULL-tz rows were already common. [metric:fivo-worker:0005, db:transactions:0002]

## Missing evidence

- No traceback tying the TypeError to the ZoneInfo call in time_features.
- No per-row join of quick-add 500 and 201 traces to merchant_tz or worker outcome. Only 4 of 21 201 traces were sampled.
- The single non-ValidationError quick-add 500, and the single /transactions and /summary 500s in the 12:20 bucket, are unidentified.
- fivo-api logs counts are partial and capped, so they are lower bounds. No pre-incident baseline was queried. fivo-api code was read at main, not at a deploy SHA.
- Raw worker error lines after 12:25 were counted but not read individually.

## Run

- system: multi_agent
- termination: accepted
- rounds: 2
- hypotheses: 4
- steps: 11
- max hops: 6
- tool calls: 50
- tool failures: 1
- guardrail rejections: 2
- llm calls: 43
- total tokens: 577156
- cost usd: 0.9936
- latency s: 807.4
- citations: 86 checked, 0 invalid, 0 facts demoted to assumptions
