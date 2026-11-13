"""Source configuration and per-source query policy, loaded from sources.yaml.

A source has a `type` (which connector talks to it) and a `kind` (which
linter checks its queries). Policies are data, so pointing the system at a new
Sumo account or database is a config change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TablePolicy(_Model):
    indexed: list[str] = Field(default_factory=list)  # columns a filter must use
    deny_columns: list[str] = Field(default_factory=list)  # never selectable
    small: bool = False  # lookup table: full scan allowed


class SqlPolicy(_Model):
    dialect: str = "postgres"
    allowed_schemas: list[str] = Field(default_factory=lambda: ["public"])
    tables: dict[str, TablePolicy] = Field(default_factory=dict)
    deny_functions: list[str] = Field(
        default_factory=lambda: [
            "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_read_file", "pg_read_binary_file",
            "pg_ls_dir", "pg_stat_file", "dblink", "dblink_exec", "lo_import", "lo_export",
            "pg_terminate_backend", "pg_cancel_backend", "set_config", "query_to_xml",
            "nextval", "setval", "pg_advisory_lock", "pg_advisory_xact_lock", "generate_series",
        ]
    )


class LogSearchPolicy(_Model):
    required_scope: list[str] = Field(default_factory=list)  # e.g. [index, source_category]
    allow_wildcard_scope: bool = False
    max_window_s: int = 3600
    raw_max_window_s: int = 900
    raw_requires_aggregate: bool = True  # count first, then pull raw lines
    max_or_terms: int = 1
    volume_key: str = "source_category"  # scope field used to look up volume
    gb_per_hour: dict[str, float] = Field(default_factory=dict)
    default_gb_per_hour: float = 1.0


class NrqlPolicy(_Model):
    max_window_s: int = 6 * 3600
    allowed_event_types: list[str] = Field(default_factory=list)  # empty = any


class Policy(_Model):
    timeout_s: float = 10.0
    max_retries: int = 2  # retries after the first attempt, timeouts and transient errors only
    backoff_s: float = 0.5
    max_rows: int = 100
    max_concurrency: int = 2
    rate_per_min: float = 30.0
    cache_ttl_s: float = 300.0

    cost_unit: str = "queries"
    max_cost_per_query: float | None = None
    approval_above: float | None = None  # soft cap: needs a human yes
    investigation_budget: float | None = None  # total cost per investigation
    require_estimate: bool = False  # reject when no estimate can be produced

    breaker_threshold: int = 3  # consecutive failures before the circuit opens
    breaker_cooldown_s: float = 60.0
    auth_cooldown_s: float = 900.0

    redact_fields: list[str] = Field(default_factory=list)  # replaced by stable tokens

    sql: SqlPolicy = Field(default_factory=SqlPolicy)
    log_search: LogSearchPolicy = Field(default_factory=LogSearchPolicy)
    nrql: NrqlPolicy = Field(default_factory=NrqlPolicy)


class SourceConfig(_Model):
    name: str
    type: str  # connector type: postgres, sumo, newrelic, git, simulated...
    kind: Literal["sql", "log_search", "nrql", "generic"] = "generic"
    description: str = ""
    auth: dict[str, Any] = Field(default_factory=dict)  # secret:// refs or plain values
    options: dict[str, Any] = Field(default_factory=dict)  # non-secret connector settings
    policy: Policy = Field(default_factory=Policy)


def load_sources(path: str | Path) -> dict[str, SourceConfig]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return parse_sources(data)


def parse_sources(data: dict[str, Any]) -> dict[str, SourceConfig]:
    out: dict[str, SourceConfig] = {}
    for name, body in (data.get("sources") or {}).items():
        out[name] = SourceConfig(name=name, **(body or {}))
    return out
