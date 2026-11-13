# Evaluation results

12 scenarios, 24 runs.

## Overall

| Metric | multi_agent | single_agent |
|---|---|---|
| Root-cause accuracy (service and category) | 1.00 | 0.92 |
| Root-cause accuracy (judged explanation) | 1.00 | 0.96 |
| Confidently wrong reports | 0 | 1 |
| Citation validity | 1.00 | 1.00 |
| Reports with an evidenced before and after (why now) | 0.92 | 1.00 |
| Key-evidence recall | 0.96 | 0.83 |
| Tool calls per run | 49.50 | 17.92 |
| Latency per run (s) | 618 | 107 |
| Tokens per run | 569,788 | 144,948 |
| Estimated cost per run (USD) | 0.94 | 0.13 |
| Crashed runs | 0 | 0 |

## Accuracy by scenario type

| Type | Scenarios | multi_agent | single_agent |
|---|---|---|---|
| conflicting | 2 | 1.00 | 1.00 |
| inconclusive_ok | 1 | 1.00 | 1.00 |
| misleading | 5 | 1.00 | 0.80 |
| multi_hop | 10 | 1.00 | 0.90 |
| tool_failure | 2 | 1.00 | 1.00 |

## Per scenario

| Scenario | System | Correct | Judge | Status | Reported cause | Key evidence cited | Tool calls | Tokens | Latency (s) |
|---|---|---|---|---|---|---|---|---|---|
| bulk-import-traffic-spike | multi_agent | yes | 1.00 | conclusive | fivo-api / traffic_change | 1.00 | 42 | 497058 | 299 |
| bulk-import-traffic-spike | single_agent | yes | 1.00 | conclusive | fivo-api / traffic_change | 0.60 | 18 | 124812 | 58.90 |
| conflicting-deploy-and-config | multi_agent | yes | 1.00 | conclusive | fivo-api / config_change | 1.00 | 29 | 335315 | 193 |
| conflicting-deploy-and-config | single_agent | yes | 1.00 | conclusive | fivo-api / config_change | 0.83 | 12 | 92995 | 43.70 |
| db-pool-config-change | multi_agent | yes | 1.00 | conclusive | fivo-api / config_change | 1.00 | 40 | 422335 | 197 |
| db-pool-config-change | single_agent | yes | 1.00 | conclusive | fivo-api / config_change | 0.80 | 12 | 84977 | 39.20 |
| groq-degradation-retry-storm | multi_agent | yes | 1.00 | conclusive | groq-llm / dependency_failure | 1.00 | 58 | 595525 | 205 |
| groq-degradation-retry-storm | single_agent | yes | 1.00 | conclusive | groq-llm / dependency_failure | 0.80 | 19 | 145603 | 47.80 |
| idempotency-key-regression | multi_agent | yes | 1.00 | conclusive | fivo-api / code_regression | 1.00 | 36 | 363417 | 193 |
| idempotency-key-regression | single_agent | yes | 1.00 | conclusive | fivo-api / code_regression | 0.75 | 13 | 104386 | 41.50 |
| migration-dropped-index | multi_agent | yes | 1.00 | conclusive | neon-postgres / code_regression | 1.00 | 61 | 697672 | 319 |
| migration-dropped-index | single_agent | yes | 1.00 | conclusive | fivo-api / code_regression | 0.80 | 20 | 123914 | 63.30 |
| poison-receipt-requeue-loop | multi_agent | yes | 1.00 | conclusive | fivo-worker / data_issue | 0.67 | 51 | 576956 | 940 |
| poison-receipt-requeue-loop | single_agent | no | 0.50 | conclusive | fivo-worker / code_regression | 1.00 | 26 | 283215 | 63.80 |
| r2-credential-rotation | multi_agent | yes | 1.00 | conclusive | fivo-worker / config_change | 1.00 | 36 | 461642 | 925 |
| r2-credential-rotation | single_agent | yes | 1.00 | conclusive | fivo-worker / config_change | 0.80 | 11 | 104485 | 44.90 |
| redis-quota-exhausted | multi_agent | yes | 1.00 | conclusive | upstash-redis / resource_exhaustion | 1.00 | 58 | 861899 | 1,228 |
| redis-quota-exhausted | single_agent | yes | 1.00 | conclusive | upstash-redis / resource_exhaustion | 0.75 | 30 | 233334 | 209 |
| voice-errors-missing-telemetry | multi_agent | yes | 1.00 | inconclusive | bolna-voice / dependency_failure | 1.00 | 83 | 940100 | 1,181 |
| voice-errors-missing-telemetry | single_agent | yes | 1.00 | conclusive | bolna-voice / dependency_failure | 1.00 | 21 | 177063 | 333 |
| worker-oom-embedding-batch | multi_agent | yes | 1.00 | conclusive | fivo-worker / code_regression | 0.80 | 50 | 508383 | 928 |
| worker-oom-embedding-batch | single_agent | yes | 1.00 | conclusive | fivo-worker / code_regression | 0.80 | 18 | 160704 | 48.10 |
| worker-regression-with-log-failures | multi_agent | yes | 1.00 | conclusive | fivo-worker / code_regression | 1.00 | 50 | 577156 | 807 |
| worker-regression-with-log-failures | single_agent | yes | 1.00 | conclusive | fivo-worker / code_regression | 1.00 | 15 | 103894 | 289 |
