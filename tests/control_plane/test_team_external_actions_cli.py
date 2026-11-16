# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""`herdr-eng rooms issue-target|external-*` and `herdr-eng assistant external-*` (slice 5) against the real control plane, the real
collab service, the real assistant ledger and the contract-level fake GitHub (no mocks of product code)."""
from __future__ import annotations

import json

import pytest

from herdr_engineering import cli
from tests.control_plane.test_assistant_team_briefs import KEY, OTHER_KEY
from tests.control_plane.test_team_external_actions import REPO, world  # noqa: F401
from tests.control_plane.test_team_rooms import USERS
from tests.control_plane.wg_pg import wg_pg  # noqa: F401


@pytest.fixture
def run(world, monkeypatch):
    for name, token in USERS.items():
        monkeypatch.setenv("TOKEN_" + name.upper(), token)
    monkeypatch.setenv("OP_KEY", KEY)
    monkeypatch.setenv("OTHER_OP_KEY", OTHER_KEY)
    url = f"http://127.0.0.1:{world.srv.port}"

    def assistant(*argv, key="OP_KEY"):
        return cli.main(["assistant", *argv, "--control-plane", url, "--api-key-env", key])

    def rooms(user, *argv):
        return cli.main(["rooms", *argv, "--control-plane", url, "--token-env", "TOKEN_" + user.upper()])
    return assistant, rooms


def out_of(capsys):
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_the_owner_sets_the_target_files_reads_the_card_approves_and_one_issue_exists(world, run, capsys):
    w = world
    assistant, rooms = run
    w.team_up()
    team = next(iter(w.svc._tr_attach))[1]
    assert rooms("alice", "issue-target", w.room, team, REPO) == 0
    assert REPO in out_of(capsys)[0]
    assert rooms("alice", "issue-target", w.room, team, "--json") == 0
    assert json.loads(out_of(capsys)[0])["repo"] == REPO
    b = w.approved_brief()
    assert assistant("external-file", w.room, "github_issue_create", b["reference"], "--label", "from-email") == 0
    filed = json.loads(out_of(capsys)[0])["result"]
    assert filed["state"] == "awaiting_approval" and filed["card"]["repository"] == REPO
    action = filed["action_id"]
    assert rooms("alice", "external-list", w.room) == 0
    text, _ = out_of(capsys)
    assert action in text and "untrusted data" in text and w.card(action)["sha256"] in text
    # approving without the sha is refused by the parser; with a wrong sha the server says 409 and nothing is created
    assert assistant("external-decide", action, "approved", "--sha256", "0" * 64) == 1
    assert json.loads(out_of(capsys)[0])["error"]["status"] == 409 and not w.issues()
    sha = w.card(action)["sha256"]
    assert assistant("external-decide", action, "approved", "--sha256", sha) == 0
    assert json.loads(out_of(capsys)[0])["result"]["state"] == "executed" and len(w.issues()) == 1
    assert assistant("external-list", "--state", "executed") == 0
    assert [x["action_id"] for x in json.loads(out_of(capsys)[0])["result"]["actions"]] == [action]
    assert assistant("external-show", action) == 0
    assert json.loads(out_of(capsys)[0])["result"]["state"] == "executed"
    assert assistant("external-retry", action, "--sha256", sha) == 1                         # nothing to retry
    out_of(capsys)
    assert len(w.issues()) == 1


def test_nobody_but_the_owner_can_decide_from_the_cli_and_another_key_sees_nothing(world, run, capsys):
    w = world
    assistant, rooms = run
    w.ready()
    b = w.approved_brief()
    s, filed = w.file(w.team["iw"], ref=b["reference"])
    assert s == 201
    sha = w.card(filed["action_id"])["sha256"]
    for user in ("bob", "carol", "dave", "erin"):
        assert rooms(user, "external-decide", w.room, filed["action_id"], "approved", "--sha256", sha) == 1
        out_of(capsys)
    assert not w.issues()
    assert assistant("external-show", filed["action_id"], key="OTHER_OP_KEY") == 1
    assert json.loads(out_of(capsys)[0])["error"]["status"] == 404
    assert rooms("alice", "external-decide", w.room, filed["action_id"], "rejected", "--sha256", sha) == 0
    assert "rejected" in out_of(capsys)[0] and not w.issues()


def test_the_cli_refuses_without_the_flag(tmp_path, wg_pg, monkeypatch, capsys):
    from tests.control_plane.test_collab import Clock
    from tests.control_plane.test_team_external_actions import XWorld
    w = XWorld(tmp_path, Clock(), wg_pg["dsn"], None)
    try:
        monkeypatch.setenv("OP_KEY", KEY)
        url = f"http://127.0.0.1:{w.srv.port}"
        for argv in (["external-list"], ["external-show", "prop_x"], ["external-file", w.room, "github_issue_create", "bref-" + "0" * 24],
                     ["external-decide", "prop_x", "approved", "--sha256", "0" * 64], ["external-retry", "prop_x", "--sha256", "0" * 64]):
            assert cli.main(["assistant", *argv, "--control-plane", url, "--api-key-env", "OP_KEY"]) == 1
            assert json.loads(out_of(capsys)[0])["error"]["status"] == 404
    finally:
        w.close()
