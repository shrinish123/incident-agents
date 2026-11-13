"""Git connector: read-only access to one or more repositories.

Each repository is kept as a bare mirror on disk and refreshed with
`git fetch`. Nothing is ever checked out, committed or pushed.

Source options:
  mirror_dir        where bare mirrors live (default .data/mirrors)
  fetch_interval_s  minimum seconds between fetches of one repo (default 60)
  max_patch_bytes   cap on diff text returned per file (default 6000)
  repos:
    <name>: {url: <https url or local path>, branch: main}
Auth (optional, shared by the repos of this source; add another source for
repos that need a different credential):
  token     access token for HTTPS remotes
  username  defaults to x-access-token

The token reaches git through GIT_ASKPASS and the child process environment.
It is never placed in a URL, in argv or in the mirror's config.

Operations (params.op):
  resolve        {repo, ref?}                          -> commit sha
  log            {repo, ref?, since?, until?, path?}   -> commits
  changed_files  {repo, base, head}                    -> files changed between two commits
  diff           {repo, base, head, path?}             -> patches per file
  show           {repo, ref?, path, start?, end?}      -> file lines at a commit
  grep           {repo, ref?, pattern, path?, regex?}  -> matching lines at a commit
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from ..querytypes import ConnectorError, ConnectorTimeout, CostEstimate, RawResult
from ..secrets import AuthRejected, Secret
from .base import register_connector

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*(?:[~^][0-9]*)*$")
_AUTH_FAIL_RE = re.compile(
    r"authentication failed|could not read username|could not read password|"
    r"invalid username or password|http 40[13]|returned error: 40[13]|permission denied \(publickey\)",
    re.I,
)
_SEP = "\x1f"

_ASKPASS = """#!/bin/sh
case "$1" in
  Username*) printf '%s' "$IA_GIT_USERNAME" ;;
  *) printf '%s' "$IA_GIT_TOKEN" ;;
esac
"""


class GitConnector:
    type = "git"

    def __init__(self, options: dict[str, Any]) -> None:
        self._repos: dict[str, dict[str, Any]] = options.get("repos") or {}
        if not self._repos:
            raise ValueError("git source needs options.repos")
        self._mirror_dir = Path(options.get("mirror_dir", ".data/mirrors")).resolve()
        self._fetch_interval_s = float(options.get("fetch_interval_s", 60))
        self._max_patch_bytes = int(options.get("max_patch_bytes", 6000))
        self._last_fetch: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._askpass: str | None = None

    async def estimate(self, params: dict[str, Any], creds: dict[str, Any]) -> CostEstimate | None:
        return None

    # ------------------------------------------------------------------ plumbing

    def _askpass_path(self) -> str:
        if self._askpass is None or not os.path.exists(self._askpass):
            fd, path = tempfile.mkstemp(prefix="ia-askpass-", suffix=".sh")
            with os.fdopen(fd, "w") as fh:
                fh.write(_ASKPASS)
            os.chmod(path, stat.S_IRWXU)
            self._askpass = path
        return self._askpass

    def _env(self, creds: dict[str, Any]) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
        token = creds.get("token")
        if token is not None:
            env["GIT_ASKPASS"] = self._askpass_path()
            env["IA_GIT_TOKEN"] = token.reveal() if isinstance(token, Secret) else str(token)
            user = creds.get("username", "x-access-token")
            env["IA_GIT_USERNAME"] = user.reveal() if isinstance(user, Secret) else str(user)
        return env

    async def _git(self, args: list[str], env: dict[str, str], timeout_s: float, ok_codes: tuple[int, ...] = (0,)) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            await proc.wait()
            raise ConnectorTimeout(f"git {args[0] if not args[0].startswith('-') else args[2]} timed out") from None
        if proc.returncode not in ok_codes:
            message = err.decode(errors="replace").strip()
            if _AUTH_FAIL_RE.search(message):
                raise AuthRejected
            last = message.splitlines()[-1] if message else f"exit {proc.returncode}"
            raise ConnectorError(f"git failed: {last[:200]}")
        return out.decode(errors="replace")

    def _repo(self, params: dict[str, Any]) -> tuple[str, dict[str, Any], Path]:
        name = params.get("repo")
        if name not in self._repos:
            raise ConnectorError(f"Unknown repo {name!r}. Known repos: {', '.join(sorted(self._repos))}")
        return name, self._repos[name], self._mirror_dir / f"{name}.git"

    async def _sync(self, name: str, cfg: dict[str, Any], mirror: Path, env: dict[str, str], timeout_s: float) -> None:
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            if not mirror.exists():
                mirror.parent.mkdir(parents=True, exist_ok=True)
                await self._git(["clone", "--quiet", "--mirror", cfg["url"], str(mirror)], env, timeout_s)
                self._last_fetch[name] = time.monotonic()
            elif time.monotonic() - self._last_fetch.get(name, 0.0) >= self._fetch_interval_s:
                await self._git(["--git-dir", str(mirror), "fetch", "--quiet", "--prune", "origin"], env, timeout_s)
                self._last_fetch[name] = time.monotonic()

    @staticmethod
    def _ref(value: Any, default: str) -> str:
        ref = str(value or default)
        if not _REF_RE.match(ref) or ".." in ref:
            raise ConnectorError(f"Invalid git ref {ref!r}")
        return ref

    @staticmethod
    def _path(value: Any) -> str:
        path = str(value)
        if not path or path.startswith(("-", "/")) or ".." in path.split("/") or "\x00" in path:
            raise ConnectorError(f"Invalid path {path!r}")
        return path

    # ------------------------------------------------------------------ execute

    async def execute(
        self, params: dict[str, Any], creds: dict[str, Any], *, timeout_s: float, max_rows: int
    ) -> RawResult:
        op = params.get("op")
        handler = getattr(self, f"_op_{op}", None)
        if handler is None:
            raise ConnectorError("params.op must be one of: resolve, log, changed_files, diff, show, grep")
        name, cfg, mirror = self._repo(params)
        env = self._env(creds)
        await self._sync(name, cfg, mirror, env, timeout_s)
        git = ["--git-dir", str(mirror)]
        default_ref = cfg.get("branch", "HEAD")
        rows = await handler(params, git, env, timeout_s, max_rows, default_ref)
        return RawResult(rows=rows[:max_rows], truncated=len(rows) > max_rows, meta={"repo": name})

    async def _op_resolve(self, p, git, env, timeout_s, max_rows, default_ref):
        ref = self._ref(p.get("ref"), default_ref)
        sha = (await self._git([*git, "rev-parse", "--verify", f"{ref}^{{commit}}"], env, timeout_s)).strip()
        return [{"repo": p["repo"], "ref": ref, "sha": sha}]

    async def _op_log(self, p, git, env, timeout_s, max_rows, default_ref):
        ref = self._ref(p.get("ref"), default_ref)
        fmt = _SEP.join(["%H", "%an", "%aI", "%s"])
        args = [*git, "log", f"--max-count={max_rows + 1}", f"--format={fmt}"]
        for key in ("since", "until"):
            if p.get(key):
                if not re.match(r"^[0-9T:\-+.Z ]+$", str(p[key])):
                    raise ConnectorError(f"Invalid {key} timestamp")
                args.append(f"--{key}={p[key]}")
        args.append(ref)
        if p.get("path"):
            args += ["--", self._path(p["path"])]
        out = await self._git(args, env, timeout_s)
        rows = []
        for line in out.splitlines():
            parts = line.split(_SEP)
            if len(parts) == 4:
                rows.append({"sha": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]})
        return rows

    async def _op_changed_files(self, p, git, env, timeout_s, max_rows, default_ref):
        base, head = self._ref(p.get("base"), ""), self._ref(p.get("head"), default_ref)
        out = await self._git([*git, "diff", "--name-status", "--no-renames", base, head], env, timeout_s)
        rows = []
        for line in out.splitlines():
            status, _, path = line.partition("\t")
            rows.append({"status": status, "path": path})
        return rows

    async def _op_diff(self, p, git, env, timeout_s, max_rows, default_ref):
        base, head = self._ref(p.get("base"), ""), self._ref(p.get("head"), default_ref)
        args = [*git, "diff", "--no-color", "--no-renames", "--unified=3", base, head]
        if p.get("path"):
            args += ["--", self._path(p["path"])]
        out = await self._git(args, env, timeout_s)
        rows = []
        for chunk in re.split(r"(?m)^(?=diff --git )", out):
            if not chunk.strip():
                continue
            m = re.match(r"diff --git a/(.+?) b/(.+)", chunk)
            patch = chunk if len(chunk) <= self._max_patch_bytes else chunk[: self._max_patch_bytes]
            row = {"path": m[2] if m else "?", "base": base, "head": head, "patch": patch}
            if len(chunk) > self._max_patch_bytes:
                row["patch_truncated"] = True
            rows.append(row)
        return rows

    async def _op_show(self, p, git, env, timeout_s, max_rows, default_ref):
        ref, path = self._ref(p.get("ref"), default_ref), self._path(p.get("path", ""))
        out = await self._git([*git, "show", f"{ref}:{path}"], env, timeout_s)
        lines = out.splitlines()
        start = max(1, int(p.get("start") or 1))
        end = min(len(lines), int(p.get("end") or start + 199), start + 399)
        body = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))
        return [{"path": path, "ref": ref, "start": start, "end": end, "total_lines": len(lines), "content": body}]

    async def _op_grep(self, p, git, env, timeout_s, max_rows, default_ref):
        ref = self._ref(p.get("ref"), default_ref)
        pattern = str(p.get("pattern") or "")
        if not pattern or len(pattern) > 200:
            raise ConnectorError("params.pattern is required (max 200 characters)")
        args = [*git, "grep", "-n", "-I", "--no-color", "-E" if p.get("regex") else "-F", "-e", pattern, ref]
        if p.get("path"):
            args += ["--", self._path(p["path"])]
        out = await self._git(args, env, timeout_s, ok_codes=(0, 1))  # 1 = no matches
        rows = []
        for line in out.splitlines():
            parts = line.split(":", 3)  # ref:path:line:text
            if len(parts) == 4 and parts[2].isdigit():
                rows.append({"path": parts[1], "line": int(parts[2]), "text": parts[3][:300]})
        return rows


@register_connector("git")
def _build(options: dict[str, Any]) -> GitConnector:
    return GitConnector(options)
