"""Lab shares: stateless HMAC sessions, the Cognito allow-list, the viewer page, its static files and the session API."""
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from herdr_engineering.control_plane import share_public
from herdr_engineering.control_plane.collab_cognito import CognitoAuthenticator
from herdr_engineering.control_plane.share_bundle import remove_state, write_state
from herdr_engineering.control_plane.share_lab import authorise_lab
from herdr_engineering.control_plane.share_session import check_session, mint_session
from tests.control_plane.test_share_public import CSP, EXPOSE, FILES, OTHER_TOKEN, TOKEN, Env

POOL = "us-west-2_TestPool1"
ISS = f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}"
CLI_CLIENT = "cli-client"
WEB_CLIENT = "web-client"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SECRET = b"k" * 48
NOW0 = 1_900_000_000.0          # Env's injected clock (PyJWT checks exp against the real clock, which is earlier)
STATIC_DIR = Path(share_public.__file__).parent / "share_static"
VIEWER_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self' https://auth.lab.example; "
              "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def jwks_doc(key=KEY, kid="k1"):
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {"keys": [{**d, "kid": kid, "alg": "RS256", "use": "sig"}]}


def _encode(claims, key, kid):
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def access(user="alice", *, groups=(), client=WEB_CLIENT, exp=None, key=KEY, kid="k1", **over):
    claims = {"iss": ISS, "sub": f"sub-{user}", "username": user, "token_use": "access", "client_id": client,
              "cognito:groups": list(groups), "exp": NOW0 + 3600 if exp is None else exp, "iat": time.time()}
    claims.update(over)
    return _encode(claims, key, kid)


def ident(user="alice", *, email="Alice@Lab.example", verified=True, aud=WEB_CLIENT, exp=None, key=KEY, kid="k1",
          **over):
    claims = {"iss": ISS, "sub": f"sub-{user}", "cognito:username": user, "token_use": "id", "aud": aud,
              "email": email, "email_verified": verified, "exp": NOW0 + 3600 if exp is None else exp,
              "iat": time.time()}
    claims.update(over)
    return _encode(claims, key, kid)


LAB_STATE = {"share_id": "shr-1", "audience": "lab", "allow_emails": ["alice@lab.example", "carol@lab.example"],
             "allow_groups": ["Readers"]}


def authenticator():
    return CognitoAuthenticator(pool_id=POOL, client_ids=[CLI_CLIENT, WEB_CLIENT], required_group="unused",
                                jwks_fetch=lambda: jwks_doc(), open_signup=True)


# ----------------------------------------------------------------------------- sessions

def test_session_round_trip_and_shape():
    s = mint_session(SECRET, "shr-1", "alice", NOW0 + 100)
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", s) and "/" not in s and len(s) <= 512
    assert check_session(SECRET, "shr-1", s, NOW0) == {"identity": "alice", "exp": NOW0 + 100}
    assert check_session(SECRET, "shr-1", s, NOW0 + 99.9) is not None


def test_session_rejections():
    s = mint_session(SECRET, "shr-1", "alice", NOW0 + 100)
    assert check_session(SECRET, "shr-2", s, NOW0) is None                       # another share
    assert check_session(b"x" * 48, "shr-1", s, NOW0) is None                    # another secret
    assert check_session(SECRET, "shr-1", s, NOW0 + 100) is None                 # expired (exp is exclusive)
    assert check_session(SECRET, "shr-1", s, NOW0 + 10_000) is None
    for i in range(len(s)):                                                      # every single-character tamper
        for repl in ("A", "_"):
            if s[i] != repl:
                assert check_session(SECRET, "shr-1", s[:i] + repl + s[i + 1:], NOW0) is None, i
    for cut in range(len(s)):                                                    # every truncation
        assert check_session(SECRET, "shr-1", s[:cut], NOW0) is None
    assert check_session(SECRET, "shr-1", s + "A", NOW0) is None
    bob = mint_session(SECRET, "shr-1", "bob", NOW0 + 100)
    a_id, a_exp, a_mac = s.split(".")
    b_id, b_exp, b_mac = bob.split(".")
    assert check_session(SECRET, "shr-1", ".".join((b_id, a_exp, a_mac)), NOW0) is None   # identity swap
    assert check_session(SECRET, "shr-1", ".".join((a_id, str(int(a_exp) + 1000), a_mac)), NOW0) is None
    for garbage in (None, 1, b"bytes", "", "...", "a.b.c", "x" * 100_000, s.replace(".", "/"), s + "\n"):
        assert check_session(SECRET, "shr-1", garbage, NOW0) is None
    assert check_session(b"", "shr-1", s, NOW0) is None
    assert check_session(SECRET, None, s, NOW0) is None


def test_mint_refuses_bad_inputs():
    for args in ((b"", "shr-1", "alice", NOW0), (b"short", "shr-1", "alice", NOW0), (SECRET, "", "alice", NOW0),
                 (SECRET, "shr-1", "", NOW0), (SECRET, "shr-1", "a" * 300, NOW0), (SECRET, "shr-1", "a\x00b", NOW0),
                 (SECRET, "shr-1", "alice", float("nan")), (SECRET, "shr-1", "alice", -1)):
        with pytest.raises(ValueError):
            mint_session(*args)


# ----------------------------------------------------------------------------- authorise_lab

def test_email_allows_without_any_group():
    got = authorise_lab(LAB_STATE, access("alice"), ident("alice"), authenticator())
    assert got == {"identity": "alice", "email": "alice@lab.example", "exp": NOW0 + 3600}


def test_group_claim_allows_and_is_case_sensitive():
    tok = access("dave", groups=("Readers",))
    got = authorise_lab(LAB_STATE, tok, ident("dave", email="dave@else.example"), authenticator())
    assert got is not None and got["identity"] == "dave" and got["email"] == "dave@else.example"
    tok = access("dave", groups=("readers", "Admins"))
    assert authorise_lab(LAB_STATE, tok, ident("dave", email="dave@else.example"), authenticator()) is None


def test_cli_client_is_allow_listed_too():
    tok = access("alice", client=CLI_CLIENT)
    assert authorise_lab(LAB_STATE, tok, ident("alice", aud=CLI_CLIENT), authenticator()) is not None


@pytest.mark.parametrize("name,make", [
    ("in pool, not on list", lambda: (access("eve"), ident("eve", email="eve@lab.example"))),
    ("unverified email", lambda: (access("alice"), ident("alice", verified=False))),
    ("unverified email, group member", lambda: (access("alice", groups=("Readers",)), ident("alice", verified=False))),
    ("email_verified string false", lambda: (access("alice"), ident("alice", verified="false"))),
    ("id token of another user", lambda: (access("eve"), ident("alice"))),
    ("id token of another user, eve in group", lambda: (access("eve", groups=("Readers",)), ident("alice"))),
    ("access token from a foreign client", lambda: (access("alice", client="other-client"), ident("alice"))),
    ("id token for a foreign client", lambda: (access("alice"), ident("alice", aud="other-client"))),
    ("expired access token", lambda: (access("alice", exp=time.time() - 5), ident("alice"))),
    ("expired id token", lambda: (access("alice"), ident("alice", exp=time.time() - 5))),
    ("wrong issuer", lambda: (access("alice", iss=ISS + "x"), ident("alice"))),
    ("wrong issuer on id token", lambda: (access("alice"), ident("alice", iss=ISS + "x"))),
    ("signed by another key", lambda: (access("alice", key=OTHER_KEY), ident("alice"))),
    ("id token as access token", lambda: (ident("alice"), ident("alice"))),
    ("access token as id token", lambda: (access("alice"), access("alice"))),
    ("no email", lambda: (access("alice"), ident("alice", email=None))),
])
def test_identity_mix_ups_are_refused(name, make):
    a, i = make()
    assert authorise_lab(LAB_STATE, a, i, authenticator()) is None, name


def test_garbage_never_raises():
    good_a, good_i = access("alice"), ident("alice")
    auth = authenticator()
    for a, i in ((None, good_i), (good_a, None), (1, good_i), (b"x", good_i), ("", ""), ("x" * 9000, good_i),
                 (good_a, "x" * 9000), (good_a + "x" * 9000, good_i), ("é", good_i), ("not.a.jwt", good_i)):
        assert authorise_lab(LAB_STATE, a, i, auth) is None
    for state in (None, [], {}, dict(LAB_STATE, audience="public"), dict(LAB_STATE, allow_emails="alice@lab.example"),
                  dict(LAB_STATE, allow_groups=None, allow_emails=None), dict(LAB_STATE, allow_emails=[None, 3])):
        assert authorise_lab(state, good_a, good_i, auth) is None
    assert authorise_lab(LAB_STATE, good_a, good_i, None) is None


# ----------------------------------------------------------------------------- the server

def lab_config(tmp_path, **over):
    sync = tmp_path / "sync"
    sync.mkdir(exist_ok=True)
    (sync / "jwks.json").write_text(json.dumps(jwks_doc()))
    cfg = dict(sync_dir=sync, pool_id=POOL, client_ids=(CLI_CLIENT,), web_client_id=WEB_CLIENT,
               cognito_domain="https://auth.lab.example", public_url="https://share.example/", signing_secret=SECRET,
               session_seconds=14400)
    cfg.update(over)
    return cfg


LAB_ALLOW = {"allow_emails": ["alice@lab.example"], "allow_groups": ["Readers"]}


@pytest.fixture
def lab(tmp_path):
    e = Env(tmp_path, audience="lab", expires_in=None, config_extra=lab_config(tmp_path), state_extra=LAB_ALLOW)
    yield e
    e.close()


def reference(env):
    status, _, body = env.request("GET", "/nothing-here")
    assert status == 404
    return body


def open_session(env, a=None, i=None, token=TOKEN, extra=None):
    headers = {"Authorization": "Bearer " + (access("alice") if a is None else a),
               "X-Id-Token": ident("alice") if i is None else i, "Content-Length": "0"}
    headers.update(extra or {})
    return env.request("POST", f"/s/{token}/session", headers)


def test_viewer_page_and_callback_served_with_strict_csp(lab):
    for path in (f"/s/{TOKEN}/", "/s/callback", "/s/callback?code=abc-123&state=xyz_9"):
        status, h, body = lab.request("GET", path)
        assert status == 200, path
        assert h["content-type"] == "text/html; charset=utf-8"
        assert h["content-security-policy"] == VIEWER_CSP
        assert h["x-content-type-options"] == "nosniff"
        assert h["cache-control"] == "no-store"
        assert h["referrer-policy"] == "no-referrer"
        assert "access-control-allow-origin" not in h
        assert body == (STATIC_DIR / "viewer.html").read_bytes()
        assert h["content-length"] == str(len(body))
    html = (STATIC_DIR / "viewer.html").read_text()
    assert '<script src="/s/static/share.js"' in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline script"
    assert not re.search(r"\son\w+\s*=", html, re.I), "inline handler"
    assert not re.search(r"\sstyle\s*=|<style", html, re.I), "inline style"
    status, h, body = lab.request("HEAD", f"/s/{TOKEN}/")
    assert status == 200 and body == b"" and h["content-security-policy"] == VIEWER_CSP


def test_static_allow_list(lab, tmp_path):
    status, h, body = lab.request("GET", "/s/static/share.js")
    assert status == 200 and h["content-type"] == "text/javascript; charset=utf-8"
    assert h["content-security-policy"] == VIEWER_CSP and h["x-content-type-options"] == "nosniff"
    assert body == (STATIC_DIR / "share.js").read_bytes()
    status, h, body = lab.request("GET", "/s/static/share.css")
    assert status == 200 and h["content-type"] == "text/css; charset=utf-8"
    assert body == (STATIC_DIR / "share.css").read_bytes()
    status, h, body = lab.request("GET", "/s/static/config.json")
    assert status == 200 and h["content-type"] == "application/json" and h["cache-control"] == "no-store"
    assert json.loads(body) == {"cognito_domain": "https://auth.lab.example", "client_id": WEB_CLIENT,
                                "redirect_uri": "https://share.example/s/callback"}
    ref = reference(lab)
    for path in ("/s/static/../static/share.js", "/s/static/%2e%2e/share.js", "/s/static/", "/s/static",
                 "/s/static/viewer.htm", "/s/static/Share.js", "/s/static/share.js/", "/s/static/sub/share.js",
                 "/s/static/share.js%00", "/s/static/share%2ejs", "/s/static/.", "/s/static/..", "/s/static/secret.txt",
                 "/s/callback/", "/s/callback/x", "/s/callbacks"):
        status, h, body = lab.request("GET", path)
        assert status == 404 and body == ref, path
    for method in ("OPTIONS", "POST", "PUT"):
        for path in ("/s/static/share.js", "/s/callback"):
            status, _, body = lab.request(method, path)
            assert status == 404 and body == ref, (method, path)


def test_static_dir_extra_files_are_not_reachable(tmp_path):
    static = tmp_path / "static"
    shutil.copytree(STATIC_DIR, static)
    (static / "secret.txt").write_text("secret")
    (static / "sub").mkdir()
    e = Env(tmp_path, audience="lab", expires_in=None, config_extra=lab_config(tmp_path, static_dir=static),
            state_extra=LAB_ALLOW)
    try:
        ref = reference(e)
        for path in ("/s/static/secret.txt", "/s/static/sub", "/s/static/sub/", "/s/static/share.js/.."):
            assert e.request("GET", path)[2] == ref, path
        (static / "share.css").unlink()
        (static / "share.css").symlink_to(static / "secret.txt")
        assert e.request("GET", "/s/static/share.css")[2] == ref            # a symlink is never followed
        assert e.request("GET", "/s/static/share.js")[0] == 200
    finally:
        e.close()


def test_session_flow_serves_files_under_the_session_base(lab, capfd):
    a, i = access("alice"), ident("alice")
    status, h, body = open_session(lab, a, i)
    assert status == 200, body
    assert h["content-type"] == "application/json" and h["cache-control"] == "no-store"
    assert h["x-content-type-options"] == "nosniff"
    assert "access-control-allow-origin" not in h
    got = json.loads(body)
    assert set(got) == {"base", "entry", "expires_at"}
    assert got["entry"] == "index.html"
    assert got["expires_at"] == NOW0 + 3600          # capped at the access token's exp (< session_seconds)
    assert re.fullmatch(rf"/s/{re.escape(TOKEN)}/k/[A-Za-z0-9_.-]+/f/", got["base"])
    assert a.encode() not in body and i.encode() not in body
    status, h, body = lab.request("GET", got["base"] + "index.html")
    assert status == 200 and body == FILES["index.html"]
    assert h["content-security-policy"] == CSP
    assert h["access-control-allow-origin"] == "*" and h["access-control-expose-headers"] == EXPOSE
    status, h, body = lab.request("GET", got["base"] + "data/table.parquet", {"Range": "bytes=-10"})
    assert status == 206 and len(body) == 10
    status, h, body = lab.request("GET", got["base"] + "my%20file%20%C3%A9.txt")
    assert status == 200 and body == b"spaced"
    status, h, _ = lab.request("OPTIONS", got["base"] + "data/table.parquet",
                               {"Origin": "null", "Access-Control-Request-Method": "GET"})
    assert status == 204
    events = [json.loads(x) for x in lab.event_lines()]
    opened = [e for e in events if e["kind"] == "opened"]
    assert opened and opened[0]["actor"] == "alice" and opened[0]["share_id"] == "shr-1"
    text = lab.events.read_text()
    captured = capfd.readouterr()
    session = got["base"].split("/")[4]
    for secret in (a, i, TOKEN, session):
        assert secret not in text and secret not in captured.out and secret not in captured.err


def test_group_member_gets_a_session(lab):
    status, _, body = open_session(lab, access("dave", groups=("Readers",)),
                                   ident("dave", email="dave@elsewhere.example"))
    assert status == 200
    status, _, _ = lab.request("GET", json.loads(body)["base"] + "index.html")
    assert status == 200


@pytest.mark.parametrize("name,make", [
    ("not on list", lambda: (access("eve"), ident("eve", email="eve@lab.example"))),
    ("unverified", lambda: (access("alice"), ident("alice", verified=False))),
    ("mismatched user", lambda: (access("eve"), ident("alice"))),
    ("foreign client", lambda: (access("alice", client="other"), ident("alice"))),
    ("expired", lambda: (access("alice", exp=time.time() - 5), ident("alice"))),
    ("wrong issuer", lambda: (access("alice", iss=ISS + "x"), ident("alice"))),
    ("garbage", lambda: ("x", "y")),
])
def test_session_failures_are_the_uniform_404(lab, name, make):
    ref = reference(lab)
    a, i = make()
    status, h, body = open_session(lab, a, i)
    assert status == 404 and body == ref, name
    assert h["connection"] == "close"
    denied = [json.loads(x) for x in lab.event_lines() if json.loads(x)["kind"] == "denied"]
    assert denied and denied[-1]["share_id"] == "shr-1"
    text = lab.events.read_text()
    assert a not in text and i not in text


def test_session_header_rules(lab):
    ref = reference(lab)
    a, i = access("alice"), ident("alice")
    cases = [
        {"Authorization": "Basic " + a, "X-Id-Token": i},
        {"Authorization": a, "X-Id-Token": i},
        {"X-Id-Token": i},
        {"Authorization": "Bearer " + a},
        {"Authorization": "Bearer " + a + "x" * 9000, "X-Id-Token": i},
        {"Authorization": "Bearer " + a, "X-Id-Token": i + "x" * 9000},
    ]
    for headers in cases:
        status, _, body = lab.request("POST", f"/s/{TOKEN}/session", dict(headers, **{"Content-Length": "0"}))
        assert status == 404 and body == ref, list(headers)
    # case-insensitive scheme is fine
    status, _, _ = lab.request("POST", f"/s/{TOKEN}/session",
                               {"Authorization": "bearer " + a, "X-Id-Token": i, "Content-Length": "0"})
    assert status == 200
    # duplicates are refused (raw socket: http.client would merge nothing, but be explicit)
    req = (f"POST /s/{TOKEN}/session HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {a}\r\nAuthorization: Bearer {a}\r\n"
           f"X-Id-Token: {i}\r\nContent-Length: 0\r\n\r\n").encode()
    out = lab.raw(req)
    assert out.startswith(b"HTTP/1.1 404 ") and out.endswith(ref)
    req = (f"POST /s/{TOKEN}/session HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {a}\r\nX-Id-Token: {i}\r\n"
           f"X-Id-Token: {i}\r\nContent-Length: 0\r\n\r\n").encode()
    assert lab.raw(req).startswith(b"HTTP/1.1 404 ")
    # no Content-Length at all is a body-less POST too
    req = (f"POST /s/{TOKEN}/session HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {a}\r\nX-Id-Token: {i}\r\n"
           "Connection: close\r\n\r\n").encode()
    assert lab.raw(req).startswith(b"HTTP/1.1 200 ")


def test_session_endpoint_is_post_only(lab):
    ref = reference(lab)
    a, i = access("alice"), ident("alice")
    for method in ("GET", "HEAD", "OPTIONS", "PUT", "DELETE"):
        status, _, body = lab.request(method, f"/s/{TOKEN}/session",
                                      {"Authorization": "Bearer " + a, "X-Id-Token": i,
                                       "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert status == 404, method
        if method != "HEAD":
            assert body == ref
    for path in (f"/s/{TOKEN}/session/", f"/s/{TOKEN}/sessionx", f"/s/{TOKEN}/f/index.html",
                 f"/s/{TOKEN}/", "/s/callback"):
        status, _, body = lab.request("POST", path,
                                      {"Authorization": "Bearer " + a, "X-Id-Token": i, "Content-Length": "0"})
        assert status == 404 and body == ref, path


@pytest.mark.parametrize("framing", [
    b"Content-Length: 5\r\n\r\nhello",
    b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
    b"Content-Length: 0\r\nContent-Length: 5\r\n\r\nhello",
])
def test_session_with_a_body_is_refused_and_closed(lab, framing):
    a, i = access("alice"), ident("alice")
    second = b"GET /s/%s/ HTTP/1.1\r\nHost: x\r\n\r\n" % TOKEN.encode()
    first = (b"POST /s/%s/session HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\nX-Id-Token: %s\r\n"
             % (TOKEN.encode(), a.encode(), i.encode())) + framing
    out = lab.raw(first + second)
    assert out.count(b"HTTP/1.1 ") == 1, out[:300]
    assert out.startswith(b"HTTP/1.1 404 ")
    assert b"\r\nConnection: close\r\n" in out


def test_session_lifetime_is_capped(tmp_path):
    def session_exp(expires_in, token_exp, session_seconds=14400):
        sub = tmp_path / f"c{len(list(tmp_path.iterdir()))}"
        sub.mkdir()
        e = Env(sub, audience="lab", expires_in=expires_in, state_extra=LAB_ALLOW,
                config_extra=lab_config(sub, session_seconds=session_seconds))
        try:
            status, _, body = open_session(e, access("alice", exp=token_exp), ident("alice", exp=NOW0 + 10 ** 6))
            assert status == 200, body
            got = json.loads(body)
            # the session dies at exactly that instant
            assert e.request("GET", got["base"] + "index.html")[0] == 200
            e.clock[0] = got["expires_at"]
            assert e.request("GET", got["base"] + "index.html")[0] == 404
            return got["expires_at"]
        finally:
            e.close()

    assert session_exp(None, NOW0 + 10 ** 6) == NOW0 + 14400                 # session_seconds
    assert session_exp(None, NOW0 + 10 ** 6, session_seconds=60) == NOW0 + 60
    assert session_exp(600.0, NOW0 + 10 ** 6) == NOW0 + 600                  # the share's expiry
    assert session_exp(None, NOW0 + 300) == NOW0 + 300                       # the access token's exp


def test_revoke_kills_a_live_session(lab):
    status, _, body = open_session(lab)
    base = json.loads(body)["base"]
    assert lab.request("GET", base + "index.html")[0] == 200
    write_state(lab.root, lab.token_hash, dict(lab.state, state="revoked"))
    assert lab.request("GET", base + "index.html")[0] == 404
    write_state(lab.root, lab.token_hash, lab.state)
    assert lab.request("GET", base + "index.html")[0] == 200
    remove_state(lab.root, lab.token_hash)
    assert lab.request("GET", base + "index.html")[0] == 404
    assert lab.request("GET", f"/s/{TOKEN}/")[0] == 404
    assert open_session(lab)[0] == 404


def test_lab_files_need_a_valid_session(lab):
    ref = reference(lab)
    other_share = mint_session(SECRET, "shr-2", "alice", NOW0 + 100)
    other_secret = mint_session(b"z" * 48, "shr-1", "alice", NOW0 + 100)
    expired = mint_session(SECRET, "shr-1", "alice", NOW0 - 1)
    good = mint_session(SECRET, "shr-1", "alice", NOW0 + 100)
    paths = [f"/s/{TOKEN}/f/index.html", f"/s/{TOKEN}/f/data/table.parquet",
             f"/s/{TOKEN}/k/garbage/f/index.html", f"/s/{TOKEN}/k//f/index.html",
             f"/s/{TOKEN}/k/{other_share}/f/index.html", f"/s/{TOKEN}/k/{other_secret}/f/index.html",
             f"/s/{TOKEN}/k/{expired}/f/index.html", f"/s/{OTHER_TOKEN}/k/{good}/f/index.html",
             f"/s/{TOKEN}/k/{good}/f/", f"/s/{TOKEN}/k/{good}/index.html",
             f"/s/{TOKEN}/k/{good}/f/../index.html", f"/s/{TOKEN}/k/{good}/f/missing",
             f"/s/{TOKEN}/k/{good}"]
    for path in paths:
        status, _, body = lab.request("GET", path)
        assert status == 404 and body == ref, path
    assert lab.request("GET", f"/s/{TOKEN}/k/{good}/f/index.html")[0] == 200


def test_public_share_refuses_sessions(tmp_path):
    e = Env(tmp_path, audience="public", config_extra=lab_config(tmp_path), state_extra=LAB_ALLOW)
    try:
        ref = reference(e)
        status, _, body = open_session(e)
        assert status == 404 and body == ref
        good = mint_session(SECRET, "shr-1", "alice", NOW0 + 100)
        assert e.request("GET", f"/s/{TOKEN}/k/{good}/f/index.html")[2] == ref
        status, h, _ = e.request("GET", f"/s/{TOKEN}/")                   # public behaviour is unchanged
        assert status == 302 and h["location"] == "f/index.html"
        assert e.request("GET", f"/s/{TOKEN}/f/index.html")[0] == 200
    finally:
        e.close()


@pytest.mark.parametrize("over", [
    {"signing_secret": b""}, {"signing_secret": b"short"}, {"pool_id": None}, {"pool_id": "bogus"},
    {"web_client_id": None}, {"cognito_domain": None}, {"cognito_domain": "http://auth.lab.example"},
    {"cognito_domain": "https://auth.lab.example/path?x"}, {"public_url": None}, {"sync_dir": None},
])
def test_lab_unconfigured_fails_closed(tmp_path, over):
    e = Env(tmp_path, audience="lab", expires_in=None, state_extra=LAB_ALLOW,
            config_extra=lab_config(tmp_path, **over))
    try:
        ref = reference(e)
        good = mint_session(SECRET, "shr-1", "alice", NOW0 + 100)
        for method, path in (("GET", f"/s/{TOKEN}/"), ("GET", "/s/callback"), ("GET", "/s/static/share.js"),
                             ("GET", "/s/static/config.json"), ("GET", f"/s/{TOKEN}/k/{good}/f/index.html")):
            assert e.request(method, path)[2] == ref, path
        assert open_session(e)[2] == ref
    finally:
        e.close()


@pytest.mark.parametrize("jwks_text", [None, "not json", json.dumps({"keys": []}),
                                       json.dumps(jwks_doc(OTHER_KEY))])
def test_missing_or_wrong_jwks_fails_closed(tmp_path, jwks_text):
    cfg = lab_config(tmp_path)
    if jwks_text is None:
        (cfg["sync_dir"] / "jwks.json").unlink()
    else:
        (cfg["sync_dir"] / "jwks.json").write_text(jwks_text)
    e = Env(tmp_path, audience="lab", expires_in=None, state_extra=LAB_ALLOW, config_extra=cfg)
    try:
        status, _, body = open_session(e)
        assert status == 404 and body == reference(e)
    finally:
        e.close()


def test_session_attempts_are_rate_limited(lab, monkeypatch):
    for _ in range(share_public.SESSION_PER_SHARE_PER_MINUTE):
        assert open_session(lab, "x", "y")[0] == 404
    assert open_session(lab)[0] == 404                       # even a valid attempt is refused now
    lab.clock[0] += 61
    assert open_session(lab)[0] == 200
    lab.server.session_global = share_public.RateLimiter(3, lambda: lab.clock[0])
    for _ in range(3):
        open_session(lab, "x", "y")
    assert open_session(lab)[0] == 404
    reasons = [json.loads(x)["detail"]["reason"] for x in lab.event_lines() if json.loads(x)["kind"] == "denied"]
    assert "rate_limited" in reasons


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_share_js_parses():
    subprocess.run(["node", "--check", str(STATIC_DIR / "share.js")], check=True, timeout=60)


def test_share_js_hygiene():
    js = (STATIC_DIR / "share.js").read_text()
    assert "innerHTML" not in js and "outerHTML" not in js and "insertAdjacentHTML" not in js
    assert "document.write" not in js and "eval(" not in js
    assert "console." not in js and "localStorage" not in js
    assert "S256" in js and "sessionStorage" in js and "crypto.subtle" in js
    assert "/oauth2/authorize" in js and "/oauth2/token" in js
    assert "X-Id-Token" in js and "location.replace" in js
