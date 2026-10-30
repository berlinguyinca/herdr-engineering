"""GitHub App authentication (github_auth) against a fake GitHub server.

The RSA key is generated per test run (never committed). Every test checks the
fail-closed contract: any failure is ``None`` (no partial credential), and no key,
JWT or token reaches a log line or an exception.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

cryptography = pytest.importorskip("cryptography")
pytest.importorskip("jwt")

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from herdr_engineering import github_auth as ga  # noqa: E402
from herdr_engineering.github import GitHubError, GitHubMonitor, GitHubProvider  # noqa: E402

APP_ID = 424242
WALL0 = 1_800_000_000.0


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def key_file(tmp_path, rsa_key):
    pem = rsa_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    path = tmp_path / "app.pem"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    os.chmod(path, 0o600)
    return str(path)


def _pem_body(rsa_key):
    pem = rsa_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return "".join(pem.strip().splitlines()[1:-1])


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeGitHub:
    """GET/POST on loopback. ``routes[(method, path)] = (status, headers, body)`` or a
    callable(handler, body) -> same (or None after writing its own answer)."""

    def __init__(self):
        self.routes = {}
        self.requests = []  # (method, path, Authorization, parsed body)
        self.lock = threading.Lock()
        self.issued = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                with fake.lock:
                    fake.requests.append((method, self.path, self.headers.get("Authorization"), body))
                route = fake.routes.get((method, self.path.split("?")[0]))
                if route is None:
                    route = (404, {}, b'{"message":"Not Found"}')
                if callable(route):
                    route = route(self, body)
                    if route is None:
                        return
                status, headers, payload = route
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except OSError:
                    pass

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]

    def token_route(self, iid, *, wall=lambda: WALL0, minutes=60, permissions=None, delay=0.0):
        def answer(_handler, body):
            if delay:
                time.sleep(delay)
            with self.lock:
                self.issued += 1
                n = self.issued
            expires = datetime.fromtimestamp(wall(), UTC) + timedelta(minutes=minutes)
            granted = permissions if permissions is not None else (body or {}).get("permissions")
            return 201, {}, json.dumps({
                "token": f"ghs_installation{iid}x{n:04d}abcdefghijklmnop",
                "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "permissions": granted, "repository_selection": "selected"}).encode()
        self.routes[("POST", f"/app/installations/{iid}/access_tokens")] = answer


@pytest.fixture()
def gh():
    fake = FakeGitHub()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def _fresh_module_state():
    ga._reset_for_tests()
    yield
    ga._reset_for_tests()


def _config(key_file, installations=None):
    return ga.parse_app_config({"app_id": APP_ID, "private_key_file": key_file,
                                "installations": installations or {"InferWeave": 11}})


def _tokens(gh, key_file, installations=None, *, clock=None, wall=None, timeout=2.0):
    return ga.GitHubAppTokens(_config(key_file, installations), api=gh.base,
                              clock=clock or Clock(), wall=wall or (lambda: WALL0), timeout=timeout)


def _b64(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


# ------------------------------------------------------------------ the JWT
def test_the_jwt_is_rs256_with_backdated_iat_and_a_valid_signature(gh, key_file, rsa_key):
    gh.token_route(11)
    token = _tokens(gh, key_file).token_for("InferWeave")
    assert token and token.startswith("ghs_installation11")
    method, path, auth, body = gh.posts()[0]
    assert (method, path) == ("POST", "/app/installations/11/access_tokens")
    assert body == {"permissions": {"contents": "read", "metadata": "read"}}
    assert auth.startswith("Bearer ")
    header_b64, claims_b64, sig_b64 = auth[len("Bearer "):].split(".")
    assert json.loads(_b64(header_b64)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_b64(claims_b64))
    assert claims == {"iat": int(WALL0) - 60, "exp": int(WALL0) + 540, "iss": str(APP_ID)}
    assert claims["exp"] - claims["iat"] <= 600
    # verified with cryptography directly, independent of the library that signed it
    rsa_key.public_key().verify(_b64(sig_b64), f"{header_b64}.{claims_b64}".encode(),
                                padding.PKCS1v15(), hashes.SHA256())


def test_only_read_permissions_are_ever_requested(gh, key_file):
    gh.token_route(11)
    tokens = _tokens(gh, key_file)
    for bad in ({"contents": "write"}, {"administration": "read"}, {"statuses": "read"},
                {"checks": "read"}, {}):
        with pytest.raises(ValueError):
            tokens.token_for("InferWeave", bad)
    assert gh.requests == []
    assert tokens.token_for("InferWeave", ga.CATALOG_PERMISSIONS)
    assert gh.posts()[0][3] == {"permissions": {"issues": "read", "metadata": "read"}}


# ------------------------------------------------------------------ owner selection
def test_each_owner_uses_its_own_installation_and_auto_is_discovered_once(gh, key_file):
    for iid in (11, 22, 33, 44):
        gh.token_route(iid)
    gh.routes[("GET", "/app/installations")] = (200, {}, json.dumps([
        {"id": 33, "account": {"login": "berlinguyinca"}},
        {"id": 44, "account": {"login": "other-auto"}},
        {"id": 99, "account": {"login": "not-configured"}}]).encode())
    tokens = _tokens(gh, key_file, {"InferWeave": 11, "metabolomics-us": "22",
                                    "berlinguyinca": "auto", "Other-Auto": "auto"})
    assert tokens.token_for("inferweave").startswith("ghs_installation11")
    assert tokens.token_for("METABOLOMICS-US").startswith("ghs_installation22")
    assert tokens.token_for("berlinguyinca").startswith("ghs_installation33")
    assert tokens.token_for("other-auto").startswith("ghs_installation44")
    assert [p for m, p, *_ in gh.requests if m == "GET"] == ["/app/installations?per_page=100&page=1"]
    assert tokens.token_for("not-configured") is None   # discovered, but never configured
    assert tokens.token_for("stranger") is None
    assert tokens.token_for(None) is None
    assert not any("/99/" in p for _m, p, *_ in gh.requests)


def test_an_auto_owner_missing_from_the_installations_gets_no_token(gh, key_file):
    gh.routes[("GET", "/app/installations")] = (200, {}, b"[]")
    tokens = _tokens(gh, key_file, {"berlinguyinca": "auto"})
    assert tokens.token_for("berlinguyinca") is None
    assert tokens.last_error["berlinguyinca"] == "not_found"


# ------------------------------------------------------------------ caching and expiry
def test_a_token_is_cached_and_refreshed_five_minutes_before_it_expires(gh, key_file):
    clock, wall = Clock(), Clock(WALL0)
    gh.token_route(11, wall=wall)
    tokens = _tokens(gh, key_file, clock=clock, wall=wall)
    first = tokens.token_for("InferWeave")
    assert tokens.token_for("InferWeave") == first and len(gh.posts()) == 1
    clock.t += 3600 - 301
    wall.t += 3600 - 301
    assert tokens.token_for("InferWeave") == first and len(gh.posts()) == 1
    clock.t += 2
    wall.t += 2
    second = tokens.token_for("InferWeave")
    assert second != first and len(gh.posts()) == 2


def test_permission_sets_are_cached_separately(gh, key_file):
    gh.token_route(11)
    tokens = _tokens(gh, key_file)
    a = tokens.token_for("InferWeave", ga.EVIDENCE_PERMISSIONS)
    b = tokens.token_for("InferWeave", ga.CATALOG_PERMISSIONS)
    assert a != b and len(gh.posts()) == 2
    assert tokens.token_for("InferWeave", ga.EVIDENCE_PERMISSIONS) == a


def test_validity_is_anchored_to_the_monotonic_clock_and_capped(gh, key_file):
    clock, wall = Clock(), Clock(WALL0)
    gh.token_route(11, wall=wall, minutes=110)          # a long expiry is capped at 1 h
    tokens = _tokens(gh, key_file, clock=clock, wall=wall)
    first = tokens.token_for("InferWeave")
    wall.t -= 10_000                                   # a wall clock jump cannot extend it
    clock.t += 3600 - 299
    assert tokens.token_for("InferWeave") != first


@pytest.mark.parametrize("minutes", [4, 5, -1, 121])
def test_an_expiry_too_close_past_or_too_far_is_refused(gh, key_file, minutes):
    gh.token_route(11, minutes=minutes)
    tokens = _tokens(gh, key_file)
    assert tokens.token_for("InferWeave") is None
    assert tokens.last_error["inferweave"] == "invalid_response"


def test_invalidate_forces_a_new_token(gh, key_file):
    gh.token_route(11)
    tokens = _tokens(gh, key_file)
    first = tokens.token_for("InferWeave")
    tokens.invalidate("inferweave")
    assert tokens.token_for("InferWeave") != first and len(gh.posts()) == 2


def test_a_current_long_installation_token_with_dash_and_dot_is_accepted(gh, key_file):
    # Real GitHub installation tokens are ~390 characters and contain "-" and ".".
    long_token = "ghs_" + "Ab9_-." * 64
    assert len(long_token) > 380
    def answer(_handler, body):
        return 201, {}, json.dumps({
            "token": long_token, "expires_at": "2027-01-15T09:00:00Z",
            "permissions": (body or {}).get("permissions"), "repository_selection": "selected"}).encode()
    gh.routes[("POST", "/app/installations/11/access_tokens")] = answer
    assert _tokens(gh, key_file).token_for("InferWeave") == long_token


# ------------------------------------------------------------------ failure modes
def _ok_body(**over):
    body = {"token": "ghs_" + "a" * 36, "expires_at": "2027-01-15T09:00:00Z",
            "permissions": {"contents": "read", "metadata": "read"}}
    body.update(over)
    return json.dumps(body).encode()


FAILURES = {
    "401": (401, {}, b'{"message":"Bad credentials"}'),
    "403": (403, {}, b'{"message":"Forbidden"}'),
    "404": (404, {}, b'{"message":"Not Found"}'),
    "422": (422, {}, b'{"message":"permissions not granted"}'),
    "500": (500, {}, b"oops"),
    "503": (503, {}, b""),
    "redirect": (302, {"Location": "/elsewhere"}, b""),
    "garbage": (201, {}, b"<html>not json</html>"),
    "list": (201, {}, b"[]"),
    "oversize": (201, {}, b"{" + b" " * (ga.MAX_RESPONSE + 10) + b"}"),
    "token_shape": (201, {}, _ok_body(token="ghp_personalTokenShape0123456789")),
    "token_space": (201, {}, _ok_body(token="ghs_abc def ghi jkl mno pqr")),
    "no_expiry": (201, {}, _ok_body(expires_at=None)),
    "bad_expiry": (201, {}, _ok_body(expires_at="tomorrow-ish")),
    "naive_expiry": (201, {}, _ok_body(expires_at="2027-01-15T09:00:00")),
    "write_grant": (201, {}, _ok_body(permissions={"contents": "write"})),
}


@pytest.mark.parametrize("name", sorted(FAILURES))
def test_every_failure_is_no_token_with_a_backoff(gh, key_file, name, caplog):
    caplog.set_level(logging.DEBUG)
    wall = Clock(datetime(2027, 1, 15, 8, 0, tzinfo=UTC).timestamp())
    gh.routes[("POST", "/app/installations/11/access_tokens")] = FAILURES[name]
    clock = Clock()
    tokens = _tokens(gh, key_file, clock=clock, wall=wall)
    with pytest.raises(ga.GitHubAuthError) as info:
        tokens.token_or_raise("InferWeave")
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert tokens.token_for("InferWeave") is None
    assert len(gh.posts()) == 1                    # the backoff holds: no second POST
    assert not any(p == "/elsewhere" for _m, p, *_ in gh.requests)  # redirect never followed
    clock.t += ga.FAILURE_BACKOFF_S + 1
    tokens.token_for("InferWeave")
    assert len(gh.posts()) == 2
    assert "ghs_" not in caplog.text and "Bearer" not in caplog.text


def test_a_hung_github_times_out_within_the_bound(gh, key_file):
    def hang(handler, _body):
        time.sleep(3)
        return 201, {}, _ok_body()
    gh.routes[("POST", "/app/installations/11/access_tokens")] = hang
    tokens = _tokens(gh, key_file, timeout=0.5)
    started = time.monotonic()
    assert tokens.token_for("InferWeave") is None
    assert time.monotonic() - started < 2.0
    assert tokens.last_error["inferweave"] == "timeout"


def test_the_timeout_is_never_above_ten_seconds(gh, key_file):
    assert _tokens(gh, key_file, timeout=60)._timeout == ga.CALL_TIMEOUT_S <= 10


@pytest.mark.parametrize("status,headers", [(429, {"Retry-After": "120"}),
                                            (403, {"X-RateLimit-Remaining": "0",
                                                   "X-RateLimit-Reset": str(int(WALL0) + 120)})])
def test_a_rate_limit_backs_off_for_its_reset(gh, key_file, status, headers):
    gh.routes[("POST", "/app/installations/11/access_tokens")] = (status, headers, b"{}")
    clock = Clock()
    tokens = _tokens(gh, key_file, clock=clock)
    assert tokens.token_for("InferWeave") is None
    assert tokens.last_error["inferweave"] == "rate_limited"
    clock.t += 100
    assert tokens.token_for("InferWeave") is None and len(gh.posts()) == 1
    clock.t += 25
    gh.token_route(11)
    assert tokens.token_for("InferWeave") and len(gh.posts()) == 2


def test_a_failed_discovery_backs_off(gh, key_file):
    gh.routes[("GET", "/app/installations")] = (500, {}, b"")
    clock = Clock()
    tokens = _tokens(gh, key_file, {"berlinguyinca": "auto"}, clock=clock)
    assert tokens.token_for("berlinguyinca") is None
    assert tokens.token_for("berlinguyinca") is None
    assert len([r for r in gh.requests if r[0] == "GET"]) == 1


# ------------------------------------------------------------------ the key file
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_a_group_or_world_readable_key_is_refused_before_any_request(gh, key_file, mode):
    os.chmod(key_file, mode)
    gh.token_route(11)
    tokens = _tokens(gh, key_file)
    assert tokens.token_for("InferWeave") is None
    assert tokens.last_error["inferweave"] == "key_permissions"
    assert gh.requests == []


def test_a_symlinked_or_missing_key_is_refused(gh, key_file, tmp_path):
    link = tmp_path / "link.pem"
    link.symlink_to(key_file)
    gh.token_route(11)
    tokens = _tokens(gh, str(link))
    assert tokens.token_for("InferWeave") is None
    assert tokens.last_error["inferweave"] == "key_permissions"
    missing = _tokens(gh, str(tmp_path / "missing.pem"))
    assert missing.token_for("InferWeave") is None
    assert missing.last_error["inferweave"] == "key_unreadable"
    assert gh.requests == []


def test_a_key_that_cannot_sign_is_refused_without_echoing_it(gh, tmp_path, caplog):
    path = tmp_path / "bad.pem"
    path.write_text("-----BEGIN PRIVATE KEY-----\nTOTALLYSECRETKEYBYTES\n-----END PRIVATE KEY-----\n")
    os.chmod(path, 0o600)
    tokens = _tokens(gh, str(path))
    with pytest.raises(ga.GitHubAuthError) as info:
        tokens.token_or_raise("InferWeave")
    assert info.value.reason == "key_invalid"
    assert "TOTALLYSECRET" not in str(info.value) + repr(info.value) + caplog.text
    assert gh.requests == []


@pytest.mark.parametrize("raw", [
    None, [], {"app_id": APP_ID}, {"app_id": 0, "private_key_file": "/k", "installations": {"a": 1}},
    {"app_id": True, "private_key_file": "/k", "installations": {"a": 1}},
    {"app_id": APP_ID, "private_key_file": "relative.pem", "installations": {"a": 1}},
    {"app_id": APP_ID, "private_key_file": "/k", "installations": {}},
    {"app_id": APP_ID, "private_key_file": "/k", "installations": {"a": "sometimes"}},
    {"app_id": APP_ID, "private_key_file": "/k", "installations": {"bad owner": 1}},
    {"app_id": APP_ID, "private_key_file": "/k", "installations": {"A": 1, "a": 2}},
    {"app_id": APP_ID, "private_key_file": "/k", "installations": {"a": 1}, "extra": 1},
])
def test_an_invalid_app_block_is_refused(raw):
    with pytest.raises(ga.GitHubAuthError) as info:
        ga.parse_app_config(raw)
    assert info.value.reason == "invalid_config"


# ------------------------------------------------------------------ resolution
def _reader(values):
    return lambda key: values.get(key)


def test_the_static_token_still_works_without_an_app_block():
    creds = ga.credentials(read=_reader({"github.token": "ghp_static_token_value"}))
    assert creds.mode == "token"
    assert creds.token_for("anyone") == creds.token_for("InferWeave") == "ghp_static_token_value"
    assert ga.credentials(read=_reader({})).token_for("x") is None
    assert ga.credentials(read=_reader({"github.token": "bad token"})).token_for("x") is None


def test_the_app_wins_over_the_token_and_warns_once(gh, key_file, caplog):
    caplog.set_level(logging.WARNING)
    gh.token_route(11, wall=time.time)            # credentials() runs on the real clocks
    values = {"github.token": "ghp_static_token_value",
              "github.app": {"app_id": APP_ID, "private_key_file": key_file,
                             "installations": {"InferWeave": 11}}}
    first = ga.credentials(read=_reader(values), api=gh.base)
    second = ga.credentials(read=_reader(values), api=gh.base)
    assert first is second and first.mode == "app"  # one shared token cache
    token = first.token_for("InferWeave")
    assert token.startswith("ghs_") and token != "ghp_static_token_value"
    warnings = [r for r in caplog.records if "both github.app and github.token" in r.getMessage()]
    assert len(warnings) == 1
    assert "ghp_static" not in caplog.text and token not in caplog.text


def test_an_invalid_app_block_fails_closed_without_falling_back_to_the_token():
    creds = ga.credentials(read=_reader({"github.token": "ghp_static_token_value",
                                         "github.app": {"app_id": "nope"}}))
    assert creds.mode is None and creds.token_for("InferWeave") is None


def test_the_app_identity_is_stable_across_token_rotation(gh, key_file):
    clock = Clock()
    gh.token_route(11)
    tokens = _tokens(gh, key_file, clock=clock)
    identity = tokens.identity
    first = tokens.token_for("InferWeave")
    clock.t += 4000
    assert tokens.token_for("InferWeave") != first
    assert tokens.identity == identity
    assert identity != _tokens(gh, key_file, {"InferWeave": 12}).identity


# ------------------------------------------------------------------ secrets
def test_no_secret_reaches_logs_reprs_or_errors(gh, key_file, rsa_key, caplog):
    caplog.set_level(logging.DEBUG)
    gh.token_route(11)
    tokens = _tokens(gh, key_file)
    token = tokens.token_for("InferWeave")
    jwt_value = gh.posts()[0][2].split(" ", 1)[1]
    gh.routes[("POST", "/app/installations/11/access_tokens")] = (401, {}, b'{"message":"x"}')
    tokens.invalidate("InferWeave")
    tokens._retry.clear()
    with pytest.raises(ga.GitHubAuthError) as info:
        tokens.token_or_raise("InferWeave")
    text = "\n".join([caplog.text, str(info.value), repr(info.value), repr(tokens),
                      repr(tokens.config), json.dumps(tokens.last_error)])
    for secret in (token, jwt_value, _pem_body(rsa_key)[:40], "PRIVATE KEY"):
        assert secret not in text


# ------------------------------------------------------------------ single flight
def test_concurrent_callers_share_one_refresh(gh, key_file):
    gh.token_route(11, delay=0.3)
    tokens = _tokens(gh, key_file)
    results, barrier = [], threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(tokens.token_for("InferWeave"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(gh.posts()) == 1
    assert len(results) == 8 and len(set(results)) == 1 and results[0].startswith("ghs_")


def test_concurrent_callers_share_one_failure(gh, key_file):
    def slow_fail(_h, _b):
        time.sleep(0.3)
        return 500, {}, b""
    gh.routes[("POST", "/app/installations/11/access_tokens")] = slow_fail
    tokens = _tokens(gh, key_file)
    results = []
    threads = [threading.Thread(target=lambda: results.append(tokens.token_for("InferWeave")))
               for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == [None] * 6 and len(gh.posts()) == 1


# ------------------------------------------------------------------ the release evidence
def test_release_evidence_uses_the_repo_owners_installation_token(gh, key_file):
    from herdr_engineering.control_plane.deployment_coordinator import ReleaseEvidence
    from herdr_engineering.control_plane.deployment_definitions import Definition
    from herdr_engineering.control_plane.deployment_evidence import GitHubReleaseEvidence

    repo, target = "InferWeave/inferweave", "a" * 40
    track = (repo, "main", "ci/woodpecker/push/rust")
    gh.token_route(11)
    gh.routes[("GET", f"/repos/{repo}/releases/tags/node-ci-latest")] = (200, {}, json.dumps(
        {"assets": [{"id": 7, "name": "inferweave.commit",
                     "url": f"{gh.base}/repos/{repo}/releases/assets/7"}]}).encode())
    gh.routes[("GET", f"/repos/{repo}/releases/assets/7")] = (200, {}, (target + "\n").encode())
    gh.routes[("GET", f"/repos/{repo}/compare/main...{target}")] = (200, {}, json.dumps(
        {"status": "behind", "ahead_by": 0, "behind_by": 2}).encode())
    defn = Definition(component="hive-gateway", stage=1, order=0, host="hive", updater="u",
                      follows={"repo": repo, "tag": "node-ci-latest", "asset": "inferweave",
                               "branch": "main", "ci_context": track[2]},
                      restart="gateway-cycle", gpu=False,
                      health={"probe": "served-request", "timeout_s": 600, "hold_s": 600},
                      rollback={"strategy": "previous-version"})
    tokens = _tokens(gh, key_file, {"InferWeave": 11, "metabolomics-us": 22})
    source = GitHubReleaseEvidence(lambda: {"hive-gateway": defn}, credentials=lambda: tokens,
                                   api=gh.base, download_hosts={"127.0.0.1"},
                                   executor=lambda fn: fn())
    assert source(track, target, None) == ReleaseEvidence(target, True)
    posts = gh.posts()
    assert [p for _m, p, *_ in posts] == ["/app/installations/11/access_tokens"]
    assert posts[0][3] == {"permissions": {"contents": "read", "metadata": "read"},
                           "repositories": ["inferweave"]}     # limited to the one repository
    issued = tokens.token_for("InferWeave", repositories=["inferweave"])
    reads = [a for m, p, a, _b in gh.requests if m == "GET"]
    assert reads and all(a == f"Bearer {issued}" for a in reads)


def test_release_evidence_is_unknown_when_no_installation_token(gh, key_file):
    from herdr_engineering.control_plane.deployment_coordinator import ReleaseEvidence
    from herdr_engineering.control_plane.deployment_definitions import Definition
    from herdr_engineering.control_plane.deployment_evidence import GitHubReleaseEvidence

    repo = "elsewhere/inferweave"
    track = (repo, "main", "ci")
    defn = Definition(component="c", stage=1, order=0, host="hive", updater="u",
                      follows={"repo": repo, "tag": "t", "asset": "a", "branch": "main",
                               "ci_context": "ci"},
                      restart="gateway-cycle", gpu=False,
                      health={"probe": "served-request", "timeout_s": 600, "hold_s": 600},
                      rollback={"strategy": "previous-version"})
    tokens = _tokens(gh, key_file)
    source = GitHubReleaseEvidence(lambda: {"c": defn}, credentials=lambda: tokens, api=gh.base,
                                   executor=lambda fn: fn())
    assert source(track, "a" * 40, None) == ReleaseEvidence()
    assert source.status_for(track)["error"] == "no GitHub App installation token"
    assert gh.requests == []


def test_release_evidence_drops_the_owners_token_on_a_401(gh, key_file):
    from herdr_engineering.control_plane.deployment_definitions import Definition
    from herdr_engineering.control_plane.deployment_evidence import GitHubReleaseEvidence

    repo = "InferWeave/inferweave"
    track = (repo, "main", "ci")
    gh.token_route(11)
    gh.routes[("GET", f"/repos/{repo}/releases/tags/t")] = (401, {}, b"{}")
    defn = Definition(component="c", stage=1, order=0, host="hive", updater="u",
                      follows={"repo": repo, "tag": "t", "asset": "a", "branch": "main",
                               "ci_context": "ci"},
                      restart="gateway-cycle", gpu=False,
                      health={"probe": "served-request", "timeout_s": 600, "hold_s": 600},
                      rollback={"strategy": "previous-version"})
    tokens = _tokens(gh, key_file)
    source = GitHubReleaseEvidence(lambda: {"c": defn}, credentials=lambda: tokens, api=gh.base,
                                   executor=lambda fn: fn())
    source(track, "a" * 40, None)
    assert not tokens._tokens  # the rejected token is gone; the next refresh mints a new one


# ------------------------------------------------------------------ the GitHub cache
class StubApp:
    """A ``token_for`` provider: a fresh token per call (as if rotated), recording owners."""

    mode = "app"

    def __init__(self, owners=("inferweave", "berlinguyinca")):
        self.owners = tuple(owners)
        self.calls, self.invalidated, self.n = [], [], 0
        self.config = type("C", (), {"app_id": APP_ID})()
        self.fail = set()

    identity = "stable-app-identity"

    def token_or_raise(self, owner, permissions=None):
        self.calls.append((owner, dict(permissions or {})))
        if owner not in self.owners or owner in self.fail:
            raise ga.GitHubAuthError("unknown_owner")
        self.n += 1
        return f"ghs_{owner}_{self.n:04d}_rotating_token"

    def token_for(self, owner, permissions=None):
        try:
            return self.token_or_raise(owner, permissions)
        except ga.GitHubAuthError:
            return None

    def invalidate(self, owner):
        self.invalidated.append(owner)


class GraphQL:
    """A fake GraphQL client: ``answer(query, variables) -> payload``; records bearers."""

    def __init__(self, answer):
        self.answer, self.seen = answer, []

    def open(self, request, timeout=None):
        body = json.loads(request.data)
        self.seen.append((request.get_header("Authorization"), body["variables"]))
        payload = self.answer(body["query"], body["variables"])
        if isinstance(payload, Exception):
            raise payload
        return _Resp(json.dumps(payload).encode())


class _Resp:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.raw


def _repo(name):
    return {"id": f"id-{name}", "nameWithOwner": name, "url": f"https://github.com/{name}",
            "isPrivate": True, "isArchived": False, "hasIssuesEnabled": True,
            "viewerPermission": None, "primaryLanguage": None, "issues": {"totalCount": 0},
            "newest": {"nodes": []}}


def _owner_page(nodes, nxt=None):
    return {"data": {"repositoryOwner": {"login": "x", "repositories": {
        "totalCount": len(nodes), "pageInfo": {"hasNextPage": nxt is not None, "endCursor": nxt},
        "nodes": nodes}}, "rateLimit": {"remaining": 4000, "resetAt": None, "cost": 1}}}


def test_the_cache_lists_each_configured_owner_with_its_own_token():
    pages = {("inferweave", None): _owner_page([_repo("InferWeave/a")], "c1"),
             ("inferweave", "c1"): _owner_page([_repo("InferWeave/b"), _repo("someone/fork")]),
             ("berlinguyinca", None): _owner_page([_repo("berlinguyinca/c")])}
    client = GraphQL(lambda q, v: pages[(v["login"], v["cursor"])])
    app = StubApp()
    provider = GitHubProvider(credentials=app, client=client)
    assert provider.available and provider.credential_identity == "stable-app-identity"
    names, cursor = [], None
    while True:
        account, connection = provider.repositories_page(cursor)
        assert account == f"github-app:{APP_ID}"
        names += [n["nameWithOwner"] for n in connection["nodes"]]
        if not connection["pageInfo"]["hasNextPage"]:
            break
        cursor = connection["pageInfo"]["endCursor"]
    assert names == ["InferWeave/a", "InferWeave/b", "berlinguyinca/c"]  # the foreign row dropped
    assert [o for o, _p in app.calls] == ["inferweave", "inferweave", "berlinguyinca"]
    assert all(p == ga.CATALOG_PERMISSIONS for _o, p in app.calls)
    assert [auth.split("_")[1] for auth, _v in client.seen] == ["inferweave", "inferweave",
                                                                "berlinguyinca"]
    with pytest.raises(GitHubError):
        provider.repositories_page("9:zzz")


def test_cache_issue_reads_route_by_repository_owner_and_fail_closed_for_others():
    issues = {"data": {"repository": {"issues": {"totalCount": 0, "pageInfo": {
        "hasNextPage": False, "endCursor": None}, "nodes": []}}, "rateLimit": {}}}
    client = GraphQL(lambda q, v: issues)
    app = StubApp()
    provider = GitHubProvider(credentials=app, client=client)
    provider.issues_page("github.com/berlinguyinca/c")
    assert app.calls[-1][0] == "berlinguyinca"
    with pytest.raises(GitHubError) as info:
        provider.issues_page("github.com/stranger/repo")
    assert info.value.reason == "unavailable" and len(client.seen) == 1  # no request without a token


def test_cache_projects_use_the_owner_token_and_items_need_a_known_owner():
    projects = {"data": {"repositoryOwner": {"login": "InferWeave", "projectsV2": {
        "totalCount": 1, "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [{"id": "PVT_known", "number": 1, "title": "t", "url": "u", "closed": False,
                   "updatedAt": None, "items": {"totalCount": 0}, "field": None}]}},
        "rateLimit": {}}}
    items = {"data": {"node": {"items": {"totalCount": 0, "pageInfo": {
        "hasNextPage": False, "endCursor": None}, "nodes": []}}, "rateLimit": {}}}
    client = GraphQL(lambda q, v: items if "id" in v else projects)
    app = StubApp()
    provider = GitHubProvider(credentials=app, client=client)
    provider.projects_page("InferWeave")
    assert app.calls[-1] == ("inferweave", ga.PROJECTS_PERMISSIONS)
    provider.project_items_page("PVT_known")
    assert app.calls[-1] == ("inferweave", ga.PROJECTS_PERMISSIONS)
    with pytest.raises(GitHubError) as info:
        provider.project_items_page("PVT_unknown")
    assert info.value.reason == "not_found"


def test_cache_drops_an_owners_token_on_a_401_and_redacts_the_used_token():
    import urllib.error
    app = StubApp()
    err = urllib.error.HTTPError("https://api.github.com/graphql", 401, "x", {}, None)
    provider = GitHubProvider(credentials=app, client=GraphQL(lambda q, v: err))
    with pytest.raises(GitHubError) as info:
        provider.issues_page("github.com/InferWeave/a")
    assert info.value.reason == "authentication" and app.invalidated == ["inferweave"]
    assert "ghs_" not in str(info.value)


def test_the_monitor_keeps_its_catalog_across_app_token_rotation():
    """Installation tokens rotate hourly; the catalog identity must not (no purge per hour)."""
    app = StubApp()
    first = GitHubProvider(credentials=app, client=GraphQL(lambda q, v: {}))
    app.token_for("inferweave")                    # a rotation happened in between
    second = GitHubProvider(credentials=app, client=GraphQL(lambda q, v: {}))
    assert first.credential_identity == second.credential_identity
    monitor = GitHubMonitor(provider_factory=lambda: GitHubProvider(credentials=app,
                                                                    client=GraphQL(lambda q, v: {})))
    monitor._view["account"] = f"github-app:{APP_ID}"
    assert monitor._project_owners(set(), {"inferweave"}) == ["inferweave"]  # no App pseudo-owner


def test_the_cache_still_uses_a_static_token():
    creds = ga.credentials(read=_reader({"github.token": "ghp_static_token_value"}))
    client = GraphQL(lambda q, v: {"data": {"viewer": {"login": "me", "repositories": {
        "totalCount": 0, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}},
        "rateLimit": {}})
    provider = GitHubProvider(credentials=creds, client=client)
    assert provider.repositories_page()[0] == "me"
    assert client.seen[0][0] == "Bearer ghp_static_token_value"


# ------------------------------------------------------------------ review follow-ups
def test_a_token_can_be_limited_to_repositories_and_is_cached_per_repo_set(gh, key_file):
    gh.token_route(11)
    tokens = _tokens(gh, key_file)
    a = tokens.token_for("InferWeave", repositories=["inferweave"])
    assert gh.posts()[0][3]["repositories"] == ["inferweave"]
    assert tokens.token_for("InferWeave", repositories="inferweave") == a
    b = tokens.token_for("InferWeave", repositories=["other"])
    c = tokens.token_for("InferWeave")
    assert len({a, b, c}) == 3 and len(gh.posts()) == 3
    assert "repositories" not in gh.posts()[2][3]
    for bad in ([], ["a/b"], [".."], ["x"] * 101):
        with pytest.raises(ValueError):
            tokens.token_for("InferWeave", repositories=bad)


@pytest.mark.parametrize("mode,ok", [(0o600, True), (0o400, True), (0o700, False),
                                     (0o500, False), (0o660, False)])
def test_only_mode_0600_or_0400_is_accepted(gh, key_file, mode, ok):
    os.chmod(key_file, mode)
    gh.token_route(11)
    tokens = _tokens(gh, key_file)
    try:
        assert (tokens.token_for("InferWeave") is not None) is ok
    finally:
        os.chmod(key_file, 0o600)
    if not ok:
        assert tokens.last_error["inferweave"] == "key_permissions" and gh.requests == []


def test_a_key_owned_by_another_user_is_refused_except_for_root(gh, key_file, monkeypatch):
    gh.token_route(11)
    real = os.geteuid()
    monkeypatch.setattr(ga.os, "geteuid", lambda: real + 1)
    tokens = _tokens(gh, key_file)
    assert tokens.token_for("InferWeave") is None
    assert tokens.last_error["inferweave"] == "key_permissions" and gh.requests == []
    monkeypatch.setattr(ga.os, "geteuid", lambda: 0)   # rootful docker: the container is root
    assert _tokens(gh, key_file).token_for("InferWeave")


def test_a_key_under_a_symlinked_directory_is_refused(gh, key_file, tmp_path):
    linked = tmp_path / "linked-dir"
    linked.symlink_to(os.path.dirname(key_file))
    gh.token_route(11)
    tokens = _tokens(gh, str(linked / "app.pem"))
    assert tokens.token_for("InferWeave") is None
    assert tokens.last_error["inferweave"] == "key_permissions" and gh.requests == []


def test_a_missing_jwt_library_has_its_own_reason(gh, key_file, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "jwt", None)
    gh.token_route(11)
    tokens = _tokens(gh, key_file)
    with pytest.raises(ga.GitHubAuthError) as info:
        tokens.token_or_raise("InferWeave")
    assert info.value.reason == "jwt_library_missing" and gh.requests == []


def test_no_proxy_is_used_for_tokens_or_the_cache(gh, key_file):
    import urllib.request
    for opener in (_tokens(gh, key_file)._opener,
                   GitHubProvider(token="ghp_static_token_value")._client):
        # ProxyHandler({}) replaces the default (environment) proxy handler and adds none
        assert not [h for h in opener.handlers
                    if isinstance(h, urllib.request.ProxyHandler) and h.proxies]
