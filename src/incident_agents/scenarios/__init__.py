"""Synthetic incident scenarios for evaluation.

Each scenario is one production incident in a small simulated product: an API,
a worker, a database, a queue and several third-party providers. Building a
scenario writes telemetry, a database, two real git repositories and the
source configuration the agents run against, plus a hidden answer key the
agents never see.

    python -m incident_agents.scenarios build [--out .data/scenarios] [--only <id>]

    scenario = load_scenario(path)
    sources, bindings = parse_sources(scenario.config), parse_bindings(scenario.config)
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CATEGORIES = (
    "code_regression", "config_change", "dependency_failure", "resource_exhaustion",
    "data_issue", "infrastructure", "traffic_change", "unknown",
)

from .catalog import SCENARIOS  # noqa: E402
from .world import World  # noqa: E402

SCENARIO_IDS = list(SCENARIOS)


@dataclass
class Scenario:
    id: str  # scenario id; names the cause, so never show it to the agents (incident["id"] is the opaque one)
    path: Path
    incident: dict[str, Any]  # what the investigating system is given
    config: dict[str, Any]  # sources + capabilities, paths absolute
    answer: dict[str, Any]  # hidden answer key, for the grader only


def build_scenario(scenario_id: str, out_dir: str | Path) -> Path:
    """Build one scenario under out_dir/<id>, replacing any earlier build."""
    spec = SCENARIOS[scenario_id]
    path = Path(out_dir).resolve() / scenario_id
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    world = World(scenario_id, path, spec["detected_at"], spec["seed"], **spec.get("world", {}))
    incident, answer = spec["build"](world)
    answer = {"id": scenario_id, **answer}
    if answer["root_cause"]["category"] not in CATEGORIES:
        raise ValueError(f"{scenario_id}: unknown category")
    # The page carries an opaque incident number: the scenario id names the cause and must not reach the agents.
    world.write({"id": f"INC-2026-{41 + SCENARIO_IDS.index(scenario_id):03d}", **incident}, answer)
    return path


def build_all(out_dir: str | Path, only: list[str] | None = None) -> list[Path]:
    return [build_scenario(sid, out_dir) for sid in SCENARIO_IDS if not only or sid in only]


def load_scenario(path: str | Path) -> Scenario:
    root = Path(path).resolve()
    config = yaml.safe_load((root / "sources.yaml").read_text())
    for source in config["sources"].values():
        options = source.get("options") or {}
        for key in ("data_dir", "mirror_dir"):
            if key in options:
                options[key] = str(root / options[key])
        for repo in (options.get("repos") or {}).values():
            repo["url"] = str(root / repo["url"])
    incident = json.loads((root / "incident.json").read_text())
    answer = json.loads((root / "answer.json").read_text())
    return Scenario(id=answer["id"], path=root, incident=incident, config=config, answer=answer)


__all__ = ["CATEGORIES", "SCENARIO_IDS", "Scenario", "build_all", "build_scenario", "load_scenario"]
