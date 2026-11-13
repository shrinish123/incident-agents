"""Evaluation: run both systems over the scenario set and grade the reports.

For every scenario and system (multi-agent, single-agent baseline) a fresh
toolbox is built from the scenario's own sources, so both systems use the same
tools, guardrails and injected tool failures. The hidden answer key is read
only here, after the report is written.

Measured per run:
  root-cause accuracy   structured: (service, category) against the key
                        judged: a model compares the explanation with the key (0, 0.5, 1)
  confidently wrong     reported as conclusive, but wrong
  evidence grounding    citation validity: cited IDs that exist
                        key-evidence recall: the evidence that proves the cause is cited
  cost                  tool calls, latency, tokens, estimated dollars
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .agents.definitions import obj, s
from .agents.runtime import AgentSpec, run_agent
from .baseline import run_baseline
from .casefile import CaseFile, CaseStore
from .evidence import EvidenceStore
from .llm import LLM, UsageLedger
from .llm import make_llm as default_llm
from .orchestrator import Investigation, Limits
from .report import cited_ids, render_markdown
from .wiring import build_toolbox

SYSTEMS = ("multi_agent", "single_agent")


def _norm(text: Any) -> str:
    return str(text or "").strip().lower()


def _service_matches(reported: str, expected: str) -> bool:
    reported, expected = _norm(reported), _norm(expected)
    return bool(expected) and (reported == expected or expected in reported)


def _leaves(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [x for v in value.values() for x in _leaves(v)]
    if isinstance(value, list):
        return [x for v in value for x in _leaves(v)]
    return [str(value)]


def _haystack(ev: Any) -> str:
    """Everything a key-evidence pattern may match: the summary, the data as JSON, and its raw values."""
    return "\n".join([ev.summary, json.dumps(ev.data, default=str), *_leaves(ev.data)]).lower()


def grade(report: dict[str, Any], answer: dict[str, Any], store: EvidenceStore, investigation_id: str) -> dict[str, Any]:
    """Deterministic grading against the answer key."""
    rc, key = report.get("root_cause") or {}, answer["root_cause"]
    service_ok = _service_matches(rc.get("service"), key["service"])
    category_ok = _norm(rc.get("category")) == _norm(key["category"])
    correct = service_ok and category_ok
    accepted_as = "primary" if correct else ""
    for alt in answer.get("acceptable") or []:
        if correct:
            break
        alt_ok = all(
            _service_matches(rc.get("service"), alt[k]) if k == "service" else _norm(rc.get(k)) == _norm(alt[k])
            for k in ("service", "category") if alt.get(k)
        )
        if alt_ok and _norm(alt.get("category")) == "unknown":
            alt_ok = report.get("status") == "inconclusive"  # "we could not tell" only counts when it is said plainly
        if alt_ok:
            correct, accepted_as = True, "acceptable"

    cited = store.get_many(investigation_id, cited_ids(report))
    cited_text = [_haystack(e) for e in cited]
    everything = [_haystack(e) for e in store.find(investigation_id, limit=5000)]
    keys = answer.get("key_evidence") or []
    cited_hits = [any(_norm(k["must_contain"]) in t for t in cited_text) for k in keys]
    found_hits = [any(_norm(k["must_contain"]) in t for t in everything) for k in keys]
    citations = report.get("citations") or {}
    n_cited = citations.get("cited", 0)
    return {
        "correct": correct,
        "accepted_as": accepted_as,
        "service_match": service_ok,
        "category_match": category_ok,
        "conclusive": report.get("status") == "conclusive",
        "confidently_wrong": report.get("status") == "conclusive" and not correct,
        "citation_validity": round(1 - citations.get("invalid", 0) / n_cited, 3) if n_cited else 0.0,
        "citations": n_cited,
        "facts_demoted": citations.get("facts_demoted", 0),
        # Did the report say what changed, with real evidence for the state before and the state after?
        "why_now_evidenced": bool(report.get("why_now_evidenced")),
        "key_evidence_recall": round(sum(cited_hits) / len(keys), 3) if keys else None,
        "key_evidence_collected": round(sum(found_hits) / len(keys), 3) if keys else None,
        "reported": {"service": rc.get("service"), "category": rc.get("category"), "status": report.get("status")},
        "expected": {"service": key["service"], "category": key["category"]},
    }


JUDGE_SCHEMA = obj(
    score=s("1: same root cause and mechanism as the key. 0.5: right component or area, but the mechanism or trigger is wrong, vague or missing. 0: a different cause, or no cause given.", ["1", "0.5", "0"]),
    reason=s("One sentence."),
)
JUDGE_SYSTEM = """\
You grade incident investigation reports against a hidden answer key. Compare the root cause the report gives with the true root cause.
Judge the substance, not the wording. A report that says the cause could not be determined scores 0 unless the answer key says that outcome is acceptable, in which case it scores 1 if the report names the evidence that was missing.
Reply with a JSON object in the required format, nothing else."""


async def judge(llm: LLM, report: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    prompt = "\n\n".join([
        "# Answer key", json.dumps({"root_cause": answer["root_cause"], "acceptable_alternatives": answer.get("acceptable", []), "tags": answer.get("tags", [])}, indent=1),
        "# Report under review", json.dumps({k: report.get(k) for k in ("status", "summary", "root_cause", "missing_evidence")}, indent=1),
    ])
    run = await run_agent(AgentSpec("judge", "judge", JUDGE_SYSTEM, JUDGE_SCHEMA, max_turns=2), llm, None, prompt)
    if not run.ok:
        return {"judge_score": None, "judge_reason": f"judge failed: {run.error}"}
    try:
        return {"judge_score": float(run.output.get("score")), "judge_reason": str(run.output.get("reason", ""))}
    except (TypeError, ValueError):
        return {"judge_score": None, "judge_reason": "judge returned no score"}


async def run_system(
    system: str,
    scenario: Any,
    llm: LLM,
    run_dir: Path,
    *,
    limits: Limits | None = None,
    on_event: Any = None,
    operator: Any = None,
    notes: list[str] | None = None,
) -> tuple[dict[str, Any], Any]:
    """Run one system on one scenario. Returns (report, toolbox). Everything it did is written to run_dir."""
    run_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("evidence.db", "case.db", "audit.jsonl"):
        (run_dir / stale).unlink(missing_ok=True)
    # The scenario id names the cause, so the id the system works under is the opaque incident number.
    inv_id = f"{scenario.incident.get('id', 'incident')}-{system}"
    toolbox = build_toolbox(scenario.config, inv_id, evidence_path=str(run_dir / "evidence.db"), audit_path=run_dir / "audit.jsonl")
    if system == "multi_agent":
        case = CaseFile(inv_id, scenario.incident, CaseStore(str(run_dir / "case.db")))
        if operator is not None and hasattr(operator, "approve"):
            toolbox.gateway.approver = operator.approve  # the same human answers costly-query approvals
        investigation = Investigation(scenario.incident, toolbox, llm, case=case, limits=limits, on_event=on_event, operator=operator, notes=notes)
        report = await investigation.run()
        (run_dir / "trace.jsonl").write_text("\n".join(json.dumps(e, default=str) for e in case.events) + "\n")
    else:
        report = await run_baseline(scenario.incident, toolbox, llm, notes=notes)
    (run_dir / "report.json").write_text(json.dumps(report, indent=1, default=str))
    (run_dir / "report.md").write_text(render_markdown(report, scenario.incident))
    (run_dir / "tool_calls.jsonl").write_text("\n".join(json.dumps(asdict(c), default=str) for c in toolbox.calls) + "\n")
    return report, toolbox


async def evaluate(
    scenarios: list[Any],
    out_dir: Path,
    *,
    systems: tuple[str, ...] = SYSTEMS,
    runs: int = 1,
    concurrency: int = 3,
    make_llm: Any = None,
    judge_llm: LLM | None = None,
    limits: Limits | None = None,
    progress: Any = print,
    max_cost_usd: float | None = None,
    resume: bool = False,
) -> list[dict[str, Any]]:
    """Run every scenario on every system and grade it.

    `max_cost_usd` is a ceiling for the whole evaluation: once the runs so far have cost that much, no
    new run starts (runs in flight finish, each under its own per-investigation ceiling). `resume`
    reuses the grade of any run that already completed in `out_dir`, so a stopped evaluation can
    be continued without paying for the finished runs again.
    """
    make_llm = make_llm or (lambda: default_llm(UsageLedger()))
    judge_llm = judge_llm or default_llm(UsageLedger(max_calls=10_000, max_tokens=50_000_000))
    gate = asyncio.Semaphore(concurrency)
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []

    def spent() -> float:
        return sum(float(r.get("cost_usd") or 0) for r in rows if not r.get("resumed")) + judge_llm.ledger.cost_usd

    async def one(scenario: Any, system: str, n: int) -> None:
        async with gate:
            run_dir = out_dir / "runs" / f"{scenario.id}__{system}__{n}"
            done = run_dir / "grade.json"
            if resume and done.exists():
                row = json.loads(done.read_text())
                if not row.get("crashed"):
                    rows.append({**row, "resumed": True})
                    progress(f"{scenario.id:<40} {system:<13} run {n}  reused from the earlier run")
                    return
            if max_cost_usd is not None and spent() >= max_cost_usd:
                skipped.append(f"{scenario.id} ({system}, run {n})")
                progress(f"{scenario.id:<40} {system:<13} run {n}  NOT RUN: evaluation cost ceiling ${max_cost_usd:.2f} reached (spent ${spent():.2f})")
                return
            started = time.monotonic()
            try:
                report, toolbox = await run_system(system, scenario, make_llm(), run_dir, limits=limits)
                row = {"scenario": scenario.id, "system": system, "run": n, "tags": scenario.answer.get("tags", []),
                       **grade(report, scenario.answer, toolbox.store, toolbox.investigation_id),
                       **await judge(judge_llm, report, scenario.answer),
                       **{k: report["meta"].get(k) for k in ("tool_calls", "tool_failures", "guardrail_rejections", "llm_calls", "input_tokens",
                                                              "output_tokens", "total_tokens", "cost_usd", "latency_s", "rounds", "max_hops", "termination")}}
            except Exception as exc:  # one broken run must not lose the rest of the evaluation
                row = {"scenario": scenario.id, "system": system, "run": n, "tags": scenario.answer.get("tags", []), "crashed": f"{type(exc).__name__}: {exc}",
                       "correct": False, "confidently_wrong": False, "judge_score": 0.0, "latency_s": round(time.monotonic() - started, 1)}
            rows.append(row)
            (run_dir / "grade.json").write_text(json.dumps(row, indent=1, default=str))
            progress(f"{scenario.id:<40} {system:<13} run {n}  correct={row.get('correct')}  judge={row.get('judge_score')}  "
                     f"tools={row.get('tool_calls')}  tokens={row.get('total_tokens')}  {row.get('latency_s')}s"
                     + (f"  CRASHED {row['crashed']}" if row.get("crashed") else ""))

    await asyncio.gather(*(one(sc, system, n) for sc in scenarios for system in systems for n in range(1, runs + 1)))
    rows.sort(key=lambda r: (r["scenario"], r["system"], r["run"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(rows, indent=1, default=str))
    summary = summarize(rows) if rows else "No runs completed.\n"
    if skipped:
        summary += f"\n\nNot run, because the evaluation cost ceiling of ${max_cost_usd:.2f} was reached: " + ", ".join(sorted(skipped)) + ".\n"
    (out_dir / "summary.md").write_text(summary)
    progress(f"Evaluation cost this session: ${spent():.2f} (judge ${judge_llm.ledger.cost_usd:.2f})")
    return rows


def _mean(values: list[Any]) -> float | None:
    nums = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return round(sum(nums) / len(nums), 3) if nums else None


def aggregate(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    by_system: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_system[r["system"]].append(r)
    for system, rs in by_system.items():
        out[system] = {
            "runs": len(rs),
            "root_cause_accuracy": _mean([1.0 if r.get("correct") else 0.0 for r in rs]),
            "judged_accuracy": _mean([r.get("judge_score") for r in rs]),
            "confidently_wrong": sum(1 for r in rs if r.get("confidently_wrong")),
            "citation_validity": _mean([r.get("citation_validity") for r in rs]),
            "why_now_evidenced": _mean([1.0 if r.get("why_now_evidenced") else 0.0 for r in rs if not r.get("crashed")]),
            "key_evidence_recall": _mean([r.get("key_evidence_recall") for r in rs]),
            "tool_calls": _mean([r.get("tool_calls") for r in rs]),
            "latency_s": _mean([r.get("latency_s") for r in rs]),
            "total_tokens": _mean([r.get("total_tokens") for r in rs]),
            "cost_usd": _mean([r.get("cost_usd") for r in rs]),
            "crashed": sum(1 for r in rs if r.get("crashed")),
        }
    return out


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.2f}" if value < 100 else f"{value:,.0f}"
    return str(value)


def summarize(rows: list[dict[str, Any]]) -> str:
    agg = aggregate(rows)
    systems = [s_ for s_ in SYSTEMS if s_ in agg]
    lines = ["# Evaluation results", "", f"{len({r['scenario'] for r in rows})} scenarios, {len(rows)} runs.", "", "## Overall", "",
             "| Metric | " + " | ".join(systems) + " |", "|---|" + "---|" * len(systems)]
    labels = [("root_cause_accuracy", "Root-cause accuracy (service and category)"), ("judged_accuracy", "Root-cause accuracy (judged explanation)"),
              ("confidently_wrong", "Confidently wrong reports"), ("citation_validity", "Citation validity"),
              ("why_now_evidenced", "Reports with an evidenced before and after (why now)"),
              ("key_evidence_recall", "Key-evidence recall"), ("tool_calls", "Tool calls per run"), ("latency_s", "Latency per run (s)"),
              ("total_tokens", "Tokens per run"), ("cost_usd", "Estimated cost per run (USD)"), ("crashed", "Crashed runs")]
    for key, label in labels:
        lines.append(f"| {label} | " + " | ".join(_fmt(agg[s_][key]) for s_ in systems) + " |")

    tags = sorted({t for r in rows for t in r.get("tags", [])})
    if tags:
        lines += ["", "## Accuracy by scenario type", "", "| Type | Scenarios | " + " | ".join(systems) + " |", "|---|---|" + "---|" * len(systems)]
        for tag in tags:
            tagged = [r for r in rows if tag in r.get("tags", [])]
            cells = [_fmt(_mean([1.0 if r.get("correct") else 0.0 for r in tagged if r["system"] == s_])) for s_ in systems]
            lines.append(f"| {tag} | {len({r['scenario'] for r in tagged})} | " + " | ".join(cells) + " |")

    lines += ["", "## Per scenario", "", "| Scenario | System | Correct | Judge | Status | Reported cause | Key evidence cited | Tool calls | Tokens | Latency (s) |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        rep = r.get("reported") or {}
        cause = f"{rep.get('service')} / {rep.get('category')}" if rep else (r.get("crashed") or "")
        lines.append(f"| {r['scenario']} | {r['system']} | {_fmt(r.get('correct'))} | {_fmt(r.get('judge_score'))} | {rep.get('status', 'n/a')} | {cause} | "
                     f"{_fmt(r.get('key_evidence_recall'))} | {_fmt(r.get('tool_calls'))} | {_fmt(r.get('total_tokens'))} | {_fmt(r.get('latency_s'))} |")
    return "\n".join(lines) + "\n"
