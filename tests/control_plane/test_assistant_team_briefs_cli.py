"""`herdr-eng assistant brief-offer|brief-list|brief-reference` and the brief card in `herdr-eng rooms queue|approve` (slice 4)
against a real control plane, a real collab service and a real assistant store (no mocks)."""
from __future__ import annotations

import json

import pytest

from herdr_engineering import cli
from tests.control_plane import brief_fixtures as fx
from tests.control_plane.test_assistant_team_briefs import (  # noqa: F401
    KEY,
    OTHER_KEY,
    TEAM,
    USERS,
    team_up,
    wg_pg,
    world,
)


@pytest.fixture
def run(world, monkeypatch):
    for name, token in USERS.items():
        monkeypatch.setenv("TOKEN_" + name.upper(), token)
    monkeypatch.setenv("BRIEF_KEY", KEY)
    monkeypatch.setenv("OTHER_BRIEF_KEY", OTHER_KEY)
    url = f"http://127.0.0.1:{world.srv.port}"

    def assistant(*argv, key="BRIEF_KEY"):
        return cli.main(["assistant", *argv, "--control-plane", url, "--api-key-env", key])

    def rooms(user, *argv):
        return cli.main(["rooms", *argv, "--control-plane", url, "--token-env", "TOKEN_" + user.upper()])
    return assistant, rooms


def out_of(capsys):
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_the_operator_offers_lists_and_follows_a_brief_through_the_cli(world, run, capsys):
    w = world
    assistant, rooms = run
    held, rev, coord = team_up(w)
    assert assistant("brief-offer", w.room, TEAM, fx.MESSAGE_ID) == 0
    offered = json.loads(out_of(capsys)[0])
    assert offered["ok"] is True and offered["result"]["brief"]["state"] == "pending"
    ref = offered["result"]["brief"]["reference"]
    assert fx.MESSAGE_ID not in json.dumps(offered)
    assert assistant("brief-offer", w.room, TEAM, fx.MESSAGE_ID) == 0                       # again: a replay
    assert json.loads(out_of(capsys)[0])["result"]["brief"]["replayed"] is True
    assert assistant("brief-list") == 0
    listing = json.loads(out_of(capsys)[0])["result"]["briefs"]
    assert [b["reference"] for b in listing] == [ref] and "message" not in listing[0]
    assert assistant("brief-reference", ref) == 0
    assert json.loads(out_of(capsys)[0])["result"]["message"]["message_id"] == fx.MESSAGE_ID
    # a second API key follows nothing
    assert assistant("brief-reference", ref, key="OTHER_BRIEF_KEY") == 1
    err = json.loads(out_of(capsys)[0])
    assert err["ok"] is False and err["error"]["status"] == 404 and fx.MESSAGE_ID not in json.dumps(err)
    assert assistant("brief-offer", w.room, "team-" + "9" * 20, fx.MESSAGE_ID) == 1            # not attached: not found
    out_of(capsys)


def test_the_queue_shows_the_card_with_the_readers_and_approval_creates_the_task(world, run, capsys):
    w = world
    assistant, rooms = run
    held, rev, coord = team_up(w)
    assert assistant("brief-offer", w.room, TEAM, fx.MESSAGE_ID) == 0
    sid = json.loads(out_of(capsys)[0])["result"]["brief"]["suggestion_id"]
    assert rooms("alice", "queue", w.room) == 0
    out, _ = out_of(capsys)
    assert "from the assistant (email, flagged: no)" in out and "readers: agents" in out
    assert "impl0" in out and "coordy" in out and fx.BODY_PHRASE in out and fx.SENDER_ADDRESS not in out
    assert rooms("alice", "approve", w.room, sid, "--agent", held[0]["agent_id"]) == 0
    assert "created for" in out_of(capsys)[0]
    assert len(w.events("task_created")) == 1
    assert rooms("alice", "queue", w.room, "--json") == 0
    row = json.loads(out_of(capsys)[0])["suggestions"][0]
    assert row["state"] == "materialized" and row["card"]["will_see"] == row["text"]


def test_the_cli_refuses_without_the_flag(tmp_path, wg_pg, monkeypatch, capsys):
    from tests.control_plane.test_assistant_team_briefs import Clock, World
    w = World(tmp_path, Clock(), wg_pg["dsn"], None)
    try:
        monkeypatch.setenv("BRIEF_KEY", KEY)
        url = f"http://127.0.0.1:{w.srv.port}"
        for argv in (["brief-offer", w.room, TEAM, fx.MESSAGE_ID], ["brief-list"], ["brief-reference", "bref-" + "0" * 24]):
            assert cli.main(["assistant", *argv, "--control-plane", url, "--api-key-env", "BRIEF_KEY"]) == 1
            assert json.loads(out_of(capsys)[0])["error"]["status"] == 404
    finally:
        w.srv.shutdown()
