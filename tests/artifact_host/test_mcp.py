"""The agent MCP endpoint: a JSON-RPC client over real HTTP against a real PublicServer and real Postgres."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from pathlib import Path
from typing import Any

from herdr_artifact_host.mcp import PROTOCOL
from herdr_artifact_host.member_api import (
    CHUNK_BYTES,
    Actor,
    ActorKind,
    MemberError,
    MemberLimits,
)
from tests.artifact_host.member_support import (
    ORIGIN,
    call,
    published,
    sha,
    sign_in,
)
from tests.artifact_host.seed import Host, Resp, access, ident, running_host

LOTS = MemberLimits(writes_per_minute=10_000, chunks_per_minute=100_000)


def mint(host: Host, session: str, label: str = "laptop") -> dict[str, Any]:
    r = call(host, session, "POST", "/agent-tokens", {"label": label})
    assert r.status == 200, r.body
    return r.json()


class Agent:
    """A minimal MCP client (Streamable HTTP, JSON responses): handshake, tools/list, tools/call."""

    def __init__(
        self,
        host: Host,
        token: str | None,
        *,
        scheme: str = "Bearer",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.host, self.token, self.scheme, self._id = host, token, scheme, 0
        self.extra = dict(headers or {})

    def post(self, message: Any) -> Resp:
        headers = {"Content-Type": "application/json", **self.extra}
        if self.token is not None:
            headers["Authorization"] = f"{self.scheme} {self.token}"
        return self.host.request("POST", "/mcp/artifacts", headers, json.dumps(message).encode())

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Resp:
        self._id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        return self.post(msg)

    def initialize(self) -> Resp:
        r = self.rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        )
        if r.status == 200:
            assert (
                self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}).status == 202
            )
        return r

    def tools(self) -> list[str]:
        return [t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]]

    def tool(self, name: str, /, **args: Any) -> tuple[bool, dict[str, Any]]:
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        assert r.status == 200, (r.status, r.body)
        res = r.json()["result"]
        return res["isError"], json.loads(res["content"][0]["text"])

    def ok(self, name: str, /, **args: Any) -> dict[str, Any]:
        err, body = self.tool(name, **args)
        assert not err, body
        return body

    def push(self, title: str, files: dict[str, bytes]) -> tuple[str, dict[str, Any]]:
        aid = self.ok("artifact_create", title=title, filename="index.html", profile="web")[
            "artifact_id"
        ]
        specs = []
        for name, data in files.items():
            spec = {"name": name, "size_bytes": len(data), "file_sha256": sha(data)}
            total = max(1, -(-len(data) // CHUNK_BYTES))
            for i in range(total):
                part = data[i * CHUNK_BYTES : (i + 1) * CHUNK_BYTES]
                self.ok(
                    "artifact_upload_chunk",
                    artifact_id=aid,
                    **spec,
                    index=i,
                    total=total,
                    sha256=sha(part),
                    data_b64=base64.b64encode(part).decode(),
                )
            specs.append(spec)
        pub = self.ok("artifact_publish", artifact_id=aid, files=specs, message="first")
        return aid, pub


def agent_for(host: Host, user: str = "alice") -> tuple[Agent, str, str]:
    s = sign_in(host, user)
    minted = mint(host, s)
    a = Agent(host, minted["token"])
    assert a.initialize().status == 200
    return a, s, minted["token"]


# ---------------------------------------------------------------- tokens


def test_mint_shows_the_secret_once_and_lists_without_secrets(host: Host) -> None:
    s = sign_in(host)
    minted = mint(host, s, "ci box")
    token = minted["token"]
    assert token.startswith("hat_") and minted["label"] == "ci box"
    listing = call(host, s, "GET", "/agent-tokens")
    assert listing.status == 200
    (row,) = listing.json()["tokens"]
    assert set(row) == {"token_id", "label", "created_at", "last_used_at", "revoked"}
    assert row["token_id"] == minted["token_id"] and row["revoked"] is False
    stored = host.rows("SELECT digest FROM agent_tokens")[0]["digest"]
    assert token not in listing.body.decode() and stored not in listing.body.decode()
    assert token not in stored  # only a digest is stored
    # last_used moves once the token is used
    Agent(host, token).initialize()
    assert call(host, s, "GET", "/agent-tokens").json()["tokens"][0]["last_used_at"] is not None


def test_mint_and_revoke_need_a_member_session_and_origin(host: Host) -> None:
    s = sign_in(host)
    assert call(host, None, "POST", "/agent-tokens", {"label": "x"}).status == 401
    assert call(host, s, "POST", "/agent-tokens", {"label": "x"}, origin=False).status == 403
    minted = mint(host, s)
    tid = minted["token_id"]
    # the agent token itself cannot mint, list or revoke
    for method, path in (("GET", "/agent-tokens"), ("POST", f"/agent-tokens/{tid}/revoke")):
        r = host.request(
            method,
            "/api/member" + path,
            {"Authorization": f"Bearer {minted['token']}", **ORIGIN},
            b"" if method == "POST" else None,
        )
        assert r.status == 401
    assert call(host, s, "POST", f"/agent-tokens/{tid}/revoke", origin=False).status == 403
    assert call(host, s, "POST", f"/agent-tokens/{tid}/revoke").status == 200
    assert call(host, s, "GET", "/agent-tokens").json()["tokens"][0]["revoked"] is True
    # someone else's token id is the same 404 as a missing one
    other = sign_in(host, "bob")
    assert call(host, other, "POST", f"/agent-tokens/{tid}/revoke").status == 404
    assert call(host, other, "POST", "/agent-tokens/atk_0000000000000000/revoke").status == 404


def test_session_and_token_are_not_interchangeable(host: Host) -> None:
    s = sign_in(host)
    minted = mint(host, s)
    assert (
        call(
            host, None, "GET", "/me", headers={"Authorization": f"Bearer {minted['token']}"}
        ).status
        == 401
    )
    assert Agent(host, s, scheme="Member").rpc("tools/list").status == 401
    assert Agent(host, s).rpc("tools/list").status == 401


def test_revoked_unknown_and_malformed_tokens_are_the_same_401(host: Host) -> None:
    s = sign_in(host)
    minted = mint(host, s)
    assert Agent(host, minted["token"]).initialize().status == 200
    assert call(host, s, "POST", f"/agent-tokens/{minted['token_id']}/revoke").status == 200
    revoked = Agent(host, minted["token"]).rpc("tools/list")
    unknown = Agent(host, "hat_" + "A" * 43).rpc("tools/list")
    malformed = Agent(host, "nope").rpc("tools/list")
    missing = Agent(host, None).rpc("tools/list")
    assert revoked.status == 401
    shapes = {r.shape() for r in (revoked, unknown, malformed, missing)}
    assert len(shapes) == 1


def test_a_member_removed_from_the_group_loses_agent_access(host: Host) -> None:
    agent, _, _ = agent_for(host, "alice")
    assert agent.rpc("tools/list").status == 200
    (host.sync_dir / "members.json").write_text(
        json.dumps({"updated": time.time(), "groups": {"local.dev": ["bob"]}})
    )
    # the authenticator caches an "allowed" for its recheck period (up to a minute); drop it to see the new directory
    host.surface.member.s.authenticator._checked.clear()
    assert agent.rpc("tools/list").status == 401


def test_token_cap_per_member(host: Host) -> None:
    s = sign_in(host)
    for _ in range(10):
        mint(host, s)
    r = call(host, s, "POST", "/agent-tokens", {"label": "one too many"})
    assert r.status == 429 and r.json()["error"] == "token_cap"


# ---------------------------------------------------------------- protocol and tools


def test_protocol_and_tool_list_without_approve(host: Host) -> None:
    agent, _, _ = agent_for(host)
    init = agent.initialize().json()["result"]
    assert init["protocolVersion"] == PROTOCOL and "tools" in init["capabilities"]
    names = agent.tools()
    assert "artifact_create" in names and "artifact_publish" in names and "artifact_search" in names
    assert not [n for n in names if "approve" in n]
    err, body = agent.tool("artifact_share_approve", artifact_id="x", share_id="y", confirm=True)
    assert err and body["error"] == "not_found"
    assert agent.rpc("nope").json()["error"]["code"] == -32601
    assert agent.rpc("ping").json()["result"] == {}
    assert agent.post([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).status == 400
    assert host.request("GET", "/mcp/artifacts", {"Authorization": "Bearer x"}).status == 405


def test_body_rules(host: Host) -> None:
    agent, _, token = agent_for(host)
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    big = b" " * (1024 * 1024 + 1)
    assert host.request("POST", "/mcp/artifacts", auth, big).status == 413
    # any Content-Encoding is the uniform 404 for the whole host (never decompressed)
    assert (
        host.request("POST", "/mcp/artifacts", {**auth, "Content-Encoding": "gzip"}, b"{}").status
        == 404
    )
    assert (
        host.request(
            "POST", "/mcp/artifacts", {**auth, "Origin": "https://evil.example"}, b"{}"
        ).status
        == 403
    )
    assert host.request("POST", "/mcp/artifacts", auth, b"{not json").status == 400
    err, body = agent.tool(
        "artifact_create", title="t", filename="a.html", profile="web", surprise=1
    )
    assert err and body["error"] == "bad_request"


def test_create_and_push_lands_pending(host: Host) -> None:
    agent, session, _ = agent_for(host)
    aid, pub = agent.push("From agent", {"index.html": b"<html>agent</html>"})
    assert pub["version_number"] == 1 and pub["ticket"]
    (row,) = host.rows("SELECT owner, intake, created_by FROM artifacts WHERE artifact_id=$1", aid)
    assert row == {"owner": "alice", "intake": "pending", "created_by": "alice"}  # bare member id
    assert agent.ok("artifact_result", ticket=pub["ticket"])["version_id"] == pub["version_id"]
    assert agent.ok("artifact_head", artifact_id=aid)["intake"] == "pending"
    # the owner sees it as pending in the web library and accepts it
    lib = call(host, session, "GET", "/library?sources=pending").json()["items"]
    assert [i["artifact_id"] for i in lib] == [aid]
    assert call(host, session, "POST", f"/pending/{aid}/accept").status == 200
    # search finds accepted work, never pending
    assert [i["artifact_id"] for i in agent.ok("artifact_search", q="agent")["items"]] == [aid]
    agent2_aid, _ = agent.push("Another", {"index.html": b"x"})
    assert agent2_aid not in [i["artifact_id"] for i in agent.ok("artifact_search")["items"]]
    assert agent.ok("artifact_delete", artifact_id=agent2_aid, confirm=True)["deleted"] is True


def test_a_multi_chunk_file_resumes_and_publishes(host: Host) -> None:
    agent, _, _ = agent_for(host)
    data = bytes(range(256)) * 5000  # three chunks
    aid = agent.ok("artifact_create", title="Big", filename="big.bin", profile="web")["artifact_id"]
    spec = {"name": "big.bin", "size_bytes": len(data), "file_sha256": sha(data)}
    part = data[CHUNK_BYTES : 2 * CHUNK_BYTES]
    agent.ok(
        "artifact_upload_chunk",
        artifact_id=aid,
        **spec,
        index=1,
        total=3,
        sha256=sha(part),
        data_b64=base64.b64encode(part).decode(),
    )
    assert agent.ok("artifact_upload_status", artifact_id=aid, **spec)["missing"] == [
        [0, 0],
        [2, 2],
    ]
    err, body = agent.tool("artifact_publish", artifact_id=aid, files=[spec])
    assert err and body["error"] == "incomplete_upload"
    for i in (0, 2):
        part = data[i * CHUNK_BYTES : (i + 1) * CHUNK_BYTES]
        agent.ok(
            "artifact_upload_chunk",
            artifact_id=aid,
            **spec,
            index=i,
            total=3,
            sha256=sha(part),
            data_b64=base64.b64encode(part).decode(),
        )
    assert agent.ok("artifact_publish", artifact_id=aid, files=[spec])["size_bytes"] == len(data)
    # a bad chunk hash and bad base64 are refused
    err, body = agent.tool(
        "artifact_upload_chunk",
        artifact_id=aid,
        **spec,
        index=0,
        total=3,
        sha256=sha(b"x"),
        data_b64=base64.b64encode(data[:CHUNK_BYTES]).decode(),
    )
    assert err and body["error"] == "bad_request"
    err, body = agent.tool(
        "artifact_upload_chunk",
        artifact_id=aid,
        **spec,
        index=0,
        total=3,
        sha256=sha(b"x"),
        data_b64="!!!",
    )
    assert err and body["error"] == "bad_request"


def test_publish_failure_reaps_orphan_blobs(host: Host) -> None:
    agent, _, _ = agent_for(host)
    aid = agent.ok("artifact_create", title="t", filename="a.html", profile="web")["artifact_id"]
    data = b"orphan candidate"
    spec = {"name": "a.html", "size_bytes": len(data), "file_sha256": sha(data)}
    agent.ok(
        "artifact_upload_chunk",
        artifact_id=aid,
        **spec,
        index=0,
        total=1,
        sha256=sha(data),
        data_b64=base64.b64encode(data).decode(),
    )
    err, body = agent.tool(
        "artifact_publish", artifact_id=aid, files=[spec], base_version_id="ver_stale"
    )
    assert err and body["error"] == "stale_base"
    assert host.rows("SELECT 1 FROM artifact_blobs WHERE sha256=$1", sha(data)) == []
    assert not list((host.blob_root).rglob(sha(data)))


# ---------------------------------------------------------------- isolation


def test_an_agent_cannot_touch_another_members_artifact(host: Host) -> None:
    bob_agent, _, _ = agent_for(host, "bob")
    bob_aid, _ = bob_agent.push("Bob private", {"index.html": b"<html>bob</html>"})
    alice_agent, alice_session, _ = agent_for(host, "alice")
    spec = {"name": "index.html", "size_bytes": 3, "file_sha256": sha(b"abc")}
    attempts: list[tuple[str, dict[str, Any]]] = [
        ("artifact_head", {"artifact_id": bob_aid}),
        ("artifact_history", {"artifact_id": bob_aid}),
        ("artifact_delete", {"artifact_id": bob_aid, "confirm": True}),
        ("artifact_publish", {"artifact_id": bob_aid, "files": [spec]}),
        ("artifact_upload_status", {"artifact_id": bob_aid, **spec}),
        (
            "artifact_upload_chunk",
            {
                "artifact_id": bob_aid,
                **spec,
                "index": 0,
                "total": 1,
                "sha256": sha(b"abc"),
                "data_b64": base64.b64encode(b"abc").decode(),
            },
        ),
        ("artifact_upload_abort", {"artifact_id": bob_aid, "upload_id": "upl_" + "0" * 24, **spec}),
        ("artifact_share_list", {"artifact_id": bob_aid}),
        ("artifact_share_revoke", {"artifact_id": bob_aid, "share_id": "shr_x"}),
        ("artifact_share_events", {"artifact_id": bob_aid, "share_id": "shr_x"}),
        (
            "artifact_share_create",
            {"artifact_id": bob_aid, "version_id": "ver_x", "audience": "public"},
        ),
    ]
    for name, args in attempts:
        err, body = alice_agent.tool(name, **args)
        err2, body2 = alice_agent.tool(name, **{**args, "artifact_id": "art_doesnotexist"})
        assert err and err2, name
        assert body == body2 and body["error"] == "not_found", (name, body, body2)
    assert [i["title"] for i in alice_agent.ok("artifact_search")["items"]] == []
    assert (
        host.rows("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", bob_aid)[0]["lifecycle"]
        == "active"
    )
    assert call(host, alice_session, "GET", "/library").json()["items"] == []


# ---------------------------------------------------------------- shares


def accepted_push(agent: Agent, host: Host, session: str, title: str = "Pushed") -> tuple[str, str]:
    """An artifact the agent created and the owner accepted: (artifact_id, version_id)."""
    aid, pub = agent.push(title, {"index.html": b"<html><body>agent made</body></html>"})
    assert call(host, session, "POST", f"/pending/{aid}/accept").status == 200
    return aid, pub["version_id"]


def on_loop(host: Host, coro: Any) -> Any:
    assert host.loop is not None
    return asyncio.run_coroutine_threadsafe(coro, host.loop).result(15)


def test_an_agent_share_is_pending_and_cannot_be_approved_by_it(host: Host) -> None:
    agent, session, _ = agent_for(host)
    aid, vid = accepted_push(agent, host, session)
    made = agent.ok("artifact_share_create", artifact_id=aid, version_id=vid, audience="public")
    assert made["state"] == "pending"
    row = host.rows(
        "SELECT state, created_by, created_via FROM shares WHERE share_id=$1", made["share_id"]
    )[0]
    assert row == {"state": "pending", "created_by": "alice", "created_via": "agent"}
    assert not [t for t in agent.tools() if "approve" in t]
    listed = agent.ok("artifact_share_list", artifact_id=aid)["links"]
    assert [x["state"] for x in listed] == ["pending"]
    # the same agent actor is refused by the service itself, not only by the missing tool
    try:
        on_loop(
            host,
            host.surface.member.svc.share_approve(
                Actor(ActorKind.AGENT, "alice"), aid, made["share_id"], True
            ),
        )
    except MemberError as exc:
        assert exc.status == 403
    else:
        raise AssertionError("an agent approved a share")
    assert (
        host.rows("SELECT state FROM shares WHERE share_id=$1", made["share_id"])[0]["state"]
        == "pending"
    )
    approved = call(
        host,
        session,
        "POST",
        f"/artifacts/{aid}/shares/{made['share_id']}/approve",
        {"confirm": True},
    )
    assert approved.status == 200
    assert (
        agent.ok("artifact_share_revoke", artifact_id=aid, share_id=made["share_id"])["state"]
        == "revoked"
    )
    assert agent.ok("artifact_share_events", artifact_id=aid, share_id=made["share_id"])["events"]


# ---------------------------------------------------------------- caps and secrets


def test_the_26th_pending_is_refused(migrated_dsn, tmp_path) -> None:
    with running_host(migrated_dsn, tmp_path, {"member_limits": LOTS}) as host:
        agent, _, _ = agent_for(host)
        for i in range(25):
            agent.ok("artifact_create", title=f"p{i}", filename="a.html", profile="web")
        err, body = agent.tool("artifact_create", title="p25", filename="a.html", profile="web")
        assert err and body["error"] == "pending_cap" and body["status"] == 429
        assert len(host.rows("SELECT 1 FROM artifacts WHERE intake='pending'")) == 25


def test_26_concurrent_creates_admit_exactly_25(migrated_dsn, tmp_path) -> None:
    with running_host(migrated_dsn, tmp_path, {"member_limits": LOTS}) as host:
        agent, _, _ = agent_for(host)
        results: list[tuple[bool, dict[str, Any]]] = []
        lock = threading.Lock()
        start = threading.Barrier(26)

        def go(i: int) -> None:
            start.wait()
            got = agent.tool("artifact_create", title=f"c{i}", filename="a.html", profile="web")
            with lock:
                results.append(got)

        threads = [threading.Thread(target=go, args=(i,)) for i in range(26)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert len(results) == 26
        assert sum(1 for err, _ in results if not err) == 25
        refused = [b for err, b in results if err]
        assert len(refused) == 1 and refused[0]["error"] == "pending_cap"
        assert len(host.rows("SELECT 1 FROM artifacts WHERE intake='pending'")) == 25


def test_agent_secrets_appear_only_in_the_mint_response(host: Host) -> None:
    s = sign_in(host)
    minted = mint(host, s)
    token = minted["token"]
    digest = host.rows("SELECT digest FROM agent_tokens")[0]["digest"]
    agent = Agent(host, token)
    seen: list[bytes] = []
    seen.append(agent.initialize().body)
    seen.append(agent.rpc("tools/list").body)
    aid, pub = agent.push("t", {"index.html": b"<html>x</html>"})
    for name, args in (
        ("artifact_head", {"artifact_id": aid}),
        ("artifact_history", {"artifact_id": aid}),
        ("artifact_search", {}),
        ("artifacts_mine", {}),
        ("artifact_share_list", {}),
        ("artifact_result", {"ticket": pub["ticket"]}),
    ):
        seen.append(agent.rpc("tools/call", {"name": name, "arguments": args}).body)
    seen.append(call(host, s, "GET", "/agent-tokens").body)
    seen.append(call(host, s, "GET", "/me").body)
    seen.append(call(host, s, "POST", f"/agent-tokens/{minted['token_id']}/revoke").body)
    seen.append(Agent(host, token).rpc("tools/list").body)  # now revoked
    for body in seen:
        assert token.encode() not in body and digest.encode() not in body


def test_per_token_rate_limit(migrated_dsn, tmp_path) -> None:
    with running_host(migrated_dsn, tmp_path) as host:
        host.surface.mcp.per_token.limit = 5
        agent, _, _ = agent_for(host)  # initialize + notification used 2
        statuses = [agent.rpc("ping").status for _ in range(6)]
        assert statuses.count(429) >= 1 and statuses[0] == 200


# ---------------------------------------------------------------- fix round 1


def test_a_blocked_client_gets_429_and_other_clients_are_unaffected(host: Host) -> None:
    _, _, token = agent_for(host)
    bad = Agent(host, "hat_" + "B" * 43, headers={"X-Forwarded-For": "10.9.0.1"})
    codes = [bad.rpc("ping").status for _ in range(60)]
    assert codes == [401] * 60
    blocked = bad.rpc("ping")
    assert blocked.status == 429 and blocked.header("Retry-After")
    # a valid token from the blocked address is held back too; from another address it works
    assert Agent(host, token, headers={"X-Forwarded-For": "10.9.0.1"}).rpc("ping").status == 429
    assert Agent(host, token, headers={"X-Forwarded-For": "10.9.0.2"}).rpc("ping").status == 200


def test_an_agent_share_response_carries_no_token_or_url(host: Host) -> None:
    agent, session, token = agent_for(host)
    aid, vid = accepted_push(agent, host, session)
    made = agent.ok(
        "artifact_share_create", artifact_id=aid, version_id=vid, audience="public", title="Hello"
    )
    assert set(made) == {"share_id", "state", "audience", "expires_at", "title"}
    assert made["state"] == "pending" and made["title"] == "Hello"
    assert host.rows("SELECT token_hash FROM shares WHERE share_id=$1", made["share_id"]) == [
        {"token_hash": None}
    ]
    listed = agent.rpc("tools/call", {"name": "artifact_share_list", "arguments": {}}).body
    assert b"token" not in listed and b"/s/" not in listed


def test_the_member_gets_the_link_once_at_approve_and_it_serves_content(host: Host) -> None:
    agent, session, _ = agent_for(host)
    aid, vid = accepted_push(agent, host, session)
    made = agent.ok("artifact_share_create", artifact_id=aid, version_id=vid, audience="public")
    path = f"/artifacts/{aid}/shares/{made['share_id']}/approve"
    key = "approve-key-1"
    r = call(host, session, "POST", path, {"confirm": True, "idempotency_key": key})
    assert r.status == 200
    body = r.json()
    assert (
        body["state"] == "live" and body["token"] and body["url"].endswith(f"/s/{body['token']}/")
    )
    assert host.get(f"/s/{body['token']}/").status == 302
    served = host.get(f"/s/{body['token']}/f/index.html")
    assert served.status == 200 and b"agent made" in served.body
    # only a digest is stored, and the agent never sees the token in any tool
    seen = b"".join(
        agent.rpc("tools/call", {"name": n, "arguments": a}).body
        for n, a in (
            ("artifact_share_list", {}),
            ("artifact_share_events", {"artifact_id": aid, "share_id": made["share_id"]}),
            ("artifact_head", {"artifact_id": aid}),
        )
    )
    assert body["token"].encode() not in seen
    # the same approve again is not a second link
    again = call(host, session, "POST", path, {"confirm": True, "idempotency_key": key})
    assert again.status == 200 and "token" not in again.json()


def test_a_web_made_share_still_shows_its_link_at_creation_only(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s)
    made = call(
        host, s, "POST", f"/artifacts/{aid}/shares", {"version_id": vid, "audience": "public"}
    )
    assert made.status == 200 and made.json()["token"] and made.json()["url"]
    approved = call(
        host,
        s,
        "POST",
        f"/artifacts/{aid}/shares/{made.json()['share_id']}/approve",
        {"confirm": True},
    )
    assert approved.status == 200 and "token" not in approved.json()


def test_an_agent_mutates_only_what_agents_created(host: Host) -> None:
    agent, session, _ = agent_for(host)
    web_aid, web_vid = published(host, session)  # made through the web page
    web_share = call(
        host,
        session,
        "POST",
        f"/artifacts/{web_aid}/shares",
        {"version_id": web_vid, "audience": "public"},
    ).json()["share_id"]
    spec = {"name": "index.html", "size_bytes": 3, "file_sha256": sha(b"abc")}
    attempts: list[tuple[str, dict[str, Any]]] = [
        ("artifact_delete", {"artifact_id": web_aid, "confirm": True}),
        ("artifact_publish", {"artifact_id": web_aid, "files": [spec]}),
        ("artifact_upload_status", {"artifact_id": web_aid, **spec}),
        (
            "artifact_upload_chunk",
            {
                "artifact_id": web_aid,
                **spec,
                "index": 0,
                "total": 1,
                "sha256": sha(b"abc"),
                "data_b64": base64.b64encode(b"abc").decode(),
            },
        ),
        ("artifact_upload_abort", {"artifact_id": web_aid, "upload_id": "upl_" + "0" * 24, **spec}),
        (
            "artifact_share_create",
            {"artifact_id": web_aid, "version_id": web_vid, "audience": "public"},
        ),
        ("artifact_share_revoke", {"artifact_id": web_aid, "share_id": web_share}),
    ]
    for name, args in attempts:
        err, body = agent.tool(name, **args)
        err2, body2 = agent.tool(name, **{**args, "artifact_id": "art_doesnotexist"})
        assert err and err2 and body == body2 and body["error"] == "not_found", (name, body)
    assert host.rows("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", web_aid)[0] == {
        "lifecycle": "active"
    }
    assert host.rows("SELECT state FROM shares WHERE share_id=$1", web_share)[0] == {
        "state": "pending"
    }
    # an agent-made artifact stays the agent's after the owner accepts it; a web share on it is not
    own, own_vid = accepted_push(agent, host, session)
    web_on_own = call(
        host,
        session,
        "POST",
        f"/artifacts/{own}/shares",
        {"version_id": own_vid, "audience": "public"},
    ).json()["share_id"]
    err, body = agent.tool("artifact_share_revoke", artifact_id=own, share_id=web_on_own)
    assert err and body["error"] == "not_found"
    agent_share = agent.ok(
        "artifact_share_create", artifact_id=own, version_id=own_vid, audience="public"
    )["share_id"]
    assert (
        agent.ok("artifact_share_revoke", artifact_id=own, share_id=agent_share)["state"]
        == "revoked"
    )
    assert agent.ok("artifact_delete", artifact_id=own, confirm=True)["deleted"] is True
    # migration backfill: pending rows are agent made, everything else web made
    assert host.rows("SELECT created_via FROM artifacts WHERE artifact_id=$1", web_aid) == [
        {"created_via": "web"}
    ]


def test_tool_descriptions_state_the_scope(host: Host) -> None:
    agent, _, _ = agent_for(host)
    tools = {t["name"]: t for t in agent.rpc("tools/list").json()["result"]["tools"]}
    for name in (
        "artifact_delete",
        "artifact_publish",
        "artifact_share_create",
        "artifact_share_revoke",
    ):
        assert "created through an agent" in tools[name]["description"], name
    assert tools["artifact_create"]["inputSchema"]["required"] == ["title", "filename", "profile"]
    assert tools["artifact_create"]["inputSchema"]["properties"]["profile"]["enum"] == [
        "web",
        "document",
    ]


def test_profile_is_required_and_limited(host: Host) -> None:
    agent, _, _ = agent_for(host)
    for args in (
        {"title": "t", "filename": "a.html"},
        {"title": "t", "filename": "a.html", "profile": "other"},
    ):
        err, body = agent.tool("artifact_create", **args)
        assert err and body["error"] == "bad_request"
    assert agent.ok("artifact_create", title="t", filename="a.html", profile="document")


def test_search_filters_before_the_limit_and_counts_truthfully(host: Host) -> None:
    agent, session, _ = agent_for(host)
    accepted = [accepted_push(agent, host, session, f"Accepted {i}")[0] for i in range(3)]
    for i in range(4):  # newer pending ones must not crowd out the accepted
        agent.push(f"Pending {i}", {"index.html": b"p"})
    page = agent.ok("artifact_search", limit=2)
    assert len(page["items"]) == 2 and page["total"] == 3
    assert {i["artifact_id"] for i in page["items"]} <= set(accepted)
    # a folder can be named by its id or by its name
    folder = call(host, session, "POST", "/folders", {"name": "Reports"}).json()["folder_id"]
    call(host, session, "POST", f"/folders/{folder}/items", {"artifact_id": accepted[0]})
    for ref in (folder, "Reports"):
        found = agent.ok("artifact_search", folder=ref)
        assert [i["artifact_id"] for i in found["items"]] == [accepted[0]] and found["total"] == 1
    assert agent.ok("artifact_search", folder="Nope") == {"items": [], "total": 0}


def test_publish_and_share_create_are_idempotent(host: Host) -> None:
    agent, session, _ = agent_for(host)
    aid = agent.ok("artifact_create", title="t", filename="index.html", profile="web")[
        "artifact_id"
    ]
    data = b"<html>once</html>"
    spec = {"name": "index.html", "size_bytes": len(data), "file_sha256": sha(data)}
    agent.ok(
        "artifact_upload_chunk",
        artifact_id=aid,
        **spec,
        index=0,
        total=1,
        sha256=sha(data),
        data_b64=base64.b64encode(data).decode(),
    )
    first = agent.ok("artifact_publish", artifact_id=aid, files=[spec], idempotency_key="publish-1")
    again = agent.ok("artifact_publish", artifact_id=aid, files=[spec], idempotency_key="publish-1")
    assert again["version_id"] == first["version_id"]
    assert host.rows("SELECT 1 FROM artifact_versions WHERE artifact_id=$1", aid) != []
    assert len(host.rows("SELECT 1 FROM artifact_versions WHERE artifact_id=$1", aid)) == 1
    err, body = agent.tool(
        "artifact_publish",
        artifact_id=aid,
        files=[{**spec, "size_bytes": 1}],
        idempotency_key="publish-1",
    )
    assert err and body["error"] == "conflict"
    assert call(host, session, "POST", f"/pending/{aid}/accept").status == 200
    vid = first["version_id"]
    one = agent.ok(
        "artifact_share_create",
        artifact_id=aid,
        version_id=vid,
        audience="public",
        idempotency_key="share-key-1",
    )
    two = agent.ok(
        "artifact_share_create",
        artifact_id=aid,
        version_id=vid,
        audience="public",
        idempotency_key="share-key-1",
    )
    assert one == two
    assert len(host.rows("SELECT 1 FROM shares WHERE artifact_id=$1", aid)) == 1
    err, body = agent.tool(
        "artifact_share_create",
        artifact_id=aid,
        version_id=vid,
        audience="lab",
        allow_emails=["a@b.example"],
        idempotency_key="share-key-1",
    )
    assert err and body["error"] == "conflict"


def test_a_zero_byte_file_publishes_without_a_chunk(host: Host) -> None:
    agent, _, _ = agent_for(host)
    aid = agent.ok("artifact_create", title="t", filename="empty.txt", profile="document")[
        "artifact_id"
    ]
    spec = {"name": "empty.txt", "size_bytes": 0, "file_sha256": sha(b"")}
    assert agent.ok("artifact_upload_status", artifact_id=aid, **spec)["complete"] is True
    pub = agent.ok("artifact_publish", artifact_id=aid, files=[spec])
    assert pub["size_bytes"] == 0
    err, body = agent.tool(
        "artifact_upload_chunk",
        artifact_id=aid,
        **spec,
        index=0,
        total=1,
        sha256=sha(b""),
        data_b64="",
    )
    assert err and body["error"] == "bad_request"


def test_the_ticket_book_is_lru_and_bounded(host: Host) -> None:
    mcp = host.surface.mcp
    mcp.max_ticket_members = 3
    first = mcp._issue("m1", {"x": 1})["ticket"]
    mcp._issue("m2", {"x": 2})
    mcp._issue("m3", {"x": 3})
    assert mcp._ticket("m1", first) is not None  # a read keeps m1 fresh
    mcp._issue("m4", {"x": 4})  # evicts the least recently used member (m2)
    assert set(mcp._tickets) == {"m1", "m3", "m4"}
    mcp.tickets_per_member = 2
    t = [mcp._issue("m1", {"n": i})["ticket"] for i in range(3)]
    assert mcp._ticket("m1", t[0]) is None and mcp._ticket("m1", t[2]) is not None


def test_agent_token_housekeeping(migrated_dsn, tmp_path) -> None:
    with running_host(migrated_dsn, tmp_path) as host:
        s = sign_in(host, "alice")
        old = mint(host, s, "old")
        mint(host, s, "kept")
        bob = sign_in(host, "bob")
        mint(host, bob, "bobs")
        host.execute(
            "UPDATE agent_tokens SET revoked_at = now() - interval '91 days' WHERE token_id=$1",
            old["token_id"],
        )
        assert on_loop(host, host.surface.mcp.sweep_agent_tokens()) == 1
        assert len(host.rows("SELECT 1 FROM agent_tokens")) == 2
        (host.sync_dir / "members.json").write_text(
            json.dumps({"updated": time.time(), "groups": {"local.dev": ["bob"]}})
        )
        assert on_loop(host, host.surface.mcp.sweep_removed_members_agent_tokens()) == 1
        rows = {
            r["member_id"]: r["revoked_at"] is not None
            for r in host.rows("SELECT member_id, revoked_at FROM agent_tokens")
        }
        assert rows == {"alice": True, "bob": False}


def test_listing_puts_active_tokens_first_and_labels_are_capped(host: Host) -> None:
    s = sign_in(host)
    first = mint(host, s, "first")
    second = mint(host, s, "second")
    assert call(host, s, "POST", f"/agent-tokens/{second['token_id']}/revoke").status == 200
    ids = [t["token_id"] for t in call(host, s, "GET", "/agent-tokens").json()["tokens"]]
    assert ids == [first["token_id"], second["token_id"]]
    assert call(host, s, "POST", "/agent-tokens", {"label": "x" * 65}).status == 400
    assert call(host, s, "POST", "/agent-tokens", {"label": "x" * 64}).status == 200
    try:
        host.execute(
            "INSERT INTO agent_tokens (token_id, member_id, label, digest) VALUES ('atk_1', 'a', $1, repeat('0', 64))",
            "y" * 65,
        )
    except Exception as exc:
        assert "agent_tokens_label_len" in str(exc)
    else:
        raise AssertionError("the database accepted a 65 character label")


def test_last_used_is_gated_in_memory(host: Host) -> None:
    s = sign_in(host)
    minted = mint(host, s)
    agent = Agent(host, minted["token"])
    assert agent.rpc("ping").status == 200
    assert host.rows("SELECT last_used_at FROM agent_tokens")[0]["last_used_at"] is not None
    host.execute("UPDATE agent_tokens SET last_used_at = NULL")
    assert agent.rpc("ping").status == 200  # within the minute: no write at all
    assert host.rows("SELECT last_used_at FROM agent_tokens")[0]["last_used_at"] is None


def test_the_aggregate_rate_limit_is_per_member_across_tokens(host: Host) -> None:
    s = sign_in(host)
    one, two = Agent(host, mint(host, s)["token"]), Agent(host, mint(host, s)["token"])
    host.surface.mcp.per_member.limit = 6
    statuses = [(one if i % 2 else two).rpc("ping").status for i in range(10)]
    assert statuses[0] == 200 and statuses.count(429) >= 3
    other = Agent(host, mint(host, sign_in(host, "bob"))["token"])
    assert other.rpc("ping").status == 200


def test_mcp_public_hosts_are_accepted_for_the_endpoint_only(migrated_dsn, tmp_path) -> None:
    over = {"public_host": "share.example", "mcp_public_hosts": ("mcp.example",)}
    with running_host(migrated_dsn, tmp_path, over) as host:
        s = sign_in_on(host, "share.example")
        token = call(
            host, s, "POST", "/agent-tokens", {"label": "x"}, headers=on("share.example")
        ).json()["token"]
        for good in ("share.example", "mcp.example"):
            r = Agent(host, token, headers=on(good)).rpc("ping")
            assert r.status == 200, good
        for bad in ("evil.example", "mcp.example.evil.example"):
            assert Agent(host, token, headers=on(bad)).rpc("ping").status == 404, bad
        # the extra host serves nothing else
        assert host.request("GET", "/api/member/me", {**on("mcp.example")}).status == 404
        assert host.request("GET", "/s/abc/", on("mcp.example")).status == 404


def on(hostname: str) -> dict[str, str]:
    return {"Host": hostname}


def sign_in_on(host: Host, hostname: str, user: str = "alice") -> str:
    r = host.request(
        "POST",
        "/api/member/session",
        {
            **ORIGIN,
            "Host": hostname,
            "Authorization": f"Bearer {access(user, groups=('local.dev',))}",
            "X-Id-Token": ident(user, email=f"{user}@lab.example", nonce=f"n-{user}"),
            "X-Nonce": f"n-{user}",
        },
        b"",
    )
    assert r.status == 200, r.body
    return r.json()["session"]


# ---------------------------------------------------------------- fix round 2


def _members_json(host: Host, listed: list[str], *, age: float = 0.0) -> None:
    (host.sync_dir / "members.json").write_text(
        json.dumps({"updated": time.time() - age, "groups": {"local.dev": listed}})
    )


def _active_tokens(host: Host) -> int:
    return len(host.rows("SELECT 1 FROM agent_tokens WHERE revoked_at IS NULL"))


def test_the_removed_member_sweep_revokes_only_on_a_fresh_listing_without_the_member(
    migrated_dsn, tmp_path
) -> None:
    with running_host(migrated_dsn, tmp_path) as host:
        for who in ("alice", "bob"):
            s = sign_in(host, who)
            mint(host, s)
            mint(host, s)
        sweep = host.surface.mcp.sweep_removed_members_agent_tokens
        # no evidence: no directory file at all (a cold start or a deployment without the sync job)
        assert on_loop(host, sweep()) == 0 and _active_tokens(host) == 4
        # a stale directory (older than the sync's max age) says nothing
        _members_json(host, ["bob"], age=1000)
        assert on_loop(host, sweep()) == 0 and _active_tokens(host) == 4
        # an unreadable / malformed directory says nothing
        (host.sync_dir / "members.json").write_text("{not json")
        assert on_loop(host, sweep()) == 0 and _active_tokens(host) == 4
        (host.sync_dir / "members.json").write_text(json.dumps({"updated": time.time()}))
        assert on_loop(host, sweep()) == 0 and _active_tokens(host) == 4
        # a fresh but EMPTY listing looks like a failed sync, not a mass removal
        _members_json(host, [])
        assert on_loop(host, sweep()) == 0 and _active_tokens(host) == 4
        # a fresh listing that lacks alice revokes alice's tokens and nobody else's
        _members_json(host, ["bob"])
        assert on_loop(host, sweep()) == 2
        rows = host.rows("SELECT member_id, revoked_at IS NOT NULL AS gone FROM agent_tokens")
        assert {(r["member_id"], r["gone"]) for r in rows} == {("alice", True), ("bob", False)}
        assert on_loop(host, sweep()) == 0


def test_a_replayed_key_never_returns_another_actors_artifact(host: Host) -> None:
    agent, session, _ = agent_for(host)
    svc = host.surface.member.svc
    user = Actor(ActorKind.USER, "alice")
    # create: a key the owner used on the web is not answered to the agent
    made = call(
        host,
        session,
        "POST",
        "/artifacts",
        {"title": "T", "filename": "a.html", "profile": "web", "idempotency_key": "shared-key-1"},
    )
    assert made.status == 200
    err, body = agent.tool(
        "artifact_create",
        title="T",
        filename="a.html",
        profile="web",
        idempotency_key="shared-key-1",
    )
    assert err and body["error"] == "not_found" and made.json()["artifact_id"] not in str(body)
    # publish and share_create on a web-made artifact: replaying the owner's key is not_found for the agent
    web_aid, web_vid = published(host, session)
    spec = {"name": "again.txt", "size_bytes": 1, "file_sha256": sha(b"x")}
    from tests.artifact_host.member_support import upload

    upload(host, session, web_aid, "again.txt", b"x")
    first = on_loop(
        host,
        svc.publish(
            user,
            web_aid,
            base_version_id=web_vid,
            files=[spec],
            message=None,
            entry=None,
            idempotency_key="replay-pub-1",
        ),
    )
    err, body = agent.tool(
        "artifact_publish",
        artifact_id=web_aid,
        base_version_id=web_vid,
        files=[spec],
        idempotency_key="replay-pub-1",
    )
    assert err and body["error"] == "not_found" and first["version_id"] not in str(body)
    share = {"version_id": web_vid, "audience": "public", "idempotency_key": "replay-shr-1"}
    on_loop(host, svc.share_create(user, web_aid, share))
    err, body = agent.tool("artifact_share_create", artifact_id=web_aid, **share)
    assert err and body["error"] == "not_found"
    # and after the agent's own artifact is deleted its stored answers are gone too
    aid = agent.ok("artifact_create", title="t", filename="a.html", profile="web")["artifact_id"]
    data = b"<html>k</html>"
    spec2 = {"name": "a.html", "size_bytes": len(data), "file_sha256": sha(data)}
    agent.ok(
        "artifact_upload_chunk",
        artifact_id=aid,
        **spec2,
        index=0,
        total=1,
        sha256=sha(data),
        data_b64=base64.b64encode(data).decode(),
    )
    agent.ok("artifact_publish", artifact_id=aid, files=[spec2], idempotency_key="own-key-1")
    agent.ok("artifact_delete", artifact_id=aid, confirm=True)
    err, body = agent.tool(
        "artifact_publish", artifact_id=aid, files=[spec2], idempotency_key="own-key-1"
    )
    assert err and body["error"] == "not_found"


def test_share_events_and_listing_show_the_true_actor(host: Host) -> None:
    agent, session, _ = agent_for(host)
    aid, vid = accepted_push(agent, host, session)
    mine = call(
        host, session, "POST", f"/artifacts/{aid}/shares", {"version_id": vid, "audience": "public"}
    ).json()["share_id"]
    theirs = agent.ok("artifact_share_create", artifact_id=aid, version_id=vid, audience="public")
    agent.ok("artifact_share_revoke", artifact_id=aid, share_id=theirs["share_id"])
    created = host.rows(
        "SELECT kind, actor FROM share_events WHERE share_id=$1 ORDER BY event_id",
        theirs["share_id"],
    )
    assert created == [
        {"kind": "created", "actor": "agent:alice"},
        {"kind": "revoked", "actor": "agent:alice"},
    ]
    assert host.rows("SELECT actor FROM share_events WHERE share_id=$1", mine) == [
        {"actor": "alice"}
    ]
    links = call(host, session, "GET", f"/artifacts/{aid}/shares").json()["links"]
    assert {x["share_id"]: x["created_via"] for x in links} == {
        mine: "web",
        theirs["share_id"]: "agent",
    }
    html = (Path(__file__).parents[2] / "herdr_artifact_host/member_static/member.js").read_text()
    assert "created_via" in html  # the member page tells agent-made links apart


def test_a_forwarded_value_that_is_not_an_address_is_ignored(host: Host) -> None:
    _, _, token = agent_for(host)
    junk = [
        Agent(host, "hat_" + "C" * 43, headers={"X-Forwarded-For": f"not-an-ip-{i}"})
        for i in range(61)
    ]
    codes = [a.rpc("ping").status for a in junk]
    assert codes[:60] == [401] * 60 and codes[60] == 429  # every junk value is the socket peer
    assert Agent(host, token, headers={"X-Forwarded-For": "10.7.7.7"}).rpc("ping").status == 200
