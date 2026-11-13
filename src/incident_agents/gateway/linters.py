"""Preflight linters: one per query kind.

A linter never executes anything. It returns violations (with a hint the agent
can act on), a possibly rewritten query (for example with a LIMIT added) and,
where policy alone is enough, a cost estimate.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .models import CostEstimate
from .policy import Policy


@dataclass
class LintContext:
    """What the gateway knows about this investigation's earlier queries."""

    aggregated_scopes: set[str] = field(default_factory=set)


@dataclass
class LintResult:
    params: dict[str, Any]
    violations: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    estimate: CostEstimate | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def hint(self) -> str:
        return " ".join(dict.fromkeys(self.hints))

    def fail(self, violation: str, hint: str = "") -> None:
        self.violations.append(violation)
        if hint:
            self.hints.append(hint)


Linter = Callable[[dict[str, Any], Policy, LintContext], LintResult]


# --- SQL ---------------------------------------------------------------------

_WRITE_NODES = tuple(
    getattr(exp, n)
    for n in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "TruncateTable",
        "Copy", "Command", "Set", "Transaction", "Commit", "Rollback", "Grant", "Analyze", "Into", "Lock",
    )
    if hasattr(exp, n)
)
_COMPARISONS = (exp.EQ, exp.In, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)


def lint_sql(params: dict[str, Any], policy: Policy, ctx: LintContext) -> LintResult:
    """Read-only, allow-listed, index-filtered, row-limited SQL.

    The index rule is a syntactic check and deliberately conservative. The
    connector's EXPLAIN-based cost estimate is the second line of defence.
    """
    res = LintResult(params=dict(params))
    sql = params.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        res.fail("params.sql is required")
        return res
    sp = policy.sql

    try:
        statements = [s for s in sqlglot.parse(sql, dialect=sp.dialect) if s is not None]
    except SqlglotError as exc:
        res.fail(f"SQL could not be parsed: {str(exc).splitlines()[0][:160]}")
        return res
    if len(statements) != 1:
        res.fail("Exactly one statement is allowed")
        return res
    root = statements[0]
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        res.fail(f"Only SELECT is allowed, got {type(root).__name__.upper()}", "This source is read-only.")
        return res

    for node in root.walk():
        if isinstance(node, _WRITE_NODES):
            res.fail(f"{type(node).__name__.upper()} is not allowed in a read-only query")

    denied_fns = {f.lower() for f in sp.deny_functions}
    for fn in root.find_all(exp.Func):
        name = (fn.name if isinstance(fn, exp.Anonymous) else fn.sql_name()).lower()
        if name in denied_fns or name.removeprefix("exploding_") in denied_fns:
            res.fail(f"Function {name}() is not allowed")

    cte_names = {c.alias for c in root.find_all(exp.CTE)}
    alias_to_table: dict[str, str] = {}
    for t in root.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            res.fail("Table functions are not allowed")
            continue
        if t.name in cte_names and not t.db:
            continue
        schema = t.db or sp.allowed_schemas[0]
        if schema not in sp.allowed_schemas or t.name not in sp.tables:
            res.fail(
                f"Table {schema}.{t.name} is not queryable",
                f"Queryable tables: {', '.join(sorted(sp.tables)) or 'none configured'}.",
            )
            continue
        alias_to_table[t.alias_or_name] = t.name
    tables = set(alias_to_table.values())

    # Columns that must never leave the database.
    deny = {(t, c) for t in tables for c in sp.tables[t].deny_columns}
    if deny:
        if any(True for _ in root.find_all(exp.Star)):
            res.fail(
                "SELECT * is not allowed on tables with restricted columns",
                "List the columns you need explicitly.",
            )
        for col in root.find_all(exp.Column):
            owners = [alias_to_table[col.table]] if col.table in alias_to_table else tables
            if any((t, col.name) in deny for t in owners):
                res.fail(f"Column {col.name} is restricted")

    # Every non-small table needs a filter on an indexed column. Checked per
    # SELECT, so a UNION branch or subquery cannot hide behind another's filter.
    for sel in root.find_all(exp.Select):
        own = lambda n, sel=sel: isinstance(n, exp.Select) and n is not sel  # noqa: E731
        aliases = {
            t.alias_or_name: t.name
            for t in sel.walk(prune=own)
            if isinstance(t, exp.Table) and t.alias_or_name in alias_to_table and alias_to_table[t.alias_or_name] == t.name
        }
        sel_tables = set(aliases.values())
        direct: set[str] = set()  # filtered against a constant
        joined: set[str] = set()  # joined on an indexed column
        for cmp_ in sel.walk(prune=own):
            if not isinstance(cmp_, _COMPARISONS) or _under_or_not(cmp_):
                continue
            cols = list(cmp_.find_all(exp.Column))
            constant = isinstance(cmp_.this, exp.Column) and len(cols) == 1
            for col in cols:
                owners = [aliases[col.table]] if col.table in aliases else (list(sel_tables) if not col.table else [])
                for t in owners:
                    if col.name in sp.tables[t].indexed:
                        if constant:
                            direct.add(t)
                        elif isinstance(cmp_, exp.EQ):
                            joined.add(t)
        for t in sorted(sel_tables):
            tp = sp.tables[t]
            if tp.small or t in direct or (t in joined and direct):
                continue
            res.fail(
                f"Query on {t} has no filter on an indexed column",
                f"Filter {t} by one of: {', '.join(tp.indexed) or 'no indexed columns configured'} "
                "(a plain comparison, not inside OR or NOT).",
            )

    if res.violations:
        return res

    limit = root.args.get("limit")
    current = None
    if limit is not None and isinstance(limit.expression, exp.Literal) and limit.expression.is_int:
        current = int(limit.expression.this)
    if current is None or current > policy.max_rows:
        # One row more than the cap, so the gateway can tell the agent when
        # rows were cut off instead of silently returning a partial answer.
        root = root.limit(policy.max_rows + 1, copy=False)
        res.notes["limit_applied"] = policy.max_rows
    res.params["sql"] = root.sql(dialect=sp.dialect)
    return res


def _under_or_not(node: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None and not isinstance(parent, (exp.Select, exp.Join)):
        if isinstance(parent, (exp.Or, exp.Not)):
            return True
        parent = parent.parent
    return False


# --- Log search (Sumo Logic and similar) ---------------------------------------


def lint_log_search(params: dict[str, Any], policy: Policy, ctx: LintContext) -> LintResult:
    """params: query, scope{...}, start, end (ISO 8601), mode (aggregate|raw), limit."""
    res = LintResult(params=dict(params))
    lp = policy.log_search
    scope = params.get("scope") or {}
    mode = params.get("mode", "aggregate")
    query = params.get("query") or ""

    if mode not in ("aggregate", "raw"):
        res.fail("mode must be 'aggregate' or 'raw'")
    for key in lp.required_scope:
        value = scope.get(key)
        if not value:
            res.fail(f"scope.{key} is required", f"Every search must name its {key}.")
        elif not lp.allow_wildcard_scope and "*" in str(value):
            res.fail(f"scope.{key} may not contain a wildcard", f"Use an exact {key}.")

    start, end = _parse_ts(params.get("start")), _parse_ts(params.get("end"))
    if start is None or end is None or end <= start:
        res.fail("start and end must be ISO 8601 timestamps with end after start")
        return res
    window_s = (end - start).total_seconds()
    cap = lp.raw_max_window_s if mode == "raw" else lp.max_window_s
    if window_s > cap:
        res.fail(
            f"{mode} window is {_dur(window_s)}, the limit is {_dur(cap)}",
            f"Use a window of {_dur(cap)} or less around the time you care about.",
        )

    or_terms = len(re.findall(r"\bor\b", query, flags=re.I))
    if or_terms > lp.max_or_terms:
        res.fail(
            f"Query has {or_terms} OR terms, the limit is {lp.max_or_terms}",
            "Run one selective term per query: stacked OR terms defeat index pruning.",
        )

    scope_key = scope_fingerprint(scope)
    if mode == "raw" and lp.raw_requires_aggregate and scope_key not in ctx.aggregated_scopes:
        res.fail(
            "Raw lines requested before any aggregate on this scope",
            "Run a count (mode: aggregate) on this scope first, then pull raw lines for the bucket that matters.",
        )

    res.params["limit"] = min(int(params.get("limit") or policy.max_rows), policy.max_rows)
    res.params["mode"] = mode
    rate = lp.gb_per_hour.get(str(scope.get(lp.volume_key, "")), lp.default_gb_per_hour)
    res.estimate = CostEstimate(
        amount=round(rate * window_s / 3600, 4),
        unit=policy.cost_unit,
        detail=f"{rate} GB/h x {_dur(window_s)}",
    )
    res.notes["scope_key"] = scope_key
    return res


def scope_fingerprint(scope: dict[str, Any]) -> str:
    return "|".join(f"{k}={scope[k]}" for k in sorted(scope))


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dur(seconds: float) -> str:
    seconds = int(seconds)
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


# --- NRQL (New Relic) -----------------------------------------------------------

_UNIT_S = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000}
_SINCE_RE = re.compile(r"\bSINCE\s+(\d+)\s+(second|minute|hour|day|week|month)s?\s+ago\b", re.I)
_LIMIT_RE = re.compile(r"\bLIMIT\s+(MAX|\d+)\b", re.I)
_FROM_RE = re.compile(r"\bFROM\s+([A-Za-z_][\w]*(?:\s*,\s*[A-Za-z_][\w]*)*)", re.I)


def lint_nrql(params: dict[str, Any], policy: Policy, ctx: LintContext) -> LintResult:
    res = LintResult(params=dict(params))
    nrql = params.get("nrql")
    if not isinstance(nrql, str) or not nrql.strip():
        res.fail("params.nrql is required")
        return res
    nrql = nrql.strip().rstrip(";").strip()
    np = policy.nrql

    if ";" in nrql:
        res.fail("Exactly one NRQL statement is allowed")
    if not re.match(r"^(SELECT|FROM)\b", nrql, flags=re.I):
        res.fail("NRQL must start with SELECT or FROM")
    if not re.search(r"\bSINCE\b", nrql, flags=re.I):
        res.fail("NRQL needs an explicit SINCE clause", f"Add a time range, for example SINCE 30 minutes ago (limit {_dur(np.max_window_s)}).")
    m = _SINCE_RE.search(nrql)
    if m:
        window_s = int(m[1]) * _UNIT_S[m[2].lower()]
        if window_s > np.max_window_s:
            res.fail(
                f"SINCE window is {_dur(window_s)}, the limit is {_dur(np.max_window_s)}",
                f"Use SINCE of {_dur(np.max_window_s)} or less.",
            )
    if np.allowed_event_types:
        fm = _FROM_RE.search(nrql)
        types = [t.strip() for t in fm[1].split(",")] if fm else []
        bad = [t for t in types if t not in np.allowed_event_types]
        if bad or not types:
            res.fail(
                f"Event type {', '.join(bad) or '(none)'} is not queryable",
                f"Queryable event types: {', '.join(np.allowed_event_types)}.",
            )
    if res.violations:
        return res

    lm = _LIMIT_RE.search(nrql)
    if lm is None:
        nrql = f"{nrql} LIMIT {policy.max_rows}"
    elif lm[1].upper() == "MAX" or int(lm[1]) > policy.max_rows:
        nrql = _LIMIT_RE.sub(f"LIMIT {policy.max_rows}", nrql, count=1)
    res.params["nrql"] = nrql
    res.estimate = CostEstimate(amount=1, unit=policy.cost_unit, detail="one NRQL query")
    return res


# --- Generic ------------------------------------------------------------------


def lint_generic(params: dict[str, Any], policy: Policy, ctx: LintContext) -> LintResult:
    res = LintResult(params=dict(params))
    if "limit" in params:
        res.params["limit"] = min(int(params["limit"] or policy.max_rows), policy.max_rows)
    return res


LINTERS: dict[str, Linter] = {
    "sql": lint_sql,
    "log_search": lint_log_search,
    "nrql": lint_nrql,
    "generic": lint_generic,
}
