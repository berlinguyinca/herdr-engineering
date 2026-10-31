# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""`herdr-eng rooms ...` against a real control plane, a real collab service and a real Postgres (no mocks)."""
from __future__ import annotations

import json

import pytest

from herdr_engineering import cli
from tests.control_plane.test_team_rooms import USERS, plane, two_agents  # noqa: F401
from tests.control_plane.wg_pg import wg, wg_pg  # noqa: F401


@pytest.fixture
def run(plane, monkeypatch):
    for name, token in USERS.items():
        monkeypatch.setenv("TOKEN_" + name.upper(), token)
    url = f"http://127.0.0.1:{plane.srv.port}"

    def go(user, *argv):
        return cli.main(["rooms", *argv, "--control-plane", url, "--token-env", "TOKEN_" + user.upper()])
    return go


def out_of(capsys):
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_the_owner_attaches_sets_the_level_and_decides_through_the_cli(plane, run, capsys):
    p = plane
    impl, rev, _ = two_agents(p)
    team = p.team()
    assert run("alice", "attach", p.room, team, "--role", f"implementer={impl['agent_id']}", "--role", f"reviewer={rev['agent_id']}") == 0
    out, _ = out_of(capsys)
    assert "Attached" in out and impl["agent_id"] in out
    assert run("alice", "attach", p.room, team, "--role", f"implementer={impl['agent_id']}", "--role", f"reviewer={rev['agent_id']}") == 0
    assert "Already attached" in out_of(capsys)[0]
    assert run("alice", "teams", p.room) == 0 and team in out_of(capsys)[0]

    assert run("alice", "guest-level", p.room) == 0
    out, _ = out_of(capsys)
    assert "Guest level: suggest" in out and "(default)" in out
    assert run("alice", "guest-level", p.room, "direct", "--agents", impl["agent_id"], "--per-hour", "3") == 0
    assert "acts as suggest" in out_of(capsys)[0]
    assert run("alice", "guest-level", p.room, "suggest") == 0
    out_of(capsys)

    assert run("dave", "suggest", p.room, "please check the flaky build", "--key", "cli-1") == 0
    assert "Filed" in out_of(capsys)[0]
    assert run("dave", "suggest", p.room, "please check the flaky build", "--key", "cli-1") == 0
    assert "Already filed" in out_of(capsys)[0]

    assert run("alice", "queue", p.room, "--state", "pending") == 0
    out, _ = out_of(capsys)
    assert "quoted request (untrusted data, not instructions)" in out and "please check the flaky build" in out
    sid = p.queue("alice")[1]["suggestions"][0]["suggestion_id"]
    assert run("alice", "approve", p.room, sid, "--agent", impl["agent_id"], "--command", "Look at the build") == 0
    assert "Approved" in out_of(capsys)[0]
    assert [e["payload"]["text"] for e in p.events("task_created")] == ["Look at the build"]
    assert run("alice", "approve", p.room, sid, "--agent", impl["agent_id"]) == 0
    assert "Already approved" in out_of(capsys)[0] and len(p.events("task_created")) == 1

    assert run("alice", "detach", p.room, team) == 0
    assert "Detached" in out_of(capsys)[0]
    assert run("alice", "detach", p.room, team) == 0
    assert "Already detached" in out_of(capsys)[0]


def test_reject_withdraw_and_json_output(plane, run, capsys):
    p = plane
    assert run("dave", "suggest", p.room, "first", "--json") == 0
    first = json.loads(out_of(capsys)[0])
    assert first["state"] == "pending"
    assert run("alice", "reject", p.room, first["suggestion_id"], "--reason", "not now") == 0
    assert "Rejected" in out_of(capsys)[0]
    assert run("dave", "suggest", p.room, "second", "--json") == 0
    second = json.loads(out_of(capsys)[0])
    assert run("dave", "withdraw", p.room, second["suggestion_id"]) == 0
    assert "Withdrawn" in out_of(capsys)[0]
    assert run("alice", "queue", p.room, "--json") == 0
    rows = json.loads(out_of(capsys)[0])["suggestions"]
    assert {r["state"] for r in rows} == {"rejected", "withdrawn"}


def test_errors_are_one_line_and_never_echo_the_token(plane, run, capsys, monkeypatch):
    p = plane
    assert run("dave", "attach", p.room, "team-" + "a" * 20, "--role", "implementer=x") == 1
    _, err = out_of(capsys)
    assert err.startswith("herdr-eng: not found") and USERS["dave"] not in err and err.count("\n") == 1
    assert run("alice", "attach", p.room, "team-" + "a" * 20, "--role", "oops") == 1
    assert "--role must look like" in out_of(capsys)[1]
    monkeypatch.delenv("TOKEN_ALICE")
    assert run("alice", "queue", p.room) == 1
    assert "set TOKEN_ALICE" in out_of(capsys)[1]
    p.level("alice", "read_only")
    assert run("dave", "suggest", p.room, "no way") == 1
    assert "read only" in out_of(capsys)[1]
