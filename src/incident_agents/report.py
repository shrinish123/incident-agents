"""Final report: citation checks that do not depend on a model, and rendering.

`finalize` is applied to every report, from the multi-agent system and from
the single-agent baseline alike:
  - every cited evidence ID is checked against the evidence store; invented
    IDs are removed and counted
  - a "fact" left with no valid citation is moved to assumptions
  - hypotheses are ordered by rank
  - "what changed" (the answer to why now) keeps only real citations; if either
    the state before or the state after is left without evidence, that is
    recorded as an assumption
"""

from __future__ import annotations

from typing import Any

from .casefile import CATEGORIES
from .evidence import EvidenceStore


def _clean(store: EvidenceStore, investigation_id: str, ids: Any, stats: dict[str, int]) -> list[str]:
    ids = [str(i) for i in ids] if isinstance(ids, list) else []
    known, invented = store.check_citations(investigation_id, ids)
    stats["cited"] += len(known) + len(invented)
    stats["invalid"] += len(invented)
    return known


def finalize(report: dict[str, Any], store: EvidenceStore, investigation_id: str) -> dict[str, Any]:
    stats = {"cited": 0, "invalid": 0, "facts_demoted": 0}
    out = dict(report)
    out["status"] = out.get("status") if out.get("status") in ("conclusive", "inconclusive") else "inconclusive"
    rc = dict(out.get("root_cause") or {})
    rc.setdefault("service", "unknown")
    rc.setdefault("component", "")
    rc.setdefault("summary", "")
    rc["category"] = rc.get("category") if rc.get("category") in CATEGORIES else "unknown"
    out["root_cause"] = rc

    assumptions = [str(a) for a in out.get("assumptions") or []]
    facts = []
    for fact in out.get("facts") or []:
        ids = _clean(store, investigation_id, fact.get("evidence_ids"), stats)
        if ids:
            facts.append({"statement": fact.get("statement", ""), "evidence_ids": ids})
        else:
            stats["facts_demoted"] += 1
            assumptions.append(f"{fact.get('statement', '')} (stated as fact without valid evidence)")
    out["facts"], out["assumptions"] = facts, assumptions

    hyps = []
    for h in out.get("hypotheses") or []:
        h = dict(h)
        for side in ("supporting", "contradicting"):
            h[side] = [
                {"statement": c.get("statement", ""), "evidence_ids": _clean(store, investigation_id, c.get("evidence_ids"), stats)}
                for c in h.get(side) or []
            ]
        try:
            h["confidence"] = max(0.0, min(1.0, float(h.get("confidence") or 0)))
        except (TypeError, ValueError):
            h["confidence"] = 0.0
        hyps.append(h)
    hyps.sort(key=lambda h: (float(h.get("rank") or 99), -h["confidence"]))
    for i, h in enumerate(hyps, 1):
        h["rank"] = i
    out["hypotheses"] = hyps

    actions = []
    for a in out.get("recommended_actions") or []:
        actions.append({**a, "evidence_ids": _clean(store, investigation_id, a.get("evidence_ids"), stats)})
    out["recommended_actions"] = actions
    out["missing_evidence"] = [str(m) for m in out.get("missing_evidence") or []]

    raw = out.get("what_changed") if isinstance(out.get("what_changed"), dict) else {}
    changed: dict[str, Any] = {}
    if str(raw.get("what_changed") or "").strip():
        changed = {key: str(raw.get(key) or "").strip() for key in ("what_changed", "changed_at", "why_not_earlier")}
        for side in ("before", "after"):
            part = raw.get(side) if isinstance(raw.get(side), dict) else {}
            changed[side] = {"statement": str(part.get("statement") or ""), "evidence_ids": _clean(store, investigation_id, part.get("evidence_ids"), stats)}
            if not changed[side]["evidence_ids"]:
                out["assumptions"].append(f"The state {side} the change ({changed[side]['statement'] or 'not described'}) is not backed by evidence")
    out["what_changed"] = changed
    out["why_now_evidenced"] = bool(changed and changed["before"]["evidence_ids"] and changed["after"]["evidence_ids"])
    out["changes_considered"] = [
        {"change": str(c.get("change") or ""), "when": str(c.get("when") or ""), "timing": str(c.get("timing") or ""),
         "disposition": str(c.get("disposition") or "unexamined"), "reason": str(c.get("reason") or ""),
         "evidence_ids": _clean(store, investigation_id, c.get("evidence_ids"), stats)}
        for c in out.get("changes_considered") or [] if isinstance(c, dict)
    ]
    out["citations"] = stats
    return out


def cited_ids(report: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for fact in report.get("facts") or []:
        ids += fact.get("evidence_ids") or []
    for h in report.get("hypotheses") or []:
        for side in ("supporting", "contradicting"):
            for c in h.get(side) or []:
                ids += c.get("evidence_ids") or []
    for a in report.get("recommended_actions") or []:
        ids += a.get("evidence_ids") or []
    for side in ("before", "after"):
        ids += ((report.get("what_changed") or {}).get(side) or {}).get("evidence_ids") or []
    for c in report.get("changes_considered") or []:
        ids += c.get("evidence_ids") or []
    return list(dict.fromkeys(ids))


def _ids(ids: list[str]) -> str:
    return f" [{', '.join(ids)}]" if ids else ""


def render_markdown(report: dict[str, Any], incident: dict[str, Any] | None = None) -> str:
    rc = report.get("root_cause") or {}
    lines = [f"# Incident report: {(incident or {}).get('title') or (incident or {}).get('id') or 'investigation'}", ""]
    lines += [f"**Status:** {report.get('status', 'inconclusive')}", "", report.get("summary", ""), ""]
    lines += ["## Root cause" if report.get("status") == "conclusive" else "## Best candidate (not confirmed)", ""]
    lines += [f"- Service: {rc.get('service', 'unknown')}", f"- Component: {rc.get('component', '') or 'not identified'}",
              f"- Category: {rc.get('category', 'unknown')}", "", rc.get("summary", ""), ""]
    changed = report.get("what_changed") or {}
    lines += ["## What changed (why now)" if report.get("status") == "conclusive" else "## What changed (why now, not confirmed)", ""]
    if changed:
        lines.append(f"{changed['what_changed']}{' (at ' + changed['changed_at'] + ')' if changed.get('changed_at') else ''}")
        lines += ["", f"- Before: {changed['before']['statement']}{_ids(changed['before']['evidence_ids'])}",
                  f"- After: {changed['after']['statement']}{_ids(changed['after']['evidence_ids'])}"]
        if changed.get("why_not_earlier"):
            lines.append(f"- Why it did not fail earlier: {changed['why_not_earlier']}")
    else:
        lines.append("Not established: nothing was shown to differ from when the system last worked.")
    considered = report.get("changes_considered") or []
    if considered:
        lines += ["", "Changes before the incident, and what was concluded about each:", ""]
        for c in considered:
            timing = f", {c['timing']}" if c.get("timing") else ""
            lines.append(f"- **{c['disposition'].replace('_', ' ')}** — {c['change']} ({c['when']}{timing}){': ' + c['reason'] if c['reason'] else ''}{_ids(c['evidence_ids'])}")
    lines += ["", "## Hypotheses, ranked", ""]
    for h in report.get("hypotheses") or []:
        lines.append(f"{h.get('rank')}. **{h.get('statement', '')}** ({h.get('service', '')}, {h.get('category', '')}) — {h.get('verdict', '')}, confidence {h.get('confidence', 0):.2f}")
        for c in h.get("supporting") or []:
            lines.append(f"   - Supports: {c['statement']}{_ids(c['evidence_ids'])}")
        for c in h.get("contradicting") or []:
            lines.append(f"   - Contradicts: {c['statement']}{_ids(c['evidence_ids'])}")
    lines += ["", "## Facts", ""] + [f"- {f['statement']}{_ids(f['evidence_ids'])}" for f in report.get("facts") or []]
    lines += ["", "## Assumptions", ""] + ([f"- {a}" for a in report.get("assumptions") or []] or ["- None stated"])
    lines += ["", "## Recommended actions", ""]
    for a in report.get("recommended_actions") or []:
        lines.append(f"- [{a.get('priority', 'soon')}] {a.get('action', '')} — {a.get('rationale', '')}{_ids(a.get('evidence_ids') or [])}")
    if report.get("missing_evidence"):
        lines += ["", "## Missing evidence", ""] + [f"- {m}" for m in report["missing_evidence"]]
    meta = report.get("meta") or {}
    if meta:
        lines += ["", "## Run", ""]
        for key in ("system", "termination", "rounds", "hypotheses", "steps", "max_hops", "tool_calls", "tool_failures", "guardrail_rejections", "llm_calls", "total_tokens", "cost_usd", "latency_s"):
            if key in meta:
                lines.append(f"- {key.replace('_', ' ')}: {meta[key]}")
        c = report.get("citations") or {}
        lines.append(f"- citations: {c.get('cited', 0)} checked, {c.get('invalid', 0)} invalid, {c.get('facts_demoted', 0)} facts demoted to assumptions")
    return "\n".join(lines) + "\n"
