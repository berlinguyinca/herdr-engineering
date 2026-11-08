"""The agent MCP endpoint: a JSON-RPC client over real HTTP against a real PublicServer and real Postgres."""

from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any

from herdr_artifact_host.mcp import PROTOCOL
from herdr_artifact_host.member_api import CHUNK_BYTES, MemberLimits
from tests.artifact_host.member_support import (
    ORIGIN,
    call,
    published,
    sha,
    sign_in,
)
from tests.artifact_host.seed import Host, Resp, running_host

LOTS = MemberLimits(writes_per_minute=10_000, chunks_per_minute=100_000)


def mint(host: Host, session: str, label: str = "laptop") -> dict[str, Any]:
    r = call(host, session, "POST", "/agent-tokens", {"label": label})
    assert r.status == 200, r.body
    return r.json()


class Agent:
    """A minimal MCP client (Streamable HTTP, JSON responses): handshake, tools/list, tools/call."""

    def __init__(self, host: Host, token: str | None, *, scheme: str = "Bearer") -> None:
        self.host, self.token, self.scheme, self._id = host, token, scheme, 0

    def post(self, message: Any) -> Resp:
        headers = {"Content-Type": "application/json"}
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
        aid = self.ok("artifact_create", title=title, filename="index.html")["artifact_id"]
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
    err, body = agent.tool("artifact_create", title="t", filename="a.html", surprise=1)
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
    aid = agent.ok("artifact_create", title="Big", filename="big.bin")["artifact_id"]
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
    aid = agent.ok("artifact_create", title="t", filename="a.html")["artifact_id"]
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


def test_an_agent_share_is_pending_and_cannot_be_approved_by_it(host: Host) -> None:
    agent, session, _ = agent_for(host)
    aid, vid = published(host, session)  # an accepted artifact made through the web page
    made = agent.ok("artifact_share_create", artifact_id=aid, version_id=vid, audience="public")
    assert made["state"] == "pending"
    row = host.rows("SELECT state, created_by FROM shares WHERE share_id=$1", made["share_id"])[0]
    assert row == {"state": "pending", "created_by": "alice"}
    assert not [t for t in agent.tools() if "approve" in t]
    listed = agent.ok("artifact_share_list", artifact_id=aid)["links"]
    assert [x["state"] for x in listed] == ["pending"]
    # the same agent actor is refused by the service itself, not only by the missing tool
    from herdr_artifact_host.member_api import Actor, ActorKind, MemberError

    async def approve(pool: Any) -> None:
        raise AssertionError

    svc = host.surface.member.svc
    import asyncio

    fut = asyncio.run_coroutine_threadsafe(
        svc.share_approve(Actor(ActorKind.AGENT, "alice"), aid, made["share_id"], True), host.loop
    )
    try:
        fut.result(10)
    except MemberError as exc:
        assert exc.status == 403
    else:
        raise AssertionError("an agent approved a share")
    assert (
        host.rows("SELECT state FROM shares WHERE share_id=$1", made["share_id"])[0]["state"]
        == "pending"
    )
    assert (
        call(
            host,
            session,
            "POST",
            f"/artifacts/{aid}/shares/{made['share_id']}/approve",
            {"confirm": True},
        ).status
        == 200
    )
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
            agent.ok("artifact_create", title=f"p{i}", filename="a.html")
        err, body = agent.tool("artifact_create", title="p25", filename="a.html")
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
            got = agent.tool("artifact_create", title=f"c{i}", filename="a.html")
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
