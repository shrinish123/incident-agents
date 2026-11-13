"""Assemble a toolbox from a config (sources + capabilities)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .evidence import EvidenceStore
from .gateway import AuditLog, BudgetLedger, QueryGateway, parse_sources
from .secrets import CredentialBroker, EnvSecretStore
from .tools import Toolbox, parse_bindings


def load_config(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text()) or {}


def build_toolbox(
    config: dict[str, Any],
    investigation_id: str,
    *,
    evidence_path: str = ":memory:",
    audit_path: str | Path | None = None,
    max_queries: int = 150,
    max_rejections: int = 40,
) -> Toolbox:
    """Credentials are resolved from the environment by the broker at call time;
    nothing secret is read here or handed to agents."""
    gateway = QueryGateway.from_config(
        parse_sources(config),
        CredentialBroker(store=EnvSecretStore()),
        ledger=BudgetLedger(max_queries=max_queries, max_rejections=max_rejections),
        audit=AuditLog(audit_path),
    )
    return Toolbox(gateway, EvidenceStore(evidence_path), parse_bindings(config), investigation_id)
