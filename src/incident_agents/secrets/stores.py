"""Secret backends. All implement `SecretStore`; the broker is the only caller."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Protocol

import httpx

from .types import Secret, SecretBackendError, SecretNotFound, SecretRef


class SecretStore(Protocol):
    name: str

    async def get(self, ref: SecretRef) -> Secret:
        """Return the current value. Never cached here; the broker caches."""
        ...


class EnvSecretStore:
    """Local development backend.

    `secret://sumo/prod#access_key` reads the env var `SUMO_PROD_ACCESS_KEY`.
    The environment is re-read on every call, so a rotated value is picked up
    as soon as the broker's cache entry is invalidated.
    """

    name = "env"

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    @staticmethod
    def var_name(ref: SecretRef) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", f"{ref.path}_{ref.field}").upper()

    async def get(self, ref: SecretRef) -> Secret:
        var = self.var_name(ref)
        value = self._environ.get(var)
        if not value:
            raise SecretNotFound(f"{ref} is not set. Export {var} or add it to .env")
        return Secret(value, version="env")


class VaultSecretStore:
    """HashiCorp Vault backend.

    Static secrets come from a KV v2 mount: `secret://sumo/prod#access_key`
    reads `<mount>/data/sumo/prod` and returns the `access_key` field.

    Paths under `dynamic_prefix` (default `database/creds/`) are read as
    dynamic secrets: Vault mints a short-lived credential on each read, which
    is how each investigation gets its own read-only database user.
    """

    name = "vault"

    def __init__(
        self,
        addr: str,
        token: Secret,
        mount: str = "secret",
        dynamic_prefix: str = "database/creds/",
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        self._addr = addr.rstrip("/")
        self._token = token
        self._mount = mount.strip("/")
        self._dynamic_prefix = dynamic_prefix
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def get(self, ref: SecretRef) -> Secret:
        dynamic = ref.path.startswith(self._dynamic_prefix)
        url = (
            f"{self._addr}/v1/{ref.path}"
            if dynamic
            else f"{self._addr}/v1/{self._mount}/data/{ref.path}"
        )
        try:
            resp = await self._client.get(url, headers={"X-Vault-Token": self._token.reveal()})
        except httpx.HTTPError as exc:
            # Only the exception type: httpx messages can include request details.
            raise SecretBackendError(f"Vault unreachable for {ref}: {type(exc).__name__}") from None
        if resp.status_code == 404:
            raise SecretNotFound(f"{ref} not found in Vault")
        if resp.status_code != 200:
            raise SecretBackendError(f"Vault returned {resp.status_code} for {ref}")

        body = resp.json()
        if dynamic:
            data = body.get("data") or {}
            version = str(body.get("lease_id", ""))
        else:
            data = (body.get("data") or {}).get("data") or {}
            version = str(((body.get("data") or {}).get("metadata") or {}).get("version", ""))
        if ref.field not in data:
            raise SecretNotFound(f"{ref}: field {ref.field!r} not present at that path")
        return Secret(str(data[ref.field]), version=version)

    async def aclose(self) -> None:
        await self._client.aclose()
