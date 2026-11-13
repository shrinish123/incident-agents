"""Case file: the shared state of one investigation.

Agents do not talk to each other directly. Each one reads the part of the case
file it needs and writes back structured records:

  Hypothesis  a candidate root cause, with a causal chain and a confidence
  Step        one question for one specialist; steps of a hypothesis form a
              thread and each depends on the step before it
  Finding     a statement backed by evidence IDs (observed) or by reasoning
              over other findings (inferred)
  Lead        something unexpected a specialist noticed, for Reasoning to weigh
  Review      the Critic's verdict on a hypothesis, with follow-up questions
  Change      a deploy or configuration change found by any agent; every recent
              one must end up as cause, contributing or ruled out, with a reason
  Note        context a human operator added at a checkpoint (a lead, never evidence)
  Event       append-only timeline of who did what, for the trace

Every mutation is written through to SQLite, so a run can be inspected or
reloaded afterwards. Evidence itself lives in the evidence store; the case
file only holds IDs.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any

CATEGORIES = (
    "code_regression", "config_change", "dependency_failure", "resource_exhaustion",
    "data_issue", "infrastructure", "traffic_change", "unknown",
)
SPECIALISTS = ("observability", "dependency", "data", "code")
CHAIN_ROLES = ("trigger", "mechanism", "symptom")
CHANGE_DISPOSITIONS = ("cause", "contributing", "ruled_out")


@dataclass
class ChainLink:
    role: str  # trigger | mechanism | symptom
    statement: str
    evidence_ids: list[str] = field(default_factory=list)
    basis: str = "observed"  # observed | inferred | assumed


@dataclass
class Hypothesis:
    id: str
    statement: str
    service: str
    category: str
    rationale: str = ""
    prior: float = 0.3
    status: str = "open"  # open | supported | refuted | inconclusive
    confidence: float = 0.0
    origin: str = "plan"  # plan | lead | critic
    round: int = 0
    chain: list[ChainLink] = field(default_factory=list)
    supporting: list[str] = field(default_factory=list)  # finding ids
    contradicting: list[str] = field(default_factory=list)  # finding ids
    assessment: str = ""
    missing: list[str] = field(default_factory=list)  # what would be needed to settle it
    # Why now: {what_changed, changed_at, before: {statement, evidence_ids}, after: {...}, why_not_earlier}
    why_now: dict[str, Any] = field(default_factory=dict)


@dataclass
class Step:
    id: str
    hypothesis_id: str | None  # None for triage
    specialist: str
    question: str
    confirms_if: str = ""
    refutes_if: str = ""
    origin: str = "plan"  # triage | plan | lead | critic
    round: int = 0
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | running | done | failed | skipped
    attempts: int = 0
    answer: str = ""
    verdict: str = ""  # supports | contradicts | inconclusive
    strength: str = ""  # strong | weak
    error: str = ""
    tool_calls: int = 0
    tool_problems: list[str] = field(default_factory=list)


@dataclass
class Finding:
    id: str
    step_id: str | None
    hypothesis_id: str | None
    agent: str
    kind: str  # observed | inferred
    statement: str
    evidence_ids: list[str] = field(default_factory=list)
    stance: str = "neutral"  # supports | contradicts | neutral
    dropped_citations: list[str] = field(default_factory=list)  # IDs the agent cited that do not exist


@dataclass
class Lead:
    id: str
    step_id: str | None
    statement: str
    evidence_ids: list[str] = field(default_factory=list)
    specialist: str = ""
    question: str = ""
    status: str = "open"  # open | adopted | dismissed


@dataclass
class Review:
    id: str
    round: int
    hypothesis_id: str
    verdict: str  # accept | needs_work | reject
    confidence_cap: float = 1.0
    link_checks: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    followups: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Change:
    """One deploy or configuration change on record. The ledger of these is what "why now" is answered from."""

    id: str
    evidence_id: str
    kind: str  # deploy | config
    service: str
    ts: str
    summary: str
    timing: str = ""  # e.g. "12 min before onset", "4 min after onset", "older than the lookback"
    recent: bool = True  # inside the lookback: must be accounted for before a conclusion stands
    disposition: str = "unexamined"  # unexamined | cause | contributing | ruled_out
    reason: str = ""
    evidence_ids: list[str] = field(default_factory=list)  # what the disposition rests on


@dataclass
class Note:
    id: str
    checkpoint: str  # start | triage | review
    text: str
    round: int = 0


_KINDS: dict[str, type] = {"hypothesis": Hypothesis, "step": Step, "finding": Finding, "lead": Lead, "review": Review,
                           "change": Change, "note": Note}


def _build(cls: type, payload: dict[str, Any]) -> Any:
    known = {f.name for f in fields(cls)}
    data = {k: v for k, v in payload.items() if k in known}
    if cls is Hypothesis:
        data["chain"] = [ChainLink(**c) for c in data.get("chain", [])]
    return cls(**data)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS case_docs (
  investigation_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY (investigation_id, kind, id)
);
CREATE TABLE IF NOT EXISTS case_events (
  investigation_id TEXT NOT NULL, seq INTEGER NOT NULL, t REAL NOT NULL, agent TEXT NOT NULL, type TEXT NOT NULL, detail TEXT NOT NULL,
  PRIMARY KEY (investigation_id, seq)
);
"""


class CaseStore:
    """SQLite persistence for case files. One file can hold many investigations."""

    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def put(self, investigation_id: str, kind: str, doc_id: str, seq: int, payload: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO case_docs VALUES (?,?,?,?,?) ON CONFLICT (investigation_id, kind, id) DO UPDATE SET payload=excluded.payload",
                (investigation_id, kind, doc_id, seq, json.dumps(payload, default=str)),
            )

    def event(self, investigation_id: str, seq: int, t: float, agent: str, type_: str, detail: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO case_events VALUES (?,?,?,?,?,?)",
                (investigation_id, seq, t, agent, type_, json.dumps(detail, default=str)),
            )

    def docs(self, investigation_id: str, kind: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM case_docs WHERE investigation_id=? AND kind=? ORDER BY seq", (investigation_id, kind)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def events(self, investigation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, t, agent, type, detail FROM case_events WHERE investigation_id=? ORDER BY seq", (investigation_id,)
            ).fetchall()
        return [{"seq": r[0], "t": r[1], "agent": r[2], "type": r[3], **json.loads(r[4])} for r in rows]


class CaseFile:
    def __init__(self, investigation_id: str, incident: dict[str, Any], store: CaseStore | None = None) -> None:
        self.id = investigation_id
        self.incident = incident
        self.store = store or CaseStore()
        self.hypotheses: dict[str, Hypothesis] = {}
        self.steps: dict[str, Step] = {}
        self.findings: dict[str, Finding] = {}
        self.leads: dict[str, Lead] = {}
        self.reviews: dict[str, Review] = {}
        self.changes: dict[str, Change] = {}
        self.notes: dict[str, Note] = {}
        self.events: list[dict[str, Any]] = []
        self.round = 0
        self.triage: dict[str, Any] = {}
        self._seq = 0
        self._started = time.monotonic()
        self.store.put(self.id, "incident", "incident", 0, incident)

    # ------------------------------------------------------------------ plumbing

    def _next(self, prefix: str, existing: dict[str, Any]) -> str:
        return f"{prefix}{len(existing) + 1}"

    def save(self, obj: Any) -> Any:
        kind = next(k for k, cls in _KINDS.items() if isinstance(obj, cls))
        self._seq += 1
        self.store.put(self.id, kind, obj.id, self._seq, asdict(obj))
        return obj

    def log(self, agent: str, type_: str, **detail: Any) -> None:
        """Append to the timeline. Shown in the trace and used to explain coordination."""
        event = {"seq": len(self.events) + 1, "t": round(time.monotonic() - self._started, 3), "agent": agent, "type": type_, **detail}
        self.events.append(event)
        self.store.event(self.id, event["seq"], event["t"], agent, type_, detail)

    @classmethod
    def load(cls, store: CaseStore, investigation_id: str) -> "CaseFile":
        incident = (store.docs(investigation_id, "incident") or [{}])[0]
        case = cls(investigation_id, incident, store)
        for kind, target in (("hypothesis", case.hypotheses), ("step", case.steps), ("finding", case.findings), ("lead", case.leads),
                             ("review", case.reviews), ("change", case.changes), ("note", case.notes)):
            for payload in store.docs(investigation_id, kind):
                obj = _build(_KINDS[kind], payload)
                target[obj.id] = obj
        case.events = store.events(investigation_id)
        case.round = max([s.round for s in case.steps.values()] + [0])
        case._seq = sum(len(d) for d in (case.hypotheses, case.steps, case.findings, case.leads, case.reviews, case.changes, case.notes))
        return case

    # ------------------------------------------------------------------ writes

    def add_hypothesis(self, statement: str, service: str, category: str, **kw: Any) -> Hypothesis:
        category = category if category in CATEGORIES else "unknown"
        h = Hypothesis(id=self._next("H", self.hypotheses), statement=statement, service=service, category=category, round=self.round, **kw)
        self.hypotheses[h.id] = h
        return self.save(h)

    def add_step(self, hypothesis_id: str | None, specialist: str, question: str, **kw: Any) -> Step:
        """Append a step to a hypothesis's thread. It waits for the step before it."""
        thread = self.thread(hypothesis_id) if hypothesis_id else []
        depends = [thread[-1].id] if thread else []
        s = Step(id=self._next("S", self.steps), hypothesis_id=hypothesis_id, specialist=specialist, question=question,
                 depends_on=depends, round=self.round, **kw)
        self.steps[s.id] = s
        return self.save(s)

    def add_finding(self, agent: str, kind: str, statement: str, evidence_ids: list[str], **kw: Any) -> Finding:
        f = Finding(id=self._next("F", self.findings), agent=agent, kind=kind if kind in ("observed", "inferred") else "inferred",
                    statement=statement, evidence_ids=list(evidence_ids), step_id=kw.pop("step_id", None),
                    hypothesis_id=kw.pop("hypothesis_id", None), **kw)
        self.findings[f.id] = f
        return self.save(f)

    def add_lead(self, statement: str, **kw: Any) -> Lead:
        lead = Lead(id=self._next("L", self.leads), statement=statement, step_id=kw.pop("step_id", None), **kw)
        self.leads[lead.id] = lead
        return self.save(lead)

    def add_review(self, hypothesis_id: str, verdict: str, **kw: Any) -> Review:
        r = Review(id=self._next("R", self.reviews), round=self.round, hypothesis_id=hypothesis_id, verdict=verdict, **kw)
        self.reviews[r.id] = r
        return self.save(r)

    def add_change(self, evidence_id: str, kind: str, service: str, ts: str, summary: str, **kw: Any) -> Change:
        c = Change(id=self._next("C", self.changes), evidence_id=evidence_id, kind=kind, service=service, ts=ts, summary=summary, **kw)
        self.changes[c.id] = c
        return self.save(c)

    def add_note(self, checkpoint: str, text: str) -> Note:
        n = Note(id=self._next("N", self.notes), checkpoint=checkpoint, text=text, round=self.round)
        self.notes[n.id] = n
        return self.save(n)

    # ------------------------------------------------------------------ reads

    def thread(self, hypothesis_id: str | None) -> list[Step]:
        return [s for s in self.steps.values() if s.hypothesis_id == hypothesis_id]

    def ready_steps(self) -> list[Step]:
        """Pending steps whose dependencies have finished (done, failed or skipped)."""
        finished = {s.id for s in self.steps.values() if s.status in ("done", "failed", "skipped")}
        return [s for s in self.steps.values() if s.status == "pending" and all(d in finished for d in s.depends_on)]

    def findings_for(self, *, step_id: str | None = None, hypothesis_id: str | None = None) -> list[Finding]:
        return [
            f for f in self.findings.values()
            if (step_id is None or f.step_id == step_id) and (hypothesis_id is None or f.hypothesis_id == hypothesis_id)
        ]

    def latest_review(self, hypothesis_id: str) -> Review | None:
        reviews = [r for r in self.reviews.values() if r.hypothesis_id == hypothesis_id]
        return reviews[-1] if reviews else None

    def ranked(self) -> list[Hypothesis]:
        order = {"supported": 0, "open": 1, "inconclusive": 2, "refuted": 3}
        return sorted(self.hypotheses.values(), key=lambda h: (order.get(h.status, 9), -h.confidence, h.id))

    def cited_evidence_ids(self) -> list[str]:
        ids: list[str] = []
        for f in self.findings.values():
            ids += f.evidence_ids
        for h in self.hypotheses.values():
            for link in h.chain:
                ids += link.evidence_ids
            for side in ("before", "after"):
                ids += (h.why_now.get(side) or {}).get("evidence_ids") or []
        for c in self.changes.values():
            ids += [c.evidence_id, *c.evidence_ids]
        return list(dict.fromkeys(ids))

    def unexamined_changes(self) -> list[Change]:
        """Recent changes nobody has yet tied to the incident or ruled out."""
        return [c for c in self.changes.values() if c.recent and c.disposition == "unexamined"]

    # ------------------------------------------------------------------ views for prompts

    def finding_view(self, f: Finding) -> dict[str, Any]:
        out: dict[str, Any] = {"id": f.id, "by": f.agent, "kind": f.kind, "statement": f.statement, "evidence": f.evidence_ids}
        if f.hypothesis_id:
            out["hypothesis"] = f.hypothesis_id
            out["stance"] = f.stance
        return out

    def step_view(self, s: Step, with_findings: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {"id": s.id, "specialist": s.specialist, "question": s.question, "status": s.status}
        if s.status == "done":
            out.update(answer=s.answer, verdict=s.verdict, strength=s.strength)
        if s.error:
            out["error"] = s.error
        if s.tool_problems:
            out["tool_problems"] = s.tool_problems
        if with_findings and s.status == "done":
            out["findings"] = [self.finding_view(f) for f in self.findings_for(step_id=s.id)]
        return out

    def hypothesis_view(self, h: Hypothesis, with_thread: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": h.id, "statement": h.statement, "service": h.service, "category": h.category,
            "status": h.status, "confidence": h.confidence, "rationale": h.rationale,
        }
        if h.chain:
            out["causal_chain"] = [asdict(link) for link in h.chain]
        if h.why_now:
            out["why_now"] = h.why_now
        if h.assessment:
            out["assessment"] = h.assessment
        if h.missing:
            out["missing"] = h.missing
        if with_thread:
            out["thread"] = [self.step_view(s) for s in self.thread(h.id)]
        review = self.latest_review(h.id)
        if review:
            out["last_review"] = {"verdict": review.verdict, "issues": review.issues, "confidence_cap": review.confidence_cap}
        return out

    def change_view(self, c: Change) -> dict[str, Any]:
        out: dict[str, Any] = {"id": c.id, "kind": c.kind, "service": c.service, "at": c.ts, "timing": c.timing,
                               "summary": c.summary, "evidence": c.evidence_id, "disposition": c.disposition}
        if c.reason:
            out.update(reason=c.reason, reason_evidence=c.evidence_ids)
        return out

    def snapshot(self) -> dict[str, Any]:
        """Everything, for Reasoning, the Critic and Reporting."""
        return {
            "incident": self.incident,
            "round": self.round,
            "triage": {**self.triage, "findings": [self.finding_view(f) for f in self.findings.values() if f.hypothesis_id is None]},
            "hypotheses": [self.hypothesis_view(h) for h in self.hypotheses.values()],
            "open_leads": [asdict(lead) for lead in self.leads.values() if lead.status == "open"],
            "changes": [self.change_view(c) for c in sorted(self.changes.values(), key=lambda c: c.ts, reverse=True) if c.recent]
                       + [self.change_view(c) for c in sorted(self.changes.values(), key=lambda c: c.ts, reverse=True) if not c.recent][:8],
            "operator_notes": [{"id": n.id, "at": n.checkpoint, "note": n.text} for n in self.notes.values()],
        }
