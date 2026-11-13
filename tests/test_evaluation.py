import json
from types import SimpleNamespace

from incident_agents.evaluation import aggregate, evaluate, grade, summarize
from incident_agents.evidence import EvidenceStore
from incident_agents.llm import ScriptedLLM
from incident_agents.report import finalize
from tests.test_orchestrator import INCIDENT, Brain
from tests.test_tools import build

ANSWER = {
    "id": "x", "tags": ["misleading"],
    "root_cause": {"service": "fivo-api", "component": "idempotency key", "category": "code_regression", "summary": "deploy dropped device_id", "trigger": {"type": "deploy", "ref": "abc"}},
    "acceptable": [{"service": "fivo-api", "category": "config_change"}, {"category": "unknown"}],
    "key_evidence": [{"kind": "diff", "must_contain": "device_id", "why": ""}, {"kind": "log", "must_contain": "IntegrityError", "why": ""}],
}


def report(service, category, status="conclusive", ids=()):
    return {"status": status, "summary": "", "root_cause": {"service": service, "component": "", "category": category, "summary": ""},
            "hypotheses": [], "facts": [{"statement": "f", "evidence_ids": list(ids)}], "assumptions": [], "recommended_actions": [], "missing_evidence": [],
            "what_changed": {}, "changes_considered": []}


def test_grading_against_the_answer_key():
    store = EvidenceStore()
    diff = store.add("inv", "diff", "api", "diff", {"patch": "- device_id"})
    store.add("inv", "log", "api", "line", {"message": "IntegrityError duplicate"})

    def g(rep):
        return grade(finalize(rep, store, "inv"), ANSWER, store, "inv")

    right = g(report("fivo-api (FastAPI)", "code_regression", ids=[diff.id, "log:ghost:0001"]))
    assert right["correct"] and right["accepted_as"] == "primary" and not right["confidently_wrong"]
    assert right["citation_validity"] == 0.5 and right["key_evidence_recall"] == 0.5 and right["key_evidence_collected"] == 1.0

    assert g(report("fivo-api", "config_change"))["accepted_as"] == "acceptable"
    wrong = g(report("neon-postgres", "resource_exhaustion"))
    assert not wrong["correct"] and wrong["confidently_wrong"] and wrong["citation_validity"] == 0.0
    # "Could not tell" is accepted only when the report says so plainly.
    assert g(report("unknown", "unknown", status="inconclusive"))["correct"]
    assert not g(report("unknown", "unknown", status="conclusive"))["correct"]


async def test_evaluation_runs_both_systems_and_writes_results(tmp_path):
    (tmp_path / "w").mkdir()
    box, _, _ = build(tmp_path / "w")
    config = {"sources": {n: s.model_dump(exclude={"name"}) for n, s in box.gateway.sources.items() if n != "appdb"},
              "capabilities": {c: {"source": b.source, **b.options} for c, b in box.bindings.items() if c != "db"}}
    scenario = SimpleNamespace(id="scn-1", incident=INCIDENT, config=config, answer=ANSWER)

    def make_llm():
        brain = Brain()
        brain.hypotheses = [brain.hypotheses[0]]

        def handler(req):
            if req.agent == "single_agent":
                return {"text": json.dumps(report("upstash-redis", "resource_exhaustion"))}
            return brain(req)

        return ScriptedLLM(handler)

    judge = ScriptedLLM(lambda req: {"text": json.dumps({"score": "1" if "fivo-api" in req.messages[0]["content"].split("# Report under review")[1] else "0", "reason": "r"})})
    lines = []
    rows = await evaluate([scenario], tmp_path / "out", make_llm=make_llm, judge_llm=judge, progress=lines.append)
    by = {r["system"]: r for r in rows}
    assert by["multi_agent"]["correct"] and by["multi_agent"]["judge_score"] == 1.0 and by["multi_agent"]["rounds"] == 2
    assert not by["single_agent"]["correct"] and by["single_agent"]["confidently_wrong"] and by["single_agent"]["judge_score"] == 0.0
    agg = aggregate(rows)
    assert agg["multi_agent"]["root_cause_accuracy"] == 1.0 and agg["single_agent"]["confidently_wrong"] == 1
    summary = (tmp_path / "out" / "summary.md").read_text()
    assert "| Root-cause accuracy (service and category) | 1.00 | 0.00 |" in summary and "| misleading | 1 |" in summary and summary == summarize(rows)
    run_dir = tmp_path / "out" / "runs" / "scn-1__multi_agent__1"
    assert {p.name for p in run_dir.iterdir()} >= {"report.md", "report.json", "trace.jsonl", "tool_calls.jsonl", "audit.jsonl", "case.db", "evidence.db", "grade.json"}
    assert len(lines) == 3 and lines[-1].startswith("Evaluation cost this session: $")

    # Resume reuses the graded runs: the model is never called again.
    def never():
        raise AssertionError("a finished run was started again")

    again = await evaluate([scenario], tmp_path / "out", make_llm=never, judge_llm=judge, progress=lines.append, resume=True)
    assert [(r["system"], r["correct"], r["resumed"]) for r in again] == [("multi_agent", True, True), ("single_agent", False, True)]

    # A cost ceiling that is already reached starts nothing, and the summary says what was left out.
    none = await evaluate([scenario], tmp_path / "capped", make_llm=never, judge_llm=judge, progress=lines.append, max_cost_usd=0.0)
    assert none == [] and "Not run, because the evaluation cost ceiling of $0.00 was reached: scn-1 (multi_agent, run 1), scn-1 (single_agent, run 1)" in (
        tmp_path / "capped" / "summary.md").read_text()
