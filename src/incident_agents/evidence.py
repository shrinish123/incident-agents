"""Evidence store: every observation an agent can cite.

A tool result becomes one or more immutable `Evidence` records with stable,
readable IDs such as `log:fivo-api:0012` or `deploy:fivo-worker:0001`. Agents
never quote raw tool output in their conclusions; they cite evidence IDs, and
`check_citations` tells the Critic and the grader which citations are real.

Each record also carries `pivots`: identifiers found in the data (trace_id,
user_id, sha, error_fingerprint...) that the next hop of an investigation can
be seeded with. The store indexes them, so "what else mentions trace t-91?"
is a lookup across every source already queried.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any

PIVOT_KEYS = (
    "trace_id", "request_id", "user_id", "job_id", "task_id", "sha", "prev_sha",
    "deploy_id", "error_fingerprint", "route", "host", "version", "idempotency_key",
)
PIVOT_ALIASES = {"commit": "sha", "commit_sha": "sha", "git_sha": "sha", "release": "version", "path": "route"}
_ERROR_LEVELS = {"error", "critical", "fatal", "warning", "warn"}

_NORMALISERS = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def fingerprint(message: str) -> str:
    """Stable ID for an error message with its variable parts removed."""
    text = message.lower()
    for pattern, token in _NORMALISERS:
        text = pattern.sub(token, text)
    return "fp_" + hashlib.sha1(text.strip().encode()).hexdigest()[:10]


def extract_pivots(row: dict[str, Any], keys: tuple[str, ...] = PIVOT_KEYS) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in row.items():
        key = PIVOT_ALIASES.get(name, name)
        if key in keys and isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip()
            if text and len(text) <= 120:
                out.setdefault(key, text)
    message = row.get("message")
    if isinstance(message, str) and message and "error_fingerprint" not in out:
        if str(row.get("level", "")).lower() in _ERROR_LEVELS or row.get("error"):
            out["error_fingerprint"] = fingerprint(message)
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9._\-]+", "-", str(text).lower()).strip("-")[:40] or "none"


@dataclass(frozen=True)
class Evidence:
    id: str
    investigation_id: str
    kind: str  # log, logagg, metric, trace, db, deploy, config, topology, incident, diff, code...
    subject: str  # service, repo or table the evidence is about
    summary: str  # one line, deterministic, safe to show in digests
    data: dict[str, Any]
    ts: str | None = None  # when the observed thing happened, if it has a time
    pivots: dict[str, str] = field(default_factory=dict)
    complete: bool = True  # False when the source returned a partial result
    source: str = ""
    tool: str = ""
    query_id: str = ""
    step_id: str | None = None

    def digest(self) -> dict[str, Any]:
        """Short form for prompts that list many pieces of evidence."""
        out: dict[str, Any] = {"id": self.id, "summary": self.summary}
        if self.ts:
            out["ts"] = self.ts
        if not self.complete:
            out["partial"] = True
        return out

    def full(self) -> dict[str, Any]:
        out = self.digest()
        out["data"] = self.data
        return out


_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
  investigation_id TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL,
  seq INTEGER NOT NULL, summary TEXT NOT NULL, data TEXT NOT NULL, ts TEXT, complete INTEGER NOT NULL,
  source TEXT, tool TEXT, query_id TEXT, step_id TEXT, hash TEXT NOT NULL,
  created INTEGER NOT NULL,
  PRIMARY KEY (investigation_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS evidence_hash ON evidence (investigation_id, hash);
CREATE TABLE IF NOT EXISTS evidence_pivots (
  investigation_id TEXT NOT NULL, evidence_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY (investigation_id, evidence_id, key)
);
CREATE INDEX IF NOT EXISTS pivot_lookup ON evidence_pivots (investigation_id, key, value);
"""


class EvidenceStore:
    """SQLite-backed. Pass a file path to keep evidence across runs, or nothing for in-memory."""

    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def add(
        self,
        investigation_id: str,
        kind: str,
        subject: str,
        summary: str,
        data: dict[str, Any],
        *,
        ts: str | None = None,
        pivots: dict[str, str] | None = None,
        complete: bool = True,
        source: str = "",
        tool: str = "",
        query_id: str = "",
        step_id: str | None = None,
    ) -> Evidence:
        """Store one observation. The same observation seen twice keeps its first ID."""
        subject = _slug(subject)
        body = json.dumps(data, sort_keys=True, default=str)
        digest = hashlib.sha1(f"{kind}|{subject}|{body}".encode()).hexdigest()
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT id FROM evidence WHERE investigation_id=? AND hash=?", (investigation_id, digest)
            ).fetchone()
            if row:
                return self._load(investigation_id, [row["id"]])[0]
            seq = self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM evidence WHERE investigation_id=? AND kind=? AND subject=?",
                (investigation_id, kind, subject),
            ).fetchone()[0]
            created = self._db.execute(
                "SELECT COUNT(*) FROM evidence WHERE investigation_id=?", (investigation_id,)
            ).fetchone()[0]
            eid = f"{kind}:{subject}:{seq:04d}"
            self._db.execute(
                "INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (investigation_id, eid, kind, subject, seq, summary[:400], body, ts, int(complete),
                 source, tool, query_id, step_id, digest, created),
            )
            self._db.executemany(
                "INSERT OR IGNORE INTO evidence_pivots VALUES (?,?,?,?)",
                [(investigation_id, eid, k, str(v)) for k, v in (pivots or {}).items()],
            )
            return self._load(investigation_id, [eid])[0]

    def _load(self, investigation_id: str, ids: list[str]) -> list[Evidence]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self._db.execute(
            f"SELECT * FROM evidence WHERE investigation_id=? AND id IN ({marks}) ORDER BY created",
            (investigation_id, *ids),
        ).fetchall()
        pivots: dict[str, dict[str, str]] = {}
        for p in self._db.execute(
            f"SELECT evidence_id, key, value FROM evidence_pivots WHERE investigation_id=? AND evidence_id IN ({marks})",
            (investigation_id, *ids),
        ):
            pivots.setdefault(p["evidence_id"], {})[p["key"]] = p["value"]
        return [
            Evidence(
                id=r["id"], investigation_id=investigation_id, kind=r["kind"], subject=r["subject"],
                summary=r["summary"], data=json.loads(r["data"]), ts=r["ts"], pivots=pivots.get(r["id"], {}),
                complete=bool(r["complete"]), source=r["source"] or "", tool=r["tool"] or "",
                query_id=r["query_id"] or "", step_id=r["step_id"],
            )
            for r in rows
        ]

    def get(self, investigation_id: str, evidence_id: str) -> Evidence | None:
        with self._lock:
            found = self._load(investigation_id, [evidence_id])
        return found[0] if found else None

    def get_many(self, investigation_id: str, ids: list[str]) -> list[Evidence]:
        with self._lock:
            return self._load(investigation_id, list(dict.fromkeys(ids)))

    def check_citations(self, investigation_id: str, ids: list[str]) -> tuple[list[str], list[str]]:
        """Split cited IDs into (real, invented). Used by the Critic and the grader."""
        unique = list(dict.fromkeys(ids))
        known = {e.id for e in self.get_many(investigation_id, unique)}
        return [i for i in unique if i in known], [i for i in unique if i not in known]

    def find(
        self,
        investigation_id: str,
        *,
        kind: str | None = None,
        subject: str | None = None,
        pivot: tuple[str, str] | None = None,
        text: str | None = None,
        step_id: str | None = None,
        limit: int = 200,
    ) -> list[Evidence]:
        sql = "SELECT e.id FROM evidence e"
        where, args = ["e.investigation_id=?"], [investigation_id]
        if pivot:
            sql += " JOIN evidence_pivots p ON p.investigation_id=e.investigation_id AND p.evidence_id=e.id"
            where += ["p.key=?", "p.value=?"]
            args += [pivot[0], str(pivot[1])]
        for column, value in (("kind", kind), ("subject", _slug(subject) if subject else None), ("step_id", step_id)):
            if value:
                where.append(f"e.{column}=?")
                args.append(value)
        if text:
            where.append("(e.summary LIKE ? OR e.data LIKE ?)")
            args += [f"%{text}%"] * 2
        with self._lock:
            ids = [r["id"] for r in self._db.execute(f"{sql} WHERE {' AND '.join(where)} ORDER BY e.created LIMIT ?", (*args, limit))]
            return self._load(investigation_id, ids)

    def pivots(self, investigation_id: str) -> dict[str, dict[str, list[str]]]:
        """Every identifier seen so far: {key: {value: [evidence ids]}}."""
        out: dict[str, dict[str, list[str]]] = {}
        with self._lock:
            for r in self._db.execute(
                "SELECT key, value, evidence_id FROM evidence_pivots WHERE investigation_id=? ORDER BY key, value, evidence_id",
                (investigation_id,),
            ):
                out.setdefault(r["key"], {}).setdefault(r["value"], []).append(r["evidence_id"])
        return out

    def count(self, investigation_id: str) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM evidence WHERE investigation_id=?", (investigation_id,)).fetchone()[0]

    def close(self) -> None:
        self._db.close()
