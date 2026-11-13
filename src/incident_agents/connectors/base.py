"""Connector contract.

A connector knows how to talk to one kind of system. It receives already
linted params and already resolved credentials from the gateway, and it never
decides policy. Raise `AuthRejected` on 401/403, `ConnectorTimeout` on a
timeout and `ConnectorError` for anything else.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ..querytypes import CostEstimate, RawResult


class Connector(Protocol):
    type: str

    async def estimate(self, params: dict[str, Any], creds: dict[str, Any]) -> CostEstimate | None:
        """Cheap cost estimate (for example EXPLAIN). Return None if unsupported."""
        ...

    async def execute(
        self, params: dict[str, Any], creds: dict[str, Any], *, timeout_s: float, max_rows: int
    ) -> RawResult: ...


ConnectorFactory = Callable[[dict[str, Any]], Connector]
_REGISTRY: dict[str, ConnectorFactory] = {}


def register_connector(type_name: str) -> Callable[[ConnectorFactory], ConnectorFactory]:
    """Register a factory that builds a connector from a source's `options`."""

    def deco(factory: ConnectorFactory) -> ConnectorFactory:
        _REGISTRY[type_name] = factory
        return factory

    return deco


def build_connector(type_name: str, options: dict[str, Any]) -> Connector:
    if type_name not in _REGISTRY:
        raise KeyError(f"No connector registered for type {type_name!r}. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[type_name](options)
