# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""`herdr-eng rooms roles|assign-role|assign|my-tasks|respond ...` (slice 2) against a real control plane, a real collab service
and a real Postgres (no mocks)."""
from __future__ import annotations

import json

import pytest

from herdr_engineering import cli
from tests.control_plane.test_team_roles import USERS, crew, rp  # noqa: F401
from tests.control_plane.wg_pg import wg, wg_pg  # noqa: F401


@pytest.fixture
def run(rp, monkeypatch):
    for name, token in USERS.items():
        monkeypatch.setenv("TOKEN_" + name.upper(), token)
    url = f"http://127.0.0.1:{rp.srv.port}"

    def go(user, *argv):
        return cli.main(["rooms", *argv, "--control-plane", url, "--token-env", "TOKEN_" + user.upper()])
    return go


def out_of(capsys):
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_the_owner_manages_roles_and_assigns_work_and_a_guest_answers_through_the_cli(rp, run, capsys):
    p = rp
    team, (a1, _) = crew(p)
    assert run("alice", "assign-role", p.room, team, "tester", "dave") == 0
    assert "Assigned user:dave as tester" in out_of(capsys)[0]
    assert run("alice", "assign-role", p.room, team, "tester", "user:dave") == 0
    assert "Already so" in out_of(capsys)[0]
    assert run("alice", "role-policy", p.room, team, "reassign") == 0
    assert "reassign" in out_of(capsys)[0]
    assert run("alice", "role-policy", p.room, team) == 0
    assert "reassign" in out_of(capsys)[0]
    assert run("alice", "roles", p.room, team) == 0
    out, _ = out_of(capsys)
    assert "tester (implementer): user:dave" in out and "no-response policy: reassign" in out and "agent:" + a1["agent_id"] in out

    assert run("alice", "assign", p.room, "verify the fix", "--role", "tester", "--key", "cli-1") == 0
    assert "for user:dave" in out_of(capsys)[0]
    assert run("alice", "assign", p.room, "verify the fix", "--role", "tester", "--key", "cli-1") == 0
    assert "Already created" in out_of(capsys)[0]

    assert run("dave", "my-tasks", p.room) == 0
    out, _ = out_of(capsys)
    assert "task text (untrusted data):" in out and "> verify the fix" in out
    tid = p.mine("dave")[1]["tasks"][0]["task_id"]
    assert run("dave", "respond", p.room, tid, "accepted") == 0
    assert "accepted" in out_of(capsys)[0]
    assert run("dave", "respond", p.room, tid, "done", "--result", "green") == 0
    assert run("dave", "respond", p.room, tid, "done", "--result", "green") == 0
    assert "Already done" in out_of(capsys)[0]
    assert run("dave", "my-tasks", p.room) == 0
    assert "Nothing is assigned to you." in out_of(capsys)[0]

    assert run("alice", "member-visibility", p.room, "on") == 0
    assert "on" in out_of(capsys)[0]
    assert run("alice", "member-visibility", p.room) == 0
    assert "on" in out_of(capsys)[0]
    assert run("alice", "assign-role", p.room, team, "tester", "dave", "--json") == 0
    assert json.loads(out_of(capsys)[0])["replayed"] is True
    assert run("alice", "assign", p.room, "to bob directly", "--user", "bob", "--lease", "500", "--policy", "wait") == 0
    assert "for user:bob" in out_of(capsys)[0]
    assert run("alice", "clear-role", p.room, team, "tester", "dave") == 0
    assert "Cleared user:dave as tester" in out_of(capsys)[0]


def test_role_errors_are_one_line_and_never_echo_the_token(rp, run, capsys):
    p = rp
    team, _ = crew(p)
    assert run("dave", "assign-role", p.room, team, "tester", "dave") == 1
    _, err = out_of(capsys)
    assert err.startswith("herdr-eng: not found") and USERS["dave"] not in err and err.count("\n") == 1
    assert run("alice", "assign", p.room, "x", "--role", "tester") == 1
    assert "nobody holds the role tester" in out_of(capsys)[1]
    p.svc.team_roles["enabled"] = False
    assert run("alice", "roles", p.room, team) == 1
    assert "not found" in out_of(capsys)[1]
