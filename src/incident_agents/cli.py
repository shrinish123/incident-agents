"""Command line.

  python -m incident_agents scenarios build            generate the synthetic incident dataset
  python -m incident_agents scenarios list
  python -m incident_agents investigate <scenario-id>  run one investigation, print the trace and report
  python -m incident_agents investigate --config sources.yaml --incident incident.json
                                                       run against your own systems
  python -m incident_agents investigate <scenario-id> --interactive --note "we rotated the R2 keys this morning"
                                                       with a human at the checkpoints
  python -m incident_agents eval                       run both systems over every scenario and grade them
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .evaluation import SYSTEMS, aggregate, evaluate, grade, run_system
from .llm import UsageLedger, detect_provider, have_credentials, make_llm
from .orchestrator import Limits
from .report import render_markdown
from .wiring import load_config

DATA = Path(".data/scenarios")


def load_dotenv(path: str = ".env") -> None:
    """Read KEY=value lines into the environment without overriding what is already set."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip().removeprefix("export ").strip(), value.strip().strip("'\""))


def print_event(event: dict[str, Any]) -> None:
    skip = {"seq", "t", "agent", "type"}
    detail = "  ".join(f"{k}={json.dumps(v, default=str) if isinstance(v, (dict, list)) else v}" for k, v in event.items() if k not in skip)
    if len(detail) > 420:
        detail = detail[:417] + "..."
    print(f"[{event['t']:7.1f}s] {event['agent']:<13} {event['type']:<14} {detail}", flush=True)


class StdinOperator:
    """The human in the loop, on a terminal. Asked after triage, after each round's review, and to approve costly queries."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()  # one question at a time, even when threads run in parallel

    async def _ask(self, prompt: str) -> str:
        async with self._lock:
            return (await asyncio.to_thread(input, prompt)).strip()

    async def __call__(self, checkpoint: str, view: dict[str, Any]) -> dict[str, Any]:
        print(f"\n--- Checkpoint: {checkpoint} " + "-" * 50)
        if checkpoint == "triage":
            triage = view.get("triage") or {}
            print(f"Symptoms: {triage.get('summary') or 'not established'}\nOnset: {triage.get('onset') or 'unclear'}")
        else:
            print(f"Round {view.get('round')}. Next: {view.get('next')}")
            for h in view.get("hypotheses") or []:
                critic = f", critic: {h['critic']['verdict']}" if h.get("critic") else ""
                print(f"  {h['id']} [{h['status']}, {h['confidence']:.2f}{critic}] {h['statement']}")
                if h.get("what_changed"):
                    print(f"      what changed: {h['what_changed']}")
        for c in view.get("changes") or []:
            print(f"  {c['id']} {c['kind']} on {c['service']} at {c['at']} ({c['timing']}): {c['disposition']}")
        answer = await self._ask("Enter to continue, 'stop' to end and report, or type what you know: ")
        if answer.lower() == "stop":
            return {"action": "stop"}
        return {"note": answer}

    async def approve(self, request: Any, estimate: Any) -> bool:
        answer = await self._ask(f"\nApprove a query on {request.source} costing about {estimate.amount:g} {estimate.unit}? ({request.intent}) [y/N] ")
        return answer.lower() in ("y", "yes")


def _scenarios(ids: list[str] | None, root: Path) -> list[Any]:
    from .scenarios import SCENARIO_IDS, build_scenario, load_scenario

    out = []
    for sid in ids or SCENARIO_IDS:
        path = root / sid
        if not (path / "incident.json").exists():
            print(f"building scenario {sid}", flush=True)
            build_scenario(sid, root)
        out.append(load_scenario(path))
    return out


def _need_credentials() -> None:
    if not have_credentials():
        sys.exit("No model credentials found. Put a provider key in .env (GROQ_API_KEY, NVIDIA_API_KEY, CEREBRAS_API_KEY or ANTHROPIC_API_KEY), "
                 "or set IA_PROVIDER=ollama for a local model. See .env.example.")
    print(f"Model provider: {detect_provider()}", flush=True)


def cmd_scenarios(args: argparse.Namespace) -> None:
    from .scenarios import SCENARIO_IDS, build_all, load_scenario

    if args.action == "build":
        for path in build_all(Path(args.out)):
            print(f"built {path}")
    else:
        for sid in SCENARIO_IDS:
            path = Path(args.out) / sid
            title = load_scenario(path).incident.get("title", "") if (path / "incident.json").exists() else "(not built)"
            print(f"{sid:<42} {title}")


def cmd_investigate(args: argparse.Namespace) -> None:
    _need_credentials()
    if args.config:
        if not args.incident:
            sys.exit("--config needs --incident <incident.json>")
        incident = json.loads(Path(args.incident).read_text())
        scenario = SimpleNamespace(id=str(incident.get("id", "incident")), incident=incident, config=load_config(args.config), answer=None)
    else:
        if not args.scenario:
            sys.exit("Give a scenario id (see: scenarios list) or --config with --incident")
        scenario = _scenarios([args.scenario], Path(args.data))[0]
    system = "single_agent" if args.single else "multi_agent"
    run_dir = Path(args.out) / f"{scenario.id}__{system}"
    print(f"Incident: {scenario.incident.get('title')}\n{scenario.incident.get('description', '')}\n", flush=True)
    report, toolbox = asyncio.run(run_system(system, scenario, make_llm(UsageLedger()), run_dir,
                                             limits=Limits(max_rounds=args.rounds), on_event=None if args.quiet else print_event,
                                             operator=StdinOperator() if args.interactive else None, notes=args.note))
    print("\n" + render_markdown(report, scenario.incident))
    if scenario.answer and args.grade:
        print("## Graded against the hidden answer key\n")
        print(json.dumps(grade(report, scenario.answer, toolbox.store, toolbox.investigation_id), indent=1))
    print(f"\nFiles: {run_dir}/ (report.md, report.json, trace.jsonl, tool_calls.jsonl, audit.jsonl, case.db, evidence.db)")


def cmd_eval(args: argparse.Namespace) -> None:
    _need_credentials()
    scenarios = _scenarios(args.scenarios.split(",") if args.scenarios else None, Path(args.data))
    systems = tuple(s for s in SYSTEMS if s in args.systems.split(","))
    out = Path(args.out)
    rows = asyncio.run(evaluate(scenarios, out, systems=systems, runs=args.runs, concurrency=args.concurrency, limits=Limits(max_rounds=args.rounds),
                                max_cost_usd=args.max_cost, resume=args.resume))
    print("\n" + json.dumps(aggregate(rows), indent=1))
    print(f"\nWritten: {out}/summary.md, {out}/results.json, {out}/runs/")


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="incident_agents", description="Multi-agent production incident investigation")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scenarios", help="build or list the synthetic incident dataset")
    p.add_argument("action", choices=["build", "list"])
    p.add_argument("--out", default=str(DATA))
    p.set_defaults(fn=cmd_scenarios)

    p = sub.add_parser("investigate", help="investigate one incident")
    p.add_argument("scenario", nargs="?", help="scenario id")
    p.add_argument("--config", help="sources and capabilities YAML for your own systems")
    p.add_argument("--incident", help="incident JSON (with --config)")
    p.add_argument("--single", action="store_true", help="run the single-agent baseline instead")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--grade", action="store_true", help="grade the report against the scenario's answer key")
    p.add_argument("--quiet", action="store_true", help="do not print the live trace")
    p.add_argument("--interactive", action="store_true", help="pause after triage and after each review so you can add context or stop")
    p.add_argument("--note", action="append", default=[], help="context from you, given to the agents as a lead (not evidence); repeatable")
    p.add_argument("--data", default=str(DATA))
    p.add_argument("--out", default="results/investigations")
    p.set_defaults(fn=cmd_investigate)

    p = sub.add_parser("eval", help="run and grade both systems over the scenario set")
    p.add_argument("--scenarios", help="comma-separated scenario ids (default: all)")
    p.add_argument("--systems", default=",".join(SYSTEMS))
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--max-cost", type=float, help="stop starting new runs once the evaluation has cost this many USD (IA_MAX_COST_USD caps a single run)")
    p.add_argument("--resume", action="store_true", help="reuse runs already graded in --out instead of running them again")
    p.add_argument("--data", default=str(DATA))
    p.add_argument("--out", default="results/eval")
    p.set_defaults(fn=cmd_eval)

    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
