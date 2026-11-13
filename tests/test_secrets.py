import json
import pickle

import httpx
import pytest

from incident_agents.secrets import (
    REDACTED,
    AuthRejected,
    CredentialBroker,
    EnvSecretStore,
    Secret,
    SecretBackendError,
    SecretNotFound,
    SecretRef,
    SecretScrubber,
    SourceAuthError,
    VaultSecretStore,
)

AUTH = {"access_id": "secret://sumo/prod#access_id", "access_key": "secret://sumo/prod#access_key", "region": "us2"}


def make_broker(env, **kw):
    return CredentialBroker(store=EnvSecretStore(env), **kw)


# --- Secret type -----------------------------------------------------------


def test_secret_never_prints_its_value():
    s = Secret("hunter2-very-secret")
    assert "hunter2" not in repr(s)
    assert "hunter2" not in str(s)
    assert "hunter2" not in f"{s}"
    assert "hunter2" not in f"{[s]} {{'k': {s!r}}}"
    assert s.reveal() == "hunter2-very-secret"


def test_secret_cannot_be_pickled_or_json_dumped():
    s = Secret("hunter2-very-secret")
    with pytest.raises(TypeError):
        pickle.dumps(s)
    with pytest.raises(TypeError):
        json.dumps({"k": s})


def test_ref_parsing():
    ref = SecretRef.parse("secret://sumo/prod#access_key")
    assert (ref.path, ref.field) == ("sumo/prod", "access_key")
    assert EnvSecretStore.var_name(ref) == "SUMO_PROD_ACCESS_KEY"
    for bad in ["sumo/prod", "secret://sumo/prod", "secret://#x", "secret://a b#c"]:
        with pytest.raises(ValueError):
            SecretRef.parse(bad)


# --- Broker ----------------------------------------------------------------


async def test_resolve_auth_resolves_refs_and_passes_plain_values():
    broker = make_broker({"SUMO_PROD_ACCESS_ID": "id-123456", "SUMO_PROD_ACCESS_KEY": "key-abcdef"})
    creds = await broker.resolve_auth(AUTH, source="sumo-prod", investigation_id="inv-1")
    assert creds["access_key"].reveal() == "key-abcdef"
    assert creds["region"] == "us2"


async def test_missing_secret_names_the_env_var_to_set():
    broker = make_broker({})
    with pytest.raises(SecretNotFound, match="SUMO_PROD_ACCESS_ID"):
        await broker.resolve_auth(AUTH, source="sumo-prod")


async def test_cache_respects_ttl():
    env = {"SUMO_PROD_ACCESS_KEY": "key-v1-aaaa"}
    now = [0.0]
    broker = make_broker(env, ttl_s=300, clock=lambda: now[0])
    ref = "secret://sumo/prod#access_key"
    assert (await broker.resolve(ref, source="s")).reveal() == "key-v1-aaaa"
    env["SUMO_PROD_ACCESS_KEY"] = "key-v2-bbbb"
    now[0] = 299
    assert (await broker.resolve(ref, source="s")).reveal() == "key-v1-aaaa"
    now[0] = 301
    assert (await broker.resolve(ref, source="s")).reveal() == "key-v2-bbbb"


async def test_rotation_mid_investigation_is_picked_up_without_restart():
    env = {"SUMO_PROD_ACCESS_ID": "id-123456", "SUMO_PROD_ACCESS_KEY": "key-old-1111"}
    broker = make_broker(env)
    seen = []

    async def call(creds):
        key = creds["access_key"].reveal()
        seen.append(key)
        if key != "key-new-2222":
            raise AuthRejected
        return "rows"

    # The old key is cached, then the key is rotated at the source.
    await broker.resolve_auth(AUTH, source="sumo-prod")
    env["SUMO_PROD_ACCESS_KEY"] = "key-new-2222"

    assert await broker.call_with_auth_retry(AUTH, call, source="sumo-prod") == "rows"
    assert seen == ["key-old-1111", "key-new-2222"]


async def test_auth_still_failing_after_refresh_raises_and_calls_exactly_twice():
    broker = make_broker({"SUMO_PROD_ACCESS_ID": "id-123456", "SUMO_PROD_ACCESS_KEY": "key-revoked"})
    calls = 0

    async def call(creds):
        nonlocal calls
        calls += 1
        raise AuthRejected

    with pytest.raises(SourceAuthError) as exc:
        await broker.call_with_auth_retry(AUTH, call, source="sumo-prod")
    assert calls == 2
    assert exc.value.source == "sumo-prod"
    assert "key-revoked" not in str(exc.value)


async def test_access_log_records_refs_and_never_values():
    broker = make_broker({"SUMO_PROD_ACCESS_ID": "id-123456", "SUMO_PROD_ACCESS_KEY": "key-abcdef"})
    await broker.resolve_auth(AUTH, source="sumo-prod", investigation_id="inv-7")
    await broker.resolve_auth(AUTH, source="sumo-prod", investigation_id="inv-7")
    log = broker.access_log
    assert [a.cache_hit for a in log] == [False, False, True, True]
    assert {a.ref for a in log} == {"secret://sumo/prod#access_id", "secret://sumo/prod#access_key"}
    assert all(a.investigation_id == "inv-7" for a in log)
    assert "key-abcdef" not in repr(log) and "id-123456" not in repr(log)


# --- Scrubber --------------------------------------------------------------


async def test_scrubber_removes_every_secret_the_broker_handed_out():
    broker = make_broker({"SUMO_PROD_ACCESS_ID": "id-123456", "SUMO_PROD_ACCESS_KEY": "zq81-odd-shape"})
    await broker.resolve_auth(AUTH, source="sumo-prod")
    out = broker.scrubber.scrub("upstream said: bad key zq81-odd-shape for id-123456")
    assert "zq81-odd-shape" not in out and "id-123456" not in out
    assert out.count(REDACTED) == 2


@pytest.mark.parametrize(
    "leak,secret",
    [
        ('could not connect: "postgresql://ro_user:S3cr3tP4ss@db.internal:5432/app"', "S3cr3tP4ss"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345", "abcdefghijklmnopqrstuvwxyz012345"),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk", "eyJhbGci"),
        ("aws key AKIAIOSFODNN7EXAMPLE leaked", "AKIAIOSFODNN7EXAMPLE"),
        ("clone failed for https://x-access-token:ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/o/r", "ghp_"),
        ("config dump: api_key=live_9f8e7d6c5b4a password: 'pa55word!'", "live_9f8e7d6c5b4a"),
        ("vault token hvs.CAESIJlWh3kq9Zc0aBcDeFgHiJkLmNoP", "hvs.CAESI"),
    ],
)
def test_scrubber_patterns_catch_unregistered_leaks(leak, secret):
    out = SecretScrubber().scrub(leak)
    assert secret not in out
    assert REDACTED in out


def test_scrubber_keeps_the_useful_parts_of_a_dsn():
    out = SecretScrubber().scrub("postgresql://ro_user:S3cr3tP4ss@db.internal:5432/app")
    assert out == f"postgresql://ro_user:{REDACTED}@db.internal:5432/app"


def test_scrubber_leaves_ordinary_log_lines_alone():
    line = "2026-11-10T08:31:02Z ERROR POST /transactions 500 IntegrityError duplicate key idempotency_key trace=4bf92f3577b34da6"
    assert SecretScrubber().scrub(line) == line


def test_scrub_obj_walks_nested_structures():
    s = SecretScrubber()
    s.register("zq81-odd-shape")
    out = s.scrub_obj({"rows": [{"msg": "key zq81-odd-shape"}], "n": 3, "zq81-odd-shape": ("a", "zq81-odd-shape")})
    assert "zq81-odd-shape" not in json.dumps(out)
    assert out["n"] == 3


# --- Vault backend ---------------------------------------------------------


def vault_store(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return VaultSecretStore("http://vault:8200", Secret("hvs.roottokenroottokenroot"), client=client)


async def test_vault_kv2_read_returns_field_and_version():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/secret/data/sumo/prod"
        assert req.headers["X-Vault-Token"] == "hvs.roottokenroottokenroot"
        return httpx.Response(200, json={"data": {"data": {"access_key": "key-abcdef"}, "metadata": {"version": 4}}})

    secret = await vault_store(handler).get(SecretRef.parse("secret://sumo/prod#access_key"))
    assert secret.reveal() == "key-abcdef" and secret.version == "4"


async def test_vault_dynamic_database_credentials():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/database/creds/investigator-ro"
        return httpx.Response(200, json={"lease_id": "database/creds/investigator-ro/abc", "data": {"username": "v-ro-1", "password": "tmp-pass-xyz"}})

    secret = await vault_store(handler).get(SecretRef.parse("secret://database/creds/investigator-ro#password"))
    assert secret.reveal() == "tmp-pass-xyz" and secret.version.endswith("/abc")


async def test_vault_errors_are_typed_and_do_not_leak_the_token():
    store = vault_store(lambda req: httpx.Response(404, json={"errors": []}))
    with pytest.raises(SecretNotFound):
        await store.get(SecretRef.parse("secret://nope/x#y"))

    store = vault_store(lambda req: httpx.Response(403, json={"errors": ["permission denied"]}))
    with pytest.raises(SecretBackendError, match="403") as exc:
        await store.get(SecretRef.parse("secret://sumo/prod#access_key"))
    assert "hvs." not in str(exc.value)

    def boom(req):
        raise httpx.ConnectError("connection refused", request=req)

    with pytest.raises(SecretBackendError, match="unreachable") as exc:
        await vault_store(boom).get(SecretRef.parse("secret://sumo/prod#access_key"))
    assert "hvs." not in str(exc.value)

    store = vault_store(lambda req: httpx.Response(200, json={"data": {"data": {"other": "x"}, "metadata": {"version": 1}}}))
    with pytest.raises(SecretNotFound, match="access_key"):
        await store.get(SecretRef.parse("secret://sumo/prod#access_key"))
