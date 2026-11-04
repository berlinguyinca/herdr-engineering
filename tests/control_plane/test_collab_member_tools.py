# ruff: noqa: F811  (imported pytest fixtures are used as test arguments)
"""Plan 5, Task 6: the member artifact MCP tools (menu, gating, argument checks, untrusted wrapping, no existence
oracle) through the real ``collab_mcp.handle``."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane.collab_member_tools import MEMBER_TOOL_NAMES
from herdr_engineering.control_plane.collab_mirror import GRANTS_NAME
from tests.control_plane.test_artifact_member_service import ART, DATA, GHOST, OTHER, World, sha
from tests.control_plane.test_collab_approval import s as approval_svc  # noqa: F401
from tests.control_plane.test_collab_external import enroll
from tests.control_plane.test_collab_mcp_artifacts import err, names, ok_json, tool

ALL = MEMBER_TOOL_NAMES | {"artifact_head", "artifact_history"}


@pytest.fixture
def world(approval_svc, tmp_path):
    w = World(approval_svc, tmp_path)
    w.view("op", [ART], shared=["art_sh1"])
    w.view("lab", [OTHER])
    w.info = w.agent("op")[1]
    yield w
    w.bridge.stop()


def chunk_args(world, index=0, **kw):
    a = world.chunk(DATA, index, **kw)
    a["data_b64"] = a.pop("data")
    return a


def test_the_menu_lists_the_member_tools_for_a_member_agent_only(world):
    assert ALL <= names(world, world.info)
    for t in world_tools(world, world.info):
        if t["name"] in MEMBER_TOOL_NAMES:
            assert t["inputSchema"]["additionalProperties"] is False
    mal = world.agent("mal")[1]
    assert not names(world, mal) & MEMBER_TOOL_NAMES
    pid = world.s.create_project(world.people["op"], {"name": "p"})["project_id"]
    room = world.s.create_room(world.people["op"], pid, {"name": "r"})["room_id"]
    _, ext, _ = enroll(SimpleNamespace(s=world.s, alice=world.people["op"], room=room))
    assert not names(world, ext) & MEMBER_TOOL_NAMES
    for info in (mal, ext):
        for name in MEMBER_TOOL_NAMES:
            assert err(world, info, name) == {"error": "artifact tools are for group members", "status": 403}


def world_tools(world, info):
    from tests.control_plane.test_collab_mcp_artifacts import rpc
    return rpc(world, info, "tools/list")["tools"]


def test_the_menu_needs_a_fresh_bridge_feed(world):
    (world.bridge.mirror / GRANTS_NAME).unlink()
    assert not names(world, world.info) & MEMBER_TOOL_NAMES


def test_unknown_and_missing_arguments_are_400(world):
    assert err(world, world.info, "artifacts_mine", extra=1)["status"] == 400
    assert err(world, world.info, "artifact_delete", artifact_id=ART, confirm=True, extra=1)["status"] == 400
    assert err(world, world.info, "artifact_delete", artifact_id=ART)["status"] == 400
    assert err(world, world.info, "artifact_head", artifact_id=ART, nope=1)["status"] == 400
    assert err(world, world.info, "artifact_upload_chunk", artifact_id=ART)["status"] == 400


def test_chunks_stage_through_mcp_and_bad_input_stages_nothing(world):
    out = ok_json(tool(world, world.info, "artifact_upload_chunk", **chunk_args(world, 0))[1])
    assert out["replayed"] is False and out["total"] == 2
    assert ok_json(tool(world, world.info, "artifact_upload_chunk", **chunk_args(world, 0))[1])["replayed"] is True
    other = world.chunk(bytes(reversed(DATA)), 0, name="g.bin")
    base = {**other, "data_b64": other.pop("data")}
    for bad in ({**base, "data_b64": "!!!"}, {**base, "sha256": "0" * 64}, {**base, "size_bytes": 1 << 50}):
        assert err(world, world.info, "artifact_upload_chunk", **bad)["status"] == 400
    assert len(world.staged("op")) == 1


def test_foreign_and_missing_artifacts_give_one_error_text(world):
    a = err(world, world.info, "artifact_upload_chunk", **chunk_args(world, 0, artifact=OTHER))
    b = err(world, world.info, "artifact_upload_chunk", **chunk_args(world, 0, artifact=GHOST))
    assert a == b and a["status"] == 404
    for name, extra in (("artifact_upload_status", {"name": "f", "size_bytes": 5, "file_sha256": sha(b"x")}),
                        ("artifact_upload_abort", {"upload_id": "upl_" + "0" * 24}),
                        ("artifact_delete", {"confirm": True}),
                        ("artifact_publish", {"files": [{"name": "a", "upload_id": "u"}], "message": "m"}),
                        ("artifact_history", {}), ("artifact_head", {})):
        x = err(world, world.info, name, artifact_id=OTHER, **extra)
        y = err(world, world.info, name, artifact_id=GHOST, **extra)
        assert x == y and x["status"] == 404, name


def test_delete_requires_confirm_true(world):
    assert err(world, world.info, "artifact_delete", artifact_id=ART, confirm=False)["status"] == 400
    out = ok_json(tool(world, world.info, "artifact_delete", artifact_id=ART, confirm=True)[1])
    assert out["status"] == "pending" and out["ticket"]
    res = ok_json(tool(world, world.info, "artifact_result", ticket=out["ticket"], seconds=0)[1])
    assert res == {"ticket": out["ticket"], "status": "pending"}
    assert err(world, world.info, "artifact_result", ticket=out["ticket"], seconds=21)["status"] == 400


def test_publish_file_count_is_limited_in_code(world):
    files = [{"name": f"f{i}", "upload_id": "upl_" + "0" * 24} for i in range(world.cfg.max_publish_files + 1)]
    assert err(world, world.info, "artifact_publish", artifact_id=ART, files=files, message="m")["status"] == 400
    ok = ok_json(tool(world, world.info, "artifact_publish", artifact_id=ART, base_version_id=None,
                      files=files[:2], message="m")[1])
    assert ok["status"] == "pending"


def test_create_goes_through_the_service(world):
    out = ok_json(tool(world, world.info, "artifact_create", title="T", filename="a.html", profile="web")[1])
    assert out["status"] == "pending"
    assert err(world, world.info, "artifact_create", title="T", filename="a.html", profile="x")["status"] == 400


def test_mine_is_the_callers_view_file_wrapped_as_untrusted(world):
    world.view("op", [ART], shared=["art_sh1"])
    out = ok_json(tool(world, world.info, "artifacts_mine")[1])
    assert [a["artifact_id"] for a in out["artifacts"]] == [ART] and out["shared_with_me"][0]["artifact_id"] == "art_sh1"
    lab = world.agent("lab")[1]
    assert [a["artifact_id"] for a in ok_json(tool(world, lab, "artifacts_mine")[1])["artifacts"]] == [OTHER]


def test_head_and_history_answer_for_the_callers_own_view(world):
    head = ok_json(tool(world, world.info, "artifact_head", artifact_id=ART)[1])
    assert head["head_version_id"] == "v_1" and head["permission"] == "owner"
    hist = ok_json(tool(world, world.info, "artifact_history", artifact_id=ART, limit=5)[1])
    assert hist["versions"][0]["message"] == "first"
    assert json.dumps(ok_json(tool(world, world.info, "artifact_history", artifact_id="art_sh1")[1]))
    assert err(world, world.info, "artifact_history", artifact_id=ART, limit=0)["status"] == 400
