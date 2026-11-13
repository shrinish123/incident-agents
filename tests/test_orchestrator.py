"""Orchestration tests with a scripted model: control flow, feedback loops, limits and recovery."""

import json
import re

import pytest

from incident_agents.agents import AgentSpec, render_result, run_agent
from incident_agents.agents.definitions import ASSESS_SCHEMA, BASELINE_REPORT_SCHEMA, PLAN_SCHEMA, SPECIALIST_SCHEMA
from incident_agents.baseline import run_baseline
from incident_agents.casefile import CaseFile, CaseStore
from incident_agents.gateway import Status
from incident_agents.llm import LLMError, ScriptedLLM, UsageLedger
from incident_agents.orchestrator import Investigation, Limits
from incident_agents.report import render_markdown
from tests.test_tools import W, build, ts

INCIDENT = {"id": "inc-1", "title": "500s on POST /transactions", "service": "fivo-api", "detected_at": ts(14), "window": W}
ID_RE = re.compile(r"\b[a-z]+:[\w.\-]+:\d{4}\b")
FIRST_TOOL = {
    "observability": ("logs_count", {"service": "fivo-api", "filters": {"level": "error"}, "group_by": ["route"], **W}),
    "dependency": ("deploys_list", {"service": "fivo-api", **W}),
    "data": ("db_query", {"sql": "SELECT user_id, count(*) AS n FROM transactions WHERE user_id = 104 GROUP BY 1"}),
    "code": ("code_search", {"repo": "api", "pattern": "idempotency_key"}),
}


def last_tool_ids(req):
    """Evidence IDs in the tool results of the latest turn."""
    content = req.messages[-1]["content"]
    if not isinstance(content, list):
        return []
    return ID_RE.findall(" ".join(str(b.get("content", "")) for b in content if b.get("type") == "tool_result"))


def prompt_ids(req):
    return list(dict.fromkeys(ID_RE.findall(req.messages[0]["content"])))


def say(obj):
    return {"text": json.dumps(obj)}


class Brain:
    """A scripted team. Override attributes to steer a scenario."""

    def __init__(self):
        self.critic_verdicts = ["needs_work", "accept"]
        self.hypotheses = [("Deploy changed the idempotency key", "fivo-api", "code_regression", ["observability", "code"]),
                           ("Database is overloaded", "neon-postgres", "resource_exhaustion", ["data"])]
        self.assess = None  # optional override: fn(req, ids) -> dict
        self.verdicts = {}  # step id -> (verdict, strength)
        self.fail = {}  # agent name -> list of exceptions to raise on successive calls
        self.report_extra_fact = True
        self.answers_why_now = True  # the assessment says what changed, with before and after evidence
        self.decides_changes = True  # the assessment gives every change in the ledger a disposition
        self.order = []

    def __call__(self, req):
        self.order.append(req.agent)
        if self.fail.get(req.agent):
            return self.fail[req.agent].pop(0)
        if req.agent in FIRST_TOOL:
            return self.specialist(req)
        if req.agent == "reasoning":
            return self.plan(req) if req.schema is PLAN_SCHEMA else self.do_assess(req)
        if req.agent == "critic":
            return self.critic(req)
        return self.report(req)

    def specialist(self, req):
        if len(req.messages) == 1:
            name, args = FIRST_TOOL[req.agent]
            return {"tool_calls": [{"name": name, "input": args}]}
        ids = last_tool_ids(req)
        if req.schema is not SPECIALIST_SCHEMA:
            return say({"summary": "Errors on /transactions since 08:10.", "onset": ts(10), "affected_services": ["fivo-api"],
                        "findings": [{"statement": "something observed", "kind": "observed", "evidence_ids": ids[:1]}], "tool_problems": []})
        step = re.search(r"Your question \(step (S\d+)\)", req.messages[0]["content"])[1]
        verdict, strength = self.verdicts.get(step, ("supports", "strong"))
        return say({"answer": f"answer for {step}", "verdict": verdict, "strength": strength,
                    "findings": [{"statement": f"finding of {step}", "kind": "observed", "evidence_ids": ids[:1] + ["log:made-up:0099"], "stance": verdict if verdict != "inconclusive" else "neutral"},
                                 {"statement": "claimed without evidence", "kind": "observed", "evidence_ids": [], "stance": "neutral"}],
                    "leads": [{"statement": "odd thing", "evidence_ids": ids[:1], "specialist": "data", "question": "check it"}] if step == "S3" else [],
                    "tool_problems": []})

    def plan(self, req):
        return say({"hypotheses": [
            {"statement": st, "service": svc, "category": cat, "rationale": "triage", "prior": 0.4,
             "test_plan": [{"specialist": sp, "question": f"{sp} check for {svc}", "confirms_if": "yes", "refutes_if": "no"} for sp in plan]}
            for st, svc, cat, plan in self.hypotheses]})

    def do_assess(self, req):
        ids = prompt_ids(req)
        return say(self.assess(req, ids) if self.assess else self.default_assess(req, ids))

    def default_assess(self, req, ids):
        chain = [{"role": r, "statement": f"{r} link", "evidence_ids": ids[i:i + 1], "basis": "observed"} for i, r in enumerate(("trigger", "mechanism", "symptom"))]
        nothing = {"what_changed": "", "changed_at": "", "before": {"statement": "", "evidence_ids": []}, "after": {"statement": "", "evidence_ids": []}, "why_not_earlier": ""}
        why = {"what_changed": "the 08:09 deploy changed how the idempotency key is built", "changed_at": ts(9),
               "before": {"statement": "key included the request id", "evidence_ids": ids[:1] + ["diff:ghost:0001"]},
               "after": {"statement": "key is the user id only", "evidence_ids": ids[1:2]}, "why_not_earlier": ""}
        changes = re.findall(r'"id": "(C\d+)"', req.messages[0]["content"]) if self.decides_changes else []
        return {"assessments": [
            {"hypothesis_id": "H1", "status": "supported", "confidence": 0.85, "assessment": "fits", "causal_chain": chain,
             "why_now": why if self.answers_why_now else nothing,
             "supporting_finding_ids": ["F3"], "contradicting_finding_ids": [], "missing": [], "next_steps": []},
            {"hypothesis_id": "H2", "status": "refuted", "confidence": 0.05, "assessment": "no", "causal_chain": [], "why_now": nothing,
             "supporting_finding_ids": [], "contradicting_finding_ids": [], "missing": [], "next_steps": []}],
            "new_hypotheses": [], "lead_decisions": [{"lead_id": "L1", "decision": "dismiss", "reason": "unrelated"}],
            "change_decisions": [{"change_id": c, "disposition": "cause", "reason": "its diff changes the key", "evidence_ids": ids[:1]} for c in dict.fromkeys(changes)]}

    def critic(self, req):
        verdict = self.critic_verdicts.pop(0) if self.critic_verdicts else "accept"
        return say({"reviews": [{"hypothesis_id": "H1", "verdict": verdict, "confidence_cap": 0.9 if verdict == "accept" else 0.55,
                                 "link_checks": [{"role": "mechanism", "supported": "partly", "issue": "diff not read"}],
                                 "issues": ["mechanism not shown"] if verdict != "accept" else [], "alternatives": [],
                                 "followups": [{"specialist": "code", "question": "read the diff of the deploy", "why": "show the mechanism"}] if verdict == "needs_work" else []}]})

    def report(self, req):
        ids = prompt_ids(req)
        facts = [{"statement": "real fact", "evidence_ids": ids[:2]}]
        if self.report_extra_fact:
            facts.append({"statement": "unsupported fact", "evidence_ids": ["metric:nope:0001"]})
        return say({"status": "conclusive", "summary": "Deploy broke idempotency keys.",
                    "root_cause": {"service": "fivo-api", "component": "idempotency_key", "category": "code_regression", "summary": "chain"},
                    "hypotheses": [{"id": "H2", "rank": 2, "statement": "db", "service": "neon-postgres", "category": "resource_exhaustion", "confidence": 0.05, "verdict": "ruled_out", "supporting": [], "contradicting": []},
                                   {"id": "H1", "rank": 1, "statement": "deploy", "service": "fivo-api", "category": "code_regression", "confidence": 0.85, "verdict": "most_likely",
                                    "supporting": [{"statement": "s", "evidence_ids": ids[:1] + ["diff:ghost:0007"]}], "contradicting": []}],
                    "facts": facts, "assumptions": ["traffic was normal"],
                    "recommended_actions": [{"action": "roll back", "rationale": "r", "priority": "immediate", "evidence_ids": ids[:1]}], "missing_evidence": []})


def make(tmp_path, brain=None, limits=None, ledger=None, faults=None, **kw):
    tmp_path.mkdir(parents=True, exist_ok=True)
    box, _, _ = build(tmp_path, faults=faults)
    brain = brain or Brain()
    llm = ScriptedLLM(brain, ledger)
    store = CaseStore(str(tmp_path / "case.db"))
    inv = Investigation(INCIDENT, box, llm, case=CaseFile(box.investigation_id, INCIDENT, store), limits=limits or Limits(), **kw)
    return inv, brain, llm, store


async def test_full_investigation_with_a_critic_feedback_loop(tmp_path):
    inv, brain, llm, store = make(tmp_path)
    report = await inv.run()
    case = inv.case

    # Triage ran both agents; the plan produced two threads with ordered, dependent steps.
    assert [s.specialist for s in case.thread(None)] == ["observability", "dependency"] and case.thread(None)[1].depends_on == []
    h1 = case.thread("H1")
    assert [(s.specialist, s.origin) for s in h1] == [("observability", "plan"), ("code", "plan"), ("code", "critic")]
    assert h1[1].depends_on == [h1[0].id] and h1[2].depends_on == [h1[1].id] and h1[2].round == 1 and all(s.status == "done" for s in h1)

    # The second step of a thread sees the first step's findings; other threads' findings are shared too.
    second = next(r for r in llm.requests if r.agent == "code" and "step S4" in r.messages[0]["content"])
    assert "answer for S3" in second.messages[0]["content"] and "This thread so far" in second.messages[0]["content"]

    # Round 1: critic asked for more. Round 2: the follow-up ran, critic accepted, investigation stopped.
    assert [r.verdict for r in case.reviews.values()] == ["needs_work", "accept"] and case.round == 2
    assert inv.termination == "accepted" and report["status"] == "conclusive"
    assert case.hypotheses["H1"].status == "supported" and case.hypotheses["H1"].confidence == 0.85
    assert case.hypotheses["H2"].status == "refuted" and case.leads["L1"].status == "dismissed"

    # Citations are checked in code: invented IDs dropped, unsupported observations and facts demoted.
    f = case.findings_for(step_id="S3")
    assert f[0].dropped_citations == ["log:made-up:0099"] and f[0].evidence_ids and f[1].kind == "inferred"
    assert [x["statement"] for x in report["facts"]] == ["real fact"]
    assert any("unsupported fact" in a for a in report["assumptions"])
    assert report["citations"]["invalid"] == 2 and report["citations"]["facts_demoted"] == 1
    assert [h["id"] for h in report["hypotheses"]] == ["H1", "H2"] and "diff:ghost:0007" not in json.dumps(report["hypotheses"])

    meta = report["meta"]
    assert meta["rounds"] == 2 and meta["max_hops"] == 3 and meta["tool_calls"] == 6 and meta["llm_calls"] == len(llm.requests)
    assert meta["dropped_citations"] >= 4 and set(meta["by_agent"]) == {"observability", "dependency", "data", "code", "reasoning", "critic", "reporting"}
    assert "## Hypotheses, ranked" in render_markdown(report, INCIDENT)

    # Why now: the deploy found in triage is in the change ledger, was given a disposition, and the report
    # carries the before and after with checked citations.
    c1 = case.changes["C1"]
    assert (c1.kind, c1.service, c1.timing, c1.disposition) == ("deploy", "fivo-api", "1 min before onset", "cause")
    assert report["why_now_evidenced"] and "diff:ghost:0001" not in json.dumps(report["what_changed"])
    assert report["changes_considered"][0]["disposition"] == "cause" and report["changes_considered"][0]["evidence_ids"][0] == c1.evidence_id
    assert meta["changes_on_record"] == 1 and meta["changes_unexamined"] == 0
    md = render_markdown(report, INCIDENT)
    assert "## What changed (why now)" in md and "- Before: key included the request id" in md and "**cause** — deploy on fivo-api" in md

    # The case file is durable and reloads to the same state.
    again = CaseFile.load(store, case.id)
    assert again.hypotheses == case.hypotheses and again.steps == case.steps and again.findings == case.findings
    assert again.changes == case.changes
    assert [e["type"] for e in again.events][:2] == ["start", "phase"] and again.events[-1]["type"] == "done"


async def test_threads_run_in_parallel_and_steps_in_order(tmp_path):
    inv, brain, llm, _ = make(tmp_path)
    await inv.run()
    starts = [(e["hypothesis"], e["step"]) for e in inv.case.events if e["type"] == "step_start" and e.get("hypothesis")]
    # Both threads start before either thread's second step.
    assert {h for h, _ in starts[:2]} == {"H1", "H2"}
    done = [e["step"] for e in inv.case.events if e["type"] == "step_done" and e.get("hypothesis") == "H1"]
    assert done == ["S3", "S4", "S6"]


async def test_a_chain_without_an_evidenced_trigger_cannot_be_reported_as_the_cause(tmp_path):
    brain = Brain()

    def assess(req, ids):
        chain = [{"role": "mechanism", "statement": "m", "evidence_ids": ids[:1], "basis": "observed"},
                 {"role": "symptom", "statement": "s", "evidence_ids": ids[1:2], "basis": "observed"},
                 {"role": "trigger", "statement": "probably the deploy", "evidence_ids": ["deploy:invented:0001"], "basis": "observed"}]
        return {"assessments": [{"hypothesis_id": "H1", "status": "supported", "confidence": 0.9, "assessment": "sure", "causal_chain": chain,
                                 "supporting_finding_ids": [], "contradicting_finding_ids": [], "missing": [], "next_steps": []}],
                "new_hypotheses": [], "lead_decisions": [], "change_decisions": []}

    brain.assess, brain.critic_verdicts = assess, ["accept"]
    inv, *_ = make(tmp_path, brain)
    report = await inv.run()
    h1 = inv.case.hypotheses["H1"]
    assert h1.status == "inconclusive" and h1.confidence == 0.5 and "the trigger has no evidence behind it" in h1.missing
    assert h1.chain[2].basis == "assumed" and inv.termination == "nothing left to test" and report["status"] == "inconclusive"


async def test_knowing_what_failed_is_not_enough_without_what_changed(tmp_path):
    # The chain is fully evidenced, but nothing says what is different from before: not a root cause.
    brain = Brain()
    brain.answers_why_now, brain.critic_verdicts = False, ["accept"]
    inv, *_ = make(tmp_path, brain)
    report = await inv.run()
    h1 = inv.case.hypotheses["H1"]
    assert h1.status == "inconclusive" and h1.confidence == 0.5 and "why now is unanswered: nothing is identified as having changed" in h1.missing
    assert report["status"] == "inconclusive" and report["what_changed"] == {} and not report["why_now_evidenced"]
    assert "Not established" in render_markdown(report, INCIDENT)

    # A "before" that cites nothing real is the same gap.
    brain = Brain()

    def assess(req, ids):
        out = brain.default_assess(req, ids)
        out["assessments"][0]["why_now"]["before"]["evidence_ids"] = ["config:invented:0001"]
        return out

    brain.assess, brain.critic_verdicts = assess, ["accept"]
    inv, *_ = make(tmp_path / "b", brain)
    await inv.run()
    assert "no evidence of the state before the change" in inv.case.hypotheses["H1"].missing and inv.termination != "accepted"


async def test_a_conclusion_does_not_stand_while_a_recent_change_is_unexamined(tmp_path):
    brain = Brain()
    brain.decides_changes, brain.critic_verdicts = False, ["accept"]
    inv, _, llm, _ = make(tmp_path, brain)
    report = await inv.run()
    h1 = inv.case.hypotheses["H1"]
    assert h1.status == "inconclusive" and any(m.startswith("recent changes not yet examined: C1 (deploy on fivo-api") for m in h1.missing)
    assert report["status"] == "inconclusive" and report["changes_considered"][0]["disposition"] == "unexamined" and report["meta"]["changes_unexamined"] == 1
    # The planner and the critic are both shown the ledger.
    plan = next(r for r in llm.requests if r.agent == "reasoning")
    assert "# Changes on record" in plan.messages[0]["content"] and '"id": "C1"' in plan.messages[0]["content"]
    critic = next(r for r in llm.requests if r.agent == "critic")
    assert "recent changes not yet examined" in critic.messages[0]["content"]


async def test_changes_older_than_the_lookback_do_not_block_a_conclusion(tmp_path):
    brain = Brain()
    brain.decides_changes, brain.critic_verdicts = False, ["accept"]
    inv, *_ = make(tmp_path, brain, limits=Limits(change_lookback_h=0.01))  # the deploy is 1 min before onset; lookback is 36 s
    report = await inv.run()
    assert inv.case.changes["C1"].recent is False and inv.termination == "accepted" and report["changes_considered"] == []


async def test_operator_notes_reach_the_agents_and_a_note_at_review_reopens_the_case(tmp_path):
    brain = Brain()
    brain.critic_verdicts = ["accept", "accept"]

    def assess(req, ids):
        out = brain.default_assess(req, ids)
        text = req.messages[0]["content"]
        if "check the redis quota" in text and "redis quota step" not in text:
            out["assessments"][0]["next_steps"] = [{"specialist": "dependency", "question": "redis quota step", "confirms_if": "full", "refutes_if": "not full"}]
        return out

    brain.assess = assess
    seen = []

    async def operator(checkpoint, view):
        seen.append((checkpoint, view))
        if checkpoint == "review" and view["round"] == 1:
            return {"note": "check the redis quota"}
        return None

    inv, _, llm, _ = make(tmp_path, brain, operator=operator, notes=["keys were rotated this morning"])
    report = await inv.run()
    case = inv.case
    assert [c for c, _ in seen] == ["triage", "review", "review"]
    assert seen[0][1]["changes"][0]["id"] == "C1" and seen[1][1]["next"] == "report as conclusive"
    assert seen[1][1]["hypotheses"][0]["what_changed"].startswith("the 08:09 deploy")
    # The note given up front reaches triage, planning and the specialists, labelled as not evidence.
    assert all("keys were rotated this morning" in r.messages[0]["content"] for r in llm.requests if r.agent in ("observability", "reasoning"))
    # Round 1 was accepted; the operator's note caused a re-assessment, which added a step, which ran in round 2.
    assert [n.text for n in case.notes.values()] == ["keys were rotated this morning", "check the redis quota"]
    last = case.thread("H1")[-1]
    assert (last.question, last.status, last.round) == ("redis quota step", "done", 1) and case.round == 2
    assert inv.termination == "accepted" and report["status"] == "conclusive" and report["meta"]["operator_notes"] == 2
    assert len([r for r in llm.requests if r.agent == "reasoning"]) == 4  # plan, assess, re-assess after the note, assess


async def test_operator_can_stop_and_a_failing_operator_is_ignored(tmp_path):
    async def stop(checkpoint, view):
        return {"action": "stop"}

    inv, _, llm, _ = make(tmp_path, operator=stop)
    report = await inv.run()
    assert inv.termination == "stopped by operator" and report["status"] == "inconclusive"
    assert not [r for r in llm.requests if r.agent == "reasoning"]

    async def broken(checkpoint, view):
        raise RuntimeError("pager offline")

    inv2, *_ = make(tmp_path / "b", operator=broken)
    report2 = await inv2.run()
    assert inv2.termination == "accepted" and report2["status"] == "conclusive"
    assert any(e["type"] == "checkpoint_failed" for e in inv2.case.events)


async def test_a_strong_contradiction_cuts_the_thread_short(tmp_path):
    brain = Brain()
    brain.verdicts = {"S3": ("contradicts", "strong")}
    inv, *_ = make(tmp_path, brain)
    await inv.run()
    s3, s4 = inv.case.steps["S3"], inv.case.steps["S4"]
    assert s3.verdict == "contradicts" and s4.status == "skipped" and "S3" in s4.error
    assert any(e["type"] == "thread_cut" for e in inv.case.events)


async def test_failed_steps_are_retried_then_routed_around(tmp_path):
    brain = Brain()
    brain.fail = {"data": [LLMError("boom"), LLMError("boom again")], "observability": [LLMError("blip", retryable=True)]}
    inv, *_ = make(tmp_path, brain)
    report = await inv.run()
    data_step = inv.case.thread("H2")[0]
    assert data_step.status == "failed" and data_step.attempts == 2 and "boom again" in data_step.error
    assert inv.case.thread(None)[0].status == "done"  # the transient model error was retried inside the agent loop
    assert inv.case.thread("H1")[0].status == "done" and report["meta"]["steps_failed"] == 1
    reporting = next(r for r in inv.llm.requests if r.agent == "reporting")
    assert "boom again" in reporting.messages[0]["content"]


async def test_tool_failures_reach_the_agent_and_the_run_still_finishes(tmp_path):
    inv, brain, llm, _ = make(tmp_path, faults=[{"fault": "timeout", "times": 1}])
    report = await inv.run()
    first = next(r for r in llm.requests if r.agent == "observability" and len(r.messages) == 3)
    result = first.messages[-1]["content"][0]
    assert result["is_error"] is True and json.loads(result["content"])["status"] == "timeout"
    assert inv.case.thread(None)[0].tool_problems == ["logs_count: timeout"] and report["meta"]["tool_failures"] == 1
    assert report["status"] in ("conclusive", "inconclusive")


async def test_limits_end_the_investigation_with_an_inconclusive_report(tmp_path):
    inv, *_ = make(tmp_path, ledger=UsageLedger(max_calls=7))
    report = await inv.run()
    assert inv.termination == "model budget used up" and report["status"] == "inconclusive"
    assert "without the reporting agent" in report["summary"] and report["hypotheses"]

    brain = Brain()
    brain.critic_verdicts = ["needs_work"] * 5
    inv2, *_ = make(tmp_path / "b", brain, limits=Limits(max_rounds=2, max_steps_per_thread=3))
    report2 = await inv2.run()
    assert inv2.termination == "round limit reached" and report2["status"] == "inconclusive" and inv2.case.round == 2
    assert len(inv2.case.thread("H1")) == 3 and any(e["type"] == "step_dropped" for e in inv2.case.events) is False


async def test_planning_failure_still_produces_a_report(tmp_path):
    brain = Brain()
    brain.fail = {"reasoning": [LLMError("down"), LLMError("down")]}
    inv, *_ = make(tmp_path, brain)
    report = await inv.run()
    assert inv.termination == "no hypothesis could be formed" and report["status"] == "inconclusive" and report["root_cause"]["category"] == "code_regression"


async def test_agent_loop_enforces_tool_budget_and_repairs_bad_answers(tmp_path):
    box, _, _ = build(tmp_path)
    turns = []

    def handler(req):
        turns.append(req)
        if len(turns) <= 2:
            return {"tool_calls": [{"name": "deploys_list", "input": {}}, {"name": "db_query", "input": {"sql": "select 1"}}]}
        if len(turns) == 3:
            return {"text": "here you go: not json"}
        return {"text": 'Sure. {"answer": "ok"}'}

    spec = AgentSpec("dependency", "specialist", "sys", {"type": "object", "required": ["answer"]}, tools=["deploys_list"], max_tool_calls=3)
    llm = ScriptedLLM(handler)
    run = await run_agent(spec, llm, box, "go")
    turns = llm.requests
    assert run.ok and run.output == {"answer": "ok"} and run.tool_calls == 3 and run.evidence_ids == ["deploy:fivo-api:0001"]
    first = turns[1].messages[-1]["content"]
    assert json.loads(first[1]["content"])["violations"] == ["Unknown tool 'db_query'"]  # outside this agent's allow-list
    second = turns[2].messages[-1]["content"]
    assert json.loads(second[1]["content"])["status"] == "budget_exhausted" and "budget used up" in second[2]["text"]
    assert "not a JSON object" in turns[3].messages[-1]["content"]
    assert [c.status for c in box.calls] == ["ok", "rejected", "ok"]

    never = await run_agent(spec, ScriptedLLM(lambda r: {"text": "nope"}), box, "go")
    assert not never.ok and "repair round" in never.error


async def test_large_tool_results_keep_every_evidence_id(tmp_path):
    box, _, _ = build(tmp_path, log_policy={"raw_requires_aggregate": False})
    res = await box.call("logs_fetch", {"service": "fivo-api", "start": ts(10), "end": ts(20)})
    assert res.status in (Status.OK, Status.PARTIAL) and len(res.evidence) == 5
    assert len(render_result(res)) > 1400
    text = render_result(res, limit=1400)
    assert len(text) <= 1400 and all(e.id in text for e in res.evidence) and "evidence_get" in text


async def test_single_agent_baseline_uses_the_same_tools_and_report_checks(tmp_path):
    box, _, _ = build(tmp_path)
    brain = Brain()

    def handler(req):
        if len(req.messages) == 1:
            return {"tool_calls": [{"name": "deploys_list", "input": {"service": "fivo-api"}}, {"name": "metrics_query", "input": {"service": "fivo-api", "metric": "error_rate", **W}}]}
        ids = last_tool_ids(req)
        req.messages[0]["content"] += " " + " ".join(ids)
        out = json.loads(brain.report(req)["text"])
        out["what_changed"] = {"what_changed": "the deploy", "changed_at": ts(9), "why_not_earlier": "",
                               "before": {"statement": "old key", "evidence_ids": ids[:1]}, "after": {"statement": "new key", "evidence_ids": ["diff:ghost:0002"]}}
        out["changes_considered"] = [{"change": "deploy of fivo-api", "when": ts(9), "disposition": "cause", "reason": "diff", "evidence_ids": ids[:1]}]
        return say(out)

    llm = ScriptedLLM(handler)
    report = await run_baseline(INCIDENT, box, llm)
    assert llm.requests[0].schema is BASELINE_REPORT_SCHEMA and len(llm.requests[0].tools) == 19
    assert report["meta"]["system"] == "single_agent" and report["meta"]["tool_calls"] == 2 and report["meta"]["llm_calls"] == 2
    assert report["facts"][0]["evidence_ids"] == ["deploy:fivo-api:0001", "metric:fivo-api:0001"] and report["citations"]["facts_demoted"] == 1
    # The single agent writes its own "what changed"; the same code checks it. An "after" citing nothing real is an assumption.
    assert report["what_changed"]["after"]["evidence_ids"] == [] and not report["why_now_evidenced"]
    assert any("state after the change" in a for a in report["assumptions"]) and report["changes_considered"][0]["disposition"] == "cause"


def test_change_summaries_are_readable_and_fall_back():
    from incident_agents.orchestrator import _change_summary

    deploy = {"id": "dep-1", "sha": "b139bb33f031f3a0", "prev_sha": "7bc71c8", "by": "github-actions", "message": "receipts: accept HEIC uploads"}
    assert _change_summary("deploy", deploy, "raw") == 'commit b139bb3 "receipts: accept HEIC uploads" by github-actions'
    config = {"key": "DB_POOL_SIZE", "old": "10", "new": "2", "by": "dashboard:dev", "note": "stay under the plan limit"}
    assert _change_summary("config", config, "raw") == 'DB_POOL_SIZE: 10 -> 2 by dashboard:dev ("stay under the plan limit")'
    assert _change_summary("config", {"name": "FLAG_X", "value": "on"}, "raw") == "FLAG_X: (unset) -> on"
    assert _change_summary("deploy", {"something": "else"}, "raw") == "raw"
