# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Helper fixes from the second automated review: room access for reading a helper, a supervisor whose output
readers never drained, symlinked login files, and an ssh dir that is the operator's own ~/.ssh. Also pins the
behaviour the review doubted but that already holds (spawn+wait keeps the id, disable is a sweep, a helper whose
owner is gone, a non-string operator cancel)."""
from __future__ import annotations

import json
import os
import threading

import pytest

from herdr_engineering.control_plane import collab, collab_mcp
from tests.control_plane.test_collab_egress import short_dir  # noqa: F401
from tests.control_plane.test_collab_helper_runner import box, home, needs_box  # noqa: F401
from tests.control_plane.test_collab_helpers import SECRET_TASK, ask, run, w  # noqa: F401
from tests.control_plane.test_collab_spawn import WORKER, runner  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, code, svc  # noqa: F401


# ------------------------------------------------------------------ A: spawn+wait always names the helper
def test_spawn_with_wait_that_does_not_finish_still_returns_the_helper_id(w, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(collab_mcp.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(w.s, "wait_for_change", lambda seq, t: clock.__setitem__(0, clock[0] + t))
    out = collab_mcp._call(w.s, w.root, "helper_spawn",
                           {"room_id": w.room, "role": "reviewer", "task": SECRET_TASK, "wait": True})
    text = out["content"][0]["text"]
    body = json.loads(text[text.index("{"):])
    assert body["status"] == "requested" and body["helper_id"] in w.s._helpers


# ------------------------------------------------------------------ B: disable sweeps only that person's helpers
def test_disabling_one_person_ends_only_their_helpers(w, monkeypatch):
    b, _ = w.agent("bob", actions=("read", "spawn_helpers"), name="bobs")
    mine, theirs = ask(w), ask(w, b)
    monkeypatch.setattr(w.s, "_approved", lambda uid: uid != "bob")      # bob is disabled
    w.s._expire_helpers()                                                 # what decide_user('disable') runs
    assert w.s._helpers[theirs["helper_id"]]["status"] == "cancelled"
    assert w.s._helpers[theirs["helper_id"]]["reason"] == "owner lost access"
    assert w.s._helpers[mine["helper_id"]]["status"] == "requested"


# ------------------------------------------------------------------ C: a helper whose owner is gone
@pytest.mark.parametrize("how", ["disabled", "deleted", "left"])
def test_a_helper_whose_owner_is_gone_has_no_access_and_raises_nothing(w, monkeypatch, how):
    b, _ = w.agent("bob", actions=("read", "post", "spawn_helpers"), name="bobs")
    rec = ask(w, b)
    h, _ = run(w, rec["helper_id"])
    room = w.s._rooms[w.room]
    assert w.s._effective(h, room) is not None                         # positive control
    if how == "disabled":
        monkeypatch.setattr(w.s, "_approved", lambda uid: uid != "bob")
    elif how == "deleted":
        monkeypatch.setattr(w.s.auth, "exists", lambda uid: uid != "bob")
    else:
        del room["members"]["bob"]                                     # not via remove_member: no cascade
    assert w.s._effective(h, room) is None
    assert code(w.s.read_events, h, w.room, 0, 10) == 404


# ------------------------------------------------------------------ D: room access decides, now
def test_an_agent_out_of_the_room_cannot_ask_for_a_helper_there(w):
    w.s.remove_agent_from_room(w.alice, w.room, w.root.id)
    assert code(ask, w) == 404


def test_an_agent_taken_out_of_the_room_can_no_longer_read_its_helpers_result(w):
    rec = ask(w)
    run(w, rec["helper_id"])
    w.s.finish_helper(w.op, rec["helper_id"], {"status": "done", "result": "room secrets inside"})
    assert w.s.helper_result(w.root, rec["helper_id"])["result"] == "room secrets inside"   # positive control
    w.s.remove_agent_from_room(w.alice, w.room, w.root.id)
    for fn in (w.s.helper_result, w.s.helper_status, w.s.cancel_helper):
        assert code(fn, w.root, rec["helper_id"]) == 404
    for tool in ("helper_result", "helper_wait"):
        with pytest.raises(collab.CollabError) as e:
            collab_mcp._call(w.s, w.root, tool, {"helper_id": rec["helper_id"]})
        assert e.value.status == 404


def test_a_lapsed_grant_ends_the_helpers_under_that_agent(w):
    rec = ask(w)
    _, c = run(w, rec["helper_id"])
    w.s._rooms[w.room]["grants"][w.root.id]["expires_at"] = w.s.clock.t + 10     # the root's grant lapses first
    w.s.clock.t += 11
    w.s._expire_helpers()
    assert w.s._helpers[rec["helper_id"]]["status"] == "cancelled"
    assert w.s._helpers[rec["helper_id"]]["reason"] == "parent lost access"
    assert w.s._agents[c["helper_agent_id"]]["status"] == "revoked"


def test_a_root_successor_keeps_running_when_nothing_lost_access(w):
    rec = ask(w, role="successor")
    run(w, rec["helper_id"])
    w.s.clock.t += 60
    w.s._expire_helpers()
    assert w.s._helpers[rec["helper_id"]]["status"] == "claimed"


def test_an_agent_whose_owner_left_the_room_cannot_read_the_result(w):
    b, _ = w.agent("bob", actions=("read", "post", "spawn_helpers"), name="bobs")
    rec = ask(w, b)
    run(w, rec["helper_id"])
    w.s.finish_helper(w.op, rec["helper_id"], {"status": "done", "result": "out"})
    w.s.remove_member(w.alice, w.room, "bob")
    assert code(w.s.helper_result, b, rec["helper_id"]) == 404


# ------------------------------------------------------------------ E: operator cancel with an odd id
@pytest.mark.parametrize("hid", [None, 5, ["x"], {"a": 1}, "", "nope"])
def test_the_operator_cancel_with_a_non_string_or_unknown_id_is_404(w, hid):
    assert code(w.s.cancel_helper_as_operator, w.op, hid) == 404


def test_the_operator_cancel_route_with_an_unknown_id_is_404(w):
    st, _ = w.s.dispatch("POST", ["fleet", "helpers", "nope", "cancel"], {}, {}, f"Bearer {USERS['mallory']}",
                         None, operator="mallory")
    assert st == 404


# ------------------------------------------------------------------ F: readers that never drain
FAKE_EGRESS = """
import socket, sys, time
sock = sys.argv[sys.argv.index("--sock") + 1]
s = socket.socket(socket.AF_UNIX); s.bind(sock); s.listen(1)
time.sleep(60)
"""


def _supervise(tmp_path, monkeypatch, sandbox_body, sock_dir):
    d = tmp_path / "hd"
    d.mkdir()
    (tmp_path / "egress.py").write_text(FAKE_EGRESS)
    (tmp_path / "sandbox.sh").write_text(sandbox_body)
    sock = sock_dir / "s.sock"
    (d / "spec.json").write_text(json.dumps({
        "sock": str(sock), "egress": str(tmp_path / "egress.py"), "services": [], "harness_dir": str(tmp_path),
        "sandbox": str(tmp_path / "sandbox.sh"), "work": str(tmp_path), "cmd": "x",
        "deadline": __import__("time").time() + 30}))
    monkeypatch.setattr(runner.sys, "stdin", type("I", (), {"buffer": type("B", (), {"read": lambda self, n: b"t"})(),
                                                            "close": lambda self: None})())
    monkeypatch.setattr(runner, "HELPER_DRAIN_WAIT", 0.5)
    runner.helper_run(d)
    return d, json.loads((d / "result.json").read_text())


def test_output_still_open_after_the_box_exits_is_a_failure_not_a_partial_result(tmp_path, monkeypatch, short_dir):
    # something outside the box's process namespace keeps stdout open: the output is not complete
    d, res = _supervise(tmp_path, monkeypatch, "echo PARTIAL\n(sleep 5; echo late) &\nexit 0\n", short_dir)
    assert res["exit"] == 0 and res["error"] == "sandbox-error" and res["output_open"] is True
    r = runner.Runner("http://127.0.0.1:9", tmp_path / "state", "herdr", "ssh", WORKER, "bender", say=lambda m: None)
    assert r.helper_outcome(res) == ("failed", "sandbox-error")
    text = r.helper_result_text(d, {})                       # what end_helper posts with the failure
    assert "PARTIAL" not in text and "still open" in text


def test_output_that_drains_is_a_normal_result(tmp_path, monkeypatch, short_dir):
    d, res = _supervise(tmp_path, monkeypatch, "echo WHOLE\nexit 0\n", short_dir)
    assert res["exit"] == 0 and res["error"] is None and res["output_open"] is False
    r = runner.Runner("http://127.0.0.1:9", tmp_path / "state", "herdr", "ssh", WORKER, "bender", say=lambda m: None)
    assert r.helper_outcome(res) == ("done", None)
    assert "WHOLE" in r.helper_result_text(d, {})
    assert r.helper_outcome({"exit": None, "timed_out": False, "error": "sandbox-error"}) == ("failed", "sandbox-error")
    assert r.helper_outcome({"exit": None, "timed_out": False, "error": None})[0] == "failed"


# ------------------------------------------------------------------ G: login files that are links or fifos
def test_a_symlinked_or_fifo_login_file_is_not_copied(tmp_path, monkeypatch):
    src = tmp_path / "codex-home"
    src.mkdir()
    (tmp_path / "elsewhere.json").write_text('{"token": "x"}')
    (src / "auth.json").symlink_to(tmp_path / "elsewhere.json")
    monkeypatch.setenv("CODEX_HOME", str(src))
    said = []
    r = runner.Runner("http://127.0.0.1:9", tmp_path / "state", "herdr", "ssh", WORKER, "bender", say=said.append)
    d = tmp_path / "h1"
    d.mkdir()
    root = r.harness_login(d, "codex", "h1")
    assert not (root / "codex" / "auth.json").exists() and any("link" in m for m in said)
    (src / "auth.json").unlink()
    os.mkfifo(src / "auth.json")
    d2 = tmp_path / "h2"
    d2.mkdir()
    t = threading.Thread(target=r.harness_login, args=(d2, "codex", "h2"), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive() and not (d2 / "harness" / "codex" / "auth.json").exists()


# ------------------------------------------------------------------ I: the operator's own ~/.ssh
@needs_box
@pytest.mark.parametrize("which", ["itself", "inside", "via-link", "ancestor"])
def test_the_ssh_dir_must_not_be_the_operators_own_dot_ssh(home, tmp_path, which):
    ssh = home / ".ssh"
    ssh.mkdir(mode=0o700)
    (ssh / "id_ed25519").write_text("OPERATOR-KEY")
    (ssh / "sub").mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(ssh)
    home.chmod(0o700)
    path = {"itself": ssh, "inside": ssh / "sub", "via-link": tmp_path / "link" / "sub", "ancestor": home}[which]
    r = box(home, "cat ~/.ssh/id_ed25519 ~/.ssh/.ssh/id_ed25519; echo ran", HERDR_SANDBOX_SSH_DIR=str(path))
    assert r.returncode == 126 and "ran" not in r.stdout and "OPERATOR-KEY" not in r.stdout
    assert "~/.ssh" in r.stderr


def test_both_sandbox_copies_are_byte_identical():
    from tests.control_plane.test_collab_helper_runner import ROOT
    assert (ROOT / "packages/collab-scripts/sandbox.sh").read_bytes() == \
        (ROOT / "deploy/control-plane/collab-sandbox.sh").read_bytes()



# ------------------------------------------------------------------ B, end to end: the disable route
def test_disabling_a_person_through_the_route_ends_their_helpers_and_nobody_elses(tmp_path):
    from tests.control_plane.test_collab_approval import ApprovalPolicy, Clock, OpenAuth, call, who
    s = collab.CollabService(OpenAuth(), sink=collab.FileEventLog(str(tmp_path / "log")), now=Clock(),
                             approval=ApprovalPolicy(auto_groups=["local.dev"], approver_groups=["Administrators"]))
    lab = who(s, "lab")                                          # auto-approved
    who(s, "bob")
    call(s, "POST", ["admin", "users", "bob", "approve"], None, operator="op")
    bob = who(s, "bob")
    pid = s.create_project(lab, {"name": "p"})["project_id"]
    room = s.create_room(lab, pid, {"name": "r"})["room_id"]
    s.add_member(lab, room, {"user_id": "bob", "role": "contributor"})
    s.set_room_profile(lab, room, {"allowed_families": ["claude-code"]})
    hids = {}
    for name, pr in (("lab", lab), ("bob", bob)):
        info = s.connect_agent(pr, {"harness_type": "claude-code"})
        agent = s.authenticate(f"Agent {info['credential']}", info["device_id"])
        out = s.grant_agent(pr, room, {"agent_id": info["agent_id"], "actions": ["read", "spawn_helpers"]})
        if out["status"] == "pending":
            s.decide_agent_request(lab, room, out["request_id"], {"decision": "approve"})
        hids[name] = s.request_helper(agent, {"room_id": room, "role": "reviewer", "task": "x"})["helper_id"]
    call(s, "POST", ["admin", "users", "bob", "disable"], None, operator="op")
    assert s._helpers[hids["bob"]]["status"] == "cancelled"
    assert s._helpers[hids["lab"]]["status"] == "requested"
