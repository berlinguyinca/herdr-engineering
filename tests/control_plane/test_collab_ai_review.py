# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Findings of the automated review of the helpers branch, each pinned by a test: the ones that were real are
fixed, the others are shown to hold (a helper task is never run truncated, a helper stays in its room even when it
is a successor, audit and helper status stay with the people they belong to, helper_wait keeps to its schema)."""
from __future__ import annotations

import json
import os

import pytest

from herdr_engineering.control_plane import collab, collab_mcp
from tests.control_plane.test_collab_helper_runner import hr  # noqa: F401
from tests.control_plane.test_collab_helpers import SECRET_TASK, ask, run, w  # noqa: F401
from tests.control_plane.test_collab_spawn import WORKER, calls, rig, runner  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, code, svc  # noqa: F401


def call(w, method, parts, body=None, auth="", device=None, operator=None):
    return w.s.dispatch(method, parts, {}, body or {}, auth, device, operator=operator)


def bearer(name):
    return f"Bearer {USERS[name]}"


# ------------------------------------------------------------------ 3+4: an oversize task is refused, never cut
class _Stdin:
    def __init__(self, data: bytes):
        self.buffer = self
        self.data = data

    def read(self, n=-1):
        return self.data if n is None or n < 0 else self.data[:n]

    def close(self):
        pass


def test_the_supervisor_refuses_a_task_over_its_stdin_cap_and_starts_nothing(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: started.append(a) or pytest.fail("started"))
    (tmp_path / "spec.json").write_text(json.dumps({"sock": str(tmp_path / "s.sock"), "egress": "/x"}))
    monkeypatch.setattr(runner.sys, "stdin", _Stdin(b"x" * (runner.HELPER_STDIN_MAX + 1)))
    assert runner.helper_run(tmp_path) != 0
    res = json.loads((tmp_path / "result.json").read_text())
    assert res["error"] == "sandbox-error" and "larger than" in res["stderr"] and started == []


def test_the_supervisor_takes_a_task_of_exactly_its_cap(tmp_path, monkeypatch):
    class FakeProxy:
        pid = 4242

        def poll(self):
            return 0                              # the proxy "exits": the run stops there, after the read
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: FakeProxy())
    monkeypatch.setattr(runner, "proc_stat", lambda pid: None)
    (tmp_path / "spec.json").write_text(json.dumps({"sock": str(tmp_path / "s.sock"), "egress": "/x"}))
    monkeypatch.setattr(runner.sys, "stdin", _Stdin(b"x" * runner.HELPER_STDIN_MAX))
    runner.helper_run(tmp_path)
    assert "larger than" not in json.loads((tmp_path / "result.json").read_text())["stderr"]
    assert (tmp_path / "proxy.json").exists()                      # it got past the read


def test_the_runner_refuses_to_launch_a_helper_whose_task_would_not_fit(hr, tmp_path):
    r = hr.mk()
    d = tmp_path / "hd"
    d.mkdir()
    c = {"agent_name": "helper-a", "role": "worker", "room_id": hr.w.room, "task": "y" * runner.HELPER_STDIN_MAX,
         "family": "claude-code", "expires_epoch": 2e9, "now": 1e9}
    with pytest.raises(runner.Failure, match="larger than"):
        r.start_helper(d, {"helper_id": "collab_x", "agent_id": "agent_x"}, c)
    assert not (d / "spec.json").exists()


# ------------------------------------------------------------------ 6: only the runner's own login files
def test_a_login_file_owned_by_someone_else_is_not_copied(hr, tmp_path, monkeypatch):
    src = tmp_path / "cfg"
    src.mkdir()
    (src / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "T"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(src))
    said = []
    r = hr.mk()
    r.say = said.append
    d = tmp_path / "hd"
    d.mkdir()
    real_fstat = os.fstat
    monkeypatch.setattr(runner.os, "fstat", lambda fd: os.stat_result(
        (*real_fstat(fd)[:4], os.getuid() + 1, *real_fstat(fd)[5:])))
    root = r.harness_login(d, "claude-code", "collab_x")
    assert not (root / "claude/.credentials.json").exists()
    assert any("not owned by" in m for m in said), said


def test_the_runners_own_login_file_is_still_copied(hr, tmp_path, monkeypatch):
    src = tmp_path / "cfg"
    src.mkdir()
    (src / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "T"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(src))
    d = tmp_path / "hd"
    d.mkdir()
    root = hr.mk().harness_login(d, "claude-code", "collab_x")
    assert json.loads((root / "claude/.credentials.json").read_text()) == {"claudeAiOauth": {"accessToken": "T"}}


# ------------------------------------------------------------------ 8: a successor stays in its room
def other_room_denials(w):
    return sum(1 for ev in w.s._log if ev["payload"].get("reason") == "helper_other_room")


@pytest.mark.parametrize("asks", ["successor", "worker"])
def test_a_successor_helper_cannot_ask_for_a_helper_in_another_room_even_holding_a_grant_there(w, asks):
    w.s.set_room_profile(w.alice, w.room2, {"allowed_families": ["claude-code", "codex"]})
    w.s.grant_agent(w.alice, w.room2, {"agent_id": w.root.id, "actions": ["read", "spawn_helpers"]})
    first, _ = run(w, ask(w, role="worker")["helper_id"])
    succ_rec = ask(w, first, role="successor", task="carry on")
    succ, sc = run(w, succ_rec["helper_id"])
    assert w.s._helper_by_agent[sc["helper_agent_id"]]["role"] == "successor"
    # get past _authorize: the successor holds a grant in room2 like the one it holds in its own room
    w.s._rooms[w.room2]["grants"][succ.id] = dict(w.s._rooms[w.room]["grants"][succ.id])
    before = other_room_denials(w)
    with pytest.raises(collab.CollabError) as e:
        w.s.request_helper(succ, {"room_id": w.room2, "role": asks, "task": "elsewhere"})
    assert e.value.status == 404 and other_room_denials(w) == before + 1      # the room rule, not a missing grant
    # positive control: in its own room it may
    assert w.s.request_helper(succ, {"room_id": w.room, "role": asks, "task": "here"})["room_id"] == w.room


# ------------------------------------------------------------------ 9: audit is a project manager's only
def test_audit_is_refused_to_a_guest_and_a_contributor_and_given_to_the_manager(w):
    pid = w.s._rooms[w.room]["project_id"]
    run(w, ask(w)["helper_id"])
    for who in ("dave", "bob", "carol"):                       # guest, contributor, viewer of the room
        with pytest.raises(collab.CollabError) as e:
            w.s.audit(getattr(w, who), pid, 0, 100)
        assert e.value.status in (403, 404), who
    out = w.s.audit(w.alice, pid, 0, 1000)
    assert any(ev["event_type"].startswith("helper_") for ev in out["events"])


# ------------------------------------------------------------------ 10: GET helpers/<id> is the owner's only
def test_helper_status_over_http_is_the_owners_only(w):
    hid = ask(w)["helper_id"]
    w.s.add_member(w.alice, w.room2, {"user_id": "mallory", "role": "contributor"})   # only in another room
    st, out = call(w, "GET", ["helpers", hid], auth=bearer("alice"))
    assert st == 200 and out["helper_id"] == hid
    for who in ("bob", "dave", "mallory"):                     # same room not owner, guest, another room's member
        st, _ = call(w, "GET", ["helpers", hid], auth=bearer(who))
        assert st == 404, who
        st, _ = call(w, "POST", ["helpers", hid, "cancel"], auth=bearer(who))
        assert st == 404, who
    assert w.s.helper_status(w.alice, hid)["status"] == "requested"          # nobody above could cancel it


# ------------------------------------------------------------------ 11+12: helper_wait keeps to its schema
@pytest.mark.parametrize("secs", [21, 500, 0, -1, True, 1.5])
def test_helper_wait_refuses_seconds_outside_its_schema(w, secs):
    rec = ask(w)
    with pytest.raises(collab.CollabError) as e:
        collab_mcp._call(w.s, w.root, "helper_wait", {"helper_id": rec["helper_id"], "seconds": secs})
    assert e.value.status == 400
    spec = next(t for t in collab_mcp.TOOLS if t["name"] == "helper_wait")
    assert spec["inputSchema"]["properties"]["seconds"]["maximum"] == collab_mcp.HELPER_WAIT_MAX == 20
