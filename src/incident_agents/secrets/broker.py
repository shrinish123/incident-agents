"""Credential broker: the only component that resolves secret references.

It lives inside the query gateway process. Agents name a source
(`sumo-prod`); the gateway asks the broker for that source's credentials at
call time. Agents have no tool that reaches this module.

Rotation: values are cached in memory for a short TTL. When a source rejects
its credentials, `call_with_auth_retry` drops the cache entry, fetches the
current value once and retries once. A second rejection raises
`SourceAuthError`, so the gateway can mark the source unavailable instead of
hammering it (which can lock the account).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .scrubber import SecretScrubber
from .stores import SecretStore
from .types import Secret, SecretRef

T = TypeVar("T")


class AuthRejected(Exception):
    """A connector raises this when the source answers 401/403."""


class SourceAuthError(Exception):
    """Credentials were refreshed and the source still rejected them."""

    def __init__(self, source: str) -> None:
        super().__init__(f"Source {source!r} rejected its credentials after a refresh")
        self.source = source


@dataclass(frozen=True)
class SecretAccess:
    """One audit record. Holds the reference and version, never the value."""

    ts: float
    source: str
    ref: str
    version: str
    investigation_id: str | None
    cache_hit: bool


@dataclass
class _Entry:
    secret: Secret
    expires_at: float


@dataclass
class CredentialBroker:
    store: SecretStore
    scrubber: SecretScrubber = field(default_factory=SecretScrubber)
    ttl_s: float = 300.0
    clock: Callable[[], float] = time.monotonic
    on_access: Callable[[SecretAccess], None] | None = None

    def __post_init__(self) -> None:
        self._cache: dict[SecretRef, _Entry] = {}
        self._locks: dict[SecretRef, asyncio.Lock] = {}
        self.access_log: list[SecretAccess] = []

    async def resolve(
        self, ref: SecretRef | str, *, source: str, investigation_id: str | None = None
    ) -> Secret:
        if isinstance(ref, str):
            ref = SecretRef.parse(ref)
        entry = self._cache.get(ref)
        if entry and entry.expires_at > self.clock():
            self._record(source, ref, entry.secret, investigation_id, cache_hit=True)
            return entry.secret

        # One fetch per ref even when several threads need it at once.
        lock = self._locks.setdefault(ref, asyncio.Lock())
        async with lock:
            entry = self._cache.get(ref)
            if entry and entry.expires_at > self.clock():
                self._record(source, ref, entry.secret, investigation_id, cache_hit=True)
                return entry.secret
            secret = await self.store.get(ref)
            self.scrubber.register(secret.reveal())
            self._cache[ref] = _Entry(secret, self.clock() + self.ttl_s)
            self._record(source, ref, secret, investigation_id, cache_hit=False)
            return secret

    async def resolve_auth(
        self, auth: Mapping[str, Any], *, source: str, investigation_id: str | None = None
    ) -> dict[str, Any]:
        """Resolve every `secret://` value in a source's auth block.

        Plain values (a username, a region) pass through unchanged.
        """
        out: dict[str, Any] = {}
        for key, value in auth.items():
            if SecretRef.is_ref(value):
                out[key] = await self.resolve(value, source=source, investigation_id=investigation_id)
            else:
                out[key] = value
        return out

    def invalidate(self, auth: Mapping[str, Any]) -> None:
        for value in auth.values():
            if SecretRef.is_ref(value):
                self._cache.pop(SecretRef.parse(value), None)

    async def call_with_auth_retry(
        self,
        auth: Mapping[str, Any],
        call: Callable[[dict[str, Any]], Awaitable[T]],
        *,
        source: str,
        investigation_id: str | None = None,
    ) -> T:
        """Run `call(creds)`. On `AuthRejected`, refresh credentials and retry once."""
        creds = await self.resolve_auth(auth, source=source, investigation_id=investigation_id)
        try:
            return await call(creds)
        except AuthRejected:
            self.invalidate(auth)
        creds = await self.resolve_auth(auth, source=source, investigation_id=investigation_id)
        try:
            return await call(creds)
        except AuthRejected:
            self.invalidate(auth)
            raise SourceAuthError(source) from None

    def _record(
        self, source: str, ref: SecretRef, secret: Secret, investigation_id: str | None, *, cache_hit: bool
    ) -> None:
        access = SecretAccess(
            ts=time.time(),
            source=source,
            ref=str(ref),
            version=secret.version,
            investigation_id=investigation_id,
            cache_hit=cache_hit,
        )
        self.access_log.append(access)
        if self.on_access:
            self.on_access(access)
