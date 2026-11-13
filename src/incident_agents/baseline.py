"""Single-agent baseline: one model, every tool, one loop.

It runs on the same tools, gateway, guardrails and evidence store as the
multi-agent system, with the same strong model and the same report format,
so the comparison isolates the coordination: no specialists, no threads, no
separate reasoning step, no critic.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .agents.definitions import BASELINE_REPORT_SCHEMA, BASELINE_SYSTEM
from .agents.runtime import AgentSpec, run_agent
from .llm import LLM
from .report import finalize
from .tools import Toolbox


async def run_baseline(incident: dict[str, Any], toolbox: Toolbox, llm: LLM, *, max_tool_calls: int = 60, notes: list[str] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    spec = AgentSpec(
        name="single_agent", role="baseline", system=BASELINE_SYSTEM, schema=BASELINE_REPORT_SCHEMA,
        tools=list(toolbox.tools), max_tool_calls=max_tool_calls, max_turns=max_tool_calls + 6,
    )
    prompt = "\n\n".join([
        "# Incident", json.dumps(incident, indent=1, default=str),
        *(["# Notes from the human operator (context, not evidence)", json.dumps(notes, indent=1)] if notes else []),
        "# Limits", f"You have at most {max_tool_calls} tool calls. Investigate, then write the report.",
    ])
    run = await run_agent(spec, llm, toolbox, prompt, intent="single-agent investigation")
    if run.ok:
        report = run.output
        termination = "agent finished"
    else:
        termination = f"agent failed: {run.error}"
        report = {
            "status": "inconclusive", "summary": f"The single agent did not produce a report ({run.error}).",
            "root_cause": {"service": "unknown", "component": "", "category": "unknown", "summary": ""},
            "hypotheses": [], "facts": [], "assumptions": [], "recommended_actions": [], "missing_evidence": [run.error],
            "what_changed": {}, "changes_considered": [],
        }
    report = finalize(report, toolbox.store, toolbox.investigation_id)
    llm_totals = llm.ledger.totals()
    calls = toolbox.calls
    report["meta"] = {
        "system": "single_agent",
        "termination": termination,
        "tool_calls": len(calls),
        "tool_failures": len([c for c in calls if c.status in ("timeout", "error", "unavailable")]),
        "guardrail_rejections": len([c for c in calls if c.status == "rejected"]),
        "evidence_records": toolbox.store.count(toolbox.investigation_id),
        "llm_calls": llm_totals["calls"],
        "input_tokens": llm_totals["input_tokens"],
        "output_tokens": llm_totals["output_tokens"],
        "total_tokens": llm_totals["total_tokens"],
        "cost_usd": llm_totals["cost_usd"],
        "latency_s": round(time.monotonic() - started, 1),
    }
    return report
