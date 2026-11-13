# Technical note

**Problem.** Find the root cause of a production incident when signals mislead, conflict or go missing, and say "inconclusive" when evidence does not settle it.

**Threads, not fan-out.** Investigations are chains of dependent hops: error counts, raw lines, a trace ID, database rows, the deploy, its diff. So the unit of work is a thread per hypothesis: steps run in order, each specialist seeded with earlier findings and identifiers; threads run in parallel.

**Collaboration through a shared case file.** Agents do not chat. Four specialists (Observability, Dependency, Data, Code) write findings with evidence IDs; Reasoning plans and re-plans from them; the Critic checks each link of the leading causal chain and sends questions back; Reporting writes only from the case file.

**Why now, not just what failed.** "The pool was exhausted" is a symptom; the root cause is what changed. Every deploy and config change found goes into a ledger, and each recent one must be ruled in or out with a reason. A supported hypothesis needs evidence of the state before and after; log and metric tools take a baseline window.

**Control is code, judgement is models.** The orchestrator is deterministic: scheduling, limits (rounds, steps, tool calls, tokens, cost, time), termination. It enforces what should not depend on a model behaving well: an evidenced trigger, mechanism and symptom; the why-now rules; conclusive only if the Critic accepted; every citation checked.

**Guarded tools.** Every query passes one gateway: read-only SELECT on an indexed column; log searches need a scope, a bounded window and a count before raw lines; cost caps; timeouts, retries, a circuit breaker, redaction, audit. Credentials never reach an agent.

**Human in the loop, optional.** An operator can add context at checkpoints, stop the run, and approve costly queries.

**Built to leave the simulator.** Tools are vendor-neutral; configuration binds them to simulated sources or to Sumo Logic, New Relic, Postgres and Git.

**Evaluation.** Twelve generated incidents needing two or more hops, with red-herring deploys, conflicting changes, injected timeouts and partial results, and one where "inconclusive" is acceptable. The single-agent baseline uses the same tools, guardrails, model (Claude Sonnet 5.5) and report format. Grading uses a hidden answer key.

**Results (one run each).** Multi-agent 12 of 12 correct; single agent 11 of 12, its miss a misclassification stated as conclusive. Multi-agent cited more of the decisive evidence (0.96 against 0.83) and cost 7 times more ($0.94 against $0.13 per incident), with 4 times the tokens and 4 to 6 times the latency.

**Trade-off.** On these incidents coordination did not buy accuracy beyond noise; it bought independent review and enforced guarantees.

**Limitations.** Synthetic incidents, one run each. Sumo Logic and Vault are tested against fakes only. Model calls are not streamed; stalled calls inflated half the multi-agent latencies. No memory across investigations, no code index, no approval UI.

**Improvements.** Run the single agent first, with the Critic as a second pass and escalation of low-confidence cases. Stream model calls. Write accepted reports back as past incidents.
