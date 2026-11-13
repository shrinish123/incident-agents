"""The agents: what each one is told, which tools it may call and what it must return.

  observability  logs, metrics and traces: what is failing, where, since when
  dependency     deploys, config changes, topology, upstream health, past incidents
  data           the application database, through guarded read-only SQL
  code           source at the deployed commit: diffs, files, search
  reasoning      forms hypotheses with test plans, then weighs what came back
  critic         attacks the leading hypothesis's causal chain before it is reported
  reporting      writes the final report from the case file

The orchestrator is code, not a model (see orchestrator.py): it schedules
steps, enforces budgets and decides when to stop.
"""

from __future__ import annotations

from typing import Any

from ..casefile import CATEGORIES, CHAIN_ROLES, CHANGE_DISPOSITIONS, SPECIALISTS
from .runtime import AgentSpec

# --- Schema helpers (structured outputs need every object closed and fully required) ---


def obj(**props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def arr(items: dict[str, Any], description: str = "") -> dict[str, Any]:
    return {"type": "array", "items": items, **({"description": description} if description else {})}


def s(description: str = "", enum: tuple[str, ...] | list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "string"}
    if description:
        out["description"] = description
    if enum:
        out["enum"] = list(enum)
    return out


def num(description: str = "") -> dict[str, Any]:
    return {"type": "number", "description": description}


IDS = arr(s(), "Evidence IDs exactly as tools returned them, e.g. log:fivo-api:0003.")
CITED = obj(statement=s(), evidence_ids=IDS)
NEXT_STEP = obj(
    specialist=s("Who should answer.", SPECIALISTS),
    question=s("One concrete question, naming the service, window and identifiers to use."),
    confirms_if=s("What result would support the hypothesis."),
    refutes_if=s("What result would rule it out."),
)
NEW_HYPOTHESIS = obj(
    statement=s("One sentence: what broke, where, and why."),
    service=s("The service or dependency at fault (not necessarily where the alert fired)."),
    category=s("Kind of cause.", CATEGORIES),
    rationale=s("Which findings point here."),
    prior=num("Your probability, 0 to 1, before testing."),
    test_plan=arr(NEXT_STEP, "Ordered steps. Each step can use what the one before it found."),
)

WHY_NOW = obj(
    what_changed=s("What is different from when the system last worked: the deploy, setting, data, load or upstream behaviour that changed. Empty string if not established."),
    changed_at=s("When it changed (ISO 8601 UTC). Empty string if unknown."),
    before=obj(statement=s("The last known good state: what the same code, setting or signal looked like before the change."), evidence_ids=IDS),
    after=obj(statement=s("The state once it had changed."), evidence_ids=IDS),
    why_not_earlier=s("If the faulty code, setting or condition was already there before the onset, what made it start failing now. Empty string if the change itself is recent."),
)

SPECIALIST_SCHEMA = obj(
    answer=s("Direct answer to the question, two to four sentences."),
    verdict=s("What your findings mean for the hypothesis.", ["supports", "contradicts", "inconclusive"]),
    strength=s("strong: the evidence settles the question. weak: suggestive or incomplete.", ["strong", "weak"]),
    findings=arr(obj(
        statement=s("One fact or inference, specific: values, times, names."),
        kind=s("observed: read directly from cited evidence. inferred: your reasoning over observations.", ["observed", "inferred"]),
        evidence_ids=IDS,
        stance=s("", ["supports", "contradicts", "neutral"]),
    )),
    leads=arr(obj(
        statement=s("Something unexpected worth following that your question did not cover."),
        evidence_ids=IDS,
        specialist=s("Who could follow it.", SPECIALISTS),
        question=s("The question to ask them."),
    )),
    tool_problems=arr(s(), "Tools that failed or returned partial data, and what that left unchecked."),
)

TRIAGE_SCHEMA = obj(
    summary=s("What is happening, in two or three sentences. Symptoms and scope only, no cause."),
    onset=s("When the problem started (ISO 8601 UTC), or an empty string if unclear."),
    affected_services=arr(s()),
    findings=arr(obj(statement=s(), kind=s("", ["observed", "inferred"]), evidence_ids=IDS)),
    tool_problems=arr(s()),
)

PLAN_SCHEMA = obj(hypotheses=arr(NEW_HYPOTHESIS))

ASSESS_SCHEMA = obj(
    assessments=arr(obj(
        hypothesis_id=s(),
        status=s("supported: evidence backs every link. refuted: evidence contradicts it. inconclusive: tested, still unclear. open: needs the next_steps.", ["supported", "refuted", "inconclusive", "open"]),
        confidence=num("Probability this is the root cause, 0 to 1."),
        assessment=s("Why, in two or three sentences, citing finding IDs."),
        causal_chain=arr(obj(
            role=s("trigger: what changed or failed first. mechanism: how that produced the failure. symptom: what the alert saw.", CHAIN_ROLES),
            statement=s(),
            evidence_ids=IDS,
            basis=s("observed: the evidence shows it. inferred: follows from observed links. assumed: no evidence yet.", ["observed", "inferred", "assumed"]),
        )),
        why_now=WHY_NOW,
        supporting_finding_ids=arr(s()),
        contradicting_finding_ids=arr(s()),
        missing=arr(s(), "Evidence that would settle this and is not yet in the case file."),
        next_steps=arr(NEXT_STEP, "Further steps for this hypothesis. Empty when nothing more is worth testing."),
    )),
    new_hypotheses=arr(NEW_HYPOTHESIS, "Only when the findings or a lead point somewhere no current hypothesis covers."),
    lead_decisions=arr(obj(lead_id=s(), decision=s("", ["adopt", "dismiss"]), reason=s())),
    change_decisions=arr(obj(
        change_id=s("ID from the changes list, e.g. C2."),
        disposition=s("cause: it is the trigger of the leading hypothesis. contributing: it made things worse. ruled_out: it cannot explain the incident.", CHANGE_DISPOSITIONS),
        reason=s("Why, from the evidence: wrong service, a diff that cannot produce the failure, timing, scope."),
        evidence_ids=IDS,
    ), "One entry for every change in the list whose disposition you can decide now."),
)

CRITIC_SCHEMA = obj(
    reviews=arr(obj(
        hypothesis_id=s(),
        verdict=s("accept: the chain holds. needs_work: a specific gap can be closed with the followups. reject: the evidence does not support it.", ["accept", "needs_work", "reject"]),
        confidence_cap=num("The highest confidence the evidence justifies, 0 to 1."),
        link_checks=arr(obj(
            role=s("A link of the chain, or why_now for the before/after comparison.", [*CHAIN_ROLES, "why_now"]),
            supported=s("Does the cited evidence actually show what the link claims?", ["yes", "partly", "no"]),
            issue=s("What is wrong or missing. Empty string if nothing."),
        )),
        issues=arr(s(), "Contradicting evidence not addressed, timing that does not fit, correlation presented as cause."),
        alternatives=arr(s(), "Other explanations the same evidence fits."),
        followups=arr(obj(specialist=s("", SPECIALISTS), question=s(), why=s()), "Only for needs_work: the fewest steps that would close the gap."),
    )),
)

REPORT_SCHEMA = obj(
    status=s("conclusive only if one hypothesis has an evidenced trigger, mechanism and symptom and survived review.", ["conclusive", "inconclusive"]),
    summary=s("Three to five sentences a responder can act on."),
    root_cause=obj(
        service=s("Service or dependency at fault. Best candidate if inconclusive; 'unknown' if there is none."),
        component=s("The specific part: function, setting, table, upstream API."),
        category=s("", CATEGORIES),
        summary=s("The causal chain in two or three sentences."),
    ),
    hypotheses=arr(obj(
        id=s(),
        rank=num("1 is most likely."),
        statement=s(),
        service=s(),
        category=s("", CATEGORIES),
        confidence=num("0 to 1."),
        verdict=s("", ["most_likely", "possible", "ruled_out"]),
        supporting=arr(CITED),
        contradicting=arr(CITED),
    ), "Every hypothesis considered, ranked, including the ones ruled out."),
    facts=arr(CITED, "What the evidence shows directly. Every fact cites evidence."),
    assumptions=arr(s(), "What the conclusion relies on without direct evidence."),
    recommended_actions=arr(obj(action=s(), rationale=s(), priority=s("", ["immediate", "soon", "later"]), evidence_ids=IDS)),
    missing_evidence=arr(s(), "What could not be checked and why (tool failures, data not available)."),
)

# The single agent writes these two itself. In the multi-agent system they come from the case file, in code.
CHANGE_FIELDS = dict(
    what_changed=WHY_NOW,
    changes_considered=arr(obj(
        change=s("The deploy or configuration change: service, what, identifier."),
        when=s("ISO 8601 UTC."),
        disposition=s("", CHANGE_DISPOSITIONS),
        reason=s(),
        evidence_ids=IDS,
    ), "Every deploy and configuration change in the day before the incident, and what you concluded about each."),
)
BASELINE_REPORT_SCHEMA = obj(**REPORT_SCHEMA["properties"], **CHANGE_FIELDS)

# --- Prompts ------------------------------------------------------------------------

EVIDENCE_RULES = """\
Evidence rules
- Every tool result that matters comes back as evidence records with IDs. Cite those IDs exactly. Never invent or alter an ID: citations are checked, and invented ones are removed.
- A finding is "observed" only if cited evidence shows it directly. Reasoning over observations is "inferred". Say which.
- A result with no matches is evidence too (it has an ID). Use it to rule things out.
- Evidence marked partial is incomplete: treat its counts as lower bounds and say so.
- Quote specifics: timestamps, counts, values, commit SHAs, setting names. All times are UTC."""

TOOL_RULES = """\
Working with tools
- Queries are guarded. A rejected call tells you why and how to fix it: read the hint and adjust rather than repeating the call.
- Start cheap: counts and aggregates before raw lines, short windows around the time that matters, one service at a time.
- Compare against normal. A number means little without the same number from before the incident. logs_count and metrics_query take baseline_start and baseline_end: pass a window from before the onset and the one evidence record holds both periods, what is new and what grew. Use it to tell a change from background noise.
- Tools can time out, error or return partial data. Retry once, then get the answer another way if you can. Record what you could not check in tool_problems. Never fill a gap with a guess.
- Call independent tools in the same turn. You have a limited number of tool calls: spend them on the question you were asked."""

SPECIALIST_TASK = """\
You are one of several agents investigating a production incident. Other agents cover other sources; the shared case file below is how you see their work. You are given one hypothesis and one question. Answer that question with evidence.

- Use the identifiers already found (trace IDs, user IDs, commit SHAs, error fingerprints, times) to aim your queries. That is how the investigation moves from one system to the next.
- Look for evidence against the hypothesis as hard as evidence for it. A clean refutation is as useful as a confirmation.
- A change that happened near the incident is not a cause until something connects it to the failure. Report the timing precisely and let the evidence decide.
- When you examine a change (a deploy, a setting, a migration), report what it was before and what it is after, with the evidence for each, and whether that difference can produce the failure.
- If you notice something that matters but is outside your question, report it as a lead instead of chasing it.
- Your final message is a JSON object in the required format, nothing else."""

SPECIALIST_ROLES = {
    "observability": "You are the Observability agent. You read logs, metrics and traces to establish what is failing, where, for whom and since when.",
    "dependency": "You are the Dependency agent. You establish what changed and what the service depends on: deploys, configuration and flag changes, service topology, the health of upstream dependencies, and similar past incidents. A past incident with the same symptom is a lead to verify, not an answer.",
    "data": "You are the Data agent. You query the application database (read-only, guarded) to check what the data itself shows: affected rows, job state, duplicates, backlogs, who is affected. Call db_schema first. Aggregate in SQL rather than pulling rows.",
    "code": "You are the Code agent. You read source code at the commit that was deployed. Use the sha and prev_sha from deploy records to diff what changed, read the changed code, and decide whether the change can produce the observed failure. Say exactly which lines do it, or that the change cannot explain it.",
}

SPECIALIST_TOOLS = {
    "observability": ["logs_count", "logs_fetch", "metrics_list", "metrics_query", "traces_search", "evidence_get", "evidence_find"],
    "dependency": ["deploys_list", "config_changes_list", "topology_get", "incidents_search", "traces_search", "metrics_list", "metrics_query", "logs_count", "evidence_get", "evidence_find"],
    "data": ["db_schema", "db_query", "evidence_get", "evidence_find"],
    "code": ["code_repos", "deploys_list", "code_log", "code_changed_files", "code_diff", "code_read", "code_search", "evidence_get", "evidence_find"],
}

TRIAGE_TOOLS = {
    "observability": ["logs_count", "metrics_list", "metrics_query", "topology_get"],
    "dependency": ["deploys_list", "config_changes_list", "topology_get", "incidents_search"],
}

TRIAGE_TASK = {
    "observability": """\
This is triage: a first, cheap look before any hypothesis exists. Establish the symptoms only.
- Which services and routes are failing or slow, and how badly compared with before the incident.
- When it started, as precisely as the data allows.
- Whether dependencies of the alerting service show errors or latency of their own.
Use counts and metrics only. Do not explain the cause; that comes later.
Your final message is a JSON object in the required format, nothing else.""",
    "dependency": """\
This is triage: a first, cheap look before any hypothesis exists. Establish what changed and what is connected.
- What the alerting service depends on, and what depends on it.
- Every deploy and configuration change, for any service in that neighbourhood, in the hours before the incident, with exact times.
- Whether a past incident looks similar.
List changes without judging them: do not say which one is the cause. Put each change in findings with its evidence. Leave onset as an empty string.
Your final message is a JSON object in the required format, nothing else.""",
}

REASONING_SYSTEM = f"""\
You are the Reasoning agent in a team investigating a production incident. Specialist agents gather evidence (observability, dependency, data, code); you decide what to test and what the results mean. You have no tools: you work from the case file.

Planning (when asked for hypotheses)
- Propose two to four hypotheses that are genuinely different explanations of the symptoms, not variations of one. Include the obvious one and at least one that the obvious one would hide (an upstream dependency, a config change, data, load).
- The alert names where the symptom showed, which is often not where the cause is.
- Give each a test plan of two to four ordered steps, each for one specialist, each a concrete question with what would confirm and what would refute. Order steps so each can use what the previous one found: symptoms in logs, then the identifiers from those logs in traces or the database, then the deploy and the code diff.
- A good plan tries to break the hypothesis early. Put the cheapest decisive test first.
- Start from what changed. The symptom says what failed; the root cause is what is different from when the system last worked. For each recent change in the changes list, ask whether it could produce these symptoms, and plan a step that would show it (the diff, the old and new value). Also keep one explanation where nothing of ours changed: an upstream, the load, the data, or a limit reached.

Assessing (when asked to weigh results)
- Update every hypothesis from the findings. Be willing to refute your own favourite.
- A root cause needs a causal chain with three evidenced links: the trigger (what changed or failed first), the mechanism (how that produced the failure) and the symptom (what the alert saw). Timing that lines up is not a mechanism. Mark a link "assumed" when nothing shows it; never dress an assumption up as observed.
- Check that the trigger came before the onset and that the scope fits: if only some users or one route are affected, the cause must explain why.
- Answer "why now". For every hypothesis you mark supported, fill why_now: what changed, when, the state before with its evidence and the state after with its evidence. One record that shows both sides (a diff, a config change with old and new values, a count or metric with a baseline) can be cited for both. If the faulty code or setting was already there before the onset, why_not_earlier must say what made it fail now (load, data, an upstream, a quota). A hypothesis with no evidenced before and after is not supported: ask for the step that would show it.
- Account for every change in the changes list through change_decisions: cause, contributing or ruled_out, each with a reason from the evidence. "Another change explains it better" is not a reason; "the diff only touches the README" or "it is a different service that is not on the failing path" is. A conclusion does not stand while a recent change is unexamined, so plan a step for any you cannot decide.
- Operator notes are context from a human responder. Use them to decide what to test. They are not evidence and cannot be cited: what rests only on a note is an assumption.
- Only cite evidence IDs and finding IDs that appear in the case file.
- Ask for next steps only when a specific missing link could be evidenced by one more question. Otherwise leave next_steps empty and say what is missing.
- If the evidence cannot separate the hypotheses, say so. Inconclusive is an acceptable result; a confident wrong answer is not.

{EVIDENCE_RULES}

Your reply is a JSON object in the required format, nothing else."""

CRITIC_SYSTEM = f"""\
You are the Critic in a team investigating a production incident. Before a conclusion is reported, you try to break it. You are given the leading hypotheses with their causal chains and the full evidence records they cite. You have no tools.

For each hypothesis, check each link of the chain against the evidence it cites:
- Does the evidence actually show what the link claims, or only something nearby? Read the data, not the summary.
- Does the timing work? The trigger must precede the onset. A change that landed after the errors began cannot have caused them.
- Does the scope fit? The cause must explain which users, routes or services are affected and which are not.
- Is the mechanism evidenced (a diff that produces this error, a setting that produces this limit, an upstream that returned these failures), or is it correlation in time presented as cause?
- Is there contradicting evidence the assessment ignored?
- What else would produce the same evidence? Name the strongest alternative.
- Is any link resting on partial data or on a tool that failed?
- Why now: does the evidence show a real difference between before and after, or only the state during the incident? If the faulty thing existed before the onset, is there an evidenced reason it failed only now? Report this as a link_check with role why_now.
- The other changes: was each one ruled out for a stated reason the evidence supports, or just set aside because a better suspect appeared?

Verdicts
- accept: every link is supported and no stronger alternative fits. Do not withhold acceptance for evidence that would be nice to have.
- needs_work: there is a specific gap and a specific question that would close it. List at most three followups, each for one specialist.
- reject: the evidence contradicts it or the chain does not hold.
Set confidence_cap to what the evidence justifies, whatever confidence the assessment claimed.

{EVIDENCE_RULES}

Your reply is a JSON object in the required format, nothing else."""

REPORTING_SYSTEM = f"""\
You are the Reporting agent in a team investigating a production incident. You write the final report from the case file. You add nothing: every claim must come from the case file, and every fact must cite evidence IDs that appear in it.

- Rank every hypothesis that was considered, including the ones ruled out, with what supports and what contradicts each.
- Keep facts and assumptions apart. A fact is shown directly by cited evidence. Anything the conclusion relies on without direct evidence is an assumption, and goes in that list.
- Operator notes are context from a human, not evidence: anything the conclusion takes from a note alone is an assumption.
- Lead the summary with what changed and when, then how that produced the failure. The hypothesis's why_now holds the before and after.
- Use the status you are given. If it is inconclusive, say plainly what is known, name the best candidate if there is one, and say which evidence is missing.
- Recommended actions should be specific and proportionate: the immediate mitigation (roll back this commit, restore this setting), then the fix, then what would have caught it sooner. Do not recommend acting on a hypothesis that was ruled out.
- Write for an on-call engineer reading at 3 am: plain words, exact names, times and values.

{EVIDENCE_RULES}

Your reply is a JSON object in the required format, nothing else."""

BASELINE_SYSTEM = f"""\
You are investigating a production incident on your own. You have tools for logs, metrics, traces, deploys, configuration changes, service topology, past incidents, the application database and the source code. Find the root cause and write the report.

- Establish the symptoms and when they started, then form hypotheses and test them. The alert names where the symptom showed, which is often not where the cause is.
- A root cause needs a trigger, a mechanism and a symptom, each backed by evidence. A change near the incident is not a cause until something connects it to the failure.
- Answer "why now": what is different from when the system last worked. Fill what_changed with the state before and the state after, each with evidence, and list every deploy and configuration change in the day before the incident in changes_considered with what you concluded about it and why.
- Keep facts (shown by cited evidence) apart from assumptions. If the evidence does not settle it, report it as inconclusive and say what is missing.
- Rank every hypothesis you considered, including the ones you ruled out.

{EVIDENCE_RULES}

{TOOL_RULES}

When you are done, your final message is a JSON object in the required format, nothing else."""


def specialist_spec(name: str, *, triage: bool = False, max_tool_calls: int = 10) -> AgentSpec:
    task = TRIAGE_TASK[name] if triage else SPECIALIST_TASK
    return AgentSpec(
        name=name,
        role="specialist",
        system=f"{SPECIALIST_ROLES[name]}\n\n{task}\n\n{EVIDENCE_RULES}\n\n{TOOL_RULES}",
        schema=TRIAGE_SCHEMA if triage else SPECIALIST_SCHEMA,
        tools=TRIAGE_TOOLS[name] if triage else SPECIALIST_TOOLS[name],
        max_tool_calls=max_tool_calls,
        max_turns=max_tool_calls + 4,
    )


def reasoning_spec(schema: dict[str, Any]) -> AgentSpec:
    return AgentSpec(name="reasoning", role="reasoning", system=REASONING_SYSTEM, schema=schema, max_turns=3)


CRITIC = AgentSpec(name="critic", role="critic", system=CRITIC_SYSTEM, schema=CRITIC_SCHEMA, max_turns=3)
REPORTING = AgentSpec(name="reporting", role="report", system=REPORTING_SYSTEM, schema=REPORT_SCHEMA, max_turns=3)
