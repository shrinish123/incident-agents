"""Orchestrator: runs one investigation over the shared case file.

The orchestrator is code, not a model. Judgement (what to test, what the
results mean, whether the conclusion holds) belongs to the agents; control
(what runs when, with which limits, and when to stop) is deterministic so it
can be tested and bounded.

    triage            observability and dependency agents, in parallel
    plan              reasoning proposes hypotheses, each with an ordered test plan
    round (repeat)
      threads         one thread per hypothesis, threads in parallel; inside a
                      thread steps run in order, each specialist seeing what the
                      previous steps found and the identifiers to pivot on
      assess          reasoning updates every hypothesis and builds causal chains;
                      may add steps, adopt leads or open new hypotheses
      critique        the critic attacks the leading chains; may send follow-up
                      questions back to the specialists
      decide          stop when the leader is accepted, nothing is left to test,
                      or a limit is hit
    report            reporting writes it up; citations are checked in code

Why now. Finding what failed is not the root cause; the root cause is what is
different from when the system last worked. Two things enforce that in code:
  - every deploy and configuration change any agent finds goes into a change
    ledger, and a conclusion does not stand while a recent one is unexamined
  - a supported hypothesis needs a `why_now`: what changed, with evidence of
    the state before and the state after

Human in the loop. An optional operator is asked at two checkpoints: after
triage (before hypotheses are formed) and after each round's review. They can
add a note, let it continue, or stop. A note is context for Reasoning, never
evidence; after a note the case is re-assessed before the next round.

Limits: rounds, hypotheses, steps per thread, attempts per step, tool calls
per step, model calls and tokens, source queries, wall-clock time.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .agents.definitions import (
    ASSESS_SCHEMA, CRITIC, PLAN_SCHEMA, REPORTING, reasoning_spec, specialist_spec,
)
from .agents.runtime import AgentRun, run_agent
from .casefile import CHAIN_ROLES, CHANGE_DISPOSITIONS, SPECIALISTS, CaseFile, ChainLink, Hypothesis, Step
from .llm import LLM
from .report import finalize
from .timeutil import parse_ts
from .tools import Toolbox

# Called at a checkpoint with its name ("triage" or "review") and a view of the case.
# Returns {"note": "...", "action": "continue" | "stop"}; None or {} means continue.
Operator = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]


@dataclass
class Limits:
    max_rounds: int = 3
    max_hypotheses: int = 5
    max_steps_per_thread: int = 6
    max_parallel_threads: int = 4
    max_step_attempts: int = 2
    tool_calls_per_step: int = 10
    tool_calls_triage: int = 8
    max_reviewed: int = 2
    accept_confidence: float = 0.6
    deadline_s: float = 1500.0
    change_lookback_h: float = 24.0  # changes this close to the onset must be accounted for


def _j(value: Any) -> str:
    """JSON for a prompt. No indentation: on nested case-file data it costs about a third more tokens and adds nothing."""
    return json.dumps(value, default=str)


def _first(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if data.get(key) not in (None, ""):
            return str(data[key])
    return ""


def _change_summary(kind: str, data: dict[str, Any], fallback: str) -> str:
    """One readable line for a deploy or config record, whatever the source calls its fields."""
    def short(text: str, width: int = 120) -> str:
        return text if len(text) <= width else text[: width - 3] + "..."

    who = _first(data, "by", "author", "user", "actor")
    if kind == "deploy":
        sha = _first(data, "sha", "commit", "revision", "version")
        message = _first(data, "message", "description", "title", "changelog")
        if not (sha or message):
            return fallback
        parts = [f"commit {sha[:7]}" if sha else "", f'"{short(message)}"' if message else "", f"by {who}" if who else ""]
        return " ".join(part for part in parts if part)
    key = _first(data, "key", "name", "setting", "parameter")
    if not key:
        return fallback
    old, new = _first(data, "old", "old_value", "previous", "from"), _first(data, "new", "new_value", "value", "to")
    text = f"{key}: {short(old, 40) or '(unset)'} -> {short(new, 40) or '(unset)'}"
    note = _first(data, "note", "reason", "message", "description")
    return " ".join(part for part in (text, f"by {who}" if who else "", f'("{short(note, 80)}")' if note else "") if part)


def _span(minutes: float) -> str:
    if minutes < 90:
        return f"{minutes:.0f} min"
    return f"{minutes / 60:.1f} h" if minutes < 48 * 60 else f"{minutes / 1440:.1f} d"


def _clamp(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


class Investigation:
    def __init__(
        self,
        incident: dict[str, Any],
        toolbox: Toolbox,
        llm: LLM,
        *,
        case: CaseFile | None = None,
        limits: Limits | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        operator: Operator | None = None,
        notes: list[str] | None = None,
    ) -> None:
        self.incident, self.toolbox, self.llm = incident, toolbox, llm
        self.operator, self._initial_notes = operator, [n for n in notes or [] if n.strip()]
        self.inv = toolbox.investigation_id
        self.store = toolbox.store
        self.case = case or CaseFile(self.inv, incident)
        self.limits = limits or Limits()
        self._on_event = on_event
        self._started = time.monotonic()
        self.dropped_citations = 0
        self.termination = ""

    # ------------------------------------------------------------------ helpers

    def _log(self, agent: str, type_: str, **detail: Any) -> None:
        self.case.log(agent, type_, **detail)
        if self._on_event:
            self._on_event(self.case.events[-1])

    def _clean(self, ids: Any) -> tuple[list[str], list[str]]:
        ids = [str(i) for i in ids] if isinstance(ids, list) else []
        known, invented = self.store.check_citations(self.inv, ids)
        self.dropped_citations += len(invented)
        return known, invented

    def _out_of_budget(self) -> str:
        if self.llm.ledger.exhausted():
            return "model budget used up"
        reason = self.toolbox.gateway.ledger.exhausted(self.inv)
        if reason:
            return f"source query budget used up ({reason})"
        if time.monotonic() - self._started > self.limits.deadline_s:
            return "time limit reached"
        return ""

    def _evidence_index(self, ids: list[str], full: bool = False, width: int = 3500) -> list[dict[str, Any]]:
        out = []
        for ev in self.store.get_many(self.inv, ids):
            if not full:
                out.append(ev.digest())
                continue
            record = ev.full()
            body = json.dumps(record["data"], default=str)
            if len(body) > width:
                record["data"] = body[:width] + "...[cut for length]"
            out.append(record)
        return out

    def _pivots(self) -> dict[str, list[str]]:
        return {key: list(values)[:6] for key, values in self.store.pivots(self.inv).items()}

    def _add_steps(self, hyp: Hypothesis, steps: Any, origin: str) -> list[Step]:
        added = []
        for raw in steps if isinstance(steps, list) else []:
            if len(self.case.thread(hyp.id)) >= self.limits.max_steps_per_thread:
                self._log("orchestrator", "step_dropped", hypothesis=hyp.id, reason="thread step limit", question=str(raw.get("question", ""))[:120])
                break
            specialist = raw.get("specialist")
            if specialist not in SPECIALISTS or not raw.get("question"):
                continue
            added.append(self.case.add_step(
                hyp.id, specialist, str(raw["question"]), confirms_if=str(raw.get("confirms_if") or raw.get("why") or ""),
                refutes_if=str(raw.get("refutes_if") or ""), origin=origin,
            ))
        return added

    def _add_hypothesis(self, raw: dict[str, Any], origin: str) -> Hypothesis | None:
        if len(self.case.hypotheses) >= self.limits.max_hypotheses or not raw.get("statement"):
            return None
        hyp = self.case.add_hypothesis(
            str(raw["statement"]), str(raw.get("service") or "unknown"), str(raw.get("category") or "unknown"),
            rationale=str(raw.get("rationale") or ""), prior=_clamp(raw.get("prior"), 0.3), origin=origin,
        )
        hyp.confidence = hyp.prior
        self.case.save(hyp)
        steps = self._add_steps(hyp, raw.get("test_plan"), origin)
        self._log("reasoning", "hypothesis", id=hyp.id, statement=hyp.statement, service=hyp.service, category=hyp.category,
                  plan=[f"{s.id} {s.specialist}: {s.question}" for s in steps])
        return hyp

    # ------------------------------------------------------------------ steps

    def _step_prompt(self, step: Step) -> str:
        case = self.case
        parts = ["# Incident", _j(self.incident)]
        if step.hypothesis_id:
            hyp = case.hypotheses[step.hypothesis_id]
            parts += [f"# Hypothesis under test ({hyp.id})", _j({"statement": hyp.statement, "service": hyp.service, "category": hyp.category, "rationale": hyp.rationale})]
            parts += [f"# Your question (step {step.id})", _j({"question": step.question, "would_confirm": step.confirms_if, "would_refute": step.refutes_if})]
            earlier = [case.step_view(s) for s in case.thread(hyp.id) if s.id != step.id and s.status in ("done", "failed")]
            parts += ["# This thread so far (earlier steps on this hypothesis)", _j(earlier) if earlier else "This is the first step."]
            others = [case.finding_view(f) for f in case.findings.values() if f.hypothesis_id != hyp.id][-30:]
            parts += ["# Shared case file: triage and other threads", _j({"triage": case.triage, "findings": others})]
        else:
            parts += ["# Task", step.question]
        pivots = self._pivots()
        if pivots:
            parts += ["# Identifiers found so far (use them to aim queries)", _j(pivots)]
        if self.case.notes:
            parts += ["# Notes from the human operator (context to aim your queries; not evidence, and not citable)", _j([n.text for n in self.case.notes.values()])]
        budget = self.limits.tool_calls_per_step if step.hypothesis_id else self.limits.tool_calls_triage
        parts += ["# Limits", f"You have at most {budget} tool calls for this task."]
        return "\n\n".join(parts)

    async def _run_step(self, step: Step) -> AgentRun:
        triage = step.hypothesis_id is None
        spec = specialist_spec(step.specialist, triage=triage,
                               max_tool_calls=self.limits.tool_calls_triage if triage else self.limits.tool_calls_per_step)
        run = AgentRun(ok=False, error="not run")
        while step.attempts < self.limits.max_step_attempts:
            if stop := self._out_of_budget():
                run = AgentRun(ok=False, error=stop)
                break
            step.attempts += 1
            step.status = "running"
            self.case.save(step)
            self._log(step.specialist, "step_start", step=step.id, hypothesis=step.hypothesis_id, attempt=step.attempts, question=step.question)
            run = await run_agent(spec, self.llm, self.toolbox, self._step_prompt(step), step_id=step.id, intent=step.question[:200])
            step.tool_calls += run.tool_calls
            if run.ok:
                break
            self._log(step.specialist, "step_error", step=step.id, attempt=step.attempts, error=run.error)
        if run.ok:
            self._ingest_step(step, run)
        else:
            step.status, step.error = "failed", run.error
            self.case.save(step)
            self._log("orchestrator", "step_failed", step=step.id, hypothesis=step.hypothesis_id, error=run.error)
        return run

    def _ingest_step(self, step: Step, run: AgentRun) -> None:
        out = run.output
        step.status = "done"
        step.answer = str(out.get("answer") or out.get("summary") or "")
        step.verdict = out.get("verdict") if out.get("verdict") in ("supports", "contradicts", "inconclusive") else "inconclusive"
        step.strength = out.get("strength") if out.get("strength") in ("strong", "weak") else "weak"
        step.tool_problems = [str(p) for p in out.get("tool_problems") or []] or run.tool_failures
        self.case.save(step)
        for raw in out.get("findings") or []:
            ids, dropped = self._clean(raw.get("evidence_ids"))
            kind = raw.get("kind", "inferred")
            if kind == "observed" and not ids:
                kind = "inferred"  # an observation with no real evidence behind it is not one
            self.case.add_finding(step.specialist, kind, str(raw.get("statement") or ""), ids, step_id=step.id,
                                  hypothesis_id=step.hypothesis_id, stance=raw.get("stance") or "neutral", dropped_citations=dropped)
        for raw in out.get("leads") or []:
            ids, _ = self._clean(raw.get("evidence_ids"))
            lead = self.case.add_lead(str(raw.get("statement") or ""), step_id=step.id, evidence_ids=ids,
                                      specialist=str(raw.get("specialist") or ""), question=str(raw.get("question") or ""))
            self._log(step.specialist, "lead", lead=lead.id, statement=lead.statement)
        self._log(step.specialist, "step_done", step=step.id, hypothesis=step.hypothesis_id, verdict=step.verdict, strength=step.strength,
                  tool_calls=step.tool_calls, findings=len(self.case.findings_for(step_id=step.id)), answer=step.answer[:300])

    async def _run_thread(self, hyp: Hypothesis, gate: asyncio.Semaphore) -> None:
        """Steps of one hypothesis, strictly in order: each one builds on the last."""
        async with gate:
            while True:
                pending = [s for s in self.case.thread(hyp.id) if s.status == "pending"]
                if not pending or self._out_of_budget():
                    return
                step = pending[0]
                await self._run_step(step)
                if step.status == "done" and step.verdict == "contradicts" and step.strength == "strong":
                    for rest in pending[1:]:
                        rest.status, rest.error = "skipped", f"hypothesis strongly contradicted by {step.id}"
                        self.case.save(rest)
                    self._log("orchestrator", "thread_cut", hypothesis=hyp.id, by=step.id, skipped=[s.id for s in pending[1:]])
                    return

    # ------------------------------------------------------------------ what changed

    def _sync_changes(self) -> None:
        """Put every deploy and configuration change any agent has found into the ledger."""
        known = {c.evidence_id for c in self.case.changes.values()}
        ref, label = parse_ts(self.case.triage.get("onset")), "onset"
        if ref is None:
            ref, label = parse_ts(self.incident.get("detected_at")), "detection"
        for kind in ("deploy", "config"):
            for ev in self.store.find(self.inv, kind=kind, limit=200):
                if ev.id in known or not ev.ts or ev.data.get("matched") == 0:
                    continue
                when, timing, recent = parse_ts(ev.ts), "", True
                if when and ref:
                    minutes = (ref - when).total_seconds() / 60
                    recent = minutes <= self.limits.change_lookback_h * 60
                    timing = f"{_span(abs(minutes))} {'before' if minutes >= 0 else 'after'} {label}"
                change = self.case.add_change(ev.id, kind, ev.subject, ev.ts, _change_summary(kind, ev.data, ev.summary), timing=timing, recent=recent)
                self._log("orchestrator", "change_found", change=change.id, kind=kind, service=ev.subject, at=ev.ts, timing=timing, recent=recent)

    def _why_now(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict) or not str(raw.get("what_changed") or "").strip():
            return {}
        out: dict[str, Any] = {key: str(raw.get(key) or "").strip() for key in ("what_changed", "changed_at", "why_not_earlier")}
        for side in ("before", "after"):
            part = raw.get(side) if isinstance(raw.get(side), dict) else {}
            ids, _ = self._clean(part.get("evidence_ids"))
            out[side] = {"statement": str(part.get("statement") or ""), "evidence_ids": ids}
        return out

    def _decide_changes(self, decisions: Any) -> None:
        for raw in decisions if isinstance(decisions, list) else []:
            change = self.case.changes.get(str(raw.get("change_id")))
            reason = str(raw.get("reason") or "").strip()
            if change is None or raw.get("disposition") not in CHANGE_DISPOSITIONS or not reason:
                continue  # a disposition with no reason is not one
            ids, _ = self._clean(raw.get("evidence_ids"))
            change.disposition, change.reason, change.evidence_ids = raw["disposition"], reason, ids
            self.case.save(change)
            self._log("reasoning", "change_decided", change=change.id, disposition=change.disposition, reason=reason[:200])

    def _timing_notes(self, hyp: Hypothesis) -> list[str]:
        """Soft checks for the Critic: both times come from agents, so a mismatch is flagged, not enforced."""
        changed, onset = parse_ts(hyp.why_now.get("changed_at")), parse_ts(self.case.triage.get("onset"))
        if changed and onset and (changed - onset).total_seconds() > 120:
            return [f"why_now dates the change at {hyp.why_now['changed_at']}, after the onset found in triage ({self.case.triage['onset']}): "
                    "one of the two times is wrong, or this change is not the trigger"]
        return []

    # ------------------------------------------------------------------ human in the loop

    async def _checkpoint(self, name: str, view: dict[str, Any]) -> tuple[str, bool]:
        """Ask the operator, if there is one. Returns (note, stop)."""
        if self.operator is None:
            return "", False
        self._log("orchestrator", "checkpoint", name=name)
        try:
            reply = await self.operator(name, view) or {}
        except Exception as exc:  # an unreachable operator must not stop the investigation
            self._log("orchestrator", "checkpoint_failed", name=name, error=str(exc))
            return "", False
        note = str(reply.get("note") or "").strip()
        if note:
            self.case.add_note(name, note)
            self._log("operator", "note", checkpoint=name, note=note)
        return note, reply.get("action") == "stop"

    def _review_view(self, termination: str) -> dict[str, Any]:
        pending = len([s for s in self.case.steps.values() if s.status == "pending"])
        hyps = []
        for h in self.case.ranked():
            review = self.case.latest_review(h.id)
            hyps.append({"id": h.id, "statement": h.statement, "service": h.service, "status": h.status, "confidence": h.confidence,
                         "what_changed": h.why_now.get("what_changed", ""), "missing": h.missing[:4],
                         "critic": {"verdict": review.verdict, "issues": review.issues[:3]} if review else None})
        return {
            "round": self.case.round,
            "next": {"accepted": "report as conclusive", "": f"continue: {pending} step(s) pending"}.get(termination, f"report as inconclusive ({termination})"),
            "hypotheses": hyps,
            "changes": [self.case.change_view(c) for c in self.case.changes.values() if c.recent],
        }

    # ------------------------------------------------------------------ phases

    async def _triage(self) -> None:
        self._log("orchestrator", "phase", name="triage")
        steps = [
            self.case.add_step(None, "observability", "Triage: establish the symptoms, their scope and when they started.", origin="triage"),
            self.case.add_step(None, "dependency", "Triage: list the dependencies of the alerting service and every recent deploy and configuration change around it.", origin="triage"),
        ]
        runs = await asyncio.gather(*(self._run_step(s) for s in steps))
        obs = runs[0].output if runs[0].ok else {}
        self.case.triage = {
            "summary": obs.get("summary", ""), "onset": obs.get("onset", ""), "affected_services": obs.get("affected_services", []),
            "changes_summary": runs[1].output.get("summary", "") if runs[1].ok else "",
            "gaps": [f"{s.specialist} triage failed: {s.error}" for s in steps if s.status == "failed"]
                    + [p for s in steps for p in s.tool_problems],
        }

    async def _reason(self, schema: dict[str, Any], prompt: str) -> AgentRun:
        run = await run_agent(reasoning_spec(schema), self.llm, None, prompt)
        if not run.ok and not self._out_of_budget():
            self._log("reasoning", "retry", error=run.error)
            run = await run_agent(reasoning_spec(schema), self.llm, None, prompt)
        return run

    async def _plan(self) -> bool:
        self._log("orchestrator", "phase", name="plan")
        self._sync_changes()
        snapshot = self.case.snapshot()
        prompt = "\n\n".join([
            "# Task", f"Propose hypotheses for this incident, each with a test plan. At most {min(4, self.limits.max_hypotheses)} hypotheses.",
            "# Incident", _j(self.incident),
            "# Triage", _j(snapshot["triage"]),
            "# Changes on record (deploys and configuration changes found in triage; timing is relative to the onset)", _j(snapshot["changes"]),
            "# Operator notes (context from a human, not evidence)", _j(snapshot["operator_notes"]),
            "# Evidence behind the triage findings", _j(self._evidence_index(self.case.cited_evidence_ids())),
            "# Specialists you can assign steps to, and their tools", _j({name: [t for t in specialist_spec(name).tools if t in self.toolbox.tools] for name in SPECIALISTS}),
        ])
        run = await self._reason(PLAN_SCHEMA, prompt)
        if not run.ok:
            self._log("orchestrator", "plan_failed", error=run.error)
            return False
        for raw in run.output.get("hypotheses") or []:
            self._add_hypothesis(raw, "plan")
        return bool(self.case.hypotheses)

    def _structural_gaps(self, hyp: Hypothesis) -> list[str]:
        """Checks on a causal chain that need no model."""
        gaps = []
        for role in CHAIN_ROLES:
            links = [l for l in hyp.chain if l.role == role]
            if not links:
                gaps.append(f"no {role} in the causal chain")
            elif all(l.basis == "assumed" or not l.evidence_ids for l in links):
                gaps.append(f"the {role} has no evidence behind it")
        partial = [e.id for e in self.store.get_many(self.inv, [i for l in hyp.chain for i in l.evidence_ids]) if not e.complete]
        if partial:
            gaps.append(f"relies on partial results: {', '.join(partial[:4])}")
        # Why now: what failed is not a root cause until something shows what is different from before.
        if not hyp.why_now:
            gaps.append("why now is unanswered: nothing is identified as having changed")
        else:
            gaps += [f"no evidence of the state {side} the change" for side in ("before", "after") if not hyp.why_now[side]["evidence_ids"]]
        unexamined = self.case.unexamined_changes()
        if unexamined:
            gaps.append("recent changes not yet examined: " + ", ".join(f"{c.id} ({c.kind} on {c.service} at {c.ts})" for c in unexamined[:5]))
        return gaps

    async def _assess(self) -> bool:
        self._log("orchestrator", "phase", name="assess", round=self.case.round)
        self._sync_changes()
        last_round = self.case.round >= self.limits.max_rounds
        prompt = "\n\n".join([
            "# Task",
            "Weigh the results. Update every hypothesis, build the causal chain and the why_now for any that the evidence supports, "
            "decide on the open leads, and give a disposition for every change in the case file's changes list."
            + (" This is the last round: no further steps will run, so leave next_steps empty." if last_round else
               f" Round {self.case.round} of {self.limits.max_rounds}. Each hypothesis may have at most {self.limits.max_steps_per_thread} steps in total."),
            "# Case file", _j(self.case.snapshot()),
            "# Evidence cited so far (summaries)", _j(self._evidence_index(self.case.cited_evidence_ids())),
        ])
        run = await self._reason(ASSESS_SCHEMA, prompt)
        if not run.ok:
            self._log("orchestrator", "assess_failed", error=run.error)
            return False
        out = run.output
        self._decide_changes(out.get("change_decisions"))  # before the gap checks below, which read the ledger
        for raw in out.get("assessments") or []:
            hyp = self.case.hypotheses.get(str(raw.get("hypothesis_id")))
            if hyp is None:
                continue
            hyp.why_now = self._why_now(raw.get("why_now"))
            hyp.chain = []
            for link in raw.get("causal_chain") or []:
                ids, _ = self._clean(link.get("evidence_ids"))
                basis = link.get("basis") if link.get("basis") in ("observed", "inferred", "assumed") else "assumed"
                if basis == "observed" and not ids:
                    basis = "assumed"
                if link.get("role") in CHAIN_ROLES:
                    hyp.chain.append(ChainLink(link["role"], str(link.get("statement") or ""), ids, basis))
            hyp.status = raw.get("status") if raw.get("status") in ("supported", "refuted", "inconclusive", "open") else "inconclusive"
            hyp.confidence = _clamp(raw.get("confidence"))
            review = self.case.latest_review(hyp.id)
            if review is not None and review.round == self.case.round:  # a re-assessment after an operator note keeps the Critic's cap
                hyp.confidence = min(hyp.confidence, review.confidence_cap)
            hyp.assessment = str(raw.get("assessment") or "")
            hyp.supporting = [f for f in raw.get("supporting_finding_ids") or [] if f in self.case.findings]
            hyp.contradicting = [f for f in raw.get("contradicting_finding_ids") or [] if f in self.case.findings]
            hyp.missing = [str(m) for m in raw.get("missing") or []]
            added = [] if last_round or hyp.status == "refuted" else self._add_steps(hyp, raw.get("next_steps"), "plan")
            gaps = self._structural_gaps(hyp)
            if hyp.status == "supported" and gaps:
                # The orchestrator will not let a chain with a missing or unevidenced link stand as supported.
                hyp.status = "open" if added else "inconclusive"
                hyp.confidence = min(hyp.confidence, 0.5)
                hyp.missing += [g for g in gaps if g not in hyp.missing]
            if hyp.status == "open" and not added and not [s for s in self.case.thread(hyp.id) if s.status == "pending"]:
                hyp.status = "inconclusive"
            self.case.save(hyp)
            self._log("reasoning", "assessment", hypothesis=hyp.id, status=hyp.status, confidence=hyp.confidence,
                      gaps=gaps, next_steps=[f"{s.id} {s.specialist}: {s.question}" for s in added], assessment=hyp.assessment[:300])
        for raw in out.get("lead_decisions") or []:
            lead = self.case.leads.get(str(raw.get("lead_id")))
            if lead and lead.status == "open":
                lead.status = "adopted" if raw.get("decision") == "adopt" else "dismissed"
                self.case.save(lead)
        if not last_round:
            for raw in out.get("new_hypotheses") or []:
                self._add_hypothesis(raw, "lead")
        return True

    async def _critique(self) -> None:
        candidates = [h for h in self.case.ranked() if h.status != "refuted" and h.chain and h.confidence >= 0.3][: self.limits.max_reviewed]
        if not candidates or self._out_of_budget():
            return
        self._log("orchestrator", "phase", name="critique", round=self.case.round, hypotheses=[h.id for h in candidates])
        last_round = self.case.round >= self.limits.max_rounds
        chain_ids = [i for h in candidates for l in h.chain for i in l.evidence_ids]
        chain_ids += [i for h in candidates for side in ("before", "after") for i in (h.why_now.get(side) or {}).get("evidence_ids", [])]
        chain_ids = list(dict.fromkeys(chain_ids))
        other_ids = [i for i in self.case.cited_evidence_ids() if i not in chain_ids]
        prompt = "\n\n".join([
            "# Task", "Review these hypotheses. Check every link of each causal chain against the evidence it cites."
            + (" This is the last round: followups cannot run, so judge on what is here." if last_round else ""),
            "# Incident", _j(self.incident),
            "# Triage", _j(self.case.snapshot()["triage"]),
            "# Hypotheses under review", _j([self.case.hypothesis_view(h) for h in candidates]),
            "# Other hypotheses (for alternatives)", _j([self.case.hypothesis_view(h, with_thread=False) for h in self.case.hypotheses.values() if h not in candidates]),
            "# Findings that contradict them, as recorded", _j([self.case.finding_view(f) for f in self.case.findings.values() if f.stance == "contradicts"]),
            "# Automatic checks", _j({h.id: (self._structural_gaps(h) + self._timing_notes(h)) or "chain is structurally complete" for h in candidates}),
            "# Changes on record and what was decided about each", _j(self.case.snapshot()["changes"]),
            "# Evidence cited by the chains (full records)", _j(self._evidence_index(chain_ids, full=True)),
            "# Other evidence in the case file (summaries)", _j(self._evidence_index(other_ids)),
        ])
        run = await run_agent(CRITIC, self.llm, None, prompt)
        if not run.ok:
            self._log("orchestrator", "critique_failed", error=run.error)
            return
        for raw in run.output.get("reviews") or []:
            hyp = self.case.hypotheses.get(str(raw.get("hypothesis_id")))
            if hyp is None or hyp not in candidates:
                continue
            verdict = raw.get("verdict") if raw.get("verdict") in ("accept", "needs_work", "reject") else "needs_work"
            followups = raw.get("followups") if isinstance(raw.get("followups"), list) else []
            review = self.case.add_review(
                hyp.id, verdict, confidence_cap=_clamp(raw.get("confidence_cap"), 1.0), link_checks=raw.get("link_checks") or [],
                issues=[str(i) for i in raw.get("issues") or []], alternatives=[str(a) for a in raw.get("alternatives") or []], followups=followups,
            )
            hyp.confidence = min(hyp.confidence, review.confidence_cap)
            added: list[Step] = []
            if verdict == "reject":
                hyp.status = "inconclusive" if hyp.status in ("supported", "open") else hyp.status
            elif verdict == "needs_work":
                added = [] if last_round else self._add_steps(hyp, followups, "critic")
                hyp.status = "open" if added else "inconclusive"
            self.case.save(hyp)
            self._log("critic", "review", hypothesis=hyp.id, verdict=verdict, confidence_cap=review.confidence_cap, issues=review.issues[:4],
                      followups=[f"{s.id} {s.specialist}: {s.question}" for s in added])

    def _decide(self) -> str:
        ranked = self.case.ranked()
        if ranked:
            top = ranked[0]
            review = self.case.latest_review(top.id)
            if (top.status == "supported" and review is not None and review.round == self.case.round
                    and review.verdict == "accept" and top.confidence >= self.limits.accept_confidence):
                return "accepted"
        if stop := self._out_of_budget():
            return stop
        if self.case.round >= self.limits.max_rounds:
            return "round limit reached"
        if not [s for s in self.case.steps.values() if s.status == "pending"]:
            return "nothing left to test"
        return ""

    # ------------------------------------------------------------------ report

    def metrics(self) -> dict[str, Any]:
        calls = self.toolbox.calls
        llm = self.llm.ledger.totals()
        threads = [[s for s in self.case.thread(h.id) if s.status == "done"] for h in self.case.hypotheses.values()]
        return {
            "system": "multi_agent",
            "termination": self.termination,
            "rounds": self.case.round,
            "hypotheses": len(self.case.hypotheses),
            "steps": len([s for s in self.case.steps.values() if s.status == "done"]),
            "steps_failed": len([s for s in self.case.steps.values() if s.status == "failed"]),
            "max_hops": max([len(t) for t in threads] + [0]),
            "tool_calls": len(calls),
            "tool_failures": len([c for c in calls if c.status in ("timeout", "error", "unavailable")]),
            "guardrail_rejections": len([c for c in calls if c.status == "rejected"]),
            "evidence_records": self.store.count(self.inv),
            "dropped_citations": self.dropped_citations,
            "changes_on_record": len([c for c in self.case.changes.values() if c.recent]),
            "changes_unexamined": len(self.case.unexamined_changes()),
            "operator_notes": len(self.case.notes),
            "llm_calls": llm["calls"],
            "input_tokens": llm["input_tokens"],
            "output_tokens": llm["output_tokens"],
            "total_tokens": llm["total_tokens"],
            "cost_usd": llm["cost_usd"],
            "latency_s": round(time.monotonic() - self._started, 1),
            "by_agent": self.llm.ledger.by_agent(),
        }

    def _fallback_report(self, status: str, why: str) -> dict[str, Any]:
        """Built from the case file alone, for when the reporting agent cannot run."""
        ranked = self.case.ranked()
        top = ranked[0] if ranked else None
        hyps = []
        for i, h in enumerate(ranked, 1):
            def cites(ids: list[str]) -> list[dict[str, Any]]:
                return [{"statement": self.case.findings[f].statement, "evidence_ids": self.case.findings[f].evidence_ids} for f in ids if f in self.case.findings]
            hyps.append({"id": h.id, "rank": i, "statement": h.statement, "service": h.service, "category": h.category, "confidence": h.confidence,
                         "verdict": "ruled_out" if h.status == "refuted" else ("most_likely" if i == 1 else "possible"),
                         "supporting": cites(h.supporting), "contradicting": cites(h.contradicting)})
        return {
            "status": status,
            "summary": f"{self.case.triage.get('summary', '')} Report assembled without the reporting agent ({why}).".strip(),
            "root_cause": {"service": top.service if top else "unknown", "component": "", "category": top.category if top else "unknown",
                           "summary": top.assessment or top.statement if top else "No hypothesis could be formed."},
            "hypotheses": hyps,
            "facts": [{"statement": f.statement, "evidence_ids": f.evidence_ids} for f in self.case.findings.values() if f.kind == "observed"][:25],
            "assumptions": [l.statement for h in ranked[:1] for l in h.chain if l.basis == "assumed"],
            "recommended_actions": [],
            "missing_evidence": [m for h in ranked[:2] for m in h.missing] + self.case.triage.get("gaps", []),
        }

    def _change_sections(self) -> dict[str, Any]:
        ranked = [h for h in self.case.ranked() if h.status != "refuted"]
        recent = sorted((c for c in self.case.changes.values() if c.recent), key=lambda c: c.ts)
        return {
            "what_changed": ranked[0].why_now if ranked else {},
            "changes_considered": [
                {"change": f"{c.kind} on {c.service}: {c.summary}", "when": c.ts, "timing": c.timing, "disposition": c.disposition,
                 "reason": c.reason, "evidence_ids": [c.evidence_id, *c.evidence_ids]}
                for c in recent
            ],
        }

    async def _report(self) -> dict[str, Any]:
        self._log("orchestrator", "phase", name="report", termination=self.termination)
        self._sync_changes()
        status = "conclusive" if self.termination == "accepted" else "inconclusive"
        failed = [f"{s.id} ({s.specialist}): {s.error}" for s in self.case.steps.values() if s.status == "failed"]
        problems = [p for s in self.case.steps.values() for p in s.tool_problems]
        prompt = "\n\n".join([
            "# Task", f"Write the final report. The status is \"{status}\" (the investigation ended because: {self.termination}).",
            "# Case file", _j(self.case.snapshot()),
            "# Critic reviews", _j([{"hypothesis": r.hypothesis_id, "round": r.round, "verdict": r.verdict, "issues": r.issues, "alternatives": r.alternatives} for r in self.case.reviews.values()]),
            "# Steps that failed and tool problems", _j({"failed_steps": failed, "tool_problems": problems}),
            "# Evidence you may cite (summaries)", _j(self._evidence_index(self.case.cited_evidence_ids())),
        ])
        run = await run_agent(REPORTING, self.llm, None, prompt) if not self.llm.ledger.exhausted() else AgentRun(ok=False, error="model budget used up")
        if run.ok:
            report = run.output
        else:
            self._log("orchestrator", "report_fallback", error=run.error)
            report = self._fallback_report(status, run.error)
        report["status"] = status  # decided by the review loop, not by the writer
        report.update(self._change_sections())  # likewise from the case file, not rewritten by the writer
        report = finalize(report, self.store, self.inv)
        report["meta"] = self.metrics()
        return report

    # ------------------------------------------------------------------ run

    async def run(self) -> dict[str, Any]:
        self._log("orchestrator", "start", incident=self.incident.get("id"), title=self.incident.get("title"))
        for text in self._initial_notes:
            self.case.add_note("start", text)
            self._log("operator", "note", checkpoint="start", note=text)
        await self._triage()
        self._sync_changes()
        _, stop = await self._checkpoint("triage", {"triage": self.case.triage, "changes": self.case.snapshot()["changes"]})
        if stop:
            self.termination = "stopped by operator"
        elif not await self._plan():
            self.termination = self._out_of_budget() or "no hypothesis could be formed"
        while not self.termination:
            self.case.round += 1
            self._log("orchestrator", "phase", name="threads", round=self.case.round,
                      threads={h.id: [s.id for s in self.case.thread(h.id) if s.status == "pending"] for h in self.case.hypotheses.values()})
            gate = asyncio.Semaphore(self.limits.max_parallel_threads)
            active = [h for h in self.case.hypotheses.values() if any(s.status == "pending" for s in self.case.thread(h.id))]
            await asyncio.gather(*(self._run_thread(h, gate) for h in active))
            if not self.llm.ledger.exhausted() and await self._assess():
                await self._critique()
            self.termination = self._decide()
            note, stop = await self._checkpoint("review", self._review_view(self.termination))
            if stop:
                self.termination = self.termination or "stopped by operator"  # stopping after acceptance keeps the acceptance
            elif note and not self._out_of_budget():
                # The note is context, not a command: Reasoning re-weighs the case with it and may add steps.
                await self._assess()
                more = any(s.status == "pending" for s in self.case.steps.values()) and self.case.round < self.limits.max_rounds
                self.termination = "" if more else self._decide()
        for step in self.case.steps.values():
            if step.status == "pending":
                step.status, step.error = "skipped", f"investigation ended: {self.termination}"
                self.case.save(step)
        self._log("orchestrator", "stop", reason=self.termination, round=self.case.round)
        report = await self._report()
        self._log("orchestrator", "done", status=report["status"], root_cause=report["root_cause"])
        return report
