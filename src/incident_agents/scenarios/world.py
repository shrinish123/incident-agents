"""The simulated world behind every scenario.

A `World` generates one consistent slice of production around an incident:
API requests, the worker tasks they cause, the worker's queue, and from those
the logs, traces, metrics and database rows. A scenario perturbs the world
(fails some requests, slows a dependency, adds a deploy with a real commit)
and everything downstream follows, so the same fact shows up in every source
that would show it in a real system.

Time is measured in minutes relative to the moment the alert fired
(`detected_at`): the world covers [-180, +20).
"""

from __future__ import annotations

import atexit
import hashlib
import heapq
import itertools
import json
import math
import os
import random
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .codebase import REPOS

BEFORE = 180
AFTER = 20
LABELS = ["groceries", "dining", "transport", "utilities", "rent", "shopping", "health", "entertainment", "other"]

MERCHANTS = [
    ("BigBasket", "Asia/Kolkata"), ("Swiggy", "Asia/Kolkata"), ("Uber", "Asia/Kolkata"), ("Zepto", "Asia/Kolkata"),
    ("Amazon", "Asia/Kolkata"), ("Airtel", "Asia/Kolkata"), ("Blue Tokai", "Asia/Kolkata"), ("IRCTC", "Asia/Kolkata"),
    ("Netflix", "America/Los_Angeles"), ("Apollo Pharmacy", "Asia/Kolkata"), ("Decathlon", "Asia/Kolkata"),
    ("BESCOM", "Asia/Kolkata"), ("Third Wave Coffee", "Asia/Kolkata"), ("Spotify", "Europe/Stockholm"),
]
MANUAL_MERCHANTS = ["vegetable vendor", "auto fare", "house help", "chai", "street food", "tailor", "laundry", "newspaper"]

# (method, route) -> default weight and downstream calls (kind, peer, name, typical ms)
ROUTES: dict[tuple[str, str], dict[str, Any]] = {
    ("GET", "/transactions"): {"w": 30, "spans": [("db", "neon-postgres", "SELECT transactions by user", 9)]},
    ("POST", "/transactions"): {"w": 22, "spans": [("db", "neon-postgres", "INSERT transactions", 7), ("queue", "upstash-redis", "enqueue categorize_transaction", 3)]},
    ("GET", "/summary"): {"w": 12, "spans": [("db", "neon-postgres", "SELECT monthly totals", 18)]},
    ("POST", "/transactions/quick-add"): {"w": 8, "spans": [("db", "neon-postgres", "INSERT transactions", 7), ("queue", "upstash-redis", "enqueue categorize_transaction", 3)]},
    ("POST", "/receipts"): {"w": 8, "spans": [("http", "r2-storage", "PUT object", 85), ("db", "neon-postgres", "INSERT receipts", 6), ("queue", "upstash-redis", "enqueue process_receipt", 3)]},
    ("GET", "/receipts/{receipt_id}"): {"w": 6, "spans": [("db", "neon-postgres", "SELECT receipts by id", 5)]},
    ("GET", "/insights"): {"w": 1.2, "spans": [("db", "neon-postgres", "SELECT monthly totals", 18), ("http", "anthropic-llm", "POST /v1/messages", 1900)]},
    ("POST", "/voice/call"): {"w": 1.5, "spans": [("db", "neon-postgres", "INSERT voice_calls", 6), ("http", "bolna-voice", "POST /call", 320)]},
    ("GET", "/categories"): {"w": 5, "spans": [("db", "neon-postgres", "SELECT categories", 4)]},
}

TOPOLOGY = [
    {"service": "fivo-api", "kind": "service", "runtime": "FastAPI on one 512 MB web instance; sleeps when idle, pinged on /health every minute",
     "repo": "fivo-api", "depends_on": ["neon-postgres", "upstash-redis", "r2-storage", "anthropic-llm", "bolna-voice", "fivo-worker"],
     "notes": "Hands background work to fivo-worker through the Redis queue. POST /transactions/quick-add waits for the worker's result."},
    {"service": "fivo-worker", "kind": "service", "runtime": "Celery on one 512 MB instance, two worker threads, beat in the same process",
     "repo": "fivo-worker", "depends_on": ["neon-postgres", "upstash-redis", "r2-storage", "groq-llm", "voyage-embeddings", "anthropic-llm"],
     "notes": "Tasks: categorize_transaction (Groq), process_receipt (R2, Anthropic, Voyage), embed_pending_receipts every 10 min (Voyage), daily_summary."},
    {"service": "neon-postgres", "kind": "database", "provider": "Neon", "used_by": ["fivo-api", "fivo-worker"], "notes": "Serverless Postgres. Schema is owned by fivo-api migrations."},
    {"service": "upstash-redis", "kind": "queue", "provider": "Upstash", "used_by": ["fivo-api", "fivo-worker"], "notes": "Celery broker and result backend. Plan has a daily command quota."},
    {"service": "r2-storage", "kind": "object_storage", "provider": "Cloudflare R2", "used_by": ["fivo-api", "fivo-worker"], "notes": "Receipt uploads. Each service has its own copy of the access key in its environment."},
    {"service": "groq-llm", "kind": "external_api", "provider": "Groq", "used_by": ["fivo-worker"], "notes": "Transaction categorisation."},
    {"service": "voyage-embeddings", "kind": "external_api", "provider": "Voyage", "used_by": ["fivo-worker"], "notes": "Receipt embeddings."},
    {"service": "anthropic-llm", "kind": "external_api", "provider": "Anthropic", "used_by": ["fivo-api", "fivo-worker"], "notes": "Insights (api), receipt field extraction and summaries (worker)."},
    {"service": "bolna-voice", "kind": "external_api", "provider": "Bolna", "used_by": ["fivo-api"], "notes": "Outbound reminder calls. No client-side metrics are collected for this provider."},
]

PAST_INCIDENTS = [
    {"id": "INC-2026-021", "ts": "2026-09-28T06:10:00Z", "service": "fivo-worker", "title": "Receipt queue stalled for 40 minutes",
     "symptoms": "queue_depth above 200, receipts stuck in uploaded, worker log quiet",
     "cause": "Worker concurrency had been raised to 8 and the broker refused new connections at the plan's connection limit.",
     "resolution": "Concurrency set back to 2.", "duration_min": 40},
    {"id": "INC-2026-024", "ts": "2026-10-08T03:55:00Z", "service": "fivo-api", "title": "5xx spike on POST /transactions",
     "symptoms": "QueuePool timeouts on every database route for six minutes",
     "cause": "The database compute had suspended and its cold start took longer than the pool timeout.",
     "resolution": "Keep-alive query added to the health pinger.", "duration_min": 6},
    {"id": "INC-2026-027", "ts": "2026-10-19T12:30:00Z", "service": "fivo-api", "title": "Insights endpoint returning 502",
     "symptoms": "GET /insights failing for all users, other routes healthy",
     "cause": "The configured model id had been retired by the provider and requests were rejected.",
     "resolution": "ANTHROPIC_MODEL updated.", "duration_min": 95},
    {"id": "INC-2026-029", "ts": "2026-10-27T15:20:00Z", "service": "fivo-worker", "title": "Categorisation backlog after card sync",
     "symptoms": "queue_depth climbing, categorize_transaction retrying with 429 from the categorisation provider",
     "cause": "A bulk card sync sent several thousand transactions at once and hit the provider's rate limit.",
     "resolution": "Backoff added to the task; card sync now enqueues in batches.", "duration_min": 55},
    {"id": "INC-2026-030", "ts": "2026-11-01T03:05:00Z", "service": "fivo-api", "title": "Elevated latency on GET /transactions",
     "symptoms": "p95 above 2 s for 15 minutes around 03:00 UTC",
     "cause": "The nightly summary job scanned a day of transactions while the database was scaling up.",
     "resolution": "None needed; self-resolved. Summary job to be moved off-peak.", "duration_min": 15},
]

DB_COLUMNS = {
    "users": "id, email, name, plan, device_count, password_hash (restricted), created_at",
    "categories": "id, name",
    "transactions": "id, user_id, device_id, client_seq, amount_minor, currency, merchant, merchant_tz, category_id, source, idempotency_key, created_at",
    "receipts": "id, user_id, object_key, content_type, size_bytes, status, created_at, processed_at, embedded_at",
    "jobs": "id, type, user_id, ref_id, status (queued|started|retrying|succeeded|failed), attempts, last_error, created_at, started_at, finished_at",
    "voice_calls": "id, user_id, status, provider_call_id, error, created_at",
    "_notes": "Timestamps are UTC text like 2026-11-10T08:00:00Z; compare them with string literals. now() is the end of the available data.",
}

DB_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, name TEXT, plan TEXT, device_count INTEGER, password_hash TEXT, created_at TEXT);
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE transactions (id INTEGER PRIMARY KEY, user_id INTEGER, device_id TEXT, client_seq INTEGER, amount_minor INTEGER, currency TEXT,
  merchant TEXT, merchant_tz TEXT, category_id INTEGER, source TEXT, idempotency_key TEXT UNIQUE, created_at TEXT);
CREATE INDEX ix_transactions_user_created ON transactions (user_id, created_at);
CREATE INDEX ix_transactions_created ON transactions (created_at);
CREATE TABLE receipts (id INTEGER PRIMARY KEY, user_id INTEGER, object_key TEXT, content_type TEXT, size_bytes INTEGER, status TEXT,
  created_at TEXT, processed_at TEXT, embedded_at TEXT);
CREATE INDEX ix_receipts_user ON receipts (user_id);
CREATE INDEX ix_receipts_created ON receipts (created_at);
CREATE INDEX ix_receipts_status ON receipts (status);
CREATE TABLE jobs (id TEXT PRIMARY KEY, type TEXT, user_id INTEGER, ref_id INTEGER, status TEXT, attempts INTEGER, last_error TEXT,
  created_at TEXT, started_at TEXT, finished_at TEXT);
CREATE INDEX ix_jobs_status_created ON jobs (status, created_at);
CREATE INDEX ix_jobs_created ON jobs (created_at);
CREATE INDEX ix_jobs_ref ON jobs (ref_id);
CREATE INDEX ix_jobs_user ON jobs (user_id);
CREATE TABLE voice_calls (id INTEGER PRIMARY KEY, user_id INTEGER, status TEXT, provider_call_id TEXT, error TEXT, created_at TEXT);
CREATE INDEX ix_voice_user ON voice_calls (user_id);
CREATE INDEX ix_voice_created ON voice_calls (created_at);
"""

SQL_TABLES = {
    "users": {"indexed": ["id"], "deny_columns": ["password_hash"]},
    "categories": {"small": True},
    "transactions": {"indexed": ["id", "user_id", "created_at", "idempotency_key"]},
    "receipts": {"indexed": ["id", "user_id", "created_at", "status"]},
    "jobs": {"indexed": ["id", "user_id", "created_at", "status", "ref_id"]},
    "voice_calls": {"indexed": ["id", "user_id", "created_at"]},
}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _hex(*parts: Any, n: int = 12) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:n]


# --- git ---------------------------------------------------------------------------


def git(cwd: Path, *args: str, date: str | None = None, author: str | None = None) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd), "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1", "TZ": "UTC", "LC_ALL": "C",
    }
    name, _, email = (author or "CI <ci@fivo.example>").partition(" <")
    env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email.rstrip(">"), GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email.rstrip(">"))
    if date:
        stamp = date.replace("Z", "+00:00")
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    done = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def _write(root: Path, path: str, content: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def build_base_repo(dest: Path, files: dict[str, str], history: list[dict[str, Any]]) -> list[str]:
    """Create a repository whose history ends at `files`. Returns commit SHAs, oldest first."""
    dest.mkdir(parents=True)
    git(dest, "init", "-q", "-b", "main")
    current = dict(files)
    for commit in reversed(history):  # rewind every later change to get the starting contents
        for path, before, after in reversed(commit.get("patch", [])):
            if after not in current[path]:
                raise ValueError(f"history patch does not apply to {path}")
            current[path] = current[path].replace(after, before, 1)
    shas = []
    for commit in history:
        for path in commit.get("add", []):
            _write(dest, path, current[path])
        for path, before, after in commit.get("patch", []):
            current[path] = current[path].replace(before, after, 1)
            _write(dest, path, current[path])
        git(dest, "add", "-A")
        git(dest, "commit", "-q", "-m", commit["message"], date=commit["date"], author=commit["author"])
        shas.append(git(dest, "rev-parse", "HEAD"))
    if current != files:
        raise ValueError("history does not end at the declared files")
    return shas


_TEMPLATES: dict[str, tuple[Path, list[str]]] = {}


def base_repo(name: str) -> tuple[Path, list[str]]:
    """Base repositories are built once per process and copied into each scenario."""
    if name not in _TEMPLATES:
        root = Path(tempfile.mkdtemp(prefix="ia-base-repo-"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        files, history = REPOS[name]
        _TEMPLATES[name] = (root / name, build_base_repo(root / name, files, history))
    return _TEMPLATES[name]


# --- world objects --------------------------------------------------------------------


@dataclass
class Req:
    t: float
    method: str
    route: str
    user_id: int | None = None
    device_id: str | None = None
    client_seq: int | None = None
    status: int = 200
    duration_ms: float = 5.0
    level: str = "info"
    message: str | None = None
    trace_id: str = ""
    spans: list[dict[str, Any]] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    task: "Task | None" = None
    traced: bool | None = None
    inserted: bool = True  # for POSTs: did the row reach the database

    def span(self, peer: str) -> dict[str, Any] | None:
        return next((s for s in self.spans if s["peer"] == peer), None)

    def fail(self, status: int, message: str, *, level: str = "error", duration_ms: float | None = None,
             peer: str | None = None, peer_ms: float | None = None, peer_error: str | None = None,
             keep_task: bool = False, inserted: bool = False) -> None:
        self.status, self.message, self.level, self.inserted = status, message, level, inserted
        if peer:
            idx = next((i for i, s in enumerate(self.spans) if s["peer"] == peer), len(self.spans) - 1)
            self.spans = self.spans[: idx + 1]  # nothing after the failing call ran
            if self.spans:
                self.spans[-1]["error"] = peer_error or message
                if peer_ms is not None:
                    self.spans[-1]["ms"] = peer_ms
        self.duration_ms = duration_ms if duration_ms is not None else 6 + sum(s["ms"] for s in self.spans)
        if self.task is not None and not keep_task:
            self.task.dropped = True

    def slow(self, peer: str, ms: float) -> None:
        s = self.span(peer)
        if s is not None:
            self.duration_ms += ms - s["ms"]
            s["ms"] = ms


@dataclass
class Task:
    name: str
    job_id: str
    queued: float
    user_id: int | None = None
    ref_id: int | None = None
    plan: list[tuple[float, str, Any]] = field(default_factory=list)  # (ms, ok|fail|retry, detail)
    crash_ms: float | None = None  # this task kills the worker this long after it starts
    restart_s: float = 30.0
    crash_text: str = ""
    trace_id: str = ""
    req: Req | None = None
    dropped: bool = False
    attempts: int = 0
    started: float | None = None
    finished: float | None = None
    status: str = "queued"
    last_error: str | None = None
    events: list[tuple[float, str, str]] = field(default_factory=list)
    runs: list[tuple[float, float, str]] = field(default_factory=list)  # (start, ms, outcome) per finished attempt


class World:
    def __init__(
        self,
        scenario_id: str,
        out: Path,
        detected_at: str,
        seed: int,
        *,
        rate: float = 10.0,
        weights: dict[tuple[str, str], float] | None = None,
        user_bias: dict[int, float] | None = None,
        multi_bias: float = 1.0,
        legacy_receipts: int = 0,
    ) -> None:
        self.id = scenario_id
        self.out = out
        self.detected = parse(detected_at)
        self.seed = seed
        self.rng = random.Random(seed)
        self.rate = rate
        self.weights = {k: v["w"] for k, v in ROUTES.items()} | (weights or {})
        self.legacy_receipts = legacy_receipts

        self.users = list(range(101, 161))
        multi = sorted(self.rng.sample(self.users, 18))
        self.devices: dict[int, list[str]] = {}
        self.seq: dict[str, int] = {}
        for u in self.users:
            first = f"{'ios' if u % 3 else 'and'}-{_hex(seed, u, 'a', n=4)}"
            self.devices[u] = [first]
            self.seq[first] = self.rng.randint(20, 400)
            if u in multi:
                second = f"{'and' if u % 3 else 'ios'}-{_hex(seed, u, 'b', n=4)}"
                self.devices[u].append(second)
                # the second phone was set up from the first one's backup, so the counters run close together
                self.seq[second] = self.seq[first]
        self.multi_device = set(multi)
        rank = {u: i for i, u in enumerate(self.rng.sample(self.users, len(self.users)))}
        self.user_weight = {
            u: (1 / (1 + 0.08 * rank[u])) * (user_bias or {}).get(u, 1.0) * (multi_bias if u in self.multi_device else 1.0)
            for u in self.users
        }

        self.requests: list[Req] = []
        self.tasks: list[Task] = []
        self.extra_logs: list[dict[str, Any]] = []
        self.extra_spans: list[dict[str, Any]] = []
        self.deploys: list[dict[str, Any]] = []
        self.config_changes: list[dict[str, Any]] = []
        self.incidents: list[dict[str, Any]] = [dict(i) for i in PAST_INCIDENTS]
        self.crashes: list[tuple[float, float, Task]] = []
        self.downtime: dict[str, list[tuple[float, float]]] = {"fivo-api": [], "fivo-worker": []}
        self._shapes: list[tuple[str, str, Callable[[int, float], float]]] = []
        self._series: dict[tuple[str, str], tuple[str, list[float]]] = {}
        self.db: dict[str, list[dict[str, Any]]] = {}
        self.sql_tables = json.loads(json.dumps(SQL_TABLES))
        self.faults: dict[str, list[dict[str, Any]]] = {}
        self.policy_overrides: dict[str, dict[str, Any]] = {}
        self.key_fn: Callable[[Req], str] = lambda r: f"{r.user_id}:{r.device_id}:{r.client_seq}"
        self._simulated = False
        self._ids = itertools.count(1)

        self.repos: dict[str, Path] = {}
        self.shas: dict[str, list[str]] = {}
        for name in REPOS:
            template, shas = base_repo(name)
            self.repos[name] = out / "repos" / name
            shutil.copytree(template, self.repos[name])
            self.shas[name] = list(shas)
        self._base_records()
        self._gen_requests()

    # ------------------------------------------------------------------ time

    def at(self, t: float) -> datetime:
        return self.detected + timedelta(minutes=t)

    def ts(self, t: float) -> str:
        return iso(self.at(t))

    @property
    def minutes(self) -> range:
        return range(-BEFORE, AFTER)

    # ------------------------------------------------------------------ records

    def _base_records(self) -> None:
        api, worker = self.shas["fivo-api"], self.shas["fivo-worker"]
        subject = lambda repo, i: REPOS[repo][1][i]["message"]  # noqa: E731
        self.deploys += [
            {"id": "dep-0398", "ts": "2026-10-26T09:10:00Z", "service": "fivo-worker", "repo": "fivo-worker", "sha": worker[-2], "prev_sha": worker[-3], "branch": "main", "by": "github-actions", "status": "superseded", "message": subject("fivo-worker", -2)},
            {"id": "dep-0403", "ts": "2026-10-29T17:05:00Z", "service": "fivo-api", "repo": "fivo-api", "sha": api[-2], "prev_sha": api[-3], "branch": "main", "by": "github-actions", "status": "superseded", "message": subject("fivo-api", -2)},
            {"id": "dep-0407", "ts": "2026-11-02T11:40:00Z", "service": "fivo-worker", "repo": "fivo-worker", "sha": worker[-1], "prev_sha": worker[-2], "branch": "main", "by": "github-actions", "status": "live", "message": subject("fivo-worker", -1)},
            {"id": "dep-0409", "ts": "2026-11-03T10:12:00Z", "service": "fivo-api", "repo": "fivo-api", "sha": api[-1], "prev_sha": api[-2], "branch": "main", "by": "github-actions", "status": "live", "message": subject("fivo-api", -1)},
        ]
        self.config_changes += [
            {"id": "cfg-0131", "ts": "2026-10-20T07:45:00Z", "service": "fivo-worker", "kind": "env_var", "key": "GROQ_MODEL", "old": "llama-3.1-70b-versatile", "new": "llama-3.3-70b-versatile", "by": "dashboard:ishaan", "note": "provider retired the old model id"},
            {"id": "cfg-0136", "ts": "2026-11-01T13:15:00Z", "service": "fivo-api", "kind": "env_var", "key": "RATE_LIMIT_RPM", "old": "180", "new": "240", "by": "dashboard:maya", "note": "headroom for the evening peak"},
        ]

    def commit(self, repo: str, t: float, author: str, message: str, *, patch: list[tuple[str, str, str]] | None = None,
               add: dict[str, str] | None = None) -> str:
        root = self.repos[repo]
        for path, before, after in patch or []:
            content = (root / path).read_text()
            if before not in content:
                raise ValueError(f"{self.id}: patch does not apply to {repo}/{path}")
            (root / path).write_text(content.replace(before, after, 1))
        for path, content in (add or {}).items():
            _write(root, path, content)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", message, date=self.ts(t), author=author)
        sha = git(root, "rev-parse", "HEAD")
        self.shas[repo].append(sha)
        return sha

    def deploy(self, service: str, t: float, *, sha: str | None = None, restart_s: float = 20.0) -> dict[str, Any]:
        """Deploy the head of the service's repo. The instance restarts."""
        repo = service
        sha = sha or self.shas[repo][-1]
        prev = next(d for d in reversed(self.deploys) if d["service"] == service)
        prev["status"] = "superseded"
        message = git(self.repos[repo], "log", "-1", "--format=%s", sha)
        record = {"id": f"dep-{410 + len(self.deploys):04d}", "ts": self.ts(t), "service": service, "repo": repo, "sha": sha,
                  "prev_sha": prev["sha"], "branch": "main", "by": "github-actions", "status": "live", "message": message}
        self.deploys.append(record)
        self.restart(service, t, restart_s, reason="deploy")
        return record

    def config_change(self, service: str, t: float, key: str, old: str, new: str, *, by: str, note: str = "",
                      kind: str = "env_var", restart: bool = True, **more: Any) -> dict[str, Any]:
        record = {"id": f"cfg-{133 + len(self.config_changes) * 3:04d}", "ts": self.ts(t), "service": service, "kind": kind,
                  "key": key, "old": old, "new": new, "by": by, "note": note, **more}
        self.config_changes.append(record)
        if restart and service in self.downtime:
            self.restart(service, t + 0.3, 18.0, reason="config")
        return record

    def restart(self, service: str, t: float, seconds: float, *, reason: str = "") -> None:
        host = f"{service}-1"
        self.log(t, service, "info", "Shutting down: received SIGTERM" if reason else "Shutting down", host=host)
        up = t + seconds / 60
        ready = "celery@fivo-worker ready." if service == "fivo-worker" else "Application startup complete."
        self.log(up, service, "info", ready, host=host)
        self.downtime[service].append((t, up))

    def log(self, t: float, service: str, level: str, message: str, **fields: Any) -> None:
        if -BEFORE <= t < AFTER:
            self.extra_logs.append({"ts": self.ts(t), "service": service, "level": level, "message": message, **fields, "_t": t})

    def shape(self, service: str, metric: str, fn: Callable[[int, float], float]) -> None:
        """Adjust a metric after its baseline is computed: fn(minute, baseline) -> value."""
        self._shapes.append((service, metric, fn))

    # ------------------------------------------------------------------ baseline traffic

    def _poisson(self, lam: float) -> int:
        limit, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= limit:
                return k
            k += 1

    def _job_id(self) -> str:
        h = _hex(self.seed, "job", next(self._ids), n=16)
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}"

    def make_request(self, t: float, method: str, route: str, user: int | None, device: str | None = None) -> Req:
        """Build one request with its downstream calls and the task it enqueues. Not yet added to the world."""
        rng = self.rng
        r = Req(t=t, method=method, route=route, user_id=user, trace_id=_hex(self.seed, "trace", next(self._ids), n=16))
        for kind, peer, name, ms in ROUTES.get((method, route), {"spans": []})["spans"]:
            actual = ms * rng.lognormvariate(0, 0.45 if peer == "anthropic-llm" else 0.25)
            r.spans.append({"kind": kind, "peer": peer, "name": name, "ms": round(actual, 1)})
        r.duration_ms = round(rng.uniform(3, 9) + sum(s["ms"] for s in r.spans), 1)
        r.status = {"POST": 201}.get(method, 200)
        if (method, route) in (("POST", "/transactions"), ("POST", "/transactions/quick-add")):
            owned = self.devices[user]
            # people with two phones use both, so the per-device counters stay close to each other
            even = min(owned, key=lambda d: self.seq[d]) if rng.random() < 0.8 else rng.choice(owned)
            r.device_id = device or even
            self.seq[r.device_id] += 1
            r.client_seq = self.seq[r.device_id]
            if rng.random() < 0.42:
                r.payload = {"merchant": rng.choice(MANUAL_MERCHANTS), "merchant_tz": None, "source": "manual"}
            else:
                merchant, tz = rng.choice(MERCHANTS)
                r.payload = {"merchant": merchant, "merchant_tz": tz, "source": "card"}
            r.payload["amount_minor"] = int(rng.lognormvariate(10.2, 0.9))
            r.task = Task("categorize_transaction", self._job_id(), t + rng.uniform(0.004, 0.012), user_id=user,
                          plan=[(max(120.0, rng.lognormvariate(5.85, 0.3)), "ok", None)], trace_id=r.trace_id, req=r)
            if rng.random() < 0.012:
                r.task.plan.insert(0, (260.0, "retry", (1, "groq returned 429 Too Many Requests")))
        elif (method, route) == ("POST", "/receipts"):
            r.status = 202
            r.payload = {"content_type": rng.choice(["image/jpeg", "image/jpeg", "image/png", "image/heic", "application/pdf"]),
                         "size_bytes": int(rng.lognormvariate(13.4, 0.6))}
            r.task = Task("process_receipt", self._job_id(), t + rng.uniform(0.004, 0.012), user_id=user,
                          plan=[(rng.uniform(6200, 9600), "ok", None)], trace_id=r.trace_id, req=r)
        elif (method, route) == ("POST", "/voice/call"):
            r.status = 202
        return r

    def _gen_requests(self) -> None:
        rng = self.rng
        routes = list(self.weights)
        weights = [self.weights[k] for k in routes]
        users, uw = self.users, [self.user_weight[u] for u in self.users]
        plan: list[tuple[float, tuple[str, str] | None, int | None]] = []
        for m in self.minutes:
            plan.append((m + rng.uniform(0.02, 0.2), None, None))  # health pinger
            lam = self.rate * (1 + 0.18 * math.sin(2 * math.pi * (m + BEFORE) / 97))
            for _ in range(self._poisson(lam)):
                plan.append((m + rng.random(), rng.choices(routes, weights)[0], rng.choices(users, uw)[0]))
        plan.sort(key=lambda p: p[0])
        for t, route, user in plan:
            if route is None:
                self.requests.append(Req(t=t, method="GET", route="/health", duration_ms=round(rng.uniform(2, 5), 1), traced=False))
                continue
            r = self.make_request(t, route[0], route[1], user)
            roll = rng.random()
            if roll < 0.004 and r.spans:
                r.fail(500, "ConnectionResetError: [Errno 104] Connection reset by peer", peer=r.spans[0]["peer"], peer_error="connection reset")
            elif roll < 0.02 and r.method == "POST":
                r.fail(422, "validation failed: amount_minor must be greater than 0", level="warning", duration_ms=4.0)
                r.spans = []
            self.add_request(r)
        for m in self.minutes:
            if (m + BEFORE) % 15 == 7:
                self.log(m + 0.4, "neon-postgres", "info", f"checkpoint complete: wrote {rng.randint(40, 380)} buffers; write={rng.uniform(1, 9):.3f} s, sync=0.0{rng.randint(10, 99)} s", host="neon-compute")
            if (m + BEFORE) % 10 == 3:
                self.tasks.append(Task("embed_pending_receipts", self._job_id(), m + 0.01, plan=[(rng.uniform(1800, 3200), "ok", None)],
                                       trace_id=_hex(self.seed, "beat", m, n=16)))

    def clone_request(self, r: Req, t: float) -> Req:
        """The client sending the same request again (same device, same entry)."""
        return Req(t=t, method=r.method, route=r.route, user_id=r.user_id, device_id=r.device_id, client_seq=r.client_seq,
                   status=r.status, duration_ms=r.duration_ms, level=r.level, message=r.message,
                   trace_id=_hex(self.seed, "trace", next(self._ids), n=16), spans=[dict(s) for s in r.spans],
                   payload=dict(r.payload), extra=dict(r.extra), inserted=r.inserted)

    def add_request(self, r: Req) -> None:
        self.requests.append(r)
        if r.task is not None:
            self.tasks.append(r.task)

    def reqs(self, method: str | None = None, route: str | None = None, start: float = -BEFORE, end: float = AFTER) -> list[Req]:
        return [r for r in self.requests if start <= r.t < end and (method is None or r.method == method)
                and (route is None or r.route == route) and r.route != "/health"]

    def tasks_of(self, name: str | None = None, start: float = -BEFORE, end: float = AFTER) -> list[Task]:
        return [t for t in self.tasks if start <= t.queued < end and (name is None or t.name == name) and not t.dropped]

    # ------------------------------------------------------------------ worker queue

    def run_worker(self, concurrency: int = 2) -> None:
        """Play the queue forward: two workers, retries with countdown, crashes that restart the instance."""
        order = itertools.count()
        heap: list[tuple[float, int, Task]] = [(t.queued, next(order), t) for t in self.tasks if not t.dropped]
        heapq.heapify(heap)
        slots = [float(-BEFORE)] * concurrency
        running: list[tuple[Task, float, float, tuple[float, str, Any]] | None] = [None] * concurrency
        crash: tuple[float, float] | None = None
        down = sorted(self.downtime["fivo-worker"])

        def adjust(start: float) -> float:
            if crash and crash[0] <= start < crash[1]:
                start = crash[1]
            for a, b in down:
                if a <= start < b:
                    start = b
            return start

        def commit(k: int) -> None:
            rec, running[k] = running[k], None
            if rec is None:
                return
            task, start, finish, (ms, outcome, detail) = rec
            if finish >= AFTER:
                return  # still running when the data ends
            task.runs.append((start, ms, outcome))
            if outcome == "ok":
                task.status, task.finished, task.last_error = "succeeded", finish, None
                task.events.append((finish, "succeeded", f"{ms / 1000:.2f}s"))
            elif outcome == "fail":
                task.status, task.finished, task.last_error = "failed", finish, str(detail)
                task.events.append((finish, "failed", str(detail)))
            else:
                countdown, exc = detail
                task.status, task.last_error = "retrying", str(exc)
                task.events.append((finish, "retry", f"Retry in {countdown}s: {exc}"))
                heapq.heappush(heap, (finish + countdown / 60, next(order), task))

        def interrupt(task: Task, when: float) -> None:
            task.events.append((when, "lost", ""))
            heapq.heappush(heap, (when, next(order), task))  # redelivered behind what is already waiting

        front: list[tuple[float, Task]] = []  # unacknowledged work, handed out first once the instance is back

        while True:
            if not heap and not front:
                pending = [k for k in range(concurrency) if running[k] is not None]
                if not pending:
                    break
                commit(min(pending, key=lambda k: running[k][2]))
                continue
            k = min(range(concurrency), key=lambda j: slots[j])
            nxt = min([heap[0][0]] if heap else []) if not front else min(front[0][0], heap[0][0] if heap else front[0][0])
            start = adjust(max(slots[k], nxt))
            done = [j for j in range(concurrency) if running[j] is not None and running[j][2] <= start]
            if done:
                for j in done:
                    commit(j)
                continue  # a retry may have joined the queue ahead of the current head
            if front and front[0][0] <= start:
                task = front.pop(0)[1]
            else:
                task = heapq.heappop(heap)[2]
            if start >= AFTER:
                continue  # never started within the data
            attempt = task.plan[min(task.attempts, len(task.plan) - 1)]
            task.attempts += 1
            task.started, task.status = start, "started"
            task.events.append((start, "received", ""))
            if task.crash_ms is not None:
                when = start + task.crash_ms / 60000
                if crash and start < crash[0] <= when:
                    task.events.append((crash[0], "lost", ""))
                    front.append((crash[1], task))
                    slots[k] = crash[1]
                    continue
                crash = (when, when + task.restart_s / 60)
                self.crashes.append((crash[0], crash[1], task))
                task.events.append((when, "crash", task.crash_text))
                front.append((crash[1], task))
                for j in range(concurrency):
                    if j != k and running[j] is not None and running[j][2] > when:
                        interrupt(running[j][0], when)  # in flight when the instance died
                        running[j] = None
                        slots[j] = crash[1]
                slots[k] = crash[1]
                continue
            finish = start + attempt[0] / 60000
            cut = crash[0] if crash and start < crash[0] < finish else next((a for a, b in down if start < a < finish), None)
            if cut is not None:
                interrupt(task, cut)
                slots[k] = adjust(cut)
                continue
            running[k] = (task, start, finish, attempt)
            slots[k] = finish

    # ------------------------------------------------------------------ simulate

    def simulate(self) -> None:
        """Run the queue, settle requests that wait on it, and build the database rows."""
        if self._simulated:
            return
        self._simulated = True
        for r in self.requests:  # requests that arrive while the API instance is restarting never reach it
            if any(a <= r.t < b for a, b in self.downtime["fivo-api"]):
                r.extra["_lost"] = True
                if r.task is not None:
                    r.task.dropped = True
        self.requests = [r for r in self.requests if not r.extra.get("_lost")]
        self.tasks = [t for t in self.tasks if not t.dropped]
        self.run_worker()
        wait_min = 8000 / 60000
        for r in self.reqs("POST", "/transactions/quick-add"):
            task = r.task
            if task is None or task.dropped or r.status >= 400:
                continue
            base = r.duration_ms
            if task.finished is not None and task.finished - r.t <= wait_min:
                waited = max(0.0, (task.finished - r.t) * 60000)
                r.spans.append({"kind": "internal", "peer": "fivo-worker", "name": "await categorisation result", "ms": round(waited, 1)})
                r.duration_ms = round(base + waited, 1)
                if task.status == "failed":
                    r.status, r.level = 500, "error"
                    r.message = "pydantic_core.ValidationError: 1 validation error for TransactionOut category: Input should be a valid string"
                    r.spans[-1]["error"] = "task result is an exception"
            else:
                r.spans.append({"kind": "internal", "peer": "fivo-worker", "name": "await categorisation result", "ms": 8000.0, "error": "result not ready"})
                r.status, r.level, r.duration_ms = 504, "error", round(base + 8000, 1)
                r.message = "categorisation result not ready after 8000ms"
        self._build_db()

    def _build_db(self) -> None:
        rng = random.Random(self.seed * 7 + 1)
        start = self.at(-BEFORE)
        users = [{"id": u, "email": f"user{u}@mail.example", "name": f"User {u}", "plan": "plus" if u % 7 == 0 else "free",
                  "device_count": len(self.devices[u]), "password_hash": "$argon2id$" + _hex(self.seed, u, "pw", n=24),
                  "created_at": iso(start - timedelta(days=30 + (u * 13) % 200))} for u in self.users]
        categories = [{"id": i + 1, "name": n} for i, n in enumerate(LABELS)]
        txns, receipts, jobs, calls = [], [], [], []

        seq_back = {d: s for d, s in self.seq.items()}
        history = []
        for i in range(640):  # the week before the window
            u = rng.choices(self.users, [self.user_weight[x] for x in self.users])[0]
            history.append((start - timedelta(minutes=rng.uniform(5, 6 * 24 * 60)), u))
        first_seq: dict[str, int] = {}
        for r in self.requests:
            if r.client_seq is not None:
                first_seq.setdefault(r.device_id, r.client_seq)
        counters = {d: first_seq.get(d, seq_back[d] + 1) for d in seq_back}
        rows = []
        for when, u in sorted(history, reverse=True):  # walk backwards so sequence numbers decrease into the past
            d = rng.choice(self.devices[u])
            counters[d] -= 1
            if counters[d] < 1:
                continue
            if rng.random() < 0.42:
                merchant, tz, source = rng.choice(MANUAL_MERCHANTS), None, "manual"
            else:
                (merchant, tz), source = rng.choice(MERCHANTS), "card"
            rows.append((when, u, d, counters[d], merchant, tz, source))
        tid = 81000
        for when, u, d, seq, merchant, tz, source in sorted(rows):
            tid += 1
            txns.append({"id": tid, "user_id": u, "device_id": d, "client_seq": seq, "amount_minor": int(rng.lognormvariate(10.2, 0.9)),
                         "currency": "INR", "merchant": merchant, "merchant_tz": tz, "category_id": rng.randint(1, len(LABELS)),
                         "source": source, "idempotency_key": f"{u}:{d}:{seq}", "created_at": iso(when)})
        rid = 4200
        for i in range(self.legacy_receipts):
            rid += 1
            when = start - timedelta(days=3, minutes=i * 0.9)
            receipts.append({"id": rid, "user_id": self.users[i % len(self.users)], "object_key": f"legacy-import/{_hex('legacy', i, n=20)}",
                             "content_type": "application/pdf", "size_bytes": int(rng.lognormvariate(12.6, 0.5)), "status": "processed",
                             "created_at": iso(when), "processed_at": iso(when), "embedded_at": None})
        receipts.sort(key=lambda x: x["created_at"])
        for i in range(90):
            rid += 1
            when = start - timedelta(minutes=rng.uniform(5, 6 * 24 * 60))
            receipts.append({"id": rid, "user_id": rng.choice(self.users), "object_key": f"receipts/{rng.choice(self.users)}/{_hex('r', i, n=20)}",
                             "content_type": rng.choice(["image/jpeg", "image/png", "image/heic", "application/pdf"]),
                             "size_bytes": int(rng.lognormvariate(13.4, 0.6)), "status": "processed", "created_at": iso(when),
                             "processed_at": iso(when + timedelta(seconds=9)), "embedded_at": iso(when + timedelta(seconds=9))})
        for i in range(160):
            when = start - timedelta(minutes=rng.uniform(5, 2 * 24 * 60))
            h = _hex(self.seed, "oldjob", i, n=16)
            kind = rng.choice(["categorize_transaction"] * 4 + ["process_receipt", "embed_pending_receipts"])
            jobs.append({"id": f"{h[:8]}-{h[8:12]}-{h[12:16]}", "type": kind, "user_id": None if kind.startswith("embed") else rng.choice(self.users),
                         "ref_id": None, "status": "succeeded", "attempts": 1, "last_error": None, "created_at": iso(when),
                         "started_at": iso(when + timedelta(seconds=1)), "finished_at": iso(when + timedelta(seconds=3))})
        cid = 900
        for r in sorted(self.requests, key=lambda x: x.t):
            if r.method != "POST":
                continue
            when = self.ts(r.t)
            if r.route in ("/transactions", "/transactions/quick-add") and (r.status < 400 or r.inserted):
                tid += 1
                task = r.task
                done = task is not None and task.status == "succeeded"
                r.extra["_row_id"] = tid
                if task is not None:
                    task.ref_id = tid
                txns.append({"id": tid, "user_id": r.user_id, "device_id": r.device_id, "client_seq": r.client_seq,
                             "amount_minor": r.payload["amount_minor"], "currency": "INR", "merchant": r.payload["merchant"],
                             "merchant_tz": r.payload["merchant_tz"], "category_id": rng.randint(1, len(LABELS)) if done else None,
                             "source": r.payload["source"], "idempotency_key": self.key_fn(r), "created_at": when})
            elif r.route == "/receipts" and r.status < 400:
                rid += 1
                task = r.task
                done = task is not None and task.status == "succeeded"
                if task is not None:
                    task.ref_id = rid
                r.extra["_row_id"] = rid
                finished = self.ts(task.finished) if done else None
                receipts.append({"id": rid, "user_id": r.user_id, "object_key": f"receipts/{r.user_id}/{_hex(self.seed, 'obj', rid, n=20)}",
                                 "content_type": r.payload["content_type"], "size_bytes": r.payload["size_bytes"],
                                 "status": "processed" if done else "uploaded", "created_at": when, "processed_at": finished, "embedded_at": finished})
            elif r.route == "/voice/call" and r.status != 422:
                cid += 1
                ok = r.status < 400
                calls.append({"id": cid, "user_id": r.user_id, "status": "queued" if ok else "failed",
                              "provider_call_id": f"exec_{_hex(self.seed, 'call', cid, n=14)}" if ok else None,
                              "error": None if ok else (r.message or "")[:200], "created_at": when})
        for t in self.tasks:
            if t.attempts == 0:
                continue  # the worker writes the row when it first starts the task
            jobs.append({"id": t.job_id, "type": t.name, "user_id": t.user_id, "ref_id": t.ref_id,
                         "status": t.status, "attempts": t.attempts, "last_error": t.last_error,
                         "created_at": self.ts(t.queued), "started_at": self.ts(t.started) if t.started is not None else None,
                         "finished_at": self.ts(t.finished) if t.finished is not None else None})
        self.db = {"users": users, "categories": categories, "transactions": txns, "receipts": receipts, "jobs": jobs, "voice_calls": calls}

    # ------------------------------------------------------------------ rendering

    def _log_lines(self) -> list[dict[str, Any]]:
        lines = [dict(line) for line in self.extra_logs]
        for r in self.requests:
            if not (-BEFORE <= r.t < AFTER):
                continue
            end = r.t + r.duration_ms / 60000
            line = {"_t": end, "ts": self.ts(end), "service": "fivo-api", "level": r.level,
                    "message": r.message or f"{r.method} {r.route} {r.status} in {r.duration_ms:.0f}ms",
                    "method": r.method, "route": r.route, "status": r.status, "duration_ms": round(r.duration_ms), "host": "fivo-api-1"}
            if r.trace_id:
                line["trace_id"] = r.trace_id
            if r.user_id is not None:
                line["user_id"] = r.user_id
            if r.device_id:
                line["device_id"] = r.device_id
            line.update({k: v for k, v in r.extra.items() if not k.startswith("_")})
            lines.append(line)
        for t in self.tasks:
            base = {"service": "fivo-worker", "task": t.name, "job_id": t.job_id, "host": "fivo-worker-1"}
            if t.trace_id:
                base["trace_id"] = t.trace_id
            if t.user_id is not None:
                base["user_id"] = t.user_id
            for when, kind, text in t.events:
                if not (-BEFORE <= when < AFTER):
                    continue
                stamp = {"_t": when, "ts": self.ts(when)}
                label = f"Task {t.name}[{t.job_id}]"
                if kind == "received":
                    lines.append({**stamp, **base, "level": "info", "message": f"{label} received"})
                elif kind == "succeeded":
                    lines.append({**stamp, **base, "level": "info", "message": f"{label} succeeded in {text}"})
                elif kind == "retry":
                    lines.append({**stamp, **base, "level": "warning", "message": f"{label} retry: {text}"})
                elif kind == "failed":
                    lines.append({**stamp, **base, "level": "error", "message": f"{label} raised unexpected: {text}"})
                elif kind == "crash":
                    lines.append({**stamp, "service": "fivo-worker", "level": "error", "message": text, "host": "fivo-worker-1", "source": "platform"})
        for down_at, up_at, _ in self.crashes:
            if up_at < AFTER:
                lines.append({"_t": up_at, "ts": self.ts(up_at), "service": "fivo-worker", "level": "info", "message": "celery@fivo-worker ready.", "host": "fivo-worker-1"})
        lines.sort(key=lambda x: (x["_t"], x["service"], x["message"]))
        for line in lines:
            line.pop("_t", None)
        return lines

    def _trace_rows(self) -> list[dict[str, Any]]:
        rng = random.Random(self.seed * 11 + 3)
        rows: list[dict[str, Any]] = []

        def emit(trace_id: str, parent: str | None, t: float, service: str, name: str, kind: str, ms: float, peer: str = "", error: str = "") -> str:
            span_id = _hex(trace_id, name, t, len(rows), n=10)
            row = {"ts": self.ts(t), "trace_id": trace_id, "span_id": span_id, "parent_id": parent, "service": service, "name": name,
                   "kind": kind, "duration_ms": round(ms, 1), "status": "error" if error else "ok"}
            if peer:
                row["peer"] = peer
            if error:
                row["error"] = error[:200]
            rows.append(row)
            return span_id

        sampled: set[str] = set()
        for r in self.requests:
            if r.t < -60 or not r.trace_id or r.traced is False:
                continue
            if not (r.traced or r.status >= 500 or rng.random() < 0.35):
                continue
            sampled.add(r.trace_id)
            root = emit(r.trace_id, None, r.t, "fivo-api", f"{r.method} {r.route}", "server", r.duration_ms,
                        error=(r.message or "") if r.status >= 500 else "")
            offset = 0.0
            for s in r.spans:
                emit(r.trace_id, root, r.t + offset / 60000, "fivo-api", s["name"], s["kind"], s["ms"], s["peer"], s.get("error", ""))
                offset += s["ms"]
        calls = {"categorize_transaction": ("http", "groq-llm", "POST /openai/v1/chat/completions"),
                 "process_receipt": ("http", "anthropic-llm", "POST /v1/messages"),
                 "embed_pending_receipts": ("http", "voyage-embeddings", "POST /v1/embeddings")}
        for t in self.tasks:
            if not t.runs or t.runs[-1][0] < -60:
                continue
            bad = t.status in ("failed", "retrying") or len(t.runs) > 1
            if not (t.trace_id in sampled or bad or rng.random() < 0.2):
                continue
            kind, peer, name = calls[t.name]
            for start, ms, outcome in t.runs[-6:]:
                error = "" if outcome == "ok" else (t.last_error or outcome)
                root = emit(t.trace_id, None, start, "fivo-worker", f"task {t.name}", "consumer", ms, error=error)
                emit(t.trace_id, root, start + 0.0001, "fivo-worker", "SELECT by id", "db", min(6.0, ms / 4), "neon-postgres")
                upstream_failed = outcome != "ok" and any(w in (t.last_error or "") for w in ("groq", "anthropic", "voyage", "timed out"))
                if outcome == "ok" or upstream_failed:
                    emit(t.trace_id, root, start + 0.0002, "fivo-worker", name, kind, max(1.0, ms - 14), peer, error if upstream_failed else "")
        rows += self.extra_spans
        rows.sort(key=lambda x: (x["ts"], x["trace_id"], x["span_id"]))
        return rows

    def _metrics(self) -> list[dict[str, Any]]:
        n = BEFORE + AFTER
        idx = lambda t: int(math.floor(t)) + BEFORE  # noqa: E731
        per_min: list[list[Req]] = [[] for _ in range(n)]
        for r in self.requests:
            if -BEFORE <= r.t < AFTER:
                per_min[idx(r.t)].append(r)
        rpm = [float(len(b)) for b in per_min]

        def window(i: int, width: int) -> list[Req]:
            return [r for b in per_min[max(0, i - width + 1): i + 1] for r in b if r.route != "/health"]

        err, p95 = [], []
        for i in range(n):
            w3 = window(i, 3)
            err.append(round(sum(1 for r in w3 if r.status >= 500) / len(w3), 4) if w3 else 0.0)
            w5 = sorted(r.duration_ms for r in window(i, 5))
            p95.append(round(w5[min(len(w5) - 1, int(0.95 * len(w5)))], 1) if w5 else 0.0)

        done = [[0, 0, []] for _ in range(n)]  # ok, failed, durations
        depth = [0] * n
        for t in self.tasks:
            for start, ms, outcome in t.runs:
                end = start + ms / 60000
                if -BEFORE <= end < AFTER:
                    cell = done[idx(end)]
                    if outcome != "retry":
                        cell[0 if outcome == "ok" else 1] += 1
                    cell[2].append(ms)
            first = t.runs[0][0] if t.runs else (t.started if t.started is not None else None)
            waiting_until = t.finished if t.finished is not None else AFTER
            if t.status == "succeeded" and t.runs:
                waiting_until = t.runs[-1][0]
            elif first is not None and t.status == "failed":
                waiting_until = t.finished or first
            for i in range(max(0, idx(t.queued) + 1), min(n, idx(min(waiting_until, AFTER - 0.001)) + 1)):
                depth[i] += 1
        completed = [float(c[0]) for c in done]
        fail_rate, task_p95 = [], []
        for i in range(n):
            ok = sum(c[0] for c in done[max(0, i - 2): i + 1])
            bad = sum(c[1] for c in done[max(0, i - 2): i + 1])
            fail_rate.append(round(bad / (ok + bad), 4) if ok + bad else 0.0)
            ds = sorted(d for c in done[max(0, i - 4): i + 1] for d in c[2])
            task_p95.append(round(ds[min(len(ds) - 1, int(0.95 * len(ds)))], 1) if ds else 0.0)
        restarts = {"fivo-api": [0.0] * n, "fivo-worker": [0.0] * n}
        for service, spans in self.downtime.items():
            for a, _ in spans:
                if -BEFORE <= a < AFTER:
                    restarts[service][idx(a)] += 1
        for a, _, _ in self.crashes:
            if -BEFORE <= a < AFTER:
                restarts["fivo-worker"][idx(a)] += 1

        def noisy(service: str, metric: str, fn: Callable[[int], float], digits: int = 1) -> list[float]:
            rng = random.Random(f"{self.seed}:{service}:{metric}")
            return [round(max(0.0, fn(i) + rng.gauss(0, 1)), digits) for i in range(n)]

        day_start = self.at(-BEFORE).replace(hour=0, minute=0, second=0)
        minutes_into_day = (self.at(-BEFORE) - day_start).total_seconds() / 60
        cmds = noisy("upstash-redis", "commands_per_min", lambda i: 262 + 9 * completed[i] + 2 * rpm[i], 0)
        used, total = [], 262.0 * minutes_into_day
        for c in cmds:
            total += c
            used.append(round(total))

        series: dict[tuple[str, str], tuple[str, list[float]]] = {
            ("fivo-api", "request_rate"): ("requests/min", rpm),
            ("fivo-api", "error_rate"): ("ratio", err),
            ("fivo-api", "p95_latency_ms"): ("ms", p95),
            ("fivo-api", "memory_mb"): ("MB", noisy("fivo-api", "memory_mb", lambda i: 204 + 6 * math.sin(i / 31) + 0.2 * rpm[i])),
            ("fivo-api", "cpu_pct"): ("percent", noisy("fivo-api", "cpu_pct", lambda i: min(96.0, 5 + 0.9 * rpm[i]))),
            ("fivo-api", "db_connections"): ("connections", noisy("fivo-api", "db_connections", lambda i: min(9.6, 2.4 + rpm[i] / 4.5), 0)),
            ("fivo-api", "restarts"): ("count", restarts["fivo-api"]),
            ("fivo-worker", "queue_depth"): ("tasks", [float(d) for d in depth]),
            ("fivo-worker", "tasks_completed"): ("tasks/min", completed),
            ("fivo-worker", "task_failure_rate"): ("ratio", fail_rate),
            ("fivo-worker", "task_duration_p95_ms"): ("ms", task_p95),
            ("fivo-worker", "memory_mb"): ("MB", noisy("fivo-worker", "memory_mb", lambda i: 238 + 5 * math.sin(i / 23) + 2 * completed[i])),
            ("fivo-worker", "cpu_pct"): ("percent", noisy("fivo-worker", "cpu_pct", lambda i: min(95.0, 6 + 3.2 * completed[i]))),
            ("fivo-worker", "restarts"): ("count", restarts["fivo-worker"]),
            ("neon-postgres", "cpu_pct"): ("percent", noisy("neon-postgres", "cpu_pct", lambda i: min(93.0, 5 + 0.55 * rpm[i]))),
            ("neon-postgres", "active_connections"): ("connections", noisy("neon-postgres", "active_connections", lambda i: min(12.4, 4.5 + rpm[i] / 4.5), 0)),
            ("neon-postgres", "slow_queries"): ("queries/min", [0.0] * n),
            ("upstash-redis", "commands_per_min"): ("commands/min", cmds),
            ("upstash-redis", "daily_commands_used"): ("commands", [float(u) for u in used]),
            ("upstash-redis", "daily_command_limit"): ("commands", [500000.0] * n),
            ("groq-llm", "client_latency_p95_ms"): ("ms", noisy("groq-llm", "client_latency_p95_ms", lambda i: 470 + 25 * math.sin(i / 17))),
            ("groq-llm", "client_error_rate"): ("ratio", [0.0] * n),
            ("voyage-embeddings", "client_latency_p95_ms"): ("ms", noisy("voyage-embeddings", "client_latency_p95_ms", lambda i: 185 + 10 * math.sin(i / 13))),
            ("voyage-embeddings", "client_error_rate"): ("ratio", [0.0] * n),
            ("anthropic-llm", "client_latency_p95_ms"): ("ms", noisy("anthropic-llm", "client_latency_p95_ms", lambda i: 3900 + 220 * math.sin(i / 19))),
            ("anthropic-llm", "client_error_rate"): ("ratio", [0.0] * n),
            ("r2-storage", "client_latency_p95_ms"): ("ms", noisy("r2-storage", "client_latency_p95_ms", lambda i: 140 + 8 * math.sin(i / 11))),
            ("r2-storage", "client_error_rate"): ("ratio", [0.0] * n),
        }
        for service, metric, fn in self._shapes:
            unit, values = series[(service, metric)]
            digits = 4 if unit == "ratio" else 1
            series[(service, metric)] = (unit, [round(max(0.0, fn(i - BEFORE, v)), digits) for i, v in enumerate(values)])
        self._series = series
        return [
            {"service": service, "metric": metric, "unit": unit, "points": [[self.ts(i - BEFORE), v] for i, v in enumerate(values)]}
            for (service, metric), (unit, values) in series.items()
        ]

    def value(self, service: str, metric: str, t: int) -> float:
        return self._series[(service, metric)][1][t + BEFORE]

    # ------------------------------------------------------------------ write

    def sources_config(self) -> dict[str, Any]:
        fast = {"rate_per_min": 0, "max_concurrency": 8, "backoff_s": 0.05, "timeout_s": 2}
        sources = {
            "logs": {"type": "sim_logs", "kind": "log_search", "description": "Application and platform logs for fivo-api, fivo-worker and managed dependencies, by service.",
                     "options": {"data_dir": "data"},
                     "policy": {**fast, "max_rows": 50, "log_search": {"required_scope": ["service"], "max_window_s": 14400, "raw_max_window_s": 900, "volume_key": "service"}}},
            "metrics": {"type": "sim_metrics", "description": "One-minute metrics for services and client-side metrics for dependencies.",
                        "options": {"data_dir": "data"}, "policy": {**fast, "max_rows": 240}},
            "records": {"type": "sim_records", "description": "Deploy history, configuration changes, service topology, trace spans and past incidents.",
                        "options": {"data_dir": "data", "collections": ["deploys", "config_changes", "topology", "traces", "incidents"]},
                        "policy": {**fast, "max_rows": 50}},
            "appdb": {"type": "sim_sql", "kind": "sql", "description": "Application database (read replica): users, transactions, receipts, jobs, voice_calls.",
                      "options": {"data_dir": "data", "now": self.ts(AFTER)},
                      "policy": {**fast, "max_rows": 50, "redact_fields": ["email"], "sql": {"tables": self.sql_tables}}},
            "code": {"type": "git", "description": "Source of fivo-api and fivo-worker, readable at any commit.",
                     "options": {"mirror_dir": ".mirrors", "fetch_interval_s": 86400,
                                 "repos": {name: {"url": f"repos/{name}", "branch": "main", "services": [name]} for name in self.repos}},
                     "policy": {"rate_per_min": 0, "max_concurrency": 4, "timeout_s": 20, "max_rows": 60}},
        }
        for name, faults in self.faults.items():
            sources[name]["options"]["faults"] = faults
        for name, override in self.policy_overrides.items():
            sources[name]["policy"].update(override)
        return {
            "sources": sources,
            "capabilities": {
                "logs": {"source": "logs", "dialect": "simple"},
                "metrics": {"source": "metrics"},
                "records": {"source": "records"},
                "db": {"source": "appdb", "columns": DB_COLUMNS},
                "code": {"source": "code"},
            },
        }

    def write(self, incident: dict[str, Any], answer: dict[str, Any]) -> None:
        self.simulate()
        data = self.out / "data"
        data.mkdir(parents=True, exist_ok=True)

        def jsonl(name: str, rows: list[dict[str, Any]]) -> None:
            (data / f"{name}.jsonl").write_text("".join(json.dumps(r, sort_keys=False) + "\n" for r in rows))

        jsonl("logs", self._log_lines())
        (data / "metrics.json").write_text(json.dumps({"series": self._metrics()}))
        jsonl("deploys", sorted(self.deploys, key=lambda d: d["ts"]))
        jsonl("config_changes", sorted(self.config_changes, key=lambda d: d["ts"]))
        jsonl("topology", TOPOLOGY)
        jsonl("traces", self._trace_rows())
        jsonl("incidents", sorted(self.incidents, key=lambda d: d["ts"]))

        db_path = data / "app.db"
        db_path.unlink(missing_ok=True)
        db = sqlite3.connect(db_path)
        db.executescript(DB_SCHEMA)
        for table, rows in self.db.items():
            if rows:
                cols = list(rows[0])
                db.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [[r[c] for c in cols] for r in rows])
        db.commit()
        db.execute("VACUUM")
        db.close()

        (self.out / "sources.yaml").write_text(yaml.safe_dump(self.sources_config(), sort_keys=False, width=200))
        (self.out / "incident.json").write_text(json.dumps(incident, indent=2) + "\n")
        (self.out / "answer.json").write_text(json.dumps(answer, indent=2) + "\n")
