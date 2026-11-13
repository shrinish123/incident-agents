"""Secret value types.

A `Secret` wraps a credential so it cannot be printed, logged or serialised by
accident. The only way to get the raw value is `reveal()`, which is called at
exactly one place per connector: where the outbound request is built.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_REF_RE = re.compile(r"^secret://(?P<path>[A-Za-z0-9_\-./]+)#(?P<field>[A-Za-z0-9_\-]+)$")


class Secret:
    """Opaque credential value. `repr`, `str` and formatting never show it."""

    __slots__ = ("_value", "version")

    def __init__(self, value: str, version: str = "") -> None:
        self._value = value
        self.version = version

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(****)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "****"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __reduce__(self):  # refuse pickling: secrets never leave process memory
        raise TypeError("Secret values cannot be pickled")


@dataclass(frozen=True)
class SecretRef:
    """A reference such as `secret://sumo/prod#access_key`. Safe to log."""

    path: str
    field: str

    @classmethod
    def parse(cls, ref: str) -> "SecretRef":
        m = _REF_RE.match(ref)
        if not m:
            raise ValueError(
                f"Invalid secret reference {ref!r}. Expected secret://<path>#<field>, "
                "for example secret://sumo/prod#access_key"
            )
        return cls(path=m["path"].strip("/"), field=m["field"])

    @staticmethod
    def is_ref(value: object) -> bool:
        return isinstance(value, str) and value.startswith("secret://")

    def __str__(self) -> str:
        return f"secret://{self.path}#{self.field}"


class SecretNotFound(LookupError):
    """The backend has no value for this reference."""


class SecretBackendError(RuntimeError):
    """The backend could not be reached or refused the request."""
