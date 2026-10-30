"""The Rakazo mobile bridge: /rpc/* and /api/auth/* for the Rakazo iPhone app, mapped onto the Herdr assistant.

A live ControlPlaneServer is driven over HTTP with the exact request shapes the upstream app sends (POST {"json": ...}
with ``origin: rakazo://``, ``Authorization: Bearer``), and the response fields the app reads are asserted by name."""
import http.client
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane import rakazo_bridge as rb
from herdr_engineering.control_plane import rakazo_events as ev
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.repo import ControlPlaneRepo

PW_A, PW_B = "correct horse battery A", "correct horse battery B"
FAST = dict(n=2 ** 14)                       # the smallest N the parser accepts: keeps the suite quick
HASH_A, HASH_B = rb.hash_password(PW_A, **FAST), rb.hash_password(PW_B, **FAST)
ACCOUNTS = [{"email": "Alice@Example.com", "password_scrypt": HASH_A, "role": "operator"},
            {"email": "bob@example.com", "password_scrypt": HASH_B, "role": "operator"}]


def bridge_section(**over):
    return {"enabled": True, "accounts": ACCOUNTS, **over}


# --------------------------------------------------------------------------------------------- harness
class Api:
    def __init__(self, port):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"

    def raw(self, method, path, body=None, headers=None):
        h = {"origin": "rakazo://", "content-type": "application/json", **(headers or {})}
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def rpc(self, proc, inp=None, token=None, **headers):
        if token:
            headers["authorization"] = f"Bearer {token}"
        status, body, hdrs = self.raw("POST", f"/rpc/{proc}", {"json": inp if inp is not None else {}}, headers)
        return status, (json.loads(body) if body else None), hdrs

    def ok(self, proc, inp=None, token=None):
        status, body, _ = self.rpc(proc, inp, token)
        assert status == 200, (proc, status, body)
        return body["json"]

    def sign_in(self, email="alice@example.com", password=PW_A):
        status, body, hdrs = self.raw("POST", "/api/auth/sign-in/email", {"email": email, "password": password})
        return status, json.loads(body), {k.lower(): v for k, v in hdrs.items()}

    def login(self, email="alice@example.com", password=PW_A):
        status, body, hdrs = self.sign_in(email, password)
        assert status == 200, body
        return hdrs["set-auth-token"]

    def stream(self, token, bot_id, cursor, timeout=5):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("POST", "/rpc/threads/subscribe", json.dumps({"json": {"botId": bot_id, "cursor": cursor}}),
                     {"content-type": "application/json", "accept": "text/event-stream", "origin": "rakazo://",
                      "authorization": f"Bearer {token}"})
        return conn, conn.getresponse()


def read_frames(resp, count, stop=lambda frames: False):
    """Parse ``data: {...}\\n\\n`` frames the way ``subscribeThread`` does; stops after ``count`` or ``stop(frames)``."""
    frames, buf = [], b""
    while len(frames) < count and not stop(frames):
        chunk = resp.fp.readline()
        if not chunk:
            break
        buf += chunk
        if buf.endswith(b"\n\n"):
            data = "".join(line[5:].strip() for line in buf.decode().split("\n") if line.startswith("data:"))
            frames.append(json.loads(data)["json"])
            buf = b""
    return frames


@pytest.fixture()
def make_server():
    started = []

    def make(bridge=None, assistant=None, dsn=None, **kw):
        svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True, **(assistant or {})}}))
        svr = ControlPlaneServer(ControlPlaneRepo(), assistant_service=svc, rakazo_bridge=bridge, **kw)
        threading.Thread(target=svr.serve, daemon=True).start()
        svr.wait_until_ready()
        started.append(svr)
        return svr, Api(svr.port)

    yield make
    for svr in started:
        svr.shutdown()


@pytest.fixture()
def srv(make_server):
    svr, api = make_server(bridge_section())
    svr.rakazo.stream_options = {"poll_seconds": 0.02, "heartbeat_seconds": 0.3, "max_seconds": 3.0}
    return svr, api


@pytest.fixture()
def alice(srv):
    return srv[1].login()


def new_bot(api, token, name="Herdr"):
    return api.ok("bots/create", {"name": name, "description": "", "instructions": "be brief", "notifyOnFinish": True,
                                  "computerMode": "team"}, token)


def wait_idle(api, token, bot_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = api.ok("threads/get", {"botId": bot_id}, token)
        if not snap["activeRuns"]:
            return snap
        time.sleep(0.02)
    raise AssertionError("the run never finished")


def send(api, token, bot_id, text):
    out = api.ok("threads/send", {"botId": bot_id, "text": text, "clientNonce": "n1"}, token)
    return out, wait_idle(api, token, bot_id)


# --------------------------------------------------------------------------------------------- disabled / config
@pytest.mark.parametrize("section", [None, {}, {"enabled": False, "accounts": ACCOUNTS}, {"enabled": "yes"}])
def test_disabled_means_404_everywhere(make_server, section):
    _, api = make_server(section)
    for method, path in [("POST", "/rpc/health"), ("POST", "/rpc/me"), ("GET", "/api/auth/capabilities"),
                         ("POST", "/api/auth/sign-in/email"), ("POST", "/api/auth/sign-out")]:
        status, _, _ = api.raw(method, path, {"json": {}})
        assert status == 404, (method, path)


@pytest.mark.parametrize("section,needle", [
    ({"enabled": True}, "accounts"),
    ({"enabled": True, "accounts": []}, "accounts"),
    ({"enabled": True, "accounts": [{"email": "a@b.c"}]}, "scrypt"),
    ({"enabled": True, "accounts": [{"email": "a@b.c", "password_scrypt": "plaintext"}]}, "scrypt"),
    ({"enabled": True, "accounts": [{"email": "a@b.c", "password_scrypt": "scrypt$1$1$1$AA==$AA=="}]}, "scrypt"),
    ({"enabled": True, "accounts": [{"email": "nope", "password_scrypt": HASH_A}]}, "email"),
    ({"enabled": True, "accounts": [{"email": "a@b.c", "password_scrypt": HASH_A, "role": "family"}]}, "family"),
    ({"enabled": True, "accounts": [{"email": "a@b.c", "password_scrypt": HASH_A, "role": "admin"}]}, "operator"),
    ({"enabled": True, "accounts": [{"email": "a@b.c", "password_scrypt": HASH_A},
                                    {"email": "A@B.c", "password_scrypt": HASH_B}]}, "repeats"),
    ({"enabled": True, "operator_email": "a@b.c", "password_scrypt": HASH_A}, "unknown keys"),
    ({"enabled": True, "accounts": ACCOUNTS, "token_ttl_days": 0}, "token_ttl_days"),
    ({"enabled": True, "accounts": ACCOUNTS, "max_tokens": "5"}, "max_tokens"),
])
def test_enabled_with_a_missing_or_malformed_section_fails_closed_at_startup(section, needle):
    with pytest.raises(ValueError, match=needle):
        ControlPlaneServer(ControlPlaneRepo(), rakazo_bridge=section)
    with pytest.raises(ValueError) as err:
        rb.rakazo_bridge_config(section)
    assert HASH_A not in str(err.value) and "plaintext" not in str(err.value)


def test_config_normalises_accounts_and_derives_stable_distinct_principals():
    cfg = rb.rakazo_bridge_config(bridge_section())
    a, b = cfg["accounts"]
    assert (cfg["token_ttl_days"], cfg["max_tokens"]) == (30, 5)
    assert a.email == "alice@example.com" and a.principal_id.startswith("rakazo:") and a.principal_id != b.principal_id
    again = rb.rakazo_bridge_config(bridge_section(accounts=[{**ACCOUNTS[0], "email": " ALICE@example.com "}]))
    assert again["accounts"][0].principal_id == a.principal_id          # derived from the normalised email only


def test_hash_password_format_and_verification():
    h = rb.hash_password("pässword-1234567", **FAST)
    params = rb.parse_scrypt(h, "x")
    assert h.startswith("scrypt$16384$8$1$") and len(params.salt) == 16 and len(params.digest) == 32
    assert rb._derive("pässword-1234567", params) == params.digest and rb._derive("other", params) != params.digest
    assert rb.hash_password("pässword-1234567", **FAST) != h            # a fresh salt every time
    with pytest.raises(ValueError):
        rb.hash_password("")


# --------------------------------------------------------------------------------------------- handshake / capabilities
def test_connect_probe_matches_what_the_app_requires(srv):
    _, api = srv
    status, body, _ = api.rpc("health")                  # no token: the probe runs before sign-in
    assert status == 200 and body["json"]["ok"] is True and isinstance(body["json"]["version"], str)
    assert api.raw("GET", "/rpc/health")[0] == 404         # only POST


def test_capabilities_advertise_email_and_password_only(srv):
    _, api = srv
    status, body, hdrs = api.raw("GET", "/api/auth/capabilities")
    caps = json.loads(body)
    assert status == 200 and hdrs["Cache-Control"] == "no-store"
    assert caps == {"sso": None, "passwordAuth": True, "passwordReset": False, "resetUrl": None, "emailCode": False,
                    "billing": False, "passkeyRpId": None, "nativePasskeys": {"ios": False, "android": False},
                    "crispWebsiteId": None}
    for path in ("sign-up/email", "email-otp/send-verification-otp", "request-password-reset", "sign-in/email-otp"):
        assert api.raw("POST", f"/api/auth/{path}", {"email": "alice@example.com"})[0] == 404


# --------------------------------------------------------------------------------------------- sign-in
def test_sign_in_returns_the_token_in_the_header_and_body(srv):
    _, api = srv
    status, body, hdrs = api.sign_in("ALICE@example.com ", PW_A)             # case and space insensitive
    assert status == 200
    token = hdrs["set-auth-token"]
    assert body["token"] == token and len(token) >= 43 and body["user"]["email"] == "alice@example.com"
    assert hdrs["cache-control"] == "no-store"
    assert api.ok("me", token=token)["email"] == "alice@example.com"


def test_failures_are_one_generic_answer_for_wrong_email_and_wrong_password(srv):
    _, api = srv
    wrong_pw = api.sign_in("alice@example.com", "nope")
    wrong_email = api.sign_in("mallory@example.com", PW_A)
    for status, body, hdrs in (wrong_pw, wrong_email):
        assert status == 401 and "set-auth-token" not in hdrs
    assert wrong_pw[1] == wrong_email[1] == {"message": "Invalid credentials", "code": "INVALID_EMAIL_OR_PASSWORD"}


def test_the_kdf_runs_for_every_attempt_that_is_not_locked_out(srv, monkeypatch):
    svr, api = srv
    calls = []
    real = rb._derive
    monkeypatch.setattr(rb, "_derive", lambda pw, params: calls.append(1) or real(pw, params))
    api.sign_in("alice@example.com", "wrong password")
    api.raw("POST", "/api/auth/sign-in/email", b"not json")
    api.raw("POST", "/api/auth/sign-in/email", {"email": 5, "password": ["x"]})
    api.raw("POST", "/api/auth/sign-in/email", {"email": "alice@example.com", "password": "x" * 5000})
    assert len(calls) == 3
    api.sign_in("nobody@example.com", PW_A)                  # a wrong email costs the same KDF as a wrong password
    assert len(calls) == 4
    status, body, _ = api.raw("POST", "/api/auth/sign-in/email", b"not json")       # not an attempt: no KDF, no strike
    assert status == 400 and body and len(calls) == 4


def test_repeated_failures_lock_the_source_out_before_any_password_work(srv, monkeypatch):
    svr, api = srv
    for _ in range(5):
        assert api.sign_in("alice@example.com", "wrong")[0] == 401
    calls = []
    monkeypatch.setattr(rb, "_derive", lambda *a: calls.append(1) or b"")
    status, body, hdrs = api.sign_in("alice@example.com", PW_A)               # even the right password
    assert status == 429 and body["code"] == "TOO_MANY_REQUESTS" and int(hdrs["retry-after"]) >= 1
    assert calls == []


def test_limiter_backs_off_exponentially_and_clears_on_success_with_no_global_lock():
    now = [0.0]
    lim = rb.SignInLimiter(lambda: now[0], free_ip=2, base_ip=10, cap=100)
    lim.failure("1.1.1.1")
    assert lim.retry_after("1.1.1.1") == 0
    lim.failure("1.1.1.1")                                    # the 2nd failure locks for 10 s
    assert 10 <= lim.retry_after("1.1.1.1") <= 11
    now[0] = 11
    assert lim.retry_after("1.1.1.1") == 0
    lim.failure("1.1.1.1")                                    # the 3rd: 20 s
    assert 20 <= lim.retry_after("1.1.1.1") <= 21
    lim.success("1.1.1.1")
    assert lim.retry_after("1.1.1.1") == 0
    for i in range(300):                                      # many failures from elsewhere never lock anybody else
        lim.failure(f"10.0.{i // 250}.{i % 250}")
    assert lim.retry_after("9.9.9.9") == 0
    assert lim.failure("9.9.9.8") >= 300                      # ...but they are counted, for logging
    now[0] = 10_000                                           # quiet time forgets everything
    assert lim.retry_after("10.0.0.1") == 0 and lim.failure("1.2.3.4") == 1


def test_100_failures_from_one_address_do_not_block_a_correct_sign_in_from_another(srv):
    svr, api = srv
    hdr = lambda ip: {"X-Forwarded-For": ip}                  # noqa: E731 - loopback is a trusted proxy in the harness
    codes = []
    for _ in range(100):
        body = json.dumps({"email": "alice@example.com", "password": "wrong"}).encode()
        codes.append(api.raw("POST", "/api/auth/sign-in/email", body, hdr("10.1.1.1"))[0])
    assert codes[:5] == [401] * 5 and set(codes[5:]) == {429}              # A is locked after 5, before any KDF
    good = json.dumps({"email": "alice@example.com", "password": PW_A}).encode()
    status, body, headers = api.raw("POST", "/api/auth/sign-in/email", good, hdr("10.2.2.2"))
    assert status == 200 and json.loads(body)["token"]
    assert api.raw("POST", "/api/auth/sign-in/email", good, hdr("10.1.1.1"))[0] == 429      # A stays locked, B was never


def test_client_address_trusts_forwarded_for_only_from_loopback_or_configured_proxies():
    cfg = rb.rakazo_bridge_config(bridge_section(trusted_proxies=["172.18.0.0/16", "100.104.39.6"]))
    bridge = rb.RakazoBridge(cfg, assistant=None, version="x")
    assert bridge.client_address("127.0.0.1", "100.64.0.9") == "100.64.0.9"
    assert bridge.client_address("172.18.0.1", "9.9.9.9, 100.64.0.9") == "100.64.0.9"      # the last (the gateway's) entry
    assert bridge.client_address("100.104.39.6", "100.64.0.9") == "100.64.0.9"
    assert bridge.client_address("100.64.0.77", "1.2.3.4") == "100.64.0.77"              # an ordinary device cannot choose
    assert bridge.client_address("172.19.0.1", "1.2.3.4") == "172.19.0.1"
    assert bridge.client_address("127.0.0.1", "not-an-ip") == "127.0.0.1"                # junk never becomes a key
    assert bridge.client_address("127.0.0.1", "") == "127.0.0.1"
    plain = rb.RakazoBridge(rb.rakazo_bridge_config(bridge_section()), assistant=None, version="x")
    assert plain.client_address("172.18.0.1", "100.64.0.9") == "172.18.0.1"              # default: loopback only
    for bad in ("nope", 5, ["10.0.0.0/99"]):
        with pytest.raises(ValueError, match="trusted_proxies"):
            rb.rakazo_bridge_config(bridge_section(trusted_proxies=bad if isinstance(bad, list) else [bad]))
    with pytest.raises(ValueError, match="trusted_proxies"):
        rb.rakazo_bridge_config(bridge_section(trusted_proxies="10.0.0.1"))


# --------------------------------------------------------------------------------------------- tokens
def test_tokens_expire_are_revoked_and_capped_per_account():
    now = [1000.0]
    store = rb.TokenStore(ttl_seconds=100, max_tokens=2, clock=lambda: now[0])
    t1, t2 = store.mint("a", "1.1.1.1"), store.mint("a", "1.1.1.1")
    other = store.mint("b", "2.2.2.2")
    now[0] += 1
    t3 = store.mint("a", "1.1.1.1")                           # evicts the oldest of account a only
    assert store.lookup(t1) is None and store.lookup(t2) == "a" and store.lookup(t3) == "a"
    assert store.lookup(other) == "b" and store.count("a") == 2 and store.count("b") == 1
    assert store.revoke(t2) == (True, True) and store.lookup(t2) is None and store.revoke(t2) == (False, True)
    now[0] += 200
    assert store.lookup(t3) is None and store.valid(other) is False


def test_only_the_hash_of_a_token_is_kept_with_bookkeeping():
    now = [5.0]
    store = rb.TokenStore(100, 5, clock=lambda: now[0])
    token = store.mint("a", "9.9.9.9")
    assert token not in json.dumps(store._tokens)
    (digest, rec), = store._tokens.items()
    assert digest == rb.TokenStore.digest(token) and len(digest) == 64
    assert rec["created"] == 5.0 and rec["expires"] == 105.0 and rec["last_ip"] == "9.9.9.9"
    now[0] = 50.0
    store.lookup(token, "8.8.8.8")
    assert rec["last_used"] == 50.0 and rec["last_ip"] == "8.8.8.8"


class FakeTokenTable:
    def __init__(self):
        self.rows, self.fail = {}, False

    def load(self):
        return [{"hash": h, **r} for h, r in self.rows.items()]

    def upsert(self, digest, rec):
        if self.fail:
            raise RuntimeError("db down")
        self.rows[digest] = {k: v for k, v in rec.items() if k != "persisted"}

    def delete(self, digest):
        self.rows.pop(digest, None)

    def touch(self, digest, when, ip):
        self.rows[digest]["last_used"], self.rows[digest]["last_ip"] = when, ip


def test_persisted_tokens_survive_a_restart_and_a_write_failure_issues_no_token():
    table = FakeTokenTable()
    first = rb.TokenStore(1000, 2, table)
    t1, t2 = first.mint("a", "ip"), first.mint("a", "ip")
    first.mint("a", "ip")                                   # evicts t1 from memory AND the table
    assert len(table.rows) == 2 and rb.TokenStore.digest(t1) not in table.rows
    assert t2 not in json.dumps(table.rows)
    reborn = rb.TokenStore(1000, 2, table)
    assert reborn.lookup(t2) == "a" and reborn.lookup(t1) is None
    table.fail = True
    with pytest.raises(rb.TokenStoreError):
        reborn.mint("a", "ip")
    assert reborn.count() == 2                               # nothing half-issued
    reborn.revoke(t2)
    assert rb.TokenStore.digest(t2) not in table.rows


def test_postgres_persistence_writes_only_hashes():
    seen = []

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args=()): seen.append((sql, args))
        def fetchall(self): return []

    class Conn:
        def cursor(self): return Cur()
        def commit(self): pass
        def close(self): pass

    p = rb.PostgresTokenPersistence("postgresql://user:pw@h/db", connect=lambda: Conn())
    store = rb.TokenStore(100, 5, p)
    token = store.mint("a", "1.2.3.4")
    store.revoke(token)
    assert any("INSERT INTO rakazo_bridge_tokens" in s for s, _ in seen)
    assert any("DELETE FROM rakazo_bridge_tokens" in s for s, _ in seen)
    assert token not in repr(seen) and rb.TokenStore.digest(token) in repr(seen)


def test_migration_defines_the_hash_keyed_table():
    from pathlib import Path
    sql = Path(__file__).parents[2].joinpath("deploy/control-plane/migrations/0036_rakazo_bridge_tokens.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS rakazo_bridge_tokens" in sql and "token_sha256" in sql and "-- DOWN:" in sql


def test_sign_out_revokes_and_gives_the_same_answer_for_any_token(srv):
    _, api = srv
    token = api.login()
    status, body, _ = api.raw("POST", "/api/auth/sign-out", {}, {"authorization": f"Bearer {token}"})
    assert (status, json.loads(body)) == (200, {"success": True})
    assert api.rpc("me", token=token)[0] == 401
    assert api.raw("POST", "/api/auth/sign-out", {}, {"authorization": "Bearer bogus"})[0] == 200


def test_token_cap_and_expiry_through_the_server(make_server):
    svr, api = make_server(bridge_section(max_tokens=2))
    first, second, third = api.login(), api.login(), api.login()
    assert api.rpc("me", token=first)[0] == 401 and api.rpc("me", token=second)[0] == 200
    assert api.rpc("me", token=third)[0] == 200
    bob = api.login("bob@example.com", PW_B)                 # the cap is per account
    assert api.rpc("me", token=bob)[0] == 200 and api.rpc("me", token=third)[0] == 200
    for rec in svr.rakazo.tokens._tokens.values():
        rec["expires"] = time.time() - 1
    assert api.rpc("me", token=third)[0] == 401


# --------------------------------------------------------------------------------------------- authorization
def test_every_procedure_requires_a_valid_bearer(srv):
    svr, api = srv
    procs = sorted(svr.rakazo._procs) + ["billing/status", "nope"]
    for proc in procs:
        for headers in ({}, {"authorization": "Bearer not-a-token"}, {"authorization": "Basic abc"}, {"authorization": "Bearer "}):
            status, body, _ = api.rpc(proc, {}, **headers)
            assert status == 401, proc
            assert body["json"]["code"] == "UNAUTHORIZED" and body["json"]["status"] == 401
            assert isinstance(body["json"]["message"], str) and "data" in body["json"]


def test_the_space_header_is_accepted_and_the_origin_header_is_not_required(srv, alice):
    _, api = srv
    assert api.rpc("me", token=alice, **{"x-rakazo-space-id": "herdr"})[0] == 200
    req = urllib.request.Request(api.base + "/rpc/me", data=b'{"json":{}}', method="POST",
                                 headers={"authorization": f"Bearer {alice}"})
    assert urllib.request.urlopen(req, timeout=5).status == 200


def test_other_paths_and_verbs_still_behave_as_before(srv):
    _, api = srv
    assert api.raw("GET", "/api/v1/health")[0] == 200
    assert api.raw("GET", "/rpc/me")[0] == 404 and api.raw("POST", "/rpc/")[0] == 404
    assert api.raw("POST", "/rpc/../api/v1/health")[0] in (401, 404)


def test_bad_framing_and_oversize_bodies_are_refused(srv):
    _, api = srv
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=5)
    try:
        conn.request("POST", "/rpc/me", b"x" * (rb.MAX_BODY + 1), {"content-type": "application/json"})
        assert conn.getresponse().status == 413
    except (BrokenPipeError, ConnectionResetError):
        pass                                         # refused before the body was read: the socket closed on the sender


# --------------------------------------------------------------------------------------------- procedures
ME_FIELDS = {"userId", "email", "name", "spaceId", "ticketBoardEnabled", "isDeploymentOwner", "needsModel",
             "defaultProvider", "defaultModel", "hostCredentialProvider", "hostCredentialSource", "computerHost",
             "canChooseHostComputer", "sandboxProvider", "avatarStyle", "billingEnabled"}


def test_me_has_every_required_field_and_no_paid_or_model_setup_prompts(srv, alice):
    _, api = srv
    me = api.ok("me", token=alice)
    assert ME_FIELDS <= set(me)
    assert me["spaceId"] == "herdr" and me["needsModel"] is False and me["billingEnabled"] is False
    assert me["isDeploymentOwner"] is False and me["avatarStyle"] in ("robot", "organic")
    assert me["userId"].startswith("rakazo:") and me["needsSignupSurvey"] is False


BOT_FIELDS = {"id", "spaceId", "name", "title", "description", "instructions", "color", "notifyOnFinish", "pinned",
              "sectionId", "archivedAt", "unread", "parentBotId", "memoryScope", "threadId", "preview", "status",
              "runErrorId", "computerMode", "updatedAt", "createdAt", "voiceId", "autoSpeak", "modelProvider",
              "modelId", "thinkingLevel", "teamChatAmbientEnabled", "teamChatRules", "webhookConfigured", "spawnKey"}


def test_bots_map_to_assistants_with_one_thread_each(srv, alice):
    svr, api = srv
    assert api.ok("bots/list", token=alice) == [] and api.ok("spaces/list", token=alice)["spaces"][0]["hasContent"] is False
    bot = new_bot(api, alice)
    assert BOT_FIELDS <= set(bot) and bot["spaceId"] == "herdr" and bot["name"] == "Herdr" and bot["threadId"]
    assert bot["instructions"] == "be brief" and bot["status"] == "idle" and bot["archivedAt"] is None
    assistants = svr.assistant.store.list_assistants(tenant_id="default", user_id=svr.rakazo.accounts[0].principal_id)
    assert [a.assistant_id for a in assistants] == [bot["id"]]                    # no second assistant is created
    assert api.ok("bots/get", {"botId": bot["id"]}, alice) == api.ok("bots/list", token=alice)[0]
    assert len(svr.assistant.store.threads) == 1
    nav = api.ok("spaces/list", token=alice)
    assert nav["current"]["id"] == "herdr" and [b["id"] for b in nav["current"]["bots"]] == [bot["id"]]
    assert set(nav["current"]) >= {"bots", "groups", "externalConversations", "botSections"}
    space = nav["spaces"][0]
    assert space["isDefault"] is True and space["hasContent"] is True and space["bots"][0]["id"] == bot["id"]
    assert api.rpc("bots/get", {"botId": "missing"}, alice)[0] == 404
    assert api.rpc("bots/create", {"name": ""}, alice)[0] == 400


def test_an_assistant_made_elsewhere_for_the_same_principal_shows_up_with_its_existing_thread(srv, alice):
    svr, api = srv
    acct = svr.rakazo.accounts[0]
    a = svr.assistant.create_assistant(tenant_id="default", user_id=acct.principal_id, name="Made in the dashboard")
    t = svr.assistant.create_thread(tenant_id="default", user_id=acct.principal_id, assistant_id=a.assistant_id)
    [bot] = api.ok("bots/list", token=alice)
    assert bot["id"] == a.assistant_id and bot["threadId"] == t.thread_id and len(svr.assistant.store.threads) == 1


def test_send_posts_to_the_assistant_thread_and_reports_the_run(srv, alice):
    svr, api = srv
    bot = new_bot(api, alice)
    before = api.ok("threads/get", {"botId": bot["id"]}, alice)
    assert before["messages"] == [] and before["run"] is None and before["olderCursor"] is None
    sent, snap = send(api, alice, bot["id"], "hello bridge")
    assert set(sent) >= {"taskId", "runId", "seq"} and sent["seq"] > before["cursor"]
    store = svr.assistant.store
    stored = store.list_messages(bot["threadId"], tenant_id="default", user_id=svr.rakazo.accounts[0].principal_id)
    assert [m.role for m in stored] == ["user", "assistant"] and stored[0].content == "hello bridge"
    run = store.runs[sent["runId"]]
    assert run.task_id == sent["taskId"] and run.thread_id == bot["threadId"] and run.state == "completed"
    assert [m["role"] for m in snap["messages"]] == ["user", "bot"]
    user_msg, bot_msg = snap["messages"]
    assert user_msg["blocks"] == [{"kind": "text", "text": "hello bridge"}] and user_msg["seq"] == sent["seq"]
    assert bot_msg["botId"] == bot["id"] and bot_msg["runId"] == sent["runId"] and bot_msg["threadId"] == bot["threadId"]
    assert snap["cursor"] >= bot_msg["seq"] > user_msg["seq"] and snap["threadId"] == bot["threadId"]
    assert snap["botId"] == bot["id"] and snap["activeRuns"] == []
    assert api.ok("bots/get", {"botId": bot["id"]}, alice)["preview"]


def test_send_validation(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    assert api.rpc("threads/send", {"botId": bot["id"], "text": "  "}, alice)[0] == 400
    assert api.rpc("threads/send", {"botId": bot["id"], "text": "x", "artifactIds": ["a"]}, alice)[0] == 400
    assert api.rpc("threads/send", {"groupId": "g", "text": "x"}, alice)[0] == 400
    assert api.rpc("threads/send", {"botId": "missing", "text": "x"}, alice)[0] == 404
    assert api.rpc("threads/send", {"text": "x"}, alice)[0] == 400


def test_a_failing_run_reports_failed_not_a_hang(srv, alice, monkeypatch):
    svr, api = srv

    def boom(run, messages):
        raise RuntimeError("model unreachable api_key=sk-livesecret123456")
    monkeypatch.setattr(svr.assistant.runner, "execute", boom)
    bot = new_bot(api, alice)
    out = api.ok("threads/send", {"botId": bot["id"], "text": "hi"}, alice)
    snap = wait_idle(api, alice, bot["id"])
    assert snap["run"]["id"] == out["runId"] and snap["run"]["status"] == "failed"
    assert "unreachable" in snap["run"]["error"] and "sk-livesecret" not in json.dumps(snap)
    events = svr.rakazo.journals.get(bot["threadId"], bot["id"]).events
    assert [e["type"] for e in events if e.get("runId") == out["runId"]] == ["run.started", "run.failed"]


def test_messages_page_backwards_by_seq(srv, alice):
    svr, api = srv
    bot = new_bot(api, alice)
    for i in range(3):
        send(api, alice, bot["id"], f"m{i}")
    full = api.ok("threads/get", {"botId": bot["id"]}, alice)
    assert len(full["messages"]) == 6
    journal = svr.rakazo.journals.get(bot["threadId"], bot["id"])
    page, older = journal.page(limit=2)
    assert [m["seq"] for m in page] == [m["seq"] for m in full["messages"][-2:]] and older == page[0]["seq"]
    out = api.ok("threads/messages", {"botId": bot["id"], "before": full["messages"][3]["seq"]}, alice)
    assert out["threadId"] == bot["threadId"] and [m["id"] for m in out["messages"]] == [m["id"] for m in full["messages"][:3]]
    assert out["olderCursor"] is None and out["coveredThroughSeq"] >= full["messages"][-1]["seq"]
    assert api.rpc("threads/messages", {"botId": bot["id"], "before": "x"}, alice)[0] == 400
    head = api.ok("threads/head", {"botId": bot["id"]}, alice)
    assert head == {"threadId": bot["threadId"], "cursor": full["cursor"]}


def test_runs_list_active_and_recent(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    assert api.ok("runs/list", {"filter": "recent"}, alice) == {"runs": []}
    sent, _ = send(api, alice, bot["id"], "what is up")
    [row] = api.ok("runs/list", {"filter": "recent"}, alice)["runs"]
    assert {"runId", "botId", "botName", "groupId", "groupName", "threadId", "status", "trigger",
            "notificationsEnabled", "promptSnippet", "agentMessage", "updatedAt"} <= set(row)
    assert row["runId"] == sent["runId"] and row["status"] == "completed" and row["promptSnippet"] == "what is up"
    assert row["botName"] == "Herdr" and row["groupId"] is None and row["agentMessage"] is None
    assert api.ok("runs/list", {"filter": "active"}, alice) == {"runs": []}
    assert api.rpc("runs/list", {"filter": "all"}, alice)[0] == 400


def test_ai_consent_names_no_recipients_so_the_app_never_prompts(srv, alice):
    _, api = srv
    status = api.ok("aiConsent/status", {"uses": ["model", "memory"], "botId": "b"}, alice)
    assert status["recipients"] == [] and status["scope"] and status["version"]
    assert api.ok("aiConsent/allow", {"scope": "s", "version": "v", "keys": ["k"]}, alice)["recipients"] == []


def test_push_registration_is_accepted_and_nothing_is_stored(srv, alice):
    svr, api = srv
    before = repr(vars(svr.rakazo.tokens)) + repr(svr.assistant.store.snapshot())
    assert api.ok("notifications/registerPush", {"token": "ExponentPushToken[abcdefgh]"}, alice) == {"ok": True}
    assert api.rpc("notifications/registerPush", {"token": "bad token"}, alice)[0] == 400
    assert api.ok("notifications/unregisterPush", None, alice) == {"ok": True}
    assert "abcdefgh" not in repr(vars(svr.rakazo.tokens)) + repr(svr.assistant.store.snapshot())
    assert before.count("ExponentPushToken") == 0


@pytest.mark.parametrize("proc", ["billing/status", "voice/status", "computer/status", "models/list", "groups/list",
                                  "capabilities/list", "routines/list", "bots/update", "totally/unknown"])
def test_unimplemented_procedures_answer_a_rakazo_shaped_404(srv, alice, proc):
    _, api = srv
    status, body, _ = api.rpc(proc, {}, alice)
    assert status == 404
    err = body["json"]
    assert err["code"] == "NOT_FOUND" and err["status"] == 404 and isinstance(err["message"], str) and err["data"] == {}
    assert api.raw("POST", "/rpc/bad%20proc", {"json": {}}, {"authorization": f"Bearer {alice}"})[0] == 404


def test_the_assistant_being_disabled_is_a_clear_400_not_a_hang(make_server):
    svr, api = make_server(bridge_section(), assistant={"enabled": False})
    token = api.login()
    assert api.ok("me", token=token)["email"] == "alice@example.com"
    status, body, _ = api.rpc("bots/list", {}, token)
    assert status == 400 and "not enabled" in body["json"]["message"]


# --------------------------------------------------------------------------------------------- SSE
def test_subscribe_frames_events_in_order_from_the_cursor(srv, alice):
    svr, api = srv
    bot = new_bot(api, alice)
    cursor = api.ok("threads/get", {"botId": bot["id"]}, alice)["cursor"]
    conn, resp = api.stream(alice, bot["id"], cursor)
    try:
        assert resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"
        assert resp.getheader("Cache-Control") == "no-store"
        assert read_frames(resp, 1) == [{"type": "heartbeat"}]
        sent = api.ok("threads/send", {"botId": bot["id"], "text": "stream me"}, alice)
        events = []
        for frame in read_frames(resp, 40, stop=lambda fs: any(f.get("type") == "run.completed" for f in fs)):
            if frame.get("type") != "heartbeat":
                events.append(frame)
        types = [e["type"] for e in events]
        assert types == ["thread.message.created", "run.started", "thread.message.created", "run.completed"] or \
            types == ["run.started", "thread.message.created", "thread.message.created", "run.completed"]
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and seqs[0] > cursor
        for e in events:
            assert {"id", "spaceId", "threadId", "botId", "seq", "type", "createdAt", "payload"} <= set(e)
            assert e["threadId"] == bot["threadId"] and e["botId"] == bot["id"] and e["spaceId"] == "herdr"
        created = [e for e in events if e["type"] == "thread.message.created"]
        user, reply = sorted(created, key=lambda e: e["seq"])
        assert user["payload"]["role"] == "user" and user["payload"]["seq"] == sent["seq"] == user["seq"]
        assert user["payload"]["blocks"] == [{"kind": "text", "text": "stream me"}]
        assert reply["payload"]["role"] == "bot" and reply["runId"] == sent["runId"] and reply["payload"]["messageId"]
        started = next(e for e in events if e["type"] == "run.started")
        assert started["runId"] == sent["runId"] and started["payload"]["trigger"] == "user"
    finally:
        conn.close()


def test_subscribing_at_the_head_replays_nothing_and_a_stale_cursor_replays_only_newer(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    send(api, alice, bot["id"], "one")
    head = api.ok("threads/get", {"botId": bot["id"]}, alice)["cursor"]
    conn, resp = api.stream(alice, bot["id"], head)
    try:
        frames = read_frames(resp, 3)
        assert [f["type"] for f in frames] == ["heartbeat"] * 3          # nothing but liveness at the head
    finally:
        conn.close()
    conn, resp = api.stream(alice, bot["id"], head - 2)
    try:
        events = [f for f in read_frames(resp, 3) if f["type"] != "heartbeat"]
        assert events and all(e["seq"] > head - 2 for e in events)
    finally:
        conn.close()


def test_the_stream_beats_inside_the_apps_45_second_silence_and_ends_cleanly(srv, alice):
    svr, api = srv
    assert svr.rakazo.stream_options["heartbeat_seconds"] <= 20
    assert ev.event_stream.__kwdefaults__["heartbeat_seconds"] <= 20 and ev.event_stream.__kwdefaults__["max_seconds"] <= 900
    bot = new_bot(api, alice)
    conn, resp = api.stream(alice, bot["id"], -1)
    try:
        t0 = time.time()
        frames = read_frames(resp, 100)                        # until the server ends the stream (max_seconds=3)
        assert time.time() - t0 < 5 and frames and all(f == {"type": "heartbeat"} for f in frames)
        assert 3 <= len(frames) <= 20
    finally:
        conn.close()


def test_event_stream_unit_framing_cursor_and_heartbeat_timing():
    now = [0.0]
    pending = [{"seq": 5, "type": "a"}, {"seq": 6, "type": "b"}]
    seen = []

    def fetch(cursor):
        seen.append(cursor)
        out, pending[:] = [e for e in pending if e["seq"] > cursor], []
        return out

    def sleep(dt):
        now[0] += dt
    frames = list(ev.event_stream(fetch, 4, poll_seconds=1, heartbeat_seconds=3, max_seconds=8,
                                  clock=lambda: now[0], sleep=sleep))
    assert frames[0] == b'data: {"json":{"type":"heartbeat"}}\n\n'
    assert frames[1] == b'data: {"json":{"seq":5,"type":"a"}}\n\n' and frames[2].startswith(b'data: {"json":{"seq":6')
    assert all(f.endswith(b"\n\n") for f in frames)
    assert seen[0] == 4 and seen[1] == 6                      # the cursor moves with what was sent
    beats = [i for i, f in enumerate(frames) if f == ev.HEARTBEAT_FRAME]
    assert len(beats) >= 2 and now[0] >= 8


def test_revoking_the_token_ends_its_stream(srv, alice):
    svr, api = srv
    svr.rakazo.stream_options = {"poll_seconds": 0.02, "heartbeat_seconds": 0.1, "max_seconds": 30}
    bot = new_bot(api, alice)
    conn, resp = api.stream(alice, bot["id"], -1)
    try:
        read_frames(resp, 1)
        api.raw("POST", "/api/auth/sign-out", {}, {"authorization": f"Bearer {alice}"})
        t0 = time.time()
        while read_frames(resp, 1):
            assert time.time() - t0 < 5
    finally:
        conn.close()


def test_subscribe_needs_the_event_stream_accept_header_and_a_valid_cursor(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    assert api.rpc("threads/subscribe", {"botId": bot["id"], "cursor": 0}, alice)[0] == 400
    assert api.rpc("threads/subscribe", {"botId": bot["id"], "cursor": "x"}, alice, accept="text/event-stream")[0] == 400
    assert api.rpc("threads/subscribe", {"botId": "missing", "cursor": 0}, alice, accept="text/event-stream")[0] == 404


def test_concurrent_streams_are_bounded(srv, alice):
    svr, api = srv
    svr.rakazo.stream_options = {"poll_seconds": 0.05, "heartbeat_seconds": 5, "max_seconds": 30}
    bot = new_bot(api, alice)
    open_ = []
    try:
        for _ in range(rb.MAX_STREAMS):
            open_.append(api.stream(alice, bot["id"], -1))
            assert open_[-1][1].status == 200
            read_frames(open_[-1][1], 1)
        conn, resp = api.stream(alice, bot["id"], -1)
        assert resp.status == 429 and json.loads(resp.read())["json"]["code"] == "TOO_MANY_REQUESTS"
        conn.close()
    finally:
        for c, _ in open_:
            c.close()


def test_journal_numbers_stay_ahead_of_any_cursor_a_client_kept_across_a_restart():
    old = ev.JournalRegistry(clock=lambda: 1_000_000)
    new = ev.JournalRegistry(clock=lambda: 1_000_100)
    j_old, j_new = old.get("t", "a"), new.get("t", "a")
    assert j_old.head == 1_000_000 * 1000 - 1 and j_new.base > j_old.base + 90_000


# --------------------------------------------------------------------------------------------- approvals
def make_approval(svr, api, token, account_index=0):
    """A pending approval on a run in the bot's thread, raised by the same gate the model uses."""
    acct = svr.rakazo.accounts[account_index]
    bot = new_bot(api, token)
    sent, _ = send(api, token, bot["id"], "please email bob")
    svc = svr.assistant
    svc.grant_tool(tenant_id="default", user_id=acct.principal_id, assistant_id=bot["id"], tool_id="send_message",
                   scope="bob@example.com")
    run = svc.store.runs[sent["runId"]]
    out = svc._execute_gated(run, "send_message", {"to": "bob@example.com", "body": "hi"}, 2000)
    assert "approval required" in out
    approval = next(a for a in svc.store.approvals.values() if a.user_id == acct.principal_id)
    return bot, sent, approval


def test_a_pending_approval_is_an_ask_card_the_app_recognises(srv, alice):
    svr, api = srv
    bot, sent, approval = make_approval(svr, api, alice)
    snap = api.ok("threads/get", {"botId": bot["id"]}, alice)
    card = next(m for m in snap["messages"] if m["id"] == "approval:" + approval.approval_id)
    [block] = card["blocks"]
    assert card["role"] == "bot" and card["runId"] == sent["runId"] and card["botId"] == bot["id"]
    assert block["kind"] == "ask" and block["status"] == "pending" and block["approvalEffectId"] == approval.approval_id
    ids = {a["id"] for a in block["actions"]}
    assert ids == {"allow", "deny"} and "send_message" in block["text"] and block["detail"]


def test_denying_goes_through_the_existing_approval_path(srv, alice):
    svr, api = srv
    bot, sent, approval = make_approval(svr, api, alice)
    cursor = api.ok("threads/get", {"botId": bot["id"]}, alice)["cursor"]
    assert api.ok("threads/answer", {"botId": bot["id"], "runId": sent["runId"],
                                     "messageId": "approval:" + approval.approval_id, "answer": "deny"}, alice) == {"ok": True}
    stored = svr.assistant.store.approvals[approval.approval_id]
    assert stored.decision == "denied" and stored.actor_id == svr.rakazo.accounts[0].principal_id
    assert svr.assistant.store.actions[approval.action_id].status == "denied"
    actions = [e["action"] for e in svr.assistant.store.audit]
    assert "assistant.approval.denied" in actions
    snap = api.ok("threads/get", {"botId": bot["id"]}, alice)
    block = next(m for m in snap["messages"] if m["id"].startswith("approval:"))["blocks"][0]
    assert block["status"] == "answered" and block["answer"] == "deny"
    events = svr.rakazo.journals.get(bot["threadId"], bot["id"]).after(cursor)
    update = [e for e in events if e["type"] == "thread.message.updated"]
    assert len(update) == 1 and update[0]["payload"]["blocks"][0]["answer"] == "deny"
    assert update[0]["payload"]["seq"] == next(m for m in snap["messages"] if m["id"].startswith("approval:"))["seq"]
    status, body, _ = api.rpc("threads/answer", {"botId": bot["id"], "runId": sent["runId"],
                                                 "messageId": "approval:" + approval.approval_id, "answer": "allow"}, alice)
    assert status == 400 and "already denied" in body["json"]["message"]          # a decision is final


def test_the_same_person_may_not_approve_a_send_unless_the_yaml_allows_it(srv, alice):
    svr, api = srv                                             # default config: allow_self_approval False
    bot, sent, approval = make_approval(svr, api, alice)
    status, body, _ = api.rpc("threads/answer", {"botId": bot["id"], "runId": sent["runId"],
                                                 "messageId": "approval:" + approval.approval_id, "answer": "allow"}, alice)
    assert status == 403 and body["json"]["code"] == "FORBIDDEN" and "differ" in body["json"]["message"]
    assert svr.assistant.store.approvals[approval.approval_id].decision == "pending"
    assert any(e["action"] == "assistant.approval.self_approval_refused" for e in svr.assistant.store.audit)


def test_with_yaml_self_approval_the_allow_answer_approves(make_server):
    svr, api = make_server(bridge_section(), assistant={"actions": {"allow_self_approval": True}})
    token = api.login()
    bot, sent, approval = make_approval(svr, api, token)
    assert api.ok("threads/answer", {"botId": bot["id"], "runId": sent["runId"],
                                     "messageId": "approval:" + approval.approval_id, "answer": "allow"}, token) == {"ok": True}
    assert svr.assistant.store.approvals[approval.approval_id].decision == "approved"
    block = next(m for m in api.ok("threads/get", {"botId": bot["id"]}, token)["messages"]
                 if m["id"].startswith("approval:"))["blocks"][0]
    assert block["status"] == "answered" and block["answer"] == "allow"


def test_answers_that_are_not_approvals_or_not_yours_are_refused(srv, alice):
    svr, api = srv
    bot, sent, approval = make_approval(svr, api, alice)
    base = {"botId": bot["id"], "runId": sent["runId"], "messageId": "approval:" + approval.approval_id}
    assert api.rpc("threads/answer", {**base, "answer": "maybe"}, alice)[0] == 400
    assert api.rpc("threads/answer", {**base, "answer": "x", "dismiss": True}, alice)[0] == 400
    assert api.rpc("threads/answer", {**base, "messageId": "msg_1", "answer": "allow"}, alice)[0] == 400
    assert api.rpc("threads/answer", {**base, "runId": "run_other", "answer": "deny"}, alice)[0] == 404
    assert api.rpc("threads/answer", {**base, "messageId": "approval:nope", "answer": "deny"}, alice)[0] == 404
    bob = api.login("bob@example.com", PW_B)
    new_bot(api, bob)
    bobs_bot = api.ok("bots/list", token=bob)[0]
    assert api.rpc("threads/answer", {**base, "botId": bobs_bot["id"], "answer": "deny"}, bob)[0] == 404
    assert api.rpc("threads/answer", {**base, "answer": "deny"}, bob)[0] == 404         # alice's bot is invisible to bob
    assert svr.assistant.store.approvals[approval.approval_id].decision == "pending"


def test_stop_cancels_an_active_run_through_the_service(srv, alice):
    svr, api = srv
    bot = new_bot(api, alice)
    sent, _ = send(api, alice, bot["id"], "x")
    from herdr_engineering.control_plane.assistant import AssistantRun
    run = AssistantRun(run_id="run_live", thread_id=bot["threadId"], assistant_id=bot["id"], tenant_id="default",
                       user_id=svr.rakazo.accounts[0].principal_id, task_id="task_live", state="running")
    svr.assistant.store.put_run(run)
    assert api.ok("threads/get", {"botId": bot["id"]}, alice)["activeRuns"][0]["status"] == "running"
    api.ok("threads/stop", {"botId": bot["id"]}, alice)
    assert svr.assistant.store.runs["run_live"].state == "cancelled" and sent["runId"] in svr.assistant.store.runs
    assert api.ok("threads/markRead", {"botId": bot["id"]}, alice) == {"ok": True, "unreadAfterSeq": None}


# --------------------------------------------------------------------------------------------- accounts
def test_two_operator_accounts_get_distinct_principals_and_cannot_read_each_others_threads(srv):
    svr, api = srv
    a_tok, b_tok = api.login(), api.login("bob@example.com", PW_B)
    a_me, b_me = api.ok("me", token=a_tok), api.ok("me", token=b_tok)
    assert a_me["userId"] != b_me["userId"] and a_me["email"] != b_me["email"]
    a_bot, b_bot = new_bot(api, a_tok, "Alice bot"), new_bot(api, b_tok, "Bob bot")
    send(api, a_tok, a_bot["id"], "alice private note")
    send(api, b_tok, b_bot["id"], "bob private note")
    assert [b["id"] for b in api.ok("bots/list", token=a_tok)] == [a_bot["id"]]
    assert [b["id"] for b in api.ok("bots/list", token=b_tok)] == [b_bot["id"]]
    for proc in ("bots/get", "threads/get", "threads/messages", "threads/head", "threads/stop"):
        assert api.rpc(proc, {"botId": a_bot["id"]}, b_tok)[0] == 404, proc
    assert api.rpc("threads/send", {"botId": a_bot["id"], "text": "intrude"}, b_tok)[0] == 404
    assert api.rpc("threads/subscribe", {"botId": a_bot["id"], "cursor": 0}, b_tok, accept="text/event-stream")[0] == 404
    assert "alice private note" not in json.dumps(api.ok("threads/get", {"botId": b_bot["id"]}, b_tok))
    assert api.ok("runs/list", {"filter": "recent"}, b_tok)["runs"][0]["botId"] == b_bot["id"]
    assert len(api.ok("runs/list", {"filter": "recent"}, b_tok)["runs"]) == 1
    store = svr.assistant.store
    owners = {a.assistant_id: a.user_id for a in store.assistants.values()}
    assert owners[a_bot["id"]] == a_me["userId"] and owners[b_bot["id"]] == b_me["userId"]
    audit_users = {e["user_id"] for e in store.audit}
    assert {a_me["userId"], b_me["userId"]} <= audit_users
    assert not any(a_me["userId"] == e["user_id"] and b_bot["id"] == e["subject_id"] for e in store.audit)
    assert api.sign_in("alice@example.com", PW_B)[0] == 401 and api.sign_in("bob@example.com", PW_A)[0] == 401


def test_a_token_is_bound_to_its_account_and_the_assistant_tenant_comes_from_yaml(make_server):
    svr, api = make_server(bridge_section(), assistant={"tenancy": {"principals": {}, "default_tenant": "household"}})
    tok = api.login()
    bot = new_bot(api, tok)
    assert svr.assistant.store.assistants[bot["id"]].tenant_id == "household"
    assert svr.rakazo.tokens.lookup(tok) == svr.rakazo.accounts[0].account_id


def test_assistant_rate_limits_apply_to_the_bridge_principal(make_server):
    svr, api = make_server(bridge_section(), assistant={"max_requests_per_minute": 3})
    tok = api.login()
    statuses = [api.rpc("me", {}, tok)[0] for _ in range(5)]
    assert statuses[:3] == [200, 200, 200] and statuses[3:] == [429, 429]


# --------------------------------------------------------------------------------------------- secrets in logs/audit
def test_no_password_token_hash_or_body_reaches_logs_audit_or_errors(srv, caplog):
    svr, api = srv
    caplog.set_level(logging.DEBUG)
    api.sign_in("alice@example.com", "a-wrong-password-9876")
    api.sign_in("nobody@example.com", "another-wrong-9876")
    api.raw("POST", "/api/auth/sign-in/email", b'{"email": "x@y.z", "password": "malformed-body-secret"')
    token = api.login()
    bot = new_bot(api, token)
    api.ok("threads/send", {"botId": bot["id"], "text": "sensitive-message-text-4242"}, token)
    wait_idle(api, token, bot["id"])
    api.rpc("billing/status", {"secret": "body-secret-in-unknown-proc"}, token)
    api.raw("POST", "/api/auth/sign-out", {}, {"authorization": f"Bearer {token}"})
    for _ in range(6):
        api.sign_in("alice@example.com", "lockout-attempt-5555")
    logs = "\n".join(r.getMessage() + (str(r.exc_info) if r.exc_info else "") for r in caplog.records)
    audit = json.dumps(svr.assistant.store.audit)
    forbidden = [PW_A, PW_B, HASH_A, HASH_B, token, "a-wrong-password-9876", "another-wrong-9876", "malformed-body-secret",
                 "sensitive-message-text-4242", "body-secret-in-unknown-proc", "lockout-attempt-5555",
                 rb.TokenStore.digest(token)]
    for secret in forbidden:
        assert secret not in logs, secret
        assert secret not in audit, secret
    assert "rakazo.bridge.sign_in" in audit and "rakazo.bridge.sign_out" in audit
    assert "failed sign-in" in logs                          # the refusal itself is logged, with the address only


def test_error_bodies_never_echo_input_or_internals(srv, alice, monkeypatch):
    svr, api = srv
    monkeypatch.setattr(svr.rakazo, "_me", lambda ctx, inp: (_ for _ in ()).throw(RuntimeError("secret-internal-detail")))
    svr.rakazo._procs["me"] = svr.rakazo._me
    status, body, _ = api.rpc("me", {}, alice)
    assert status == 500 and "secret-internal-detail" not in json.dumps(body)


# --------------------------------------------------------------------------------------------- CLI
def test_cli_hash_password_reads_stdin_and_prints_only_the_hash(monkeypatch, capsys):
    from herdr_engineering import cli
    monkeypatch.setattr("sys.stdin", io.StringIO("a long enough password\n"))
    assert cli.main(["bridge", "hash-password"]) == 0
    out = capsys.readouterr()
    line = out.out.strip()
    assert out.err == "" and "a long enough password" not in line and "\n" not in line
    params = rb.parse_scrypt(line, "cli")
    assert params.n == 2 ** 15 and params.r == 8 and params.p == 1 and rb._derive("a long enough password", params) == params.digest


def test_cli_hash_password_rejects_short_passwords_and_takes_no_password_argument(monkeypatch, capsys):
    from herdr_engineering import cli
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert cli.main(["bridge", "hash-password"]) == 1
    assert "at least 12" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["bridge", "hash-password", "--password", "x"])


def test_the_cli_validates_the_section_before_serving():
    from herdr_engineering import cli
    src = open(cli.__file__).read()
    assert "rakazo_bridge_config(cp.get(\"rakazo_bridge\"))" in src and "rakazo_bridge_dsn=dsn" in src


# --------------------------------------------------------------------------------------------- revocation and the database
class FlakyTable(FakeTokenTable):
    def __init__(self):
        super().__init__()
        self.fail_delete, self.deletes = False, 0

    def delete(self, digest):
        self.deletes += 1
        if self.fail_delete:
            raise RuntimeError("db down")
        super().delete(digest)


def test_a_failed_database_delete_leaves_the_token_dead_and_is_retried_with_backoff():
    now = [1000.0]
    table = FlakyTable()
    store = rb.TokenStore(10_000, 5, table, clock=lambda: now[0])
    token = store.mint("a", "ip")
    other = store.mint("a", "ip")
    table.fail_delete = True
    assert store.revoke(token) == (True, False)               # dead at once, but the row is still owed a delete
    assert store.lookup(token) is None and store.valid(token) is False and store.pending_deletes == 1
    assert rb.TokenStore.digest(token) in table.rows
    calls = table.deletes
    store.lookup(other)                                       # inside the backoff window: no new attempt
    assert table.deletes == calls
    now[0] += 6
    store.lookup(other)                                       # the window passed: retried, fails, backs off longer
    assert table.deletes == calls + 1 and store.pending_deletes == 1
    now[0] += 6
    store.lookup(other)
    assert table.deletes == calls + 1                         # the doubled window (10 s) has not passed
    table.fail_delete = False
    now[0] += 6
    store.lookup(other)
    assert store.pending_deletes == 0 and rb.TokenStore.digest(token) not in table.rows
    assert store.lookup(other) == "a"


def test_evicted_and_expired_tokens_also_keep_a_tombstone_when_the_delete_fails():
    now = [0.0]
    table = FlakyTable()
    store = rb.TokenStore(100, 1, table, clock=lambda: now[0])
    first = store.mint("a", "ip")
    table.fail_delete = True
    second = store.mint("a", "ip")                            # evicts the first; its delete fails
    assert store.lookup(first) is None and store.lookup(second) == "a" and store.pending_deletes == 1
    now[0] = 500
    assert store.lookup(second) is None and store.pending_deletes == 2      # expiry: same
    table.fail_delete = False
    now[0] = 1000
    store.lookup("anything")
    assert store.pending_deletes == 0 and table.rows == {}


def test_sign_out_reports_a_failed_durable_delete_without_reviving_the_token(srv):
    svr, api = srv
    table = FlakyTable()
    svr.rakazo.tokens._p = table
    token = api.login()
    table.fail_delete = True
    status, body, _ = api.raw("POST", "/api/auth/sign-out", {}, {"authorization": f"Bearer {token}"})
    assert status == 500 and "session record" in json.loads(body)["message"]
    assert api.rpc("me", token=token)[0] == 401              # dead regardless
    table.fail_delete = False
    svr.rakazo.tokens._retry_at = 0
    assert svr.rakazo.tokens._flush(force=True) and svr.rakazo.tokens.pending_deletes == 0


def _stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_tokens_valid_after_kills_older_tokens_whatever_the_database_holds():
    from datetime import UTC, datetime, timedelta
    table = FlakyTable()
    old = rb.TokenStore(10_000, 5, table, clock=lambda: 1000.0)
    stale, kept = old.mint("a", "ip"), old.mint("b", "ip")
    table.fail_delete = True                                   # the revoking delete never reached the database
    old.revoke(stale)
    assert rb.TokenStore.digest(stale) in table.rows
    # the process restarts: the tombstone is gone, the row is back, the YAML cut-off still kills it
    table.fail_delete = False
    reborn = rb.TokenStore(10_000, 5, table, clock=lambda: 2000.0, cutoffs={"a": 1500.0})
    assert reborn.lookup(stale) is None and rb.TokenStore.digest(stale) not in table.rows
    assert reborn.lookup(kept) == "b"                          # another account has no cut-off
    fresh = reborn.mint("a", "ip")                             # signing in again after the cut-off works
    assert reborn.lookup(fresh) == "a"
    live = rb.TokenStore(10_000, 5, clock=lambda: 1000.0, cutoffs={"a": 900.0})
    t = live.mint("a", "ip")
    assert live.lookup(t) == "a"
    live._cutoffs["a"] = 1100.0
    live._clock = lambda: 1200.0
    assert live.lookup(t) is None                             # created at 1000 < 1100: dead without any database
    now = datetime.now(UTC)
    cfg = rb.rakazo_bridge_config(bridge_section(accounts=[{**ACCOUNTS[0], "tokens_valid_after": _stamp(now - timedelta(days=1))}]))
    assert abs(cfg["accounts"][0].tokens_valid_after - (now - timedelta(days=1)).timestamp()) < 2
    for bad in ("yesterday", "2026-01-01T00:00:00", 5, _stamp(now + timedelta(days=2))):
        with pytest.raises(ValueError, match="tokens_valid_after"):
            rb.rakazo_bridge_config(bridge_section(accounts=[{**ACCOUNTS[0], "tokens_valid_after": bad}]))


def test_the_cut_off_applies_through_the_server(make_server):
    from datetime import UTC, datetime, timedelta
    svr, api = make_server(bridge_section())
    token = api.login()
    assert api.rpc("me", token=token)[0] == 200
    cutoff = datetime.now(UTC) + timedelta(seconds=1)          # the YAML says: nothing issued before this counts
    svr.rakazo.tokens._cutoffs[svr.rakazo.accounts[0].account_id] = cutoff.timestamp()
    time.sleep(1.2)
    assert api.rpc("me", token=token)[0] == 401
    assert api.rpc("me", token=api.login())[0] == 200          # a new sign-in is after the cut-off


def test_database_calls_run_outside_the_lock_so_a_stuck_database_blocks_nobody_else():
    gate, entered = threading.Event(), threading.Event()

    class Blocking(FakeTokenTable):
        block = False

        def upsert(self, digest, rec):
            if self.block:
                entered.set()
                gate.wait(10)
                raise RuntimeError("timed out")
            super().upsert(digest, rec)

        def touch(self, digest, when, ip):
            raise RuntimeError("db down")

    table = Blocking()
    store = rb.TokenStore(10_000, 5, table)
    existing = store.mint("a", "ip")
    table.block = True
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(_try(lambda: store.mint("a", "ip"))), daemon=True)
    worker.start()
    assert entered.wait(5)
    t0 = time.time()
    assert store.lookup(existing) == "a" and store.valid(existing) and store.count() >= 1       # not blocked
    store.revoke("nothing")
    assert time.time() - t0 < 1.0
    gate.set()
    worker.join(5)
    assert isinstance(outcome[0], rb.TokenStoreError)
    assert store.count("a") == 1                              # the failed mint left no usable token behind
    store._tokens[rb.TokenStore.digest(existing)]["persisted"] = 0           # a failing touch is ignored
    assert store.lookup(existing) == "a"


def _try(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return exc


def test_postgres_connections_always_carry_timeouts_even_if_the_dsn_says_otherwise(monkeypatch):
    import sys
    import types
    seen = {}

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args=()): pass
        def fetchall(self): return []

    class Conn:
        def cursor(self): return Cur()
        def commit(self): pass
        def close(self): pass

    def connect(dsn, **kw):
        seen["dsn"], seen["kw"] = dsn, kw
        return Conn()
    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=connect))
    rb.PostgresTokenPersistence("postgresql://u:p@h/db?connect_timeout=999").delete("0" * 64)
    assert seen["kw"]["connect_timeout"] == 3
    assert "statement_timeout=3000" in seen["kw"]["options"] and "lock_timeout=3000" in seen["kw"]["options"]


# --------------------------------------------------------------------------------------------- hostile input, bounds
def test_hostile_bodies_get_a_generic_400_without_error_logs(srv, alice, caplog):
    _, api = srv
    caplog.set_level(logging.DEBUG)
    deep = b"[" * 200_000
    assert api.raw("POST", "/api/auth/sign-in/email", deep)[0] == 400
    status, body, _ = api.raw("POST", "/rpc/me", deep, {"authorization": f"Bearer {alice}"})
    assert status == 400 and json.loads(body)["json"]["code"] == "BAD_REQUEST"
    assert api.raw("POST", "/rpc/me", b"\xff\xfe", {"authorization": f"Bearer {alice}"})[0] == 400
    assert api.raw("POST", "/api/auth/sign-in/email", b"[1,2]")[0] == 400
    assert api.raw("POST", "/api/auth/sign-in/email", b"9" * 5000)[0] == 400
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_runs_list_reads_the_store_under_its_lock_and_is_bounded(srv, alice):
    svr, api = srv
    bot = new_bot(api, alice)
    store = svr.assistant.store
    from herdr_engineering.control_plane.assistant import AssistantRun
    user = svr.rakazo.accounts[0].principal_id
    for i in range(40):
        store.put_run(AssistantRun(run_id=f"run_x{i}", thread_id=bot["threadId"], assistant_id=bot["id"],
                                   tenant_id="default", user_id=user, task_id=f"t{i}", state="completed"))
    for i in range(60):
        store.put_run(AssistantRun(run_id=f"run_a{i}", thread_id=bot["threadId"], assistant_id=bot["id"],
                                   tenant_id="default", user_id=user, task_id=f"ta{i}", state="running"))
    held = []
    real = store.list_runs
    store.list_runs = lambda **kw: (held.append(store._lock._is_owned()), real(**kw))[1]
    assert len(api.ok("runs/list", {"filter": "recent"}, alice)["runs"]) == rb.MAX_RUN_ROWS
    assert len(api.ok("runs/list", {"filter": "active"}, alice)["runs"]) == rb.MAX_ACTIVE_ROWS
    assert held == [True, True]


def test_bots_per_account_are_capped_with_a_rakazo_shaped_error(srv):
    svr, api = srv
    alice, bob = api.login(), api.login("bob@example.com", PW_B)
    for i in range(rb.MAX_BOTS):
        new_bot(api, alice, f"bot {i}")
    status, body, _ = api.rpc("bots/create", {"name": "one too many"}, alice)
    err = body["json"]
    assert status == 400 and err["code"] == "BAD_REQUEST" and err["status"] == 400 and str(rb.MAX_BOTS) in err["message"]
    assert len(api.ok("bots/list", token=alice)) == rb.MAX_BOTS
    new_bot(api, bob)                                         # the cap is per account
