# ruff: noqa: F811  (imported pytest fixtures are used as test arguments)
"""Task 8a: the seven artifact tools on the collab MCP server, through ``collab_mcp.handle`` over a real service,
a real bridge and a hand-written mirror (the fixtures of test_collab_artifacts.py)."""
from __future__ import annotations

import json

import pytest

from herdr_engineering.control_plane import collab_mcp
from herdr_engineering.control_plane.collab_bridge_files import CHUNK_BYTES, upload_id_for
from tests.control_plane.test_collab_artifacts import (  # noqa: F401  (fixtures)
    BIG,
    TEXT,
    X,
    Y,
    chunk_args,
    sha,
    svc,
    world,
)
from tests.control_plane.test_collab_external import enroll

TOOLS = {"artifacts_list", "artifact_head", "artifact_read", "artifact_history", "artifact_stage_chunk",
         "artifact_commit", "artifact_commit_result"}
NOTICE = collab_mcp.UNTRUSTED


def rpc(world, info, method, params=None):
    status, body = collab_mcp.handle(world.s, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                                     f"Agent {info['credential']}", info["device_id"])
    assert status == 200
    return body["result"]


def names(world, info):
    return {t["name"] for t in rpc(world, info, "tools/list")["tools"]}


def tool(world, info, tool_name, **args):
    out = rpc(world, info, "tools/call", {"name": tool_name, "arguments": args})
    text = out["content"][0]["text"]
    return out, text


def err(world, info, tool_name, **args):
    out, text = tool(world, info, tool_name, **args)
    assert out["isError"] is True
    return json.loads(text)


def ok_json(text):
    assert text.startswith(NOTICE + "\n\n")
    return json.loads(text[len(NOTICE) + 2:])


def commit_args(**kw):
    return {"artifact_id": X, "base_version_id": "v_x2", "message": "m",
            "changes": [{"name": "n.txt", "op": "put", "text": "hi"}], **kw}


def test_an_agent_without_grants_sees_no_artifact_tools_and_calls_are_unknown(world):
    assert not names(world, world.w.agent("alice", name="plain")[1]) & TOOLS
    info = world.w.agent("alice", name="plain2")[1]
    assert err(world, info, "artifacts_list") == {"error": "unknown tool", "status": 404}


def test_a_granted_agent_gets_all_seven(world):
    assert TOOLS <= names(world, world.a_info)
    assert TOOLS <= names(world, world.b_info)          # a read grant lists them too


@pytest.mark.parametrize("grant", [None, "read", "write"])
def test_no_tool_name_mentions_share_acl_or_grant(world, grant):
    ext, out, _inv = enroll(world.w)
    if grant:
        world.mirror.grant(ext.id, X, grant)
        world.mirror.grants()
    for pr in (world.a, world.b, ext):
        for tl in collab_mcp.tools_for(world.s, pr) + collab_mcp.tools_for(world.s):
            assert not any(w in tl["name"] for w in ("share", "acl", "grant")), tl["name"]
    assert (TOOLS <= {t["name"] for t in collab_mcp.tools_for(world.s, ext)}) == bool(grant)


def test_a_stale_feed_hides_the_tools_and_calls_are_503_not_unknown(world):
    world.mirror.grants(age=121)
    assert not names(world, world.a_info) & TOOLS
    assert err(world, world.a_info, "artifacts_list") == {"error": "collaboration bridge unavailable", "status": 503}
    world.mirror.grants(age=119)
    assert TOOLS <= names(world, world.a_info)


def test_no_bridge_is_unknown_tool(world):
    world.s.artifact_bridge = None
    assert not names(world, world.a_info) & TOOLS
    assert err(world, world.a_info, "artifacts_list")["error"] == "unknown tool"


def test_an_artifact_tool_call_needs_no_room_id(world):
    _out, text = tool(world, world.a_info, "artifact_head", artifact_id=X)
    assert ok_json(text)["artifact_id"] == X


CALLS = {
    "artifacts_list": {}, "artifact_head": {"artifact_id": X}, "artifact_read": {"artifact_id": X, "name": "a.txt"},
    "artifact_history": {"artifact_id": X}, "artifact_commit_result": {"ticket": "t"},
    "artifact_stage_chunk": chunk_args(X, BIG, 0) | {"data_b64": "AA=="},
    "artifact_commit": commit_args(),
}


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_an_unknown_argument_is_400(world, name):
    args = dict(CALLS[name])
    args.pop("data", None)
    got = err(world, world.a_info, name, **args, bogus=1)
    assert (got["status"], got["error"]) == (400, "unknown argument")


@pytest.mark.parametrize("name,args", [
    ("artifact_read", {"artifact_id": X, "name": "a.txt", "max_bytes": True}),
    ("artifact_read", {"artifact_id": X, "name": "a.txt", "max_bytes": 1048577}),
    ("artifact_read", {"artifact_id": X, "name": "a.txt", "offset": -1}),
    ("artifact_read", {"artifact_id": X, "name": "a.txt", "offset": 1.0}),
    ("artifact_history", {"artifact_id": X, "limit": 201}),
    ("artifact_history", {"artifact_id": X, "limit": True}),
    ("artifact_head", {"artifact_id": 5}),
    ("artifact_commit_result", {"ticket": "t", "seconds": 21}),
    ("artifact_commit_result", {"ticket": "t", "seconds": True}),
    ("artifact_commit", commit_args(message="x" * 1001)),
    ("artifact_commit", commit_args(changes="nope")),
    ("artifact_stage_chunk", {**{k: v for k, v in chunk_args(X, BIG, 0).items() if k != "data"}, "data_b64": "AA==",
                              "index": True}),
])
def test_invalid_parameters_are_400(world, name, args):
    assert err(world, world.a_info, name, **args)["status"] == 400


def test_read_history_head_list_are_wrapped_untrusted(world):
    out, text = tool(world, world.a_info, "artifact_read", artifact_id=X, name="a.txt")
    assert not out["isError"] and ok_json(text)["text"] == TEXT.decode()
    for name, args in (("artifacts_list", {}), ("artifact_head", {"artifact_id": X}),
                       ("artifact_history", {"artifact_id": X, "limit": 1})):
        out, text = tool(world, world.a_info, name, **args)
        assert text.startswith(NOTICE), name


def test_stage_chunk_and_commit_replies_are_wrapped_and_the_ticket_resolves(world):
    for i in range(3):
        args = chunk_args(X, BIG, i)
        args["data_b64"] = args.pop("data")
        out, text = tool(world, world.a_info, "artifact_stage_chunk", **args)
        assert not out["isError"] and ok_json(text)["index"] == i
    upl = upload_id_for(world.a.id, X, "big.bin", sha(BIG), len(BIG))
    out, text = tool(world, world.a_info, "artifact_commit",
                     **commit_args(changes=[{"name": "big.bin", "op": "put", "upload_id": upl}]))
    reply = ok_json(text)
    assert reply["status"] == "pending"
    out, text = tool(world, world.a_info, "artifact_commit_result", ticket=reply["ticket"], seconds=0)
    assert text.startswith(NOTICE) and not out["isError"]


def test_an_incomplete_upload_reports_the_missing_indices(world):
    for i in (0, 2):
        args = chunk_args(X, BIG, i)
        args["data_b64"] = args.pop("data")
        tool(world, world.a_info, "artifact_stage_chunk", **args)
    upl = upload_id_for(world.a.id, X, "big.bin", sha(BIG), len(BIG))
    got = err(world, world.a_info, "artifact_commit",
              **commit_args(changes=[{"name": "big.bin", "op": "put", "upload_id": upl}]))
    assert got == {"error": "upload incomplete", "status": 409, "missing": [1]}


def test_a_read_grant_cannot_write_and_an_ungranted_artifact_is_not_found(world):
    assert err(world, world.b_info, "artifact_commit", **commit_args(artifact_id=Y))["status"] == 403
    assert err(world, world.b_info, "artifact_head", artifact_id=X)["status"] == 404


def test_chunk_size_constant_matches(world):
    assert CHUNK_BYTES == 524_288


# ------------------------------------------------------------------------------ Task 8b: the /mcp large-body gate
import http.client  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402

from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.collab_bridge_files import MCP_UPLOAD_MAX_BODY  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

KIB = 1024
CHUNK_DATA = bytes((i * 13 + 5) % 256 for i in range(CHUNK_BYTES))


@pytest.fixture
def edge(world, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=world.s, public_port=0,
                             web_root=web, collab_require_https=True)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    yield srv
    srv.shutdown()
    t.join(timeout=5)
    collab_mcp.reset_large_body_admissions()


def stage_chunk_message(rid=1):
    args = chunk_args(X, CHUNK_DATA * 2, 0, name="big.bin")
    args["data_b64"] = args.pop("data")
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": "artifact_stage_chunk", "arguments": args}}


def padded(name, size, rid=1):
    """A tools/call whose serialised body is about ``size`` bytes."""
    msg = {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
           "params": {"name": name, "arguments": {"pad": ""}}}
    msg["params"]["arguments"]["pad"] = "a" * (size - len(json.dumps(msg)))
    return msg


def raw_post(edge, info, declared, body=b"", *, extra=(), send_body=True, recv_timeout=3.0):
    """POST /mcp over a raw socket on the public listener. Returns (status, headers dict, body)."""
    head = ["POST /mcp HTTP/1.1", "Host: x", "X-Forwarded-Proto: https", "Content-Type: application/json"]
    if info:
        head += [f"Authorization: Agent {info['credential']}", f"X-Herdr-Device: {info['device_id']}"]
    if declared is not None:
        head.append(f"Content-Length: {declared}")
    head += list(extra)
    s = socket.create_connection(("127.0.0.1", edge.public_port), timeout=recv_timeout)
    try:
        s.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + (body if send_body else b""))
        buf = b""
        while b"\r\n\r\n" not in buf:
            part = s.recv(65536)
            if not part:
                break
            buf += part
        head_txt, _, rest = buf.partition(b"\r\n\r\n")
        lines = head_txt.decode().split("\r\n")
        hdrs = {ln.split(":", 1)[0].lower(): ln.split(":", 1)[1].strip() for ln in lines[1:] if ":" in ln}
        want = int(hdrs.get("content-length", "0"))
        while len(rest) < want:
            part = s.recv(65536)
            if not part:
                break
            rest += part
        return int(lines[0].split()[1]), hdrs, rest
    finally:
        s.close()


def post_msg(edge, info, msg):
    body = json.dumps(msg).encode()
    return raw_post(edge, info, len(body), body)


def test_big_body_without_credentials_is_413_close_and_the_body_is_never_read(edge):
    # the 700 KiB body is declared but never sent: a server that tried to read it would block until our timeout
    status, hdrs, _ = raw_post(edge, None, 700 * KIB, send_body=False)
    assert status == 413 and hdrs["connection"] == "close"


def test_big_body_from_a_bad_credential_is_413_and_counted_once(edge, world):
    before = world.s.stats["auth_failures"]
    bad = {"credential": "nope", "device_id": "dev"}
    status, hdrs, _ = raw_post(edge, bad, 700 * KIB, send_body=False)
    assert status == 413 and hdrs["connection"] == "close"
    assert world.s.stats["auth_failures"] == before + 1


def test_big_body_from_a_read_only_agent_is_413_without_reading(edge, world):
    status, hdrs, _ = raw_post(edge, world.b_info, 700 * KIB, send_body=False)
    assert status == 413 and hdrs["connection"] == "close"


def test_a_write_granted_agents_full_chunk_is_accepted_through_the_gate(edge, world):
    status, _, body = post_msg(edge, world.a_info, stage_chunk_message())
    assert 65536 < len(json.dumps(stage_chunk_message())) < MCP_UPLOAD_MAX_BODY
    assert status == 200
    out = json.loads(body)["result"]
    assert out["isError"] is False and ok_json(out["content"][0]["text"])["index"] == 0


def test_a_big_commit_is_32602_not_executed(edge, world):
    status, _, body = post_msg(edge, world.a_info, padded("artifact_commit", 700 * KIB))
    assert status == 200 and json.loads(body)["error"]["code"] == -32602
    assert json.loads(body)["error"]["message"] == "request too large for this tool"


def test_a_big_non_tool_call_is_32602(edge, world):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"pad": "a" * (700 * KIB)}}
    status, _, body = post_msg(edge, world.a_info, msg)
    assert status == 200 and json.loads(body)["error"]["code"] == -32602


def test_the_authenticated_principal_reaches_handle_so_the_caller_is_authenticated_once(edge, world, monkeypatch):
    real, calls = world.s.authenticate, []      # a counting wrapper around the real method, not a replacement
    monkeypatch.setattr(world.s, "authenticate", lambda *a, **k: calls.append(1) or real(*a, **k))
    before = world.s.stats["auth_failures"]
    status, _, _ = post_msg(edge, world.a_info, stage_chunk_message())
    assert status == 200 and len(calls) == 1 and world.s.stats["auth_failures"] == before
    calls.clear()
    status, _, _ = post_msg(edge, world.a_info, {"jsonrpc": "2.0", "id": 1, "method": "ping"})     # small body
    assert status == 200 and len(calls) == 1


def test_a_registered_admission_admits_its_tool_and_not_the_built_in_one(edge, world):
    collab_mcp.register_large_body_admission(lambda s, pr: True, frozenset({"x_chunk"}))
    # bob has no write grant: only the test admission holds for him
    status, _, body = post_msg(edge, world.b_info, padded("x_chunk", 700 * KIB))
    assert status == 200 and json.loads(body)["result"]["isError"] is True      # unknown tool, but past the gate
    status, _, body = post_msg(edge, world.b_info, padded("artifact_stage_chunk", 700 * KIB))
    assert status == 200 and json.loads(body)["error"]["code"] == -32602
    # alice: both admissions hold, so the union has both tools
    status, _, body = post_msg(edge, world.a_info, padded("x_chunk", 700 * KIB))
    assert status == 200 and "error" not in json.loads(body)


def test_reset_removes_a_test_admission(edge, world):
    collab_mcp.register_large_body_admission(lambda s, pr: True, frozenset({"x_chunk"}))
    collab_mcp.reset_large_body_admissions()
    status, hdrs, _ = raw_post(edge, world.b_info, 700 * KIB, send_body=False)
    assert status == 413 and hdrs["connection"] == "close"


def test_an_admission_that_raises_fails_closed(edge, world):
    def boom(s, pr):
        raise RuntimeError("x")
    collab_mcp.register_large_body_admission(boom, frozenset({"x_chunk"}))
    status, hdrs, _ = raw_post(edge, world.b_info, 700 * KIB, send_body=False)
    assert status == 413 and hdrs["connection"] == "close"
    # a raising predicate does not take the working one with it
    status, _, _ = post_msg(edge, world.a_info, stage_chunk_message())
    assert status == 200


def test_over_the_upload_cap_is_413_for_everyone(edge, world):
    for info in (None, world.a_info, world.b_info):
        status, hdrs, _ = raw_post(edge, info, 800 * KIB, send_body=False)
        assert status == 413 and hdrs["connection"] == "close"
    status, _, _ = raw_post(edge, world.a_info, MCP_UPLOAD_MAX_BODY + 1, send_body=False)
    assert status == 413


@pytest.mark.parametrize("declared,extra", [
    (None, ()), ("-5", ()), ("abc", ()), ("0", ()), ("700000, 700000", ()), ("0x100000", ()),
    ("1" * 12, ()), ("700000", ("Content-Length: 700000",)),
    ("700000", ("Transfer-Encoding: chunked",)),
])
def test_malformed_framing_never_reaches_the_big_body_path(edge, world, declared, extra):
    status, hdrs, _ = raw_post(edge, world.a_info, declared, send_body=False, extra=extra)
    assert status in (400, 413) and hdrs["connection"] == "close"


def test_chunked_transfer_encoding_cannot_bypass_the_gate(edge, world):
    body = b"%x\r\n" % 700000 + b"a" * 700000 + b"\r\n0\r\n\r\n"
    status, hdrs, _ = raw_post(edge, world.a_info, None, body, extra=("Transfer-Encoding: chunked",))
    assert status == 400 and hdrs["connection"] == "close"


def test_small_bodies_still_behave_as_before(edge, world):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    status, _, body = post_msg(edge, world.a_info, msg)
    assert status == 200 and {t["name"] for t in json.loads(body)["result"]["tools"]} >= TOOLS
    status, _, _ = post_msg(edge, {"credential": "nope", "device_id": "d"}, msg)
    assert status == 401


def test_the_a2a_endpoint_gets_no_large_body_allowance(edge, world):
    head = ["POST /a2a HTTP/1.1", "Host: x", "X-Forwarded-Proto: https", "Content-Type: application/json",
            f"Authorization: Agent {world.a_info['credential']}", f"X-Herdr-Device: {world.a_info['device_id']}",
            f"Content-Length: {700 * KIB}"]
    s = socket.create_connection(("127.0.0.1", edge.public_port), timeout=3)
    s.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
    assert s.recv(4096).startswith(b"HTTP/1.1 413")
    s.close()


def test_mcp_has_its_own_1800_per_minute_ceiling_and_other_routes_keep_600(edge):
    c = http.client.HTTPConnection("127.0.0.1", edge.public_port, timeout=10)
    hdr = {"X-Forwarded-Proto": "https", "Content-Type": "application/json", "X-Forwarded-For": "10.9.9.9"}
    body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})   # 202, no auth work
    statuses = []
    for _ in range(1801):
        c.request("POST", "/mcp", body, hdr)
        r = c.getresponse()
        r.read()
        statuses.append(r.status)
    assert 429 not in statuses[:1800] and statuses[1800] == 429
    # a dashboard-class route from the same client is governed by the shared 600/min limiter only
    codes = []
    for _ in range(601):
        c.request("GET", "/api/v1/collab/config", headers=hdr)
        r = c.getresponse()
        r.read()
        codes.append(r.status)
    assert 429 not in codes[:600] and codes[600] == 429
    c.close()
