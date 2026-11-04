# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""`herdr-eng rooms coordinator|coordinator-clear|external-coordination|coordination ...` (slice 3) against a real control plane and a
real collab service (no mocks)."""
from __future__ import annotations

import json

import pytest

from herdr_engineering import cli
from tests.control_plane.test_team_coordinators import (  # noqa: F401
    TEAM,
    TEAM2,
    USERS,
    cp,
    ext_root,
)


@pytest.fixture
def run(cp, monkeypatch):
    for name, token in USERS.items():
        monkeypatch.setenv("TOKEN_" + name.upper(), token)
    url = f"http://127.0.0.1:{cp.srv.port}"

    def go(user, *argv):
        return cli.main(["rooms", *argv, "--control-plane", url, "--token-env", "TOKEN_" + user.upper()])
    return go


def out_of(capsys):
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_the_owner_designates_an_agent_and_an_external_person_and_revokes_through_the_cli(cp, run, capsys):
    p = cp
    impl, coord = p.agent(name="impl"), p.agent(name="coordy")
    assert p.attach({"implementer": [impl["agent_id"]]})[0] == 200
    assert p.attach({"implementer": [p.agent(name="w")["agent_id"]]}, team=TEAM2)[0] == 200

    assert run("alice", "coordinator", p.room, TEAM) == 0
    assert "coordinator: nobody" in out_of(capsys)[0]
    assert run("alice", "coordinator", p.room, TEAM, coord["agent_id"]) == 0
    out, _ = out_of(capsys)
    assert f"coordinator: agent:{coord['agent_id']} (operator)" in out and "external coordination: off" in out
    assert run("alice", "coordinator", p.room, TEAM, "--json") == 0
    assert json.loads(out_of(capsys)[0])["holder"] == "agent:" + coord["agent_id"]

    assert run("alice", "external-coordination", p.room, TEAM2) == 0
    assert "External coordination: off" in out_of(capsys)[0]
    assert run("alice", "external-coordination", p.room, TEAM2, "on", "--roles", "implementer", "--per-hour", "2") == 0
    assert "External coordination on" in out_of(capsys)[0]
    assert run("alice", "external-coordination", p.room, TEAM2, "on", "--roles", "implementer", "--per-hour", "2") == 0
    assert "Already External coordination on" in out_of(capsys)[0]
    assert run("alice", "external-coordination", p.room, TEAM2, "on") == 1                    # no roles: the server refuses
    assert "limits.roles" in out_of(capsys)[1]
    assert run("alice", "coordinator", p.room, TEAM2, "user:dave") == 0
    assert "coordinator: user:dave (external)" in out_of(capsys)[0]

    root = ext_root(p)
    assert p.make("dave", target_role="implementer", parent_task_id=root, text="part")[0] == 201
    assert run("dave", "coordination", p.room) == 0
    out, _ = out_of(capsys)
    assert "Coordinating team " + TEAM2 in out and "[pending]" in out and "attempt 1" in out
    assert run("dave", "coordination", p.room, "--json") == 0
    assert "result" not in json.loads(out_of(capsys)[0])["tasks"][0]

    assert run("alice", "external-coordination", p.room, TEAM2, "off") == 0
    assert "External coordination off: cancelled 0, requeued 1" in out_of(capsys)[0]
    assert run("dave", "coordination", p.room) == 1                                            # revoked
    assert "not found" in out_of(capsys)[1]
    assert run("alice", "coordinator-clear", p.room, TEAM) == 0
    assert "Cleared: cancelled 0, requeued 0" in out_of(capsys)[0]
    assert run("alice", "coordinator-clear", p.room, TEAM) == 0
    assert "Already clear" in out_of(capsys)[0]


def test_coordinator_errors_are_one_line_and_never_echo_the_token(cp, run, capsys):
    p = cp
    assert p.attach({"implementer": [p.agent(name="w")["agent_id"]]})[0] == 200
    assert run("dave", "coordinator", p.room, TEAM, "user:dave") == 1
    _, err = out_of(capsys)
    assert err.startswith("herdr-eng: not found") and USERS["dave"] not in err and err.count("\n") == 1
    assert run("alice", "coordinator", p.room, TEAM, "user:dave") == 1                         # no opt-in yet
    assert "turn on external coordination" in out_of(capsys)[1]
    p.svc.coordinators["enabled"] = False
    assert run("alice", "coordinator", p.room, TEAM) == 1
    assert "not found" in out_of(capsys)[1]
