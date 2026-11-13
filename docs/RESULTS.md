# Evaluation results

Run on 13 November 2026. Twelve synthetic incidents, each investigated once by the multi-agent system and once by the single-agent baseline: 24 runs, none crashed. Both systems used the same model for every role (`claude-sonnet-5-5`), the same 19 tools, the same guardrails and injected tool failures, and the same report format and grader. Raw output is in `results/eval/` (`summary.md`, `results.json`, and one folder per run with the report, trace, tool calls and audit log).

## Headline

| Metric | Multi-agent | Single agent |
|---|---|---|
| Root-cause accuracy (service and category match the hidden key) | 12 of 12 | 11 of 12 |
| Root-cause accuracy (explanation judged by a model against the key) | 1.00 | 0.96 |
| Confidently wrong reports | 0 | 1 |
| Citation validity (cited evidence IDs that exist) | 1.00 (1,002 citations) | 1.00 (589 citations) |
| Key evidence cited | 0.96 | 0.83 |
| Reports with an evidenced before and after (why now) | 11 of 12 | 12 of 12 |
| Tool calls per run | 49.5 | 17.9 |
| Tokens per run | 569,788 | 144,948 |
| Estimated cost per run | $0.94 | $0.13 |
| Latency per run, as measured | 618 s mean, 563 s median | 107 s mean, 54 s median |
| Latency per run, without time lost to stalled model calls | 251 s mean, 201 s median | not separable (see below) |

The whole evaluation cost about $13 in model usage ($11.28 multi-agent, $1.56 single agent, $0.11 for the judge).

## What the numbers say

**The single agent is nearly as accurate, at about a seventh of the cost.** With a capable model, good tools and the same method in its instructions (trigger, mechanism and symptom; why now; account for every change), one agent in one context solved 11 of these 12 incidents, including both with a conflicting deploy and config change and both with injected tool failures. On this dataset the coordination did not buy root-cause accuracy that is distinguishable from noise: the difference is one run, and each scenario ran once.

**The one difference is a classification, not a different mechanism.** On `poison-receipt-requeue-loop` both systems found the same thing: a 131-byte HEIC receipt that crashes the worker natively, redelivered after every restart, 219 times. The multi-agent system called it a data issue (the key's answer). The single agent called it a code regression in an earlier commit that added HEIC decoding, and reported that as conclusive. The judge gave it 0.5.

**What the multi-agent system did add:**

- It cited more of the evidence that the answer key marks as decisive (0.96 against 0.83). The specialists go deeper on each hop, and the Critic asks for links that are missing.
- No report was conclusive without the Critic accepting an evidenced causal chain. Eleven were accepted, ten of them in the first or second round.
- On `voice-errors-missing-telemetry`, where traces are unavailable and the cause is a third party with no status data, it named the third party but reported "inconclusive" and listed what was missing. The single agent named the same cause as conclusive. The key accepts both; which is better depends on how much an operator should trust a conclusion that rests on an absence.

**What it cost:** 7.2 times the money, 3.9 times the tokens, 2.8 times the tool calls, and nearly four times the time at the median even with stalls removed (201 s against 54 s).

**Reading for a real deployment:** run the single agent first, and spend the multi-agent budget where it pays: as an independent review of the single agent's conclusion, or on incidents the single agent reports with low confidence. That is a follow-up, not something this submission implements.

## Latency caveat

Six multi-agent runs that overlapped in time each lost 523 to 806 seconds to model calls that never returned and were retried after the four-minute client timeout. The trace shows them as silent gaps of about 250, 500 or 750 seconds (one, two or three timeouts) before a call that then completes in 30 to 50 seconds. The six runs without a stall took 193 to 319 seconds. "Without stalls" above subtracts those gaps; it is a post-hoc reading of the traces, not a second measurement.

Three single-agent runs took 209 to 333 seconds against 39 to 64 for the other nine, which is consistent with one stalled call each. The baseline writes no timeline, so this cannot be confirmed and its figures are left as measured.

Model calls are not streamed, so a stalled call is only noticed at the timeout. Streaming would detect it within seconds; that is not done yet.

## Limits of this evaluation

- One run per scenario and system. A difference of one run in twelve is within sampling noise.
- All incidents are synthetic and generated from one model of one application. The scenarios were written by the same person who wrote the system.
- Both systems and the judge use the same model. The service-and-category check is exact matching in code; only the explanation score is judged by a model.
- Cost is estimated from token counts and list prices, including cached input.
- Two fixes were made after the first live run and before these runs: a capped log sample was wrongly treated as a partial result, and large output schemas were rejected by the API. No prompt was tuned against the answer keys.

## Reproduce

```bash
uv run python -m incident_agents scenarios build
IA_MAX_COST_USD=2.5 uv run python -m incident_agents eval --max-cost 25 --out results/eval
```

`IA_MAX_COST_USD` caps one investigation, `--max-cost` caps the whole evaluation, and `--resume` reuses runs that already finished.

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
