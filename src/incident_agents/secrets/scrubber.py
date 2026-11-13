"""Removes credentials from any text before it reaches an LLM, a trace or a log.

Two layers:
1. Exact match on every secret value the broker has handed out in this process.
2. Patterns for common credential shapes, for secrets we were never told about
   (for example a connection string printed by a database driver error).
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # scheme://user:password@host  ->  keep scheme, user and host
    ("url_password", re.compile(r"(?P<pre>\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+(?P<post>@)", re.I)),
    ("bearer", re.compile(r"(?P<pre>\b(?:Bearer|Basic|token)\s+)[A-Za-z0-9._~+/\-]{16,}=*", re.I)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("vault_token", re.compile(r"\bhv[sbr]\.[A-Za-z0-9_\-]{20,}\b")),
    ("llm_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("newrelic_key", re.compile(r"\bNR(?:AK|II|AL)-[A-Za-z0-9]{20,}\b")),
    (
        "key_value",
        re.compile(
            r"(?P<pre>\b(?:password|passwd|pwd|secret|api[_\-]?key|access[_\-]?key|"
            r"access[_\-]?token|auth[_\-]?token|private[_\-]?key)\b[\"']?\s*[=:]\s*[\"']?)"
            r"[^\s\"',;&]{6,}",
            re.I,
        ),
    ),
]

# Shorter values would cause false positives all over ordinary text.
_MIN_LIVE_LEN = 6


class SecretScrubber:
    def __init__(self) -> None:
        self._live: set[str] = set()
        self._live_re: re.Pattern[str] | None = None

    def register(self, value: str) -> None:
        """Remember a live secret value so it is always removed verbatim."""
        if len(value) >= _MIN_LIVE_LEN and value not in self._live:
            self._live.add(value)
            # Longest first so a secret that contains another is fully removed.
            ordered = sorted(self._live, key=len, reverse=True)
            self._live_re = re.compile("|".join(re.escape(v) for v in ordered))

    def scrub(self, text: str) -> str:
        if not text:
            return text
        if self._live_re is not None:
            text = self._live_re.sub(REDACTED, text)
        for _, pattern in _PATTERNS:
            text = pattern.sub(_replace, text)
        return text

    def scrub_obj(self, obj: Any) -> Any:
        """Scrub every string inside a JSON-like structure, keys included."""
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, dict):
            return {self.scrub_obj(k): self.scrub_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return type(obj)(self.scrub_obj(v) for v in obj)
        return obj


def _replace(match: re.Match[str]) -> str:
    groups = match.groupdict()
    return f"{groups.get('pre') or ''}{REDACTED}{groups.get('post') or ''}"
