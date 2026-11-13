"""The tool catalogue, grouped by capability.

  logs      logs_count, logs_fetch
  metrics   metrics_list, metrics_query
  records   deploys_list, config_changes_list, topology_get, traces_search, incidents_search
  db        db_schema, db_query
  code      code_repos, code_log, code_changed_files, code_diff, code_read, code_search
  evidence  evidence_get, evidence_find

A result with no matches is still recorded as evidence ("no deploys of
fivo-api in the window"), because absence is often what rules a hypothesis out.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import sqlglot
from sqlglot import exp

from ..evidence import Evidence, extract_pivots
from ..querytypes import QueryResult, Status
from ..timeutil import parse_ts
from .core import Tool, ToolContext, ToolResult

_NAME_RE = re.compile(r"^[A-Za-z0-9][\w.\-/]{0,63}$")
_FIELD_RE = re.compile(r"^[A-Za-z_][\w.]*$")
_SLICE_RE = re.compile(r"^\d{1,3}[smh]$")

SERVICE = {"type": "string", "description": "Service name, as it appears in topology_get."}
START = {"type": "string", "description": "Window start, ISO 8601 UTC, e.g. 2026-11-10T08:00:00Z."}
END = {"type": "string", "description": "Window end, ISO 8601 UTC (exclusive)."}
BASELINE = {
    "baseline_start": {"type": "string", "description": "Optional. Start of a comparison window from before the incident (ISO 8601 UTC). Give it with baseline_end."},
    "baseline_end": {"type": "string", "description": "Optional. End of the comparison window; must not be later than start."},
}
LIMIT = {"type": "integer", "description": "Maximum rows to return. The source's own cap still applies."}


def _brief(row: dict[str, Any], width: int = 220) -> str:
    parts = []
    for key, value in row.items():
        if value in (None, "", [], {}):
            continue
        text = str(value)
        parts.append(f"{key}={text if len(text) <= 80 else text[:77] + '...'}")
    out = " ".join(parts)
    return out if len(out) <= width else out[: width - 3] + "..."


def _trim(row: dict[str, Any], width: int = 600) -> dict[str, Any]:
    return {k: (v[:width] + "...[cut]" if isinstance(v, str) and len(v) > width else v) for k, v in row.items()}


def _merged_pivots(rows: list[dict[str, Any]], extra: dict[str, Any] | None = None) -> dict[str, str]:
    out = extract_pivots(extra or {})
    for row in rows[:20]:
        for key, value in extract_pivots(row).items():
            out.setdefault(key, value)
    return out


def _partial_tag(result: QueryResult) -> str:
    return "" if result.status is Status.OK and not result.truncated else " (partial result)"


def _window(args: dict[str, Any]) -> str:
    return f"{args.get('start', '...')} to {args.get('end', '...')}"


# --- Logs ---------------------------------------------------------------------------


def _quote(value: Any) -> str:
    return '"' + str(value).replace('"', "").replace("|", " ") + '"'


def build_log_query(
    dialect: str,
    *,
    text: str = "",
    filters: dict[str, Any] | None = None,
    group_by: list[str] | None = None,
    timeslice: str = "",
    count: bool = False,
    parse_stage: str = "| json auto",
) -> str:
    """Render a structured search in the query language of the bound log source."""
    filters = filters or {}
    by = (["_timeslice"] if timeslice else []) + list(group_by or [])
    head = _quote(text) if text else ""
    stages: list[str] = []
    if dialect == "sumo":
        if filters or [f for f in by if not f.startswith("_")]:
            stages.append(parse_stage.lstrip("| ").strip())
        stages += [f"where {k} = {_quote(v)}" for k, v in filters.items()]
    else:
        pairs = " ".join(f"{k}={_quote(v)}" for k, v in filters.items())
        head = " ".join(p for p in (head, pairs) if p) or "*"
    if timeslice:
        stages.append(f"timeslice {timeslice}")
    if count:
        stages.append("count by " + ", ".join(by) if by else "count")
    return " | ".join([head or "*", *stages]) if stages else (head or "*")


def _log_params(ctx: ToolContext, args: dict[str, Any], *, count: bool) -> dict[str, Any] | ToolResult:
    opts = ctx.binding("logs").options
    service = args["service"]
    filters, group_by, timeslice = args.get("filters") or {}, args.get("group_by") or [], args.get("timeslice") or ""
    if not _NAME_RE.match(service):
        return ctx.refuse(f"Invalid service name {service!r}")
    bad = [f for f in [*filters, *group_by] if not _FIELD_RE.match(str(f))]
    if bad:
        return ctx.refuse(f"Invalid field name(s): {', '.join(map(str, bad))}", "Field names are plain identifiers such as route or status.")
    if timeslice and not _SLICE_RE.match(timeslice):
        return ctx.refuse("timeslice must look like 1m, 5m or 1h")
    template = (opts.get("services") or {}).get(service) or opts.get("scope") or {"service": "{service}"}
    scope = {k: str(v).replace("{service}", service) for k, v in template.items()}
    query = build_log_query(
        opts.get("dialect", "simple"), text=args.get("text") or "", filters=filters, group_by=group_by,
        timeslice=timeslice, count=count, parse_stage=opts.get("parse_stage", "| json auto"),
    )
    params = {"query": query, "scope": scope, "start": args["start"], "end": args["end"], "mode": "aggregate" if count else "raw"}
    if args.get("limit"):
        params["limit"] = args["limit"]
    return params


def _describe_search(args: dict[str, Any]) -> str:
    bits = []
    if args.get("text"):
        bits.append(f'"{args["text"]}"')
    bits += [f"{k}={v}" for k, v in (args.get("filters") or {}).items()]
    return " ".join(bits) or "all lines"


def _baseline_window(ctx: ToolContext, args: dict[str, Any]) -> tuple[str, str, float] | ToolResult | None:
    """The comparison window, if one was asked for: (start, end, window length / baseline length)."""
    b_start, b_end = args.get("baseline_start"), args.get("baseline_end")
    if not b_start and not b_end:
        return None
    start, end, bs, be = (parse_ts(v) for v in (args.get("start"), args.get("end"), b_start, b_end))
    if None in (start, end, bs, be) or be <= bs or end <= start:
        return ctx.refuse("baseline_start and baseline_end must both be ISO 8601 timestamps, with the end after the start")
    if be > start:
        return ctx.refuse("The baseline window must end at or before the start of the main window",
                          "Use a period from before the incident, e.g. the same length immediately before `start`.")
    return str(b_start), str(b_end), (end - start).total_seconds() / (be - bs).total_seconds()


def compare_counts(rows: list[dict[str, Any]], base_rows: list[dict[str, Any]], keys: list[str], count_key: str, scale: float) -> dict[str, Any]:
    """What is new, gone or changed between a baseline table of counts and the current one.

    `scale` is window length / baseline length, so counts are compared at the same rate.
    """
    def table(rs: list[dict[str, Any]]) -> dict[tuple[str, ...], int]:
        out: dict[tuple[str, ...], int] = {}
        for r in rs:
            key = tuple(str(r.get(k)) for k in keys)
            out[key] = out.get(key, 0) + int(r.get(count_key) or 0)
        return out

    now, before = table(rows), table(base_rows)
    total, base_total = sum(now.values()), sum(before.values())
    groups = []
    for key in {**before, **now}:
        n, expected = now.get(key, 0), before.get(key, 0) * scale
        change = "new" if not before.get(key) and n else "gone" if not n else f"x{n / expected:.1f}"
        groups.append({**dict(zip(keys, key)), "count": n, "baseline_count": before.get(key, 0),
                       "expected_at_baseline_rate": round(expected, 1), "change": change, "_delta": abs(n - expected)})
    groups.sort(key=lambda g: -g.pop("_delta"))
    expected_total = base_total * scale
    return {
        "total": total, "baseline_total": base_total, "expected_at_baseline_rate": round(expected_total, 1),
        "change": "new" if not base_total and total else "none" if not total and not base_total else f"x{total / expected_total:.1f}" if expected_total else "gone",
        "groups": groups[:20] if keys else [],
    }


def _group_label(group: dict[str, Any], keys: list[str]) -> str:
    return " ".join(f"{k}={group.get(k)}" for k in keys)


async def logs_count(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    params = _log_params(ctx, args, count=True)
    if isinstance(params, ToolResult):
        return params
    baseline = _baseline_window(ctx, args)
    if isinstance(baseline, ToolResult):
        return baseline
    if baseline and args.get("timeslice"):
        return ctx.refuse("timeslice cannot be combined with a baseline window", "Drop timeslice to compare two periods, or drop the baseline to see a time series.")
    result = await ctx.query("logs", params)
    if not result.ok:
        return ctx.failed(result)
    rows = result.rows
    count_key = next((k for k in ("_count", "count") if rows and k in rows[0]), "_count")
    total = sum(int(r.get(count_key) or 0) for r in rows)
    grouped = bool(args.get("group_by") or args.get("timeslice"))
    summary = f"{total} log lines for {args['service']} matching {_describe_search(args)}, {_window(args)}"
    largest = ""
    if grouped and rows:
        top = sorted(rows, key=lambda r: -int(r.get(count_key) or 0))[:3]
        largest = "; largest groups: " + "; ".join(_brief(r, 90) for r in top)
    data = {
        "service": args["service"], "search": _describe_search(args), "start": args["start"], "end": args["end"],
        "group_by": (["_timeslice"] if args.get("timeslice") else []) + list(args.get("group_by") or []),
        "total": total, "rows": rows,
    }
    note = ""
    if baseline:
        b_start, b_end, scale = baseline
        base = await ctx.query("logs", {**params, "start": b_start, "end": b_end})
        if base.ok:
            keys = list(args.get("group_by") or [])
            base_key = next((k for k in ("_count", "count") if base.rows and k in base.rows[0]), count_key)
            for r in base.rows:
                r.setdefault(count_key, r.get(base_key))
            cmp = compare_counts(rows, base.rows, keys, count_key, scale)
            data["baseline"] = {"start": b_start, "end": b_end, "complete": not base.truncated and base.status is Status.OK, **cmp}
            summary += (f". Baseline {b_start} to {b_end}: {cmp['baseline_total']} lines"
                        f" ({cmp['expected_at_baseline_rate']} expected at that rate), change {cmp['change']}")
            fresh = [g for g in cmp["groups"] if g["change"] == "new"][:3]
            if fresh:  # what is new matters more than what is largest, and the summary is short
                largest = "; new since baseline: " + "; ".join(f"{_group_label(g, keys)} ({g['count']})" for g in fresh)
            if base.truncated or base.status is not Status.OK:
                summary += " (baseline partial)"
        else:
            note = f"The baseline query failed ({base.error or base.status.value}); this record has the main window only."
    summary += largest
    ev = ctx.add(result, "logagg", args["service"], summary + _partial_tag(result), data, pivots=_merged_pivots(rows, args.get("filters")))
    return ctx.done(result, [ev], **({"note": note} if note else {}))


async def logs_fetch(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    params = _log_params(ctx, args, count=False)
    if isinstance(params, ToolResult):
        return params
    result = await ctx.query("logs", params)
    if not result.ok:
        return ctx.failed(result)
    if not result.rows:
        return ctx.done(result, [_nothing(ctx, result, "log", args["service"], f"No log lines for {args['service']} matching {_describe_search(args)}, {_window(args)}", args)])
    evidence = []
    for row in result.rows:
        row = _trim(row)
        message = str(row.get("message") or row.get("_raw") or "")[:160]
        summary = " ".join(str(p) for p in (row.get("ts", ""), row.get("level", ""), message) if p)
        evidence.append(ctx.add(result, "log", args["service"], summary, row, ts=row.get("ts")))
    return ctx.done(result, evidence)


def _nothing(ctx: ToolContext, result: QueryResult, kind: str, subject: str, summary: str, args: dict[str, Any]) -> Evidence:
    """Evidence that a query matched nothing."""
    return ctx.add(result, kind, subject, summary + _partial_tag(result), {"matched": 0, "query": args}, pivots={})


# --- Metrics ------------------------------------------------------------------------


def series_stats(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Min, max, mean and the single largest level shift in a series."""
    values = [float(p["value"]) for p in points if p.get("value") is not None]
    if not values:
        return {"points": 0}
    stats: dict[str, Any] = {
        "points": len(values), "min": round(min(values), 4), "max": round(max(values), 4),
        "mean": round(sum(values) / len(values), 4), "first": round(values[0], 4), "last": round(values[-1], 4),
        "peak_at": points[values.index(max(values))].get("ts"),
    }
    best: tuple[float, int, float, float] | None = None
    for i in range(2, len(values) - 1):
        before, after = sum(values[:i]) / i, sum(values[i:]) / (len(values) - i)
        if best is None or abs(after - before) > best[0]:
            best = (abs(after - before), i, before, after)
    spread = max(values) - min(values)
    if best and spread > 0 and best[0] >= 0.4 * spread:
        stats["shift"] = {"at": points[best[1]].get("ts"), "mean_before": round(best[2], 4), "mean_after": round(best[3], 4)}
    # Where the series first leaves its starting level and stays away: the onset of a ramp
    # (queue depth, memory, quota), which a single level shift places too late.
    head = sorted(values[: max(3, len(values) // 4)])
    base = head[len(head) // 2]
    band = max(0.1 * spread, 1.5 * (head[-1] - head[0]))
    if spread > 0 and len(values) >= 8:
        for i in range(len(head), len(values) - 2):
            if all(abs(v - base) > band for v in values[i:i + 3]):
                stats["departs_baseline"] = {"at": points[i].get("ts"), "baseline": round(base, 4)}
                break
    return stats


async def metrics_list(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    opts = ctx.binding("metrics").options
    if opts.get("dialect") == "nrql":
        return ToolResult(ctx.tool, Status.OK, info=[{"metric": m} for m in sorted(opts.get("templates") or {})])
    result = await ctx.query("metrics", {"op": "list", **({"service": args["service"]} if args.get("service") else {})})
    return ctx.done(result, [], info=result.rows) if result.ok else ctx.failed(result)


async def _metric_points(ctx: ToolContext, service: str, metric: str, start: str, end: str, step: int, agg: str) -> tuple[QueryResult, list[dict[str, Any]], str] | ToolResult:
    """One series from the bound metrics source, as [{ts, value}]."""
    opts = ctx.binding("metrics").options
    if opts.get("dialect") != "nrql":
        result = await ctx.query("metrics", {"service": service, "metric": metric, "start": start, "end": end, "step_s": step, "agg": agg})
        return result, (result.rows if result.ok else []), str(result.meta.get("unit", "")) if result.ok else ""
    template = (opts.get("templates") or {}).get(metric)
    if template is None:
        return ctx.refuse(f"Unknown metric {metric!r}", f"Known metrics: {', '.join(sorted(opts.get('templates') or {}))}.")
    s_dt, e_dt = parse_ts(start), parse_ts(end)
    nrql = template.format(service=service, step_s=step, start=s_dt.strftime("%Y-%m-%d %H:%M:%S"), end=e_dt.strftime("%Y-%m-%d %H:%M:%S"))
    result = await ctx.query("metrics", {"nrql": nrql})
    points = []
    for row in result.rows if result.ok else []:
        value = _first_number({k: v for k, v in row.items() if k not in ("beginTimeSeconds", "endTimeSeconds")})
        begin = parse_ts_epoch(row.get("beginTimeSeconds"))
        if begin:
            points.append({"ts": begin, "value": value})
    return result, points, ""


async def metrics_query(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    service, metric = args["service"], args["metric"]
    if not _NAME_RE.match(service) or not _NAME_RE.match(metric):
        return ctx.refuse("Invalid service or metric name")
    start, end = parse_ts(args["start"]), parse_ts(args["end"])
    if start is None or end is None or end <= start:
        return ctx.refuse("start and end must be ISO 8601 timestamps with end after start")
    baseline = _baseline_window(ctx, args)
    if isinstance(baseline, ToolResult):
        return baseline
    step, agg = int(args.get("step_s") or 60), args.get("agg") or "avg"
    fetched = await _metric_points(ctx, service, metric, args["start"], args["end"], step, agg)
    if isinstance(fetched, ToolResult):
        return fetched
    result, points, unit = fetched
    if not result.ok:
        return ctx.failed(result)
    stats = series_stats(points)
    label = f"{metric}{f' ({unit})' if unit else ''} for {service}, {_window(args)}"
    if not stats["points"]:
        summary = f"No data points for {label}"
    else:
        summary = f"{label}: min {stats['min']}, max {stats['max']}, mean {stats['mean']}, peak at {stats['peak_at']}"
        if "shift" in stats:
            s = stats["shift"]
            summary += f"; level shift at {s['at']} from about {s['mean_before']} to about {s['mean_after']}"
        else:
            summary += "; no clear level shift"
        if "departs_baseline" in stats:
            d = stats["departs_baseline"]
            summary += f"; first leaves its baseline of about {d['baseline']} at {d['at']}"
    data = {"service": service, "metric": metric, "unit": unit, "start": args["start"], "end": args["end"], "step_s": step, "stats": stats, "points": points}
    note = ""
    if baseline:
        b_start, b_end, _ = baseline
        base = await _metric_points(ctx, service, metric, b_start, b_end, step, agg)
        if isinstance(base, ToolResult) or not base[0].ok:
            note = "The baseline query failed; this record has the main window only."
        else:
            b_stats = {k: v for k, v in series_stats(base[1]).items() if k in ("points", "min", "max", "mean")}
            data["baseline"] = {"start": b_start, "end": b_end, "complete": not base[0].truncated and base[0].status is Status.OK, "stats": b_stats}
            if b_stats["points"] and stats["points"]:
                ratio = f"x{stats['mean'] / b_stats['mean']:.1f}" if b_stats["mean"] else "from zero"
                data["baseline"]["mean_change"] = ratio
                summary += f". Baseline {b_start} to {b_end}: min {b_stats['min']}, max {b_stats['max']}, mean {b_stats['mean']} (mean change {ratio})"
            else:
                summary += f". Baseline {b_start} to {b_end}: no data points"
    ev = ctx.add(result, "metric", service, summary + _partial_tag(result), data, pivots={})
    return ctx.done(result, [ev], **({"note": note} if note else {}))


def _first_number(value: Any) -> float | None:
    """NRQL returns plain numbers for most functions and nested objects for percentiles."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, dict):
        for inner in value.values():
            found = _first_number(inner)
            if found is not None:
                return found
    return None


def parse_ts_epoch(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


# --- Records: deploys, config changes, topology, traces, past incidents --------------


async def _records(
    ctx: ToolContext, args: dict[str, Any], collection: str, kind: str, label: str, filter_keys: tuple[str, ...] = ("service",)
) -> ToolResult:
    opts = ctx.binding("records").options
    params: dict[str, Any] = {"collection": (opts.get("collections") or {}).get(collection, collection)}
    filters = {k: args[k] for k in filter_keys if args.get(k) not in (None, "")}
    filters.update(args.get("filters") or {})
    if filters:
        params["filters"] = filters
    for key in ("text", "start", "end", "limit"):
        if args.get(key):
            params[key] = args[key]
    result = await ctx.query("records", params)
    if not result.ok:
        return ctx.failed(result)
    subject = str(args.get("service") or collection)
    if not result.rows:
        wanted = " ".join(f"{k}={v}" for k, v in filters.items()) or "any"
        when = f", {_window(args)}" if args.get("start") or args.get("end") else ""
        text = f" matching \"{args['text']}\"" if args.get("text") else ""
        return ctx.done(result, [_nothing(ctx, result, kind, subject, f"No {label} found for {wanted}{text}{when}", args)])
    evidence = [
        ctx.add(result, kind, str(row.get("service") or subject), _brief(row), _trim(row), ts=row.get("ts"))
        for row in result.rows
    ]
    return ctx.done(result, evidence)


async def deploys_list(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _records(ctx, args, "deploys", "deploy", "deploys")


async def config_changes_list(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _records(ctx, args, "config_changes", "config", "config changes")


async def topology_get(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _records(ctx, args, "topology", "topology", "topology entries")


async def traces_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _records(ctx, args, "traces", "trace", "trace spans", ("service", "trace_id"))


async def incidents_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _records(ctx, args, "incidents", "incident", "past incidents")


# --- Database -----------------------------------------------------------------------


async def db_schema(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    src = ctx.gateway.sources[ctx.binding("db").source]
    tables = {
        name: {
            "filter_on_one_of": t.indexed,
            **({"restricted_columns": t.deny_columns} if t.deny_columns else {}),
            **({"small_table": True} if t.small else {}),
        }
        for name, t in src.policy.sql.tables.items()
    }
    info = {
        "dialect": src.policy.sql.dialect,
        "tables": tables,
        "columns": ctx.binding("db").options.get("columns") or "Not listed. Select from information_schema is not allowed; ask for the columns you expect and read the error if one is missing.",
        "rules": [
            "One SELECT per call. The database is read-only.",
            "Every table needs a plain comparison on one of its filter columns, unless it is a small table.",
            f"At most {src.policy.max_rows} rows come back; aggregate in SQL instead of pulling rows.",
            "No SELECT * on tables with restricted columns.",
        ],
    }
    return ToolResult(ctx.tool, Status.OK, info=info)


def _tables(sql: str, dialect: str) -> list[str]:
    try:
        return sorted({t.name for t in sqlglot.parse_one(sql, dialect=dialect).find_all(exp.Table) if t.name})
    except Exception:
        return []


async def db_query(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    result = await ctx.query("db", {"sql": args["sql"]})
    if not result.ok:
        return ctx.failed(result)
    src = ctx.gateway.sources[ctx.binding("db").source]
    sql = str(result.executed_params.get("sql") or args["sql"])
    tables = _tables(sql, src.policy.sql.dialect)
    rows = [_trim(r, 300) for r in result.rows]
    summary = f"{len(rows)} row{'s' if len(rows) != 1 else ''} from {', '.join(tables) or 'query'}"
    if rows:
        summary += f"; first: {_brief(rows[0], 160)}"
    data = {"sql": args["sql"], "row_count": len(rows), "rows": rows}
    ev = ctx.add(result, "db", tables[0] if tables else "query", summary + _partial_tag(result), data, pivots=_merged_pivots(rows))
    return ctx.done(result, [ev])


# --- Code ---------------------------------------------------------------------------

REPO = {"type": "string", "description": "Repository name from code_repos."}
REF = {"type": "string", "description": "Commit SHA to read at. Use the SHA from the deploy record so you read the code that was running, not today's branch head."}


def _short(ref: Any) -> str:
    text = str(ref or "")
    return text[:8] if re.fullmatch(r"[0-9a-f]{12,40}", text) else text


async def code_repos(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    src = ctx.gateway.sources[ctx.binding("code").source]
    repos = [
        {"repo": name, "default_branch": cfg.get("branch", "HEAD"), **({"services": cfg["services"]} if cfg.get("services") else {})}
        for name, cfg in (src.options.get("repos") or {}).items()
    ]
    return ToolResult(ctx.tool, Status.OK, info=repos)


async def _git(ctx: ToolContext, op: str, args: dict[str, Any], keys: tuple[str, ...]) -> QueryResult:
    return await ctx.query("code", {"op": op, **{k: args[k] for k in keys if args.get(k) not in (None, "")}})


async def code_log(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    result = await _git(ctx, "log", args, ("repo", "ref", "since", "until", "path"))
    if not result.ok:
        return ctx.failed(result)
    if not result.rows:
        return ctx.done(result, [_nothing(ctx, result, "commit", args["repo"], f"No commits in {args['repo']} for the given range", args)])
    evidence = [
        ctx.add(result, "commit", args["repo"], f"{row['date']} {row['sha'][:8]} {row['subject']} ({row['author']})",
                {"repo": args["repo"], **row}, ts=row.get("date"), pivots={"sha": row["sha"]})
        for row in result.rows
    ]
    return ctx.done(result, evidence)


async def code_changed_files(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    result = await _git(ctx, "changed_files", args, ("repo", "base", "head"))
    if not result.ok:
        return ctx.failed(result)
    paths = [f"{r['status']} {r['path']}" for r in result.rows]
    span = f"{_short(args['base'])}..{_short(args['head'])}"
    summary = f"{len(paths)} files changed in {args['repo']} {span}: {', '.join(paths[:8])}{' ...' if len(paths) > 8 else ''}"
    data = {"repo": args["repo"], "base": args["base"], "head": args["head"], "files": result.rows}
    return ctx.done(result, [ctx.add(result, "changes", args["repo"], summary + _partial_tag(result), data, pivots={"sha": str(args["head"])})])


async def code_diff(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    result = await _git(ctx, "diff", args, ("repo", "base", "head", "path"))
    if not result.ok:
        return ctx.failed(result)
    span = f"{_short(args['base'])}..{_short(args['head'])}"
    if not result.rows:
        where = f" in {args['path']}" if args.get("path") else ""
        return ctx.done(result, [_nothing(ctx, result, "diff", args["repo"], f"No differences{where} in {args['repo']} {span}", args)])
    evidence = []
    for row in result.rows:
        lines = row["patch"].splitlines()
        added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
        summary = f"diff of {row['path']} in {args['repo']} {span}: +{added} -{removed} lines"
        evidence.append(ctx.add(result, "diff", args["repo"], summary, {"repo": args["repo"], **row}, pivots={"sha": str(args["head"])}))
    return ctx.done(result, evidence)


async def code_read(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    result = await _git(ctx, "show", args, ("repo", "ref", "path", "start", "end"))
    if not result.ok:
        return ctx.failed(result)
    row = result.rows[0]
    summary = f"{row['path']} in {args['repo']} at {_short(row['ref'])}, lines {row['start']}-{row['end']} of {row['total_lines']}"
    pivots = {"sha": row["ref"]} if re.fullmatch(r"[0-9a-f]{12,40}", str(row["ref"])) else {}
    return ctx.done(result, [ctx.add(result, "code", args["repo"], summary, {"repo": args["repo"], **row}, pivots=pivots)])


async def code_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    result = await _git(ctx, "grep", args, ("repo", "ref", "pattern", "path", "regex"))
    if not result.ok:
        return ctx.failed(result)
    hits = result.rows
    at = f" at {_short(args['ref'])}" if args.get("ref") else ""
    summary = f"{len(hits)} matches for \"{args['pattern']}\" in {args['repo']}{at}"
    if hits:
        summary += ": " + ", ".join(f"{h['path']}:{h['line']}" for h in hits[:6]) + (" ..." if len(hits) > 6 else "")
    data = {"repo": args["repo"], "ref": args.get("ref"), "pattern": args["pattern"], "hits": hits}
    return ctx.done(result, [ctx.add(result, "codehit", args["repo"], summary + _partial_tag(result), data, pivots={})])


# --- Evidence already collected -----------------------------------------------------


async def evidence_get(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ids = [str(i) for i in args["ids"]][:20]
    found = ctx.store.get_many(ctx.investigation_id, ids)
    missing = [i for i in ids if i not in {e.id for e in found}]
    note = f"No such evidence: {', '.join(missing)}. Only IDs returned by tools exist." if missing else ""
    return ToolResult(ctx.tool, Status.OK, evidence=found, note=note)


async def evidence_find(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pivot = (args["pivot_key"], args["pivot_value"]) if args.get("pivot_key") and args.get("pivot_value") else None
    if pivot is None and not args.get("text") and not args.get("kind"):
        return ctx.refuse("Give a pivot_key with pivot_value, a text, or a kind")
    found = ctx.store.find(ctx.investigation_id, pivot=pivot, text=args.get("text"), kind=args.get("kind"), limit=30)
    return ToolResult(ctx.tool, Status.OK, info=[e.digest() for e in found], note="Digests only. Call evidence_get for the full record.")


# --- Catalogue ----------------------------------------------------------------------

_LOG_ARGS = {
    "service": SERVICE,
    "text": {"type": "string", "description": "Phrase the line must contain. Omit to match every line."},
    "filters": {"type": "object", "description": "Exact field matches, e.g. {\"level\": \"error\", \"route\": \"/transactions\"}."},
    "start": START,
    "end": END,
}

TOOLS: list[Tool] = [
    Tool(
        "logs_count", "logs",
        "Count log lines for one service, optionally grouped by fields and/or bucketed over time. "
        "Always start here: it is cheap, and raw lines can only be fetched after a count on the same service. "
        "Returns one evidence record holding the table of counts. With a baseline window the record also holds the same "
        "count from before the incident and, per group, what is new, gone or changed.",
        {**_LOG_ARGS,
         "group_by": {"type": "array", "items": {"type": "string"}, "description": "Fields to group by, e.g. [\"route\", \"status\"]."},
         "timeslice": {"type": "string", "description": "Bucket size for a time series of counts, e.g. 1m, 5m."},
         **BASELINE},
        ["service", "start", "end"], logs_count,
    ),
    Tool(
        "logs_fetch", "logs",
        "Fetch raw log lines for one service in a short window. Each line becomes its own evidence record and "
        "exposes identifiers (trace_id, user_id, error_fingerprint) to follow in other tools. "
        "Run logs_count on the service first and keep the window to a few minutes.",
        {**_LOG_ARGS, "limit": LIMIT},
        ["service", "start", "end"], logs_fetch,
    ),
    Tool(
        "metrics_list", "metrics", "List the metrics that exist, optionally for one service.",
        {"service": SERVICE}, [], metrics_list,
    ),
    Tool(
        "metrics_query", "metrics",
        "Read one metric for one service as a time series. The evidence record includes min, max, mean, the peak "
        "time and the largest level shift, so you can compare the shift time with deploys and config changes. "
        "With a baseline window it also holds the same metric from before the incident.",
        {"service": SERVICE, "metric": {"type": "string", "description": "Metric name from metrics_list."}, "start": START, "end": END,
         "step_s": {"type": "integer", "description": "Bucket size in seconds. Default 60."},
         "agg": {"type": "string", "enum": ["avg", "max", "min", "sum", "last"], "description": "How to combine points in a bucket. Default avg."},
         **BASELINE},
        ["service", "metric", "start", "end"], metrics_query,
    ),
    Tool(
        "deploys_list", "records",
        "List deploys, optionally for one service and time window. Each record has the deployed commit (sha), the "
        "previous commit (prev_sha) and the repo, which are the inputs to the code tools.",
        {"service": SERVICE, "start": START, "end": END, "limit": LIMIT}, [], deploys_list,
    ),
    Tool(
        "config_changes_list", "records",
        "List configuration, environment variable, feature flag and infrastructure changes, optionally for one service and window.",
        {"service": SERVICE, "start": START, "end": END, "text": {"type": "string", "description": "Phrase to look for, e.g. a setting name."}, "limit": LIMIT},
        [], config_changes_list,
    ),
    Tool(
        "topology_get", "records",
        "Show services and what each depends on (databases, queues, third-party APIs). Omit service to see everything.",
        {"service": SERVICE}, [], topology_get,
    ),
    Tool(
        "traces_search", "records",
        "Find trace spans by trace_id, service or a phrase. Use a trace_id from a log line to see which downstream call was slow or failed.",
        {"trace_id": {"type": "string", "description": "Exact trace ID."}, "service": SERVICE,
         "text": {"type": "string", "description": "Phrase to look for in the span."}, "start": START, "end": END, "limit": LIMIT},
        [], traces_search,
    ),
    Tool(
        "incidents_search", "records",
        "Search past incidents and their recorded causes. A similar past incident is a lead, not proof: confirm it against current evidence.",
        {"text": {"type": "string", "description": "Phrase to look for, e.g. an error message or component."}, "service": SERVICE, "limit": LIMIT},
        [], incidents_search,
    ),
    Tool(
        "db_schema", "db",
        "Show which tables can be queried, which columns a query must filter on, and the query rules. Call before db_query.",
        {}, [], db_schema,
    ),
    Tool(
        "db_query", "db",
        "Run one read-only SELECT against the application database. Queries are checked before they run: unfiltered "
        "scans, writes and expensive plans are refused with a reason. Prefer COUNT and GROUP BY over pulling rows.",
        {"sql": {"type": "string", "description": "A single SELECT statement."}},
        ["sql"], db_query,
    ),
    Tool("code_repos", "code", "List the repositories that can be read and their default branches.", {}, [], code_repos),
    Tool(
        "code_log", "code", "List commits in a repository, newest first, optionally limited to a path or date range.",
        {"repo": REPO, "ref": REF, "since": {"type": "string", "description": "Only commits after this ISO 8601 time."},
         "until": {"type": "string", "description": "Only commits before this ISO 8601 time."},
         "path": {"type": "string", "description": "Only commits touching this file or directory."}},
        ["repo"], code_log,
    ),
    Tool(
        "code_changed_files", "code",
        "List the files that changed between two commits, typically prev_sha and sha from a deploy record.",
        {"repo": REPO, "base": {"type": "string", "description": "Older commit SHA."}, "head": {"type": "string", "description": "Newer commit SHA."}},
        ["repo", "base", "head"], code_changed_files,
    ),
    Tool(
        "code_diff", "code",
        "Show the line-level diff between two commits, one evidence record per file. Pass path to read one file's diff.",
        {"repo": REPO, "base": {"type": "string", "description": "Older commit SHA."}, "head": {"type": "string", "description": "Newer commit SHA."},
         "path": {"type": "string", "description": "Limit the diff to this file or directory."}},
        ["repo", "base", "head"], code_diff,
    ),
    Tool(
        "code_read", "code", "Read a file at a specific commit, with line numbers. Up to 400 lines per call.",
        {"repo": REPO, "ref": REF, "path": {"type": "string", "description": "File path in the repository."},
         "start": {"type": "integer", "description": "First line. Default 1."}, "end": {"type": "integer", "description": "Last line."}},
        ["repo", "path"], code_read,
    ),
    Tool(
        "code_search", "code",
        "Search file contents at a specific commit, for example for the text of an error message or a function name.",
        {"repo": REPO, "ref": REF, "pattern": {"type": "string", "description": "Text to find."},
         "path": {"type": "string", "description": "Limit to this file or directory."},
         "regex": {"type": "boolean", "description": "Treat pattern as an extended regular expression."}},
        ["repo", "pattern"], code_search,
    ),
    Tool(
        "evidence_get", "evidence", "Re-read evidence records by ID.",
        {"ids": {"type": "array", "items": {"type": "string"}, "description": "Evidence IDs, at most 20."}},
        ["ids"], evidence_get,
    ),
    Tool(
        "evidence_find", "evidence",
        "Look through evidence already collected in this investigation, by identifier (pivot), kind or phrase. "
        "Costs nothing: check here before querying a source again.",
        {"pivot_key": {"type": "string", "description": "Identifier type, e.g. trace_id, user_id, sha, error_fingerprint."},
         "pivot_value": {"type": "string", "description": "Identifier value."},
         "kind": {"type": "string", "description": "Evidence kind, e.g. log, metric, deploy, diff."},
         "text": {"type": "string", "description": "Phrase to look for."}},
        [], evidence_find,
    ),
]
