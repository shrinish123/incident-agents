"""Git connector tests against real repositories created in a temp directory."""

import subprocess

import pytest

from incident_agents.connectors.git import GitConnector
from incident_agents.gateway import ConnectorError, QueryGateway, QueryRequest, Status, parse_sources
from incident_agents.secrets import AuthRejected, CredentialBroker, EnvSecretStore, Secret


def run(cwd, *args):
    env = {
        "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd),
    }
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path, files_v1, files_v2):
    path.mkdir(parents=True)
    run(path, "init", "-q", "-b", "main")
    for name, body in files_v1.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(body)
    run(path, "add", "-A")
    run(path, "commit", "-q", "-m", "initial version")
    sha1 = run(path, "rev-parse", "HEAD")
    for name, body in files_v2.items():
        (path / name).write_text(body)
    run(path, "add", "-A")
    run(path, "commit", "-q", "-m", "drop device_id from idempotency key")
    return sha1, run(path, "rev-parse", "HEAD")


V1 = "def idempotency_key(user_id, device_id, ts):\n    return f'{user_id}:{device_id}:{ts}'\n"
V2 = "def idempotency_key(user_id, device_id, ts):\n    return f'{user_id}:{ts}'\n"


@pytest.fixture
def repos(tmp_path):
    api = make_repo(tmp_path / "src" / "api", {"app/keys.py": V1, "README.md": "api\n"}, {"app/keys.py": V2})
    worker = make_repo(tmp_path / "src" / "worker", {"jobs.py": "def run():\n    pass\n"}, {"jobs.py": "def run():\n    return 1\n"})
    options = {
        "mirror_dir": str(tmp_path / "mirrors"),
        "fetch_interval_s": 0,
        "repos": {
            "api": {"url": str(tmp_path / "src" / "api"), "branch": "main"},
            "worker": {"url": str(tmp_path / "src" / "worker"), "branch": "main"},
        },
    }
    return options, api, worker, tmp_path


async def call(conn, **params):
    return (await conn.execute(params, {}, timeout_s=20, max_rows=50)).rows


async def test_multiple_repos_resolve_log_and_diff(repos):
    options, (api1, api2), (_, worker2), _ = repos
    conn = GitConnector(options)

    assert (await call(conn, op="resolve", repo="api"))[0]["sha"] == api2
    assert (await call(conn, op="resolve", repo="worker"))[0]["sha"] == worker2

    log = await call(conn, op="log", repo="api")
    assert [c["subject"] for c in log] == ["drop device_id from idempotency key", "initial version"]

    assert await call(conn, op="changed_files", repo="api", base=api1, head=api2) == [{"status": "M", "path": "app/keys.py"}]

    diff = await call(conn, op="diff", repo="api", base=api1, head=api2)
    assert diff[0]["path"] == "app/keys.py"
    assert "-    return f'{user_id}:{device_id}:{ts}'" in diff[0]["patch"]
    assert "+    return f'{user_id}:{ts}'" in diff[0]["patch"]


async def test_code_is_read_at_the_commit_that_was_deployed_not_at_head(repos):
    options, (api1, api2), _, _ = repos
    conn = GitConnector(options)
    old = await call(conn, op="show", repo="api", ref=api1, path="app/keys.py")
    new = await call(conn, op="show", repo="api", ref=api2, path="app/keys.py")
    assert "device_id}:{ts}" in old[0]["content"] and "device_id}:{ts}" not in new[0]["content"]
    assert new[0]["content"].startswith("1: def idempotency_key") and new[0]["total_lines"] == 2

    hits = await call(conn, op="grep", repo="api", ref=api2, pattern="idempotency_key")
    assert hits == [{"path": "app/keys.py", "line": 1, "text": "def idempotency_key(user_id, device_id, ts):"}]
    assert await call(conn, op="grep", repo="api", pattern="no_such_symbol") == []


async def test_new_commits_are_picked_up_by_fetch(repos):
    options, _, _, tmp = repos
    conn = GitConnector(options)
    before = (await call(conn, op="resolve", repo="worker"))[0]["sha"]
    (tmp / "src" / "worker" / "jobs.py").write_text("def run():\n    return 2\n")
    run(tmp / "src" / "worker", "commit", "-q", "-am", "hotfix")
    after = (await call(conn, op="resolve", repo="worker"))[0]["sha"]
    assert after != before
    assert (await call(conn, op="log", repo="worker"))[0]["subject"] == "hotfix"


@pytest.mark.parametrize(
    "params",
    [
        {"op": "resolve", "repo": "nope"},
        {"op": "push", "repo": "api"},
        {"op": "resolve", "repo": "api", "ref": "--upload-pack=touch /tmp/x"},
        {"op": "resolve", "repo": "api", "ref": "main..HEAD"},
        {"op": "show", "repo": "api", "path": "../../etc/passwd"},
        {"op": "show", "repo": "api", "path": "-x"},
        {"op": "show", "repo": "api", "path": "missing.py"},
        {"op": "log", "repo": "api", "since": "$(reboot)"},
        {"op": "grep", "repo": "api", "pattern": ""},
    ],
)
async def test_bad_input_is_refused(repos, params):
    conn = GitConnector(repos[0])
    with pytest.raises(ConnectorError):
        await conn.execute(params, {}, timeout_s=20, max_rows=50)


async def test_token_goes_through_askpass_and_never_into_argv_or_config(repos, monkeypatch):
    options, _, _, tmp = repos
    conn = GitConnector(options)
    seen = {}
    real = conn._git

    async def spy(args, env, timeout_s, ok_codes=(0,)):
        seen.setdefault("argv", []).extend(args)
        seen["env"] = env
        return await real(args, env, timeout_s, ok_codes)

    monkeypatch.setattr(conn, "_git", spy)
    await conn.execute({"op": "resolve", "repo": "api"}, {"token": Secret("ghp_tok3nTOK3NtokenTOKEN0000000000")}, timeout_s=20, max_rows=5)
    assert "ghp_tok3n" not in " ".join(seen["argv"])
    assert seen["env"]["IA_GIT_TOKEN"].startswith("ghp_") and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    askpass = subprocess.run([seen["env"]["GIT_ASKPASS"], "Password for 'https://github.com':"], env=seen["env"], capture_output=True, text=True)
    assert askpass.stdout == "ghp_tok3nTOK3NtokenTOKEN0000000000"
    config = (tmp / "mirrors" / "api.git" / "config").read_text()
    assert "ghp_" not in config


async def test_auth_failure_from_the_remote_is_recognised(repos, monkeypatch):
    conn = GitConnector(repos[0])

    class FakeProc:
        returncode = 128

        async def communicate(self):
            return b"", b"remote: Invalid username or password.\nfatal: Authentication failed for 'https://github.com/o/r.git/'"

    async def fake_exec(*a, **k):
        return FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(AuthRejected):
        await conn.execute({"op": "resolve", "repo": "api"}, {}, timeout_s=5, max_rows=5)


async def test_through_the_gateway(repos):
    options, (api1, api2), _, _ = repos
    sources = parse_sources({"sources": {"code": {"type": "git", "options": options, "policy": {"rate_per_min": 0, "max_rows": 30}}}})
    gw = QueryGateway.from_config(sources, CredentialBroker(store=EnvSecretStore({})))
    ok = await gw.execute(QueryRequest("code", {"op": "diff", "repo": "api", "base": api1, "head": api2}))
    assert ok.status is Status.OK and ok.rows[0]["path"] == "app/keys.py" and ok.meta == {"repo": "api"}
    bad = await gw.execute(QueryRequest("code", {"op": "show", "repo": "api", "path": "missing.py"}))
    assert bad.status is Status.ERROR and "git failed" in bad.error
