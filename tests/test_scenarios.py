"""Every scenario builds deterministically, hides its answer, and can be solved with the tools."""

import json
import re
import sqlite3
import subprocess

import pytest

from incident_agents.evidence import EvidenceStore
from incident_agents.gateway import QueryGateway, parse_sources
from incident_agents.scenarios import CATEGORIES, SCENARIO_IDS, build_all, load_scenario
from incident_agents.secrets import CredentialBroker, EnvSecretStore
from incident_agents.tools import Toolbox, parse_bindings

GIVEAWAYS = re.compile(
    r"root[ _-]?cause|red[ _-]?herring|\bculprit\b|\bscenario\b|\bsynthetic\b|\bsimulated\b|\binjected\b|golden[ _]path|answer[ _]key|\bregression\b",
    re.I,
)


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("scenarios")
    return {p.name: load_scenario(p) for p in build_all(out)}


def toolbox(scenario):
    gw = QueryGateway.from_config(parse_sources(scenario.config), CredentialBroker(store=EnvSecretStore({})))
    return Toolbox(gw, EvidenceStore(), parse_bindings(scenario.config), scenario.id)


def data_text(scenario) -> str:
    parts = [(scenario.path / "incident.json").read_text()]
    for f in sorted((scenario.path / "data").iterdir()):
        if f.suffix in (".jsonl", ".json"):
            parts.append(f.read_text())
    db = sqlite3.connect(scenario.path / "data" / "app.db")
    parts.append("\n".join(db.iterdump()))
    db.close()
    return "\n".join(parts)


def leaves(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from leaves(v)
    elif isinstance(value, list):
        for v in value:
            yield from leaves(v)
    else:
        yield str(value)


def test_the_dataset_has_the_twelve_scenarios(built):
    assert list(built) == SCENARIO_IDS and len(built) == 12
    tags = [t for s in built.values() for t in s.answer["tags"]]
    assert tags.count("misleading") >= 3 and tags.count("conflicting") >= 2 and tags.count("tool_failure") >= 2
    differs = [s.id for s in built.values() if s.incident["service"] != s.answer["root_cause"]["service"]]
    assert len(differs) >= 4, differs
    assert len({s.answer["root_cause"]["category"] for s in built.values()}) >= 6


def test_builds_are_deterministic(built, tmp_path):
    sample = ["idempotency-key-regression", "poison-receipt-requeue-loop", "worker-regression-with-log-failures"]
    again = {p.name: load_scenario(p) for p in build_all(tmp_path, only=sample)}
    for sid in sample:
        a, b = built[sid].path, again[sid].path
        for name in ("incident.json", "answer.json", "sources.yaml", "data/logs.jsonl", "data/metrics.json", "data/deploys.jsonl",
                     "data/config_changes.jsonl", "data/traces.jsonl", "data/incidents.jsonl", "data/topology.jsonl"):
            assert (a / name).read_bytes() == (b / name).read_bytes(), f"{sid}: {name} differs between builds"
        dumps = []
        for root in (a, b):
            db = sqlite3.connect(root / "data" / "app.db")
            dumps.append("\n".join(db.iterdump()))
            db.close()
        assert dumps[0] == dumps[1]
        for repo in ("fivo-api", "fivo-worker"):
            heads = [subprocess.run(["git", "-C", str(root / "repos" / repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout
                     for root in (a, b)]
            assert heads[0] == heads[1] and len(heads[0].strip()) == 40


@pytest.mark.parametrize("sid", SCENARIO_IDS)
def test_layout_incident_and_answer_shape(built, sid):
    s = built[sid]
    names = {p.name for p in (s.path / "data").iterdir()}
    assert names == {"logs.jsonl", "metrics.json", "deploys.jsonl", "config_changes.jsonl", "topology.jsonl", "traces.jsonl", "incidents.jsonl", "app.db"}
    assert (s.path / "answer.json").exists() and "answer" not in (s.path / "sources.yaml").read_text()
    assert set(s.incident) >= {"id", "title", "service", "severity", "detected_at", "window", "description"}
    assert s.incident["window"]["start"] < s.incident["detected_at"] < s.incident["window"]["end"]
    answer = s.answer
    assert s.id == sid and re.fullmatch(r"INC-2026-\d{3}", s.incident["id"]) and sid not in json.dumps(s.incident)
    assert answer["id"] == sid and answer["root_cause"]["category"] in CATEGORIES and answer["min_hops"] >= 2
    assert answer["root_cause"]["trigger"]["type"] in {"deploy", "config_change", "external", "data", "traffic", "none"}
    assert all(a["category"] in CATEGORIES for a in answer["acceptable"])
    assert len(answer["key_evidence"]) >= 3 and answer["red_herrings"] and len(answer["golden_path"]) >= 5
    assert len({step["tool"].split("_")[0] for step in answer["golden_path"]}) >= 3, "the path must cross several kinds of source"
    logs = [json.loads(line) for line in (s.path / "data" / "logs.jsonl").read_text().splitlines()]
    assert len(logs) > 3000 and logs[0]["ts"] < s.incident["window"]["start"] and logs[-1]["ts"] > s.incident["detected_at"]
    assert {"fivo-api", "fivo-worker"} <= {line["service"] for line in logs}
    for repo in ("fivo-api", "fivo-worker"):
        count = subprocess.run(["git", "-C", str(s.path / "repos" / repo), "rev-list", "--count", "HEAD"], capture_output=True, text=True, check=True)
        assert int(count.stdout) >= 7


@pytest.mark.parametrize("sid", SCENARIO_IDS)
def test_the_answer_is_not_leaked(built, sid):
    s = built[sid]
    text = data_text(s)
    found = GIVEAWAYS.search(text)
    assert found is None, f"giveaway {found.group(0)!r} near: {text[max(0, found.start() - 80): found.end() + 80]!r}"
    lowered = text.lower()
    for sentence in re.split(r"(?<=[.!?])\s+", s.answer["root_cause"]["summary"]):
        assert sentence.lower() not in lowered
    # The page describes symptoms; it must not name the trigger.
    trigger = s.answer["root_cause"]["trigger"]["ref"]
    page = json.dumps(s.incident)
    assert not trigger or trigger not in page
    assert not re.search(r"deploy|config|commit|quota|migration|rotat", s.incident["description"], re.I)
    for source in s.config["sources"].values():
        assert "answer" not in json.dumps(source)


@pytest.mark.parametrize("sid", SCENARIO_IDS)
async def test_the_golden_path_reaches_the_key_evidence(built, sid):
    s = built[sid]
    box = toolbox(s)
    assert len(box.specs()) == 19
    collected = []
    for step in s.answer["golden_path"]:
        result = await box.call(step["tool"], step["args"])
        if step.get("expect") == "error":
            assert not result.ok, f"{step['tool']} was expected to fail"
            continue
        if step.get("retry") and not result.ok:
            first = result.status.value
            result = await box.call(step["tool"], step["args"])
            assert first in ("timeout", "error")
        assert result.ok, f"{step['tool']} {step['args']} -> {result.status.value}: {result.violations or result.error}"
        collected += result.evidence
    seen = "\n".join(e.summary + "\n" + json.dumps(e.data, default=str) + "\n" + "\n".join(leaves(e.data)) for e in collected)
    for item in s.answer["key_evidence"]:
        assert item["must_contain"] in seen, f"{sid}: no collected evidence contains {item['must_contain']!r}"
    known, invented = box.store.check_citations(s.id, [e.id for e in collected])
    assert not invented and len(known) >= 5
    sources_used = {c.tool.split("_")[0] for c in box.calls}
    assert len(sources_used) >= 3


async def test_tool_failures_are_real_in_the_failure_scenarios(built):
    logs_down = toolbox(built["worker-regression-with-log-failures"])
    window = built["worker-regression-with-log-failures"].incident["window"]
    first = await logs_down.call("logs_count", {"service": "fivo-api", **window})
    assert first.status.value == "timeout"
    second = await logs_down.call("logs_count", {"service": "fivo-api", **window})
    assert second.ok
    third = await logs_down.call("logs_count", {"service": "fivo-worker", "group_by": ["level"], **window})
    assert third.status.value == "partial" and not third.evidence[0].complete

    voice = toolbox(built["voice-errors-missing-telemetry"])
    for _ in range(3):
        traces = await voice.call("traces_search", {"service": "fivo-api"})
        assert traces.status.value == "error" and "not available" in traces.error
    deploys = await voice.call("deploys_list", {})
    assert deploys.ok and len(deploys.evidence) == 4  # the other collections of the same source still answer
    assert (await voice.call("metrics_list", {"service": "bolna-voice"})).info == []
