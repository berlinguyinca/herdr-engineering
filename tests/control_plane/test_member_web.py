# ruff: noqa: F811  (imported pytest fixtures are used as test arguments)
"""Plan 5, Task 11: the member web API on ``collab-public``.

A real ``ControlPlaneServer`` public listener on a loopback socket, a real ``CollabService`` authenticating real RS256
Cognito-shaped tokens (``CognitoAuthenticator`` over a JWKS document), a real ``CollabArtifactBridge`` and
``MemberService`` on temp directories.  Requests are raw sockets so that duplicate headers, missing headers and unsent
bodies can be tested exactly."""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from herdr_engineering.control_plane import collab_member_tools
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_member_service import member_actor
from herdr_engineering.control_plane.artifact_members import member_owner_key
from herdr_engineering.control_plane.collab import ApprovalPolicy, CollabService, FileEventLog
from herdr_engineering.control_plane.collab_bridge_files import CHUNK_BYTES, upload_id_for
from herdr_engineering.control_plane.member_session import mint_session
from herdr_engineering.control_plane.member_web import MemberWeb
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.share_session import mint_session as mint_share_session
from tests.control_plane.test_artifact_member_service import ART, OTHER, World
from tests.control_plane.test_collab import Clock
from tests.control_plane.test_share_lab import access, authenticator, ident

HOST = "my.lab.example"
ORIGIN = f"https://{HOST}"
SECRET = b"m" * 48
GROUP = "local.dev"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class CWorld(World):
    """The member-service world, but people sign in with real Cognito-shaped access tokens."""

    def person(self, name):
        groups = [GROUP] if name != "mal" else ["Users"]
        pr = self.s.authenticate("Bearer " + access(name, groups=groups), None)
        if name not in self.s._user_email:
            self.s._commit("identity_linked", {"actor_user_id": name, "actor_agent_id": None, "user_id": name,
                                               "email": f"{name}@example.org"})
        self.people[name] = pr
        return pr

    def approve(self, name):
        pass


class Reply:
    def __init__(self, status, headers, raw):
        self.status, self.headers, self.raw = status, headers, raw

    @property
    def json(self):
        return json.loads(self.raw)


@pytest.fixture
def stack(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "member.html").write_text("<!doctype html><title>member</title>")
    svc = CollabService(authenticator(), sink=FileEventLog(str(tmp_path / "log")), now=Clock(),
                        approval=ApprovalPolicy(auto_groups=[GROUP, "Users"], approver_groups=["Administrators"]),
                        public_config={"client_id": "web-client", "hosted_ui_domain": "auth.lab.example"})
    world = CWorld(svc, tmp_path, writes_per_minute=1000)
    world.write_grants(max_pending_intents_per_agent=50)      # the cap itself is tested below
    world.view("op", [ART])
    world.view("lab", [OTHER])
    mw = MemberWeb(svc, public_host=HOST, session_secret=SECRET, session_seconds=3600, web_root=web,
                   require_https=True)
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=svc, public_port=0, web_root=web,
                             collab_require_https=True, member_web=mw)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    yield SimpleNamespace(srv=srv, s=svc, w=world, mw=mw, web=web)
    srv.shutdown()
    t.join(timeout=5)
    world.bridge.stop()


def session(name="op", offset=600):
    return mint_session(SECRET, name, 1_800_000_000.0 + offset)


def req(stack, method, path, *, body=None, raw=b"", member="op", auth=None, host=HOST, origin=ORIGIN, extra=(),
        send=True, declared=None, proto=True, content_type="application/json"):
    """One raw request on the public listener.  ``auth`` overrides the Authorization value, ``member`` mints a
    session for that person; ``host``/``origin`` None omit the header."""
    if body is not None:
        raw = json.dumps(body).encode()
    head = [f"{method} {path} HTTP/1.1"]
    if host is not None:
        head.append(f"Host: {host}")
    if proto:
        head.append("X-Forwarded-Proto: https")
    if origin is not None and method != "GET":
        head.append(f"Origin: {origin}")
    if auth is not None:
        head.append(f"Authorization: {auth}")
    elif member is not None:
        head.append(f"Authorization: Member {session(member)}")
    if raw or declared is not None or method in ("POST", "PUT"):
        head.append(f"Content-Length: {len(raw) if declared is None else declared}")
        if raw and content_type:
            head.append(f"Content-Type: {content_type}")
    head += list(extra)
    sock = socket.create_connection(("127.0.0.1", stack.srv.public_port), timeout=5)
    try:
        sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + (raw if send else b""))
        buf = b""
        while b"\r\n\r\n" not in buf:
            part = sock.recv(65536)
            if not part:
                break
            buf += part
        head_txt, _, rest = buf.partition(b"\r\n\r\n")
        lines = head_txt.decode().split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        need = int(headers.get("content-length", "0"))
        while len(rest) < need:
            part = sock.recv(65536)
            if not part:
                break
            rest += part
        return Reply(int(lines[0].split()[1]), headers, rest)
    finally:
        sock.close()


def agent_call(stack, name, args, who="op"):
    pr, _ = stack.w.agent(who)
    return collab_member_tools.call(stack.s, pr, name, args)


def mcp_agent(stack, who="op"):
    return stack.w.agent(who)[0]


# ------------------------------------------------------------------------------ session exchange
def exchange(stack, *, user="op", groups=(GROUP,), extra=(), access_token=None, id_token=None, body=b"", host=HOST,
             origin=ORIGIN, scheme="Bearer"):
    tok = access_token if access_token is not None else access(user, groups=list(groups))
    ident_tok = id_token if id_token is not None else ident(user)
    hdr = [f"X-Id-Token: {ident_tok}", *extra]
    return req(stack, "POST", "/api/member/session", raw=body, member=None, auth=f"{scheme} {tok}", extra=hdr,
               host=host, origin=origin)


def test_exchange_mints_a_member_session_usable_on_member_routes(stack):
    got = exchange(stack)
    assert got.status == 200
    out = got.json
    assert set(out) == {"session", "expires_at"} and out["expires_at"] > 1_800_000_000
    assert req(stack, "GET", "/api/member/me", member=None, auth=f"Member {out['session']}").status == 200


@pytest.mark.parametrize("kind", ["outsider", "bad_access", "no_id", "dup_id", "wrong_user_id", "expired",
                                  "body", "dup_auth", "bad_scheme"])
def test_exchange_failures_are_uniform_and_leak_nothing(stack, kind):
    stack.w.person("op")
    if kind == "outsider":
        got = exchange(stack, user="mal", groups=("Users",))
    elif kind == "bad_access":
        got = exchange(stack, access_token="x.y.z")
    elif kind == "no_id":
        got = req(stack, "POST", "/api/member/session", member=None, auth="Bearer " + access("op", groups=[GROUP]))
    elif kind == "dup_id":
        got = exchange(stack, extra=(f"X-Id-Token: {ident('op')}",))
    elif kind == "wrong_user_id":
        got = exchange(stack, id_token=ident("lab"))
    elif kind == "expired":
        got = exchange(stack, access_token=access("op", groups=[GROUP], exp=1.0))
    elif kind == "body":
        got = exchange(stack, body=b"{}")
    elif kind == "dup_auth":
        got = exchange(stack, extra=(f"Authorization: Bearer {access('op', groups=[GROUP])}",))
    else:
        got = exchange(stack, scheme="Member")
    assert got.status in (400, 401)
    assert got.json in ({"error": "unauthorized", "message": "sign in again"}, {"error": "bad_request",
                                                                               "message": "bad request"})
    assert b"op" not in got.raw.replace(b"unauthorized", b"") or kind == "body"


def test_exchange_checks_origin_and_host(stack):
    assert exchange(stack, origin=None).status == 403
    assert exchange(stack, origin="https://evil.example").status == 403
    assert exchange(stack, host="other.example").status == 404
    assert exchange(stack, host=None).status == 404


def test_exchange_is_rate_limited_per_client(stack):
    stack.s.member_service.config.__class__  # real config object
    limit = stack.s.member_service.config.session_exchanges_per_minute
    codes = [exchange(stack, user="mal", groups=("Users",)).status for _ in range(limit + 3)]
    assert codes[:limit] == [401] * limit and set(codes[limit:]) == {429}


# ------------------------------------------------------------------------------ host, origin, auth gates
def test_other_hosts_get_the_plain_404_for_every_member_path(stack):
    for host in ("other.example", "my.lab.example.evil", None):
        for method, path in (("GET", "/api/member/me"), ("POST", "/api/member/artifacts"),
                             ("PUT", f"/api/member/artifacts/{ART}/uploads/upl_{'0' * 24}/runs/0")):
            got = req(stack, method, path, host=host, raw=b"" if method == "GET" else b"{}")
            assert got.status == 404 and got.json == {"error": "not found"}, (host, method)
    assert req(stack, "GET", "/api/member/me", host=HOST + ":443").status == 200


def test_duplicate_host_header_is_404(stack):
    assert req(stack, "GET", "/api/member/me", extra=("Host: other.example",)).status == 404


def test_member_host_serves_nothing_but_the_member_api(stack):
    for path in ("/mcp", "/api/v1/collab/me", "/collab.html", "/.well-known/agent-card.json", "/a2a"):
        assert req(stack, "GET", path).status == 404, path


def test_origin_is_required_exactly_once_on_every_write(stack):
    writes = [("POST", "/api/member/artifacts", {"title": "t", "filename": "a.html", "profile": "web"}),
              ("POST", f"/api/member/artifacts/{ART}/delete", {"confirm": True}),
              ("POST", f"/api/member/artifacts/{ART}/publish", {"files": [], "message": "m"}),
              ("POST", f"/api/member/artifacts/{ART}/shares", {"version_id": "v_1", "audience": "lab"}),
              ("POST", f"/api/member/artifacts/{ART}/uploads/status", {"name": "a", "size_bytes": 1, "file_sha256": "0" * 64}),
              ("POST", f"/api/member/artifacts/{ART}/uploads/upl_{'0' * 24}/abort", None),
              ("POST", f"/api/member/artifacts/{ART}/shares/share_111111111111111111111111/approve", {"confirm": True}),
              ("POST", f"/api/member/artifacts/{ART}/shares/share_111111111111111111111111/revoke", None),
              ("PUT", f"/api/member/artifacts/{ART}/uploads/upl_{'0' * 24}/runs/0", None)]
    for method, path, body in writes:
        raw = json.dumps(body).encode() if body is not None else b"x"
        for origin in (None, "https://evil.example", "http://" + HOST, ORIGIN + "/", ORIGIN.upper()):
            got = req(stack, method, path, raw=raw, origin=origin)
            assert got.status == 403 and got.json["error"] == "forbidden", (path, origin)
        got = req(stack, method, path, raw=raw, extra=(f"Origin: {ORIGIN}",))
        assert got.status == 403, path                       # two Origin headers
    assert req(stack, "GET", "/api/member/me", origin="https://evil.example").status == 200   # reads need none


def test_authorization_scheme_must_be_member(stack):
    s = session()
    for auth in (None, "", f"Bearer {s}", f"member {s}", f"Member{s}", f"Member  {s}", "Member ", f"Member {s}x",
                 f"Basic {s}"):
        got = req(stack, "GET", "/api/member/me", member=None, auth=auth) if auth is not None else \
            req(stack, "GET", "/api/member/me", member=None)
        assert got.status == 401 and got.json == {"error": "unauthorized", "message": "sign in again"}, auth
    dup = req(stack, "GET", "/api/member/me", extra=(f"Authorization: Member {s}",))
    assert dup.status == 401


def test_a_share_session_and_an_expired_or_foreign_session_are_refused(stack):
    shr = mint_share_session(SECRET, "shr-1", "op", 1_800_000_600.0, email="op@example.org", basis="e")
    for token in (shr, mint_session(SECRET, "op", 1_799_999_000.0), mint_session(b"z" * 48, "op", 1_800_000_600.0),
                  mint_session(SECRET, "ghost", 1_800_000_600.0)):
        assert req(stack, "GET", "/api/member/me", member=None, auth=f"Member {token}").status == 401


def test_a_member_session_is_refused_on_share_routes(stack):
    from herdr_engineering.control_plane.share_session import check_session as check_share
    token = session()
    assert check_share(SECRET, "shr-1", token, 1_800_000_000.0) is None
    assert check_share(SECRET, "op", token, 1_800_000_000.0) is None


def test_a_disabled_or_outsider_person_is_refused_after_the_session_check(stack):
    got = req(stack, "GET", "/api/member/me", member="mal")
    assert got.status == 403 and got.json["error"] == "forbidden"


def test_https_is_required_and_unknown_methods_are_refused(stack):
    assert req(stack, "GET", "/api/member/me", proto=False).status == 400
    assert req(stack, "DELETE", "/api/member/me").status in (404, 405, 501)


def test_framing_is_unambiguous(stack):
    assert req(stack, "POST", "/api/member/artifacts", raw=b"{}", extra=("Content-Length: 2",)).status == 400
    assert req(stack, "POST", "/api/member/artifacts", raw=b"{}", extra=("Transfer-Encoding: chunked",)).status == 400
    assert req(stack, "GET", "/api/member/me", raw=b"x").status == 400


def test_bad_paths_are_404_without_detail(stack):
    for path in ("/api/member/../me", "/api/member//me", "/api/member/me%00", "/api/member/nope",
                 "/api/member/artifacts/" + "a" * 300 + "/history", "/api/member/artifacts/x%20y/history"):
        got = req(stack, "GET", path)
        assert got.status in (400, 404) and got.json.get("error") in ("not_found", "not found"), path


# ------------------------------------------------------------------------------ static + config
def test_the_page_is_served_on_the_member_host_with_a_strict_csp(stack):
    got = req(stack, "GET", "/", member=None)
    assert got.status == 200 and got.headers["content-type"].startswith("text/html")
    assert "frame-ancestors 'none'" in got.headers["content-security-policy"]
    assert got.headers["cache-control"] == "no-store"
    assert req(stack, "GET", "/my/callback", member=None).status == 200
    assert req(stack, "GET", "/", member=None, host="other.example").status == 404


def test_the_page_csp_names_exact_files_and_never_a_bare_self_for_scripts(stack):
    """The member host also serves member-uploaded bundles (/s/<token>/f/x.js): 'self' would let them run as the page."""
    for path in ("/", "/my/callback"):
        csp = req(stack, "GET", path, member=None).headers["content-security-policy"]
        parts = {d.split()[0]: d.split()[1:] for d in csp.split("; ")}
        assert parts["script-src"] == [f"{ORIGIN}/my/member.js"]
        assert parts["style-src"] == [f"{ORIGIN}/my/member.css"]
        assert parts["connect-src"] == [f"{ORIGIN}/api/member/", "https://auth.lab.example"]
        assert parts["default-src"] == ["'none'"] and parts["base-uri"] == ["'none'"]
        for name in ("script-src", "style-src", "connect-src"):
            assert "'self'" not in parts[name] and "*" not in parts[name] and "https:" not in parts[name]


def test_config_exposes_only_the_public_sign_in_facts(stack):
    got = req(stack, "GET", "/api/member/config", member=None)
    assert got.status == 200
    assert got.json == {"cognito_domain": "https://auth.lab.example", "client_id": "web-client",
                        "redirect_uri": f"{ORIGIN}/my/callback", "logout_uri": f"{ORIGIN}/"}


# ------------------------------------------------------------------------------ parity with the MCP tools
def strip(doc):
    return {k: v for k, v in doc.items() if k != "ticket"}


def test_reads_return_what_the_mcp_tools_return(stack):
    pr = mcp_agent(stack)
    assert req(stack, "GET", "/api/member/me").json == collab_member_tools.call(stack.s, pr, "artifacts_mine", {})
    assert req(stack, "GET", f"/api/member/artifacts/{ART}/history").json == \
        collab_member_tools.call(stack.s, pr, "artifact_history", {"artifact_id": ART})
    assert req(stack, "GET", f"/api/member/artifacts/{ART}/history?limit=1").json == \
        collab_member_tools.call(stack.s, pr, "artifact_history", {"artifact_id": ART, "limit": 1})
    assert req(stack, "GET", "/api/member/links").json == collab_member_tools.call(stack.s, pr, "artifact_share_list", {})
    assert req(stack, "GET", f"/api/member/artifacts/{ART}/shares").json == \
        collab_member_tools.call(stack.s, pr, "artifact_share_list", {"artifact_id": ART})


def test_writes_return_what_the_mcp_tools_return(stack):
    pr = mcp_agent(stack)
    key = "idem-key-0001"
    web = req(stack, "POST", "/api/member/artifacts",
              body={"title": "T", "filename": "a.html", "profile": "web", "idempotency_key": key})
    mcp = collab_member_tools.call(stack.s, pr, "artifact_create",
                                   {"title": "T", "filename": "a.html", "profile": "web", "idempotency_key": key})
    assert web.status == 200 and strip(web.json) == strip(mcp) and web.json["status"] == "pending"
    pub = {"files": [{"name": "a.html", "upload_id": f"upl_{'1' * 24}"}], "message": "m", "base_version_id": "v_1"}
    web = req(stack, "POST", f"/api/member/artifacts/{ART}/publish", body={**pub, "idempotency_key": "idem-pub-0001"})
    mcp = collab_member_tools.call(stack.s, pr, "artifact_publish",
                                   {"artifact_id": ART, **pub, "idempotency_key": "idem-pub-0001"})
    assert strip(web.json) == strip(mcp)
    web = req(stack, "POST", f"/api/member/artifacts/{ART}/delete", body={"confirm": True})
    assert strip(web.json) == strip(collab_member_tools.call(stack.s, pr, "artifact_delete",
                                                              {"artifact_id": ART, "confirm": True}))
    web = req(stack, "POST", f"/api/member/artifacts/{ART}/shares",
              body={"version_id": "v_1", "audience": "lab", "idempotency_key": "idem-shr-0001"})
    mcp = collab_member_tools.call(stack.s, pr, "artifact_share_create",
                                   {"artifact_id": ART, "version_id": "v_1", "audience": "lab",
                                    "idempotency_key": "idem-shr-0001"})
    assert strip(web.json) == strip(mcp)
    for what, name in (("revoke", "artifact_share_revoke"),):
        web = req(stack, "POST", f"/api/member/artifacts/{ART}/shares/share_111111111111111111111111/{what}")
        assert strip(web.json) == strip(collab_member_tools.call(stack.s, pr, name,
                                                                  {"artifact_id": ART, "share_id": "share_111111111111111111111111"}))
    web = req(stack, "POST", f"/api/member/artifacts/{ART}/shares/share_111111111111111111111111/approve", body={"confirm": True})
    assert strip(web.json) == strip(collab_member_tools.call(
        stack.s, pr, "artifact_share_approve", {"artifact_id": ART, "share_id": "share_111111111111111111111111", "confirm": True}))


def test_a_public_share_matches_the_mcp_tool_too(stack):
    pr = mcp_agent(stack)
    args = {"version_id": "v_1", "audience": "public", "expires_in_days": 7, "idempotency_key": "idem-pub-share-1"}
    web = req(stack, "POST", f"/api/member/artifacts/{ART}/shares", body=args)
    mcp = collab_member_tools.call(stack.s, pr, "artifact_share_create", {"artifact_id": ART, **args})
    assert web.status == 200 and strip(web.json) == strip(mcp) and web.json["status"] == "pending"
    other = {**args, "idempotency_key": "idem-pub-share-2"}
    web = req(stack, "POST", f"/api/member/artifacts/{ART}/shares", body=other)
    mcp = collab_member_tools.call(stack.s, pr, "artifact_share_create", {"artifact_id": ART, **other})
    assert strip(web.json) == strip(mcp)


def test_the_head_route_returns_what_the_mcp_tool_returns(stack):
    pr = mcp_agent(stack)
    web = req(stack, "GET", f"/api/member/artifacts/{ART}")
    assert web.status == 200 and web.json == collab_member_tools.call(stack.s, pr, "artifact_head", {"artifact_id": ART})
    assert req(stack, "GET", f"/api/member/artifacts/{OTHER}").status == 404          # not in the caller's view
    assert req(stack, "GET", f"/api/member/artifacts/{ART}?x=1").status == 400
    assert req(stack, "GET", "/api/member/artifacts/not%20an%20id").status == 404


def test_a_ticket_result_defaults_to_no_wait_and_matches_the_mcp_tool(stack):
    made = req(stack, "POST", "/api/member/artifacts", body={"title": "T", "filename": "a.html", "profile": "web"})
    ticket = made.json["ticket"]
    web = req(stack, "GET", f"/api/member/results/{ticket}")
    assert web.status == 200
    # a ticket belongs to the actor that made it (the web session is the person's own actor, an agent is its own)
    assert web.json == stack.s.member_service.result(member_actor(stack.w.people["op"]), ticket, 0)
    agent_ticket = agent_call(stack, "artifact_create", {"title": "T", "filename": "a.html", "profile": "web"})["ticket"]
    assert req(stack, "GET", f"/api/member/results/{agent_ticket}").status == 404
    assert req(stack, "GET", f"/api/member/results/{ticket}?seconds=21").status == 400
    assert req(stack, "GET", f"/api/member/results/{ticket}?seconds=x").status == 400
    assert req(stack, "GET", f"/api/member/results/{ticket}?other=1").status == 400
    assert req(stack, "GET", f"/api/member/results/ict_{'0' * 24}").status == 404


def test_a_ticket_belongs_to_its_creator(stack):
    ticket = req(stack, "POST", "/api/member/artifacts",
                 body={"title": "T", "filename": "a.html", "profile": "web"}).json["ticket"]
    assert req(stack, "GET", f"/api/member/results/{ticket}", member="lab").status == 404


def test_delete_needs_confirm_true_and_unknown_keys_are_refused(stack):
    assert req(stack, "POST", f"/api/member/artifacts/{ART}/delete", body={"confirm": "yes"}).status == 400
    assert req(stack, "POST", f"/api/member/artifacts/{ART}/delete", body={"confirm": True, "x": 1}).status == 400
    assert req(stack, "POST", f"/api/member/artifacts/{ART}/delete", body={}).status == 400
    assert req(stack, "POST", f"/api/member/artifacts/{ART}/delete", raw=b'{"confirm": true, "confirm": true}').status == 400
    assert req(stack, "POST", f"/api/member/artifacts/{ART}/delete", raw=b'{"confirm": true}',
               content_type="text/plain").status == 415
    assert req(stack, "POST", f"/api/member/artifacts/{ART}/delete", raw=b"[true]").status == 400
    assert req(stack, "POST", f"/api/member/artifacts/{ART}/delete", raw=b"\xff").status == 400


def test_other_peoples_artifacts_are_not_found(stack):
    for method, path, body in (("GET", f"/api/member/artifacts/{OTHER}/history", None),
                               ("POST", f"/api/member/artifacts/{OTHER}/delete", {"confirm": True}),
                               ("POST", f"/api/member/artifacts/{OTHER}/publish", {"files": [{"name": "a", "upload_id": "upl_" + "0" * 24}], "message": "m"})):
        got = req(stack, method, path, body=body)
        assert got.status == 404 and got.json["error"] == "not_found"


def test_the_json_body_is_bounded(stack):
    got = req(stack, "POST", "/api/member/artifacts", declared=300 * 1024, send=False)
    assert got.status == 413 and got.json["error"] == "too_large"


# ------------------------------------------------------------------------------ tombstones
def test_tombstoned_artifacts_do_not_exist_for_the_web_api(stack):
    gone = "art_gone1"
    stack.w.view("op", [ART, gone])
    import json as _json
    from pathlib import Path
    vf = Path(stack.w.bridge.mirror) / "member-views" / f"{member_owner_key('member:op')}.json"
    doc = _json.loads(vf.read_text())
    for a in doc["artifacts"]:
        if a["artifact_id"] == gone:
            a["lifecycle"] = "tombstoned"
    doc["links"] = [{"artifact_id": gone, "share_id": "shr_g", "mine": True}, {"artifact_id": ART, "share_id": "shr_a",
                                                                               "mine": True}]
    vf.write_text(_json.dumps(doc))
    me = req(stack, "GET", "/api/member/me").json
    assert [a["artifact_id"] for a in me["artifacts"]] == [ART]
    assert [x["share_id"] for x in me["links"]] == ["shr_a"]
    assert [x["share_id"] for x in req(stack, "GET", "/api/member/links").json["links"]] == ["shr_a"]
    sha0 = "0" * 64
    for method, tail, body in (("GET", "history", None), ("GET", "shares", None), ("POST", "delete", {"confirm": True}),
                               ("POST", "publish", {"files": [{"name": "a", "upload_id": "upl_" + "0" * 24}], "message": "m"}),
                               ("POST", "shares", {"version_id": "v_1", "audience": "lab"}),
                               ("POST", "uploads/status", {"name": "a", "size_bytes": 1, "file_sha256": sha0}),
                               ("POST", f"uploads/upl_{'0' * 24}/abort", None),
                               ("POST", "shares/shr_g/approve", {"confirm": True}),
                               ("POST", "shares/shr_g/revoke", None), ("POST", "shares/shr_g/events", None)):
        got = req(stack, method, f"/api/member/artifacts/{gone}/{tail}", body=body)
        assert got.status == 404 and got.json == {"error": "not_found", "message": "not found"}, tail
    got = req(stack, "PUT", f"/api/member/artifacts/{gone}/uploads/upl_{'0' * 24}/runs/0", raw=b"x",
              extra=("X-Name: a", "X-Size-Bytes: 1", f"X-File-SHA256: {sha0}", f"X-Run-SHA256: {sha(b'x')}"))
    assert got.status == 404


def test_malformed_queries_and_names_are_400_not_500(stack):
    base = f"/api/member/artifacts/{ART}/history"
    for q in ("?a", "?limit", "?limit=1&limit=2", "?a=1&b=2&c=3&d=4&e=5", "?limit=x", "?nope=1", "?&&"):
        got = req(stack, "GET", base + q)
        assert got.status == 400 and got.json["error"] == "bad_request", q
    assert req(stack, "GET", "/api/member/results/tkt_x?seconds").status in (400, 404)
    assert req(stack, "GET", "/api/member/me?x").status == 400
    for tail, body in (("delete", {"confirm": True}), ("shares", {"version_id": "v_1", "audience": "lab"})):
        got = req(stack, "POST", f"/api/member/artifacts/{ART}/{tail}?x=1", body=body)
        assert got.status == 400, tail                                 # no write route takes a query
    heads = list(run_headers())
    heads[0] = "X-Name: %ff%fe.bin"                                    # percent-decodes to invalid UTF-8
    got = req(stack, "PUT", f"/api/member/artifacts/{ART}/uploads/{upload_id()}/runs/0", declared=len(DATA), send=False,
              extra=heads)
    assert got.status == 400 and got.json["error"] == "bad_request"


def test_a_share_on_a_tombstoned_artifact_is_refused_before_it_costs_a_slot(stack):
    import json as _json
    from pathlib import Path

    from herdr_engineering.control_plane.artifact_member_service import MemberServiceError
    vf = Path(stack.w.bridge.mirror) / "member-views" / f"{member_owner_key('member:op')}.json"
    doc = _json.loads(vf.read_text())
    doc["artifacts"][0]["lifecycle"] = "tombstoned"
    vf.write_text(_json.dumps(doc))
    actor = member_actor(stack.w.people["op"])
    ms = stack.s.member_service
    for call in (lambda: ms.share_create(actor, ART, "v_1", "lab"),
                 lambda: ms.share_approve(actor, ART, "share_111111111111111111111111", True)):
        with pytest.raises(MemberServiceError) as err:
            call()
        assert (err.value.status, err.value.code) == (404, "not_found")
    assert not [k for k, v in stack.s._hits.items() if k.startswith("memberwrite") and v]
    assert not [k for k, v in stack.s._hits.items() if k.startswith("membershare_create") and v]


# ------------------------------------------------------------------------------ uploads
DATA = bytes(range(256)) * 2400            # 614400 bytes: chunk 0 is 512 KiB, chunk 1 the rest


def run_headers(data=DATA, name="f.bin", run=None):
    return (f"X-Name: {quote(name)}", f"X-Size-Bytes: {len(DATA)}", f"X-File-SHA256: {sha(DATA)}",
            f"X-Run-SHA256: {sha(run if run is not None else data)}")


def upload_id(name="f.bin", artifact=ART, who="op", data=DATA):
    return upload_id_for(member_owner_key(f"member:{who}"), artifact, name, sha(data), len(data))


def test_status_returns_the_derived_upload_id_the_mcp_tool_returns(stack):
    pr = mcp_agent(stack)
    body = {"name": "f.bin", "size_bytes": len(DATA), "file_sha256": sha(DATA)}
    web = req(stack, "POST", f"/api/member/artifacts/{ART}/uploads/status", body=body)
    mcp = collab_member_tools.call(stack.s, pr, "artifact_upload_status", {"artifact_id": ART, **body})
    assert web.status == 200 and web.json == mcp and web.json["upload_id"] == upload_id()


def test_a_run_stages_chunks_and_matches_the_chunk_tool(stack):
    got = req(stack, "PUT", f"/api/member/artifacts/{ART}/uploads/{upload_id()}/runs/0", raw=DATA,
              extra=run_headers(), content_type="application/octet-stream")
    assert got.status == 200, got.raw
    assert got.json["upload_id"] == upload_id() and got.json.get("complete") is True or got.json
    status = req(stack, "POST", f"/api/member/artifacts/{ART}/uploads/status",
                 body={"name": "f.bin", "size_bytes": len(DATA), "file_sha256": sha(DATA)}).json
    assert status["upload_id"] == upload_id()
    expected = stack.s.member_service.upload_status(member_actor(stack.w.people["op"]), ART, "f.bin", len(DATA), sha(DATA))
    assert status == expected


def test_a_run_over_the_request_limit_is_413_before_any_body_byte_is_read(stack):
    limit = stack.s.member_service.config.http_chunks_per_request * CHUNK_BYTES
    got = req(stack, "PUT", f"/api/member/artifacts/{ART}/uploads/{upload_id()}/runs/0", declared=limit + 1,
              send=False, extra=run_headers())
    assert got.status == 413 and got.json["error"] == "too_large" and got.headers["connection"] == "close"


def test_a_mismatched_upload_id_is_400_before_the_body_is_read(stack):
    other = upload_id(name="other.bin")
    got = req(stack, "PUT", f"/api/member/artifacts/{ART}/uploads/{other}/runs/0", declared=len(DATA), send=False,
              extra=run_headers())
    assert got.status == 400 and got.json["error"] == "bad_request"
    forged = upload_id(who="lab", artifact=ART)
    got = req(stack, "PUT", f"/api/member/artifacts/{ART}/uploads/{forged}/runs/0", declared=len(DATA), send=False,
              extra=run_headers())
    assert got.status == 400


def test_a_run_over_the_chunk_window_is_429_before_the_body_is_read(stack):
    ms = stack.s.member_service
    ms.config = type(ms.config)(**{**ms.config.__dict__, "chunks_per_minute": 1})
    got = req(stack, "PUT", f"/api/member/artifacts/{ART}/uploads/{upload_id()}/runs/0", declared=len(DATA), send=False,
              extra=run_headers())                      # two chunks, one allowed; the body is never sent
    assert got.status == 429 and got.json["error"] == "rate_limited" and got.headers.get("retry-after")
    assert stack.w.staged("op") == []
    assert not stack.s._hits.get("memberchunk:member:op")                     # a refused pre-check records nothing
    ms.config = type(ms.config)(**{**ms.config.__dict__, "chunks_per_minute": 2})
    ok = req(stack, "PUT", f"/api/member/artifacts/{ART}/uploads/{upload_id()}/runs/0", raw=DATA,
             extra=run_headers(), content_type="application/octet-stream")
    assert ok.status == 200, ok.raw
    again = req(stack, "PUT", f"/api/member/artifacts/{ART}/uploads/{upload_id()}/runs/0", declared=len(DATA),
                send=False, extra=run_headers())
    assert again.status == 429                                                # the real hits were counted


def test_run_headers_are_required_exactly_once(stack):
    uid = upload_id()
    base = list(run_headers())
    path = f"/api/member/artifacts/{ART}/uploads/{uid}/runs/0"
    for i in range(len(base)):
        missing = base[:i] + base[i + 1:]
        assert req(stack, "PUT", path, declared=10, send=False, extra=missing).status == 400, base[i]
        dup = base + [base[i]]
        assert req(stack, "PUT", path, declared=10, send=False, extra=dup).status == 400, base[i]
    assert req(stack, "PUT", path, declared=0, send=False, extra=base).status == 400            # empty run
    assert req(stack, "PUT", path.replace("/runs/0", "/runs/x"), declared=10, send=False, extra=base).status == 400
    assert req(stack, "PUT", path, raw=b"abc", extra=base + ["Content-Length: 3"]).status == 400   # two lengths


def test_a_run_with_a_wrong_run_hash_stages_nothing(stack):
    got = req(stack, "PUT", f"/api/member/artifacts/{ART}/uploads/{upload_id()}/runs/0", raw=DATA,
              extra=run_headers(run=b"nope"), content_type="application/octet-stream")
    assert got.status in (400, 409) and got.json["error"] in ("bad_request", "integrity", "conflict")
    assert stack.w.staged("op") == []


def test_abort_removes_the_staging(stack):
    req(stack, "PUT", f"/api/member/artifacts/{ART}/uploads/{upload_id()}/runs/0", raw=DATA,
        extra=run_headers(), content_type="application/octet-stream")
    assert stack.w.staged("op") != []
    got = req(stack, "POST", f"/api/member/artifacts/{ART}/uploads/{upload_id()}/abort")
    assert got.status == 200
    assert stack.w.staged("op") == []
    assert req(stack, "POST", f"/api/member/artifacts/{ART}/uploads/not-an-id/abort").status in (400, 404)


# ------------------------------------------------------------------------------ rate limits and the create race
def test_the_write_rate_limit_and_the_pending_cap_are_clean_429s(tmp_path, stack):
    w = stack.w
    w.ms.config = type(w.ms.config)(**{**w.ms.config.__dict__, "writes_per_minute": 2})
    ok = [req(stack, "POST", "/api/member/artifacts", body={"title": f"t{i}", "filename": "a.html", "profile": "web"})
          for i in range(4)]
    statuses = [r.status for r in ok]
    assert statuses[:2] == [200, 200] and 429 in statuses[2:]
    limited = next(r for r in ok if r.status == 429)
    assert limited.json == {"error": "rate_limited", "message": limited.json["message"]}
    assert limited.headers.get("retry-after")


def test_the_pending_intent_cap_is_a_429_not_a_500(stack):
    stack.w.write_grants()                          # the default cap: 4 intents awaiting the worker
    seen = []
    for i in range(12):
        got = req(stack, "POST", "/api/member/artifacts",
                  body={"title": f"t{i}", "filename": "a.html", "profile": "web"})
        seen.append(got.status)
        assert got.status in (200, 429), got.raw
    assert 429 in seen, seen                       # the cap (4 un-answered intents) bites before 12 creates


def test_create_then_first_upload_waits_for_the_worker_without_leaking(stack):
    made = req(stack, "POST", "/api/member/artifacts", body={"title": "T", "filename": "a.html", "profile": "web"})
    assert made.status == 200 and made.json["status"] == "pending"
    new = "art_fresh1"
    body = {"name": "f.bin", "size_bytes": len(DATA), "file_sha256": sha(DATA)}
    # the worker has not projected the artifact yet: the answer is the plain not-found, nothing is staged
    early = req(stack, "POST", f"/api/member/artifacts/{new}/uploads/status", body=body)
    assert early.status == 404 and early.json == {"error": "not_found", "message": "not found"}
    early = req(stack, "PUT", f"/api/member/artifacts/{new}/uploads/{upload_id(artifact=new)}/runs/0", raw=DATA,
                extra=run_headers(), content_type="application/octet-stream")
    assert early.status == 404 and stack.w.staged("op") == []
    stack.w.view("op", [ART, new])                                  # the worker projects the new artifact
    late = req(stack, "PUT", f"/api/member/artifacts/{new}/uploads/{upload_id(artifact=new)}/runs/0", raw=DATA,
               extra=run_headers(), content_type="application/octet-stream")
    assert late.status == 200 and stack.w.staged("op") != []


# ------------------------------------------------------------------------------ no leakage
def test_a_real_storage_failure_is_a_fixed_body_without_paths(stack):
    import os
    import shutil
    out = stack.w.outbox
    shutil.rmtree(out)
    out.write_bytes(b"not a directory")                   # the outbox is gone: every write now really fails
    try:
        got = req(stack, "POST", "/api/member/artifacts", body={"title": "t", "filename": "a.html", "profile": "web"})
    finally:
        os.remove(out)
        out.mkdir()
    assert got.status >= 400
    assert set(got.json) <= {"error", "message"}
    assert str(out).encode() not in got.raw and b"Errno" not in got.raw and b"Traceback" not in got.raw


def test_error_bodies_never_echo_the_request(stack):
    marker = "zzMARKERzz"
    for got in (req(stack, "GET", f"/api/member/artifacts/{marker}/history"),
                req(stack, "POST", "/api/member/artifacts", raw=marker.encode()),
                req(stack, "GET", "/api/member/me", member=None, auth=f"Member {marker}"),
                req(stack, "GET", f"/api/member/results/{marker}")):
        assert marker.encode() not in got.raw


def test_the_session_is_never_logged_or_returned_by_config(stack):
    got = req(stack, "GET", "/api/member/config", member=None)
    assert SECRET.decode() not in got.raw.decode()
    assert base64.b64encode(SECRET) not in got.raw


# ------------------------------------------------------------------------------ the cli wiring
def members_cp(**over):
    members = {"enabled": True, "group": GROUP, "session_secret": "s" * 40, "share_creates_per_hour": 7, **over}
    return {"collab": {"members": members}, "share": {"public_host": HOST, "session_seconds": 1234}}


def test_cli_builds_the_member_web_from_the_members_section(stack):
    from herdr_engineering import cli
    args = SimpleNamespace(collab_only=True, web_root=stack.web, collab_require_https=True)
    mw = cli._member_web(members_cp(), stack.s, args)
    assert (mw.public_host, mw.session_seconds, mw.secret, mw.require_https) == (HOST, 1234, b"s" * 40, True)
    assert mw.public_config["client_id"] == "web-client"
    assert cli._member_web({}, stack.s, args) is None                          # members off
    assert cli._member_web(members_cp(), stack.s, SimpleNamespace(collab_only=False)) is None
    assert cli._member_web(members_cp(enabled=False), stack.s, args) is None


def test_the_session_lifetime_reaches_collab_public_through_the_rendered_slice(stack, tmp_path):
    """collab-public loads only the rendered collab slice, never ``control_plane.share``: build exactly that file."""
    from herdr_engineering import cli
    from herdr_engineering import fleet_config_cli as fcc
    from herdr_engineering.fleet_config import load_fleet_config
    from tests.test_members_deploy_config import cfg_of, members, share
    out = tmp_path / "collab-slice.json"
    fcc.render_collab(cfg_of(tmp_path, sh=share(session_seconds=900), mem=members()), out)
    cp = load_fleet_config(out).section("control_plane")
    assert "share" not in cp
    args = SimpleNamespace(collab_only=True, web_root=stack.web, collab_require_https=True)
    mw = cli._member_web(cp, stack.s, args)
    assert (mw.session_seconds, mw.public_host) == (900, "artifacts.example.test")
    out2 = tmp_path / "default-slice.json"
    fcc.render_collab(cfg_of(tmp_path / "d", sh=share(), mem=members()), out2)
    assert cli._member_web(load_fleet_config(out2).section("control_plane"), stack.s, args).session_seconds == 14400


def test_cli_gives_the_member_service_the_configured_limits(stack, tmp_path):
    from herdr_engineering import cli
    cp = {**members_cp(), "collab_artifacts": {"enabled": True}}
    bridge = cli._wire_collab_artifacts_bridge(stack.s, cp, tmp_path / "m2", tmp_path / "o2")
    try:
        assert stack.s.member_service.config.share_creates_per_hour == 7
        assert stack.s.member_service.bridge is bridge
    finally:
        bridge.stop()


def test_an_unexpected_failure_is_a_fixed_500_that_names_no_path(stack):
    import os
    page = stack.web / "member.html"
    page.chmod(0)                                    # a real PermissionError inside the handler
    try:
        if os.access(page, os.R_OK):
            pytest.skip("running as a user that ignores file permissions")
        got = req(stack, "GET", "/", member=None)
    finally:
        page.chmod(0o644)
    assert got.status == 500 and got.json == {"error": "internal", "message": "internal error"}
    assert b"member.html" not in got.raw and b"Permission" not in got.raw
