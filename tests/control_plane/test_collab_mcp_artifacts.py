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
