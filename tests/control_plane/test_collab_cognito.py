"""HCN Cognito authenticator: token validation, group gating, revocation, proxy rate key."""
from __future__ import annotations

import json
import threading
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab import CollabService
from herdr_engineering.control_plane.collab_cognito import CognitoAuthenticator
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab import BASE, Api, read_frame

POOL = "us-west-2_TestPool1"
ISS = f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}"
CLIENT = "client-abc"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(key=KEY, kid="k1"):
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {"keys": [{**d, "kid": kid, "alg": "RS256", "use": "sig"}]}


def token(user="alice", *, groups=("dev",), key=KEY, kid="k1", alg="RS256", **over):
    claims = {"iss": ISS, "sub": f"sub-{user}", "username": user, "token_use": "access",
              "client_id": CLIENT, "cognito:groups": list(groups),
              "exp": time.time() + 3600, "iat": time.time()}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def auth(**kw):
    return CognitoAuthenticator(pool_id=POOL, client_ids=[CLIENT], required_group="dev",
                                jwks_fetch=lambda: jwks(), **kw)


def test_valid_access_token_in_group_is_accepted():
    a = auth()
    uid, exp = a.verify(token("alice"))
    assert uid == "alice" and exp > time.time()


@pytest.mark.parametrize("bad", [
    lambda: token(iss="https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Other"),
    lambda: token(exp=time.time() - 5),
    lambda: token(token_use="id"),
    lambda: token(client_id="someone-elses-client"),
    lambda: token(client_id=None),
    lambda: token(groups=()),
    lambda: token(groups=("Administrators", "llm-users")),   # other groups grant nothing
    lambda: token(key=OTHER),                                  # signed by a different key
    lambda: token(kid="unknown-kid"),
    lambda: jwt.encode({"iss": ISS, "sub": "x", "username": "x", "token_use": "access",
                        "client_id": CLIENT, "cognito:groups": ["dev"],
                        "exp": time.time() + 99}, "secret", algorithm="HS256",
                       headers={"kid": "k1"}),                 # algorithm confusion
    lambda: jwt.encode({"iss": ISS, "username": "x"}, None, algorithm="none"),
    lambda: "not.a.jwt",
    lambda: "",
])
def test_invalid_tokens_are_rejected(bad):
    assert auth().verify(bad()) is None


def test_aud_is_not_required_for_access_tokens():
    assert auth().verify(token(aud=None)) is not None


def test_removed_from_group_takes_effect_within_recheck_window():
    clock = [1000.0]
    groups = {"alice": ["dev"]}
    a = auth(group_lookup=lambda u: groups.get(u), now=lambda: clock[0])
    tok = token("alice", exp=time.time() + 99999)
    assert a.verify(tok) and a.exists("alice")
    groups["alice"] = []                  # removed from dev (JWT still says dev)
    assert a.verify(tok) is not None      # cached for the recheck window
    clock[0] += 61
    assert a.verify(tok) is None and not a.exists("alice")


def test_directory_outage_fails_closed_for_new_users():
    def boom(_):
        raise RuntimeError("down")
    assert auth(group_lookup=boom).verify(token("alice")) is None


def test_jwks_refetch_is_rate_limited():
    calls = []
    clock = [10_000.0]
    a = CognitoAuthenticator(pool_id=POOL, client_ids=[CLIENT], required_group="dev",
                             jwks_fetch=lambda: calls.append(1) or jwks(), now=lambda: clock[0])
    for _ in range(20):
        a.verify(token(kid="nope"))
    assert len(calls) == 1


def test_config_validation():
    with pytest.raises(ValueError):
        CognitoAuthenticator(pool_id="bad", client_ids=["c"], required_group="dev")
    with pytest.raises(ValueError):
        CognitoAuthenticator(pool_id=POOL, client_ids=[], required_group="dev")


@pytest.fixture
def server(acknowledged_sink):
    a = auth()
    svc = CollabService(a, sink=acknowledged_sink, stream_poll_seconds=0.05)
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=svc)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    yield Api(srv.port), svc
    srv.shutdown()
    t.join(timeout=5)


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def test_http_flow_with_cognito_tokens(server):
    api, svc = server
    assert api.call("POST", f"{BASE}/projects", {"name": "p"}, headers=bearer(token("alice", groups=())))[0] == 401
    s, p = api.call("POST", f"{BASE}/projects", {"name": "p"}, headers=bearer(token("alice")))
    assert s == 201
    s, r = api.call("POST", f"{BASE}/projects/{p['project_id']}/rooms", {"name": "r"},
                    headers=bearer(token("alice")))
    # bob has logged in once (so the pilot directory knows him), then gets invited
    assert api.call("GET", f"{BASE}/rooms", headers=bearer(token("bob")))[0] == 200
    assert api.call("POST", f"{BASE}/rooms/{r['room_id']}/members",
                    {"user_id": "bob", "role": "contributor"}, headers=bearer(token("alice")))[0] == 201
    assert api.call("POST", f"{BASE}/rooms/{r['room_id']}/messages",
                    {"text": "hi", "idempotency_key": "k"}, headers=bearer(token("bob")))[0] == 201
    assert api.call("GET", f"{BASE}/rooms/{r['room_id']}", headers=bearer(token("carol")))[0] == 404


def test_stream_ends_when_the_token_expires(server):
    api, svc = server
    s, p = api.call("POST", f"{BASE}/projects", {"name": "p"}, headers=bearer(token("alice")))
    s, r = api.call("POST", f"{BASE}/projects/{p['project_id']}/rooms", {"name": "r"},
                    headers=bearer(token("alice")))
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=5)
    conn.request("GET", f"{BASE}/rooms/{r['room_id']}/events/stream?cursor=0",
                 headers=bearer(token("alice", exp=time.time() + 1.2)))
    resp = conn.getresponse()
    assert resp.status == 200
    last = None
    while True:
        f = read_frame(resp)
        if f is None:
            break
        last = f
        if f.get("type") == "closed":
            break
    assert last == {"type": "closed", "reason": "access_revoked"}
    conn.close()


def test_rate_limit_is_per_forwarded_client_behind_the_proxy(acknowledged_sink):
    from herdr_engineering.control_plane.api import _RateLimiter
    svc = CollabService(auth(), sink=acknowledged_sink)
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=svc)
    srv._limiter = _RateLimiter(max_requests=3, window_seconds=60)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    try:
        api = Api(srv.port)

        def hit(ip):
            return api.call("GET", f"{BASE}/rooms",
                            headers={**bearer(token("alice")), "X-Forwarded-For": ip})[0]
        assert [hit("100.64.0.1") for _ in range(4)] == [200, 200, 200, 429]
        assert hit("100.64.0.2") == 200          # another tailnet client has its own budget
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_require_https_refuses_plain_requests_but_serves_proxied_ones(acknowledged_sink):
    svc = CollabService(auth(), sink=acknowledged_sink)
    srv = ControlPlaneServer(ControlPlaneRepo(), host="0.0.0.0", port=0, collab=svc,
                             allow_open=True, collab_require_https=True)   # no tls-terminated assertion needed
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    try:
        api = Api(srv.port)
        plain = api.call("GET", f"{BASE}/rooms", headers=bearer(token("alice")))
        assert plain[0] == 400
        ok = api.call("GET", f"{BASE}/rooms", headers={**bearer(token("alice")),
                                                       "X-Forwarded-Proto": "https"})
        assert ok[0] == 200
        # the rest of the server is untouched by the flag
        assert api.call("GET", "/api/v1/health")[0] == 200
    finally:
        srv.shutdown()
        t.join(timeout=5)
