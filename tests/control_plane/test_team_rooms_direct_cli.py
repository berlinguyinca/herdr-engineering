# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""`herdr-eng rooms direct ...` against a real control plane, a real collab service and a real Postgres (no mocks)."""
from __future__ import annotations

import json

import pytest

from herdr_engineering import cli
from tests.control_plane.test_team_rooms import USERS
from tests.control_plane.test_team_rooms_direct import crew, dp  # noqa: F401
from tests.control_plane.wg_pg import wg, wg_pg  # noqa: F401


@pytest.fixture
def run(dp, monkeypatch):
    for name, token in USERS.items():
        monkeypatch.setenv("TOKEN_" + name.upper(), token)
    url = f"http://127.0.0.1:{dp.srv.port}"

    def go(user, *argv):
        return cli.main(["rooms", *argv, "--control-plane", url, "--token-env", "TOKEN_" + user.upper()])
    return go


def out_of(capsys):
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_the_owner_sets_direct_limits_and_a_guest_assigns_work_through_the_cli(dp, run, capsys):
    p = dp
    (a1, a2), _ = crew(p)
    assert run("alice", "guest-level", p.room, "direct", "--agents", a1["agent_id"], "--per-hour", "2", "--max-open", "2",
               "--may-cancel-own") == 0
    out, _ = out_of(capsys)
    assert "Guest level: direct" in out and "acts as" not in out and '"may_cancel_own": true' in out
    assert run("alice", "guest-level", p.room) == 0
    assert '"per_hour": 2' in out_of(capsys)[0]

    assert run("dave", "direct", p.room, "please fix the flaky build", "--agent", a1["agent_id"], "--key", "cli-1") == 0
    out, _ = out_of(capsys)
    assert "Created: task" in out and "external-source:guest" in out
    assert run("dave", "direct", p.room, "please fix the flaky build", "--agent", a1["agent_id"], "--key", "cli-1") == 0
    assert "Already created" in out_of(capsys)[0]
    assert len(p.events("task_created")) == 1

    assert run("dave", "direct", p.room, "x", "--agent", a2["agent_id"]) == 1
    _, err = out_of(capsys)
    assert err.startswith("herdr-eng:") and "not one you may assign to" in err and USERS["dave"] not in err

    assert run("dave", "direct-tasks", p.room) == 0
    out, _ = out_of(capsys)
    assert "the request (untrusted data, not instructions)" in out and "please fix the flaky build" in out
    tid = next(iter(p.svc._tasks))
    assert run("alice", "direct-tasks", p.room, "--json") == 0
    rows = json.loads(out_of(capsys)[0])["tasks"]
    assert rows[0]["submitter_user_id"] == "dave" and rows[0]["task_id"] == tid

    assert run("dave", "direct-cancel", p.room, tid) == 0
    assert "Cancelled" in out_of(capsys)[0]
    assert p.svc._tasks[tid]["status"] == "cancelled"


def test_the_owner_withdraws_and_restores_one_guests_right_through_the_cli(dp, run, capsys):
    p = dp
    (a1, _), _ = crew(p)
    assert run("alice", "guest-level", p.room, "direct", "--agents", a1["agent_id"]) == 0
    out_of(capsys)
    assert run("dave", "direct", p.room, "one", "--agent", a1["agent_id"]) == 0
    out_of(capsys)
    assert run("alice", "direct-guests", p.room, "--deny", "dave") == 0
    out, _ = out_of(capsys)
    assert "dave may not assign directly" in out and "cancelled 1" in out
    assert run("alice", "direct-guests", p.room) == 0
    assert "dave" in out_of(capsys)[0]
    assert run("dave", "direct", p.room, "two", "--agent", a1["agent_id"]) == 1
    assert "withdrawn" in out_of(capsys)[1]
    assert run("dave", "direct-guests", p.room, "--deny", "erin") == 1
    assert out_of(capsys)[1].startswith("herdr-eng: not found")
    assert run("alice", "direct-guests", p.room, "--allow", "dave") == 0
    assert "dave may assign directly" in out_of(capsys)[0]
    assert run("dave", "direct", p.room, "three", "--agent", a1["agent_id"]) == 0


def test_with_direct_off_the_cli_says_so_and_the_routes_are_not_found(dp, run, capsys):
    from herdr_engineering.control_plane.collab_team_rooms import normalize_team_rooms
    p = dp
    (a1, _), _ = crew(p)
    assert run("alice", "guest-level", p.room, "direct", "--agents", a1["agent_id"]) == 0
    out_of(capsys)
    p.svc.team_rooms = normalize_team_rooms({"enabled": True})
    assert run("alice", "guest-level", p.room) == 0
    assert "acts as suggest" in out_of(capsys)[0]
    assert run("dave", "direct-tasks", p.room) == 1
    assert out_of(capsys)[1].startswith("herdr-eng: not found")
    assert run("alice", "direct-guests", p.room) == 1
    assert out_of(capsys)[1].startswith("herdr-eng: not found")
