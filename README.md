# incident-agents

A multi-agent system that investigates production incidents. Given an alert, it gathers evidence from logs, metrics, traces, deploy and config history, the application database and the source code, tests competing hypotheses, has a critic attack the leading one, and writes a report with ranked hypotheses, cited evidence, confidence, recommended actions, and facts kept apart from assumptions. When the evidence does not settle the question, it says so.

- [Architecture and coordination](docs/ARCHITECTURE.md)
- [Technical note](docs/TECHNICAL_NOTE.md)
- [Evaluation results](docs/RESULTS.md)

## Setup

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), git, and a model: a Groq, NVIDIA, Cerebras or Anthropic API key, or a local model served by Ollama. No database or Docker is needed for the synthetic dataset.

```bash
uv sync
cp .env.example .env        # then set one provider key, or IA_PROVIDER=ollama for a local model
uv run pytest -q            # offline: uses a scripted model, no API key needed
```

## Run

```bash
# 1. Generate the synthetic incidents (deterministic; writes .data/scenarios/)
uv run python -m incident_agents scenarios build
uv run python -m incident_agents scenarios list

# 2. Investigate one incident with the multi-agent system (prints a live trace, then the report)
uv run python -m incident_agents investigate idempotency-key-regression --grade

#    The same incident with the single-agent baseline
uv run python -m incident_agents investigate idempotency-key-regression --single --grade

#    With a human in the loop: pauses after triage and after each review; you can add context or stop
uv run python -m incident_agents investigate r2-credential-rotation --interactive
uv run python -m incident_agents investigate r2-credential-rotation --note "storage keys were rotated this morning"

# 3. Evaluate both systems on every incident (about $13 and 40 minutes on Claude Sonnet 5.5)
IA_MAX_COST_USD=2.5 uv run python -m incident_agents eval --max-cost 25 --out results/eval
```

`IA_MAX_COST_USD` stops one investigation at a dollar ceiling (the report is then written from what it has). `--max-cost` stops the evaluation from starting new runs once the total reaches it, and `--resume` reuses runs that already finished.

Each run writes a folder with `report.md`, `report.json`, `trace.jsonl` (who did what, in order), `tool_calls.jsonl`, `audit.jsonl` (every query decision the gateway made), `case.db` and `evidence.db`. The evaluation also writes `summary.md` and `results.json`.

`--grade` compares the report with the scenario's hidden answer key after the run. The agents cannot read the key: it sits outside the directory the sources serve.

### Against your own systems

```bash
uv run python -m incident_agents investigate --config config/sources.yaml --incident incident.json
```

`config/sources.example.yaml` shows the format: sources (Sumo Logic, New Relic, Postgres, Git) with their guardrail policies, and a `capabilities` block that binds each vendor-neutral tool family to a source. Credentials are references such as `secret://sumo/prod#access_key`, resolved from the environment (`SUMO_PROD_ACCESS_KEY`) or from Vault at call time. `incident.json` needs `id`, `title`, `service`, `detected_at`, `window: {start, end}` and `description`.

## Results

Twelve incidents, one run per system, same model (Claude Sonnet 5.5), tools and guardrails. Full reading in [docs/RESULTS.md](docs/RESULTS.md).

| | Multi-agent | Single agent |
|---|---|---|
| Root cause correct | 12 of 12 | 11 of 12 |
| Confidently wrong | 0 | 1 |
| Citation validity | 1.00 | 1.00 |
| Key evidence cited | 0.96 | 0.83 |
| Tool calls per run | 49.5 | 17.9 |
| Tokens per run | 569,788 | 144,948 |
| Cost per run | $0.94 | $0.13 |
| Latency per run (mean, measured) | 618 s | 107 s |

The single agent is nearly as accurate at about a seventh of the cost. The multi-agent system adds independent review and enforced guarantees, and cites more of the decisive evidence. Half of the multi-agent latencies include model calls that stalled until a timeout; without them the median is 201 s.

## How it works, in short

- **Seven agents.** Observability, Dependency, Data and Code gather evidence. Reasoning forms hypotheses with ordered test plans and weighs results. The Critic checks each link of the leading causal chain against the evidence. Reporting writes the report. The Orchestrator is code: it schedules, enforces limits and decides when to stop.
- **Threads.** Each hypothesis gets a thread of ordered steps. Steps in a thread run one after another, each seeded with what the previous one found (trace IDs, user IDs, commit SHAs); threads run in parallel.
- **Shared case file.** Agents coordinate through structured records in SQLite, not messages.
- **Evidence IDs.** Every tool result becomes evidence records with IDs. Conclusions cite IDs, and code checks every citation.
- **Why now.** What failed is not the root cause; what changed is. Every deploy and configuration change any agent finds goes into a change ledger, and each recent one must end as cause, contributing or ruled out, with a reason. A hypothesis is only "supported" with a `why_now`: what changed, plus evidence of the state before and the state after. `logs_count` and `metrics_query` take a baseline window, so one evidence record holds both periods and what is new between them. The report opens with "What changed".
- **Human in the loop (optional).** With `--interactive`, an operator is asked after triage and after each round's review, and approves queries above a source's soft cost cap. A note from the operator is context for the agents, never evidence: it cannot be cited, and after a note the case is re-assessed before the next round.
- **Guarded queries.** Every query passes a gateway that lints it, estimates its cost, and applies budgets, timeouts, retries and a circuit breaker, so an investigation cannot cause a second incident.

## Dataset

Twelve incidents on one synthetic application (an API service, a worker, Postgres, a Redis broker, object storage and third-party LLM, embedding and voice APIs). Each scenario is generated: telemetry files, a seeded SQLite database standing in for Postgres, two real git repositories with commit history, and an answer key.

| Scenario | Root cause | What makes it hard |
|---|---|---|
| `idempotency-key-regression` | Code regression in the API | Straightforward multi-hop: logs → database → deploy → diff |
| `db-pool-config-change` | Config change | An innocent deploy landed just before |
| `groq-degradation-retry-storm` | Third-party dependency | Alert fires on a different service; no change to blame |
| `worker-oom-embedding-batch` | Code regression in the worker | Symptom is a stalled queue, cause is memory |
| `migration-dropped-index` | Code regression (migration) | Latency only; evidence is in a migration diff |
| `poison-receipt-requeue-loop` | Data issue | Past incident with the same symptom, different cause |
| `redis-quota-exhausted` | Resource exhaustion | Unrelated deploy in the same hour |
| `r2-credential-rotation` | Config change | Same dependency works from one service, fails from another |
| `bulk-import-traffic-spike` | Traffic change | Deploy in the window |
| `worker-regression-with-log-failures` | Code regression | Log source times out, then returns partial data |
| `conflicting-deploy-and-config` | Config change | A suspicious deploy and a config change minutes apart |
| `voice-errors-missing-telemetry` | Third-party dependency | Traces unavailable; "inconclusive" is an acceptable answer |

## Tests

```bash
uv run pytest -q
```

The suite covers the gateway guardrails, the credential broker, each connector, the evidence store, the tools, the agent loop, the orchestrator's control flow (feedback loop, parallel threads, limits, recovery) with a scripted model, the grader, and every scenario (it builds, is deterministic, is solvable through the tools, and does not leak its answer). Postgres connector tests run only if a local Postgres is up (`docker compose up -d`); otherwise they are skipped.

## What is not complete

- **Sumo Logic connector:** written and tested against a scripted fake of the Search Job API; not run against a live account.
- **Vault secret backend:** tested against mocked HTTP only. The environment backend is what the examples use.
- **Git over HTTPS with a token:** the token path (askpass, never in argv or config) is tested locally; not run against a private remote.
- **Code index:** there is no symbol or embedding index. The Code agent works with diff, file read and text search at a commit.
- **Human in the loop** is a terminal prompt (`--interactive`): checkpoints and query approval. There is no web UI, and no Slack or pager integration.
- **No memory across investigations.** Each run starts from zero; a finished report is not written back to the past-incidents source.
- **Baselines** compare with a window the agent chooses from before the incident. The synthetic data holds about 30 minutes before each onset, so "same hour yesterday" comparisons are only possible against real sources.
- **Past incidents** are searched by text only.
- **Evaluation** runs each scenario once per system by default (`--runs N` for more), so small differences are within noise.
- **Model calls are not streamed.** A call that stalls is only noticed at the client timeout (`IA_LLM_TIMEOUT_S`, 240 s) and then retried. This inflated the latency of six evaluation runs.
- **Largest output schemas are not constrained.** The API refuses to compile the two largest JSON schemas for constrained output, so they are given in the instructions and checked in code, with one repair round.

## External dependencies

- A model. One client covers every OpenAI-compatible endpoint: Groq, NVIDIA (build.nvidia.com), Cerebras, a local Ollama server, or any other via `IA_BASE_URL`. The published results used Anthropic (`IA_PROVIDER=anthropic`) with `claude-sonnet-5-5` for every role. Choose a provider with `IA_PROVIDER`; override models with `IA_MODEL_STRONG` and `IA_MODEL_FAST`. On the OpenAI-compatible providers the default is the open-weight `openai/gpt-oss-120b` (`gpt-oss:20b` on Ollama). Of those, only NVIDIA's free endpoint completed a live investigation (correct, but 53 minutes); Groq's free plan (8,000 tokens per minute) is too small, and Cerebras and Ollama were not run.
- Python packages: `anthropic`, `httpx`, `pydantic`, `pyyaml`, `sqlglot`, `asyncpg`.
- `git` on the path.
- Optional, only for real sources: Sumo Logic, New Relic, Postgres, a Git host, HashiCorp Vault.

## Layout

```
src/incident_agents/
  orchestrator.py      control loop: triage, plan, threads, assess, critique, report
  agents/              agent loop (runtime.py) and agent prompts, tools and schemas (definitions.py)
  casefile.py          shared state: hypotheses, steps, findings, leads, reviews, change ledger, operator notes, timeline
  evidence.py          evidence store, IDs, pivots, citation check
  tools/               19 vendor-neutral tools and capability bindings
  gateway/             query gateway: policy, linters, budgets, breaker, audit
  secrets/             credential broker, secret stores, scrubber
  connectors/          sumo, newrelic, postgres, git, simulated sources
  llm.py               model gateway: roles, usage ledger, budget
  baseline.py          single-agent baseline
  scenarios/           scenario generator and the twelve incidents
  evaluation.py        runner, grader, judge, summary
  report.py            citation checks and markdown rendering
config/sources.example.yaml
docs/
tests/
```
