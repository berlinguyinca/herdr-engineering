# ruff: noqa: F811  (pytest fixtures are imported by name from the collaboration suites)
"""The external-bridge client and relay against a real control plane, with a fake provider adapter."""
import json
import threading

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.external_bridge import config as bcfg
from herdr_engineering.external_bridge.client import BridgeClient, BridgeError
from herdr_engineering.external_bridge.relay import Inbound, Relay
from tests.control_plane.test_collab_tasks import USERS, W, svc, user  # noqa: F401


@pytest.fixture
def world(svc, tmp_path):
    w = W(svc)
    server = ControlPlaneServer(ControlPlaneRepo(), collab=svc)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    w.url, w.tmp = f"http://127.0.0.1:{server.port}", tmp_path
    yield w
    server.shutdown()


def join(world, actions=("read", "post")):
    inv = world.s.create_external_invite(world.alice, world.room, {"label": "tg", "actions": list(actions)})
    return BridgeClient.join(world.url, inv["code"], world.tmp / "state.json")


class FakeAdapter:
    """A stand-in provider: ``inbox`` is what the outside system says, ``sent`` is what the relay delivered to it."""
    name = "fake"

    def __init__(self, inbox=()):
        self.inbox, self.sent = list(inbox), []

    def poll(self, timeout):
        out, self.inbox = self.inbox, []
        return out

    def send(self, text):
        self.sent.append(text)


def test_join_stores_a_private_state_file_and_refuses_a_bad_code(world):
    client = join(world)
    assert (world.tmp / "state.json").stat().st_mode & 0o777 == 0o600
    assert client.room_id == world.room and client.can_post
    with pytest.raises(BridgeError):
        BridgeClient.join(world.url, "hcj_" + "x" * 32, world.tmp / "other.json")
    with pytest.raises(BridgeError, match="https"):
        BridgeClient.join("http://example.com", "hcj_x", world.tmp / "o.json")


def room_messages(world):
    return [e["text"] for e in world.s.read_events(world.alice, world.room, 0, 50)["events"] if e["event_type"] == "message"]


def test_provider_messages_reach_the_room_and_room_chat_reaches_the_provider(world):
    client = join(world)
    adapter = FakeAdapter([Inbound("1", "dana", "hello room")])
    relay = Relay(client, adapter)
    relay.step(0)
    assert room_messages(world) == ["[fake] dana: hello room"]
    world.s.post_message(world.alice, world.room, {"text": "hi from alice", "idempotency_key": "a1"})
    relay.step(0)
    assert adapter.sent == ["user:alice: hi from alice"]                 # its own post was not echoed back
    relay.step(0)
    assert len(adapter.sent) == 1 and len(room_messages(world)) == 2


def test_a_retried_provider_message_is_not_posted_twice(world):
    client = join(world)
    Relay(client, FakeAdapter([Inbound("7", "dana", "once")])).step(0)
    client.post("[fake] dana: once", key="fake-7")                        # the same idempotency key again
    assert room_messages(world) == ["[fake] dana: once"]


def test_a_read_only_invite_drops_inbound_and_a_revoked_one_stops_the_relay(world):
    client = join(world, actions=("read",))
    adapter = FakeAdapter([Inbound("1", "dana", "nobody listens")])
    relay = Relay(client, adapter)
    relay.step(0)
    assert room_messages(world) == []
    with pytest.raises(BridgeError, match="read-only"):
        client.post("x", "k")
    world.s.revoke_external(world.alice, world.room, client.agent_id)
    with pytest.raises(BridgeError) as e:
        relay.step(0)
    assert e.value.status in (401, 403)


def test_a_bridge_sees_chat_only_and_only_from_when_it_joined(world):
    world.s.post_message(world.alice, world.room, {"text": "before it joined", "idempotency_key": "old"})
    client = join(world)
    adapter = FakeAdapter()
    Relay(client, adapter).step(0)
    assert adapter.sent == []                                             # nothing from before enrolment
    world.s.post_message(world.alice, world.room, {"text": "after", "idempotency_key": "new"})
    Relay(client, adapter).step(0)
    assert adapter.sent == ["user:alice: after"]                          # and no membership/grant/task events


def test_presence_makes_the_agent_online_and_rotation_replaces_the_secret(world):
    client = join(world)
    Relay(client, FakeAdapter()).step(0)
    mine = next(a for a in world.s.list_my_agents(world.alice)["agents"] if a["agent_id"] == client.agent_id)
    assert mine["online"] is True
    old = client.state["credential"]
    assert client.rotate_if_needed(every=0) and client.state["credential"] != old
    assert json.loads((world.tmp / "state.json").read_text())["credential"] == client.state["credential"]
    client.heartbeat("idle", "still works after rotation")
    assert not client.rotate_if_needed()                                  # fresh secret: nothing to do
    assert client.expires_soon(margin=10 ** 9) and not client.expires_soon(margin=1)


def test_a_wrong_or_replayed_credential_is_refused(world):
    client = join(world)
    other = BridgeClient(world.tmp / "state.json")
    other.state["credential"] = "hca_" + "x" * 40
    with pytest.raises(BridgeError) as e:
        other.heartbeat()
    assert e.value.status == 401
    client.rotate_if_needed(every=0)
    stale = BridgeClient(world.tmp / "state.json")
    stale.state["credential"] = "hca_old"
    with pytest.raises(BridgeError):
        stale.events()


def test_an_unreachable_control_plane_is_a_clear_error(world):
    client = join(world)
    client.state["base_url"] = "http://127.0.0.1:9"
    with pytest.raises(BridgeError, match="cannot reach"):
        client.heartbeat()


def test_config_is_yaml_only_and_secret_files_must_be_private(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("control_plane_url: https://x.example\nstate_file: /tmp/s.json\n")
    assert bcfg.load(cfg)["state_file"] == "/tmp/s.json"
    for bad in ("control_plane_url: x\n", "foo: 1\n", "control_plane_url: x\nstate_file: y\ntelegram: {}\n", "[]"):
        cfg.write_text(bad)
        with pytest.raises(ValueError):
            bcfg.load(cfg)
    tok = tmp_path / "tok"
    tok.write_text("secret\n")
    tok.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        bcfg.read_secret(str(tok))
    tok.chmod(0o600)
    assert bcfg.read_secret(str(tok)) == "secret"


def test_the_cli_joins_from_a_code_file_and_reports_status(world, capsys):
    import argparse

    from herdr_engineering import cli
    inv = world.s.create_external_invite(world.alice, world.room, {"label": "cli", "actions": ["read", "post"]})
    code = world.tmp / "code"
    code.write_text(inv["code"] + "\n")
    cfg = world.tmp / "bridge.yaml"
    cfg.write_text(f"control_plane_url: {world.url}\nstate_file: {world.tmp}/cli-state.json\n")
    assert cli._cmd_relay_join(argparse.Namespace(config=str(cfg), code_file=str(code))) == 0
    assert "joined room" in capsys.readouterr().out
    assert (world.tmp / "cli-state.json").stat().st_mode & 0o777 == 0o600
    assert cli._cmd_relay_status(argparse.Namespace(config=str(cfg))) == 0
    assert "can post: True" in capsys.readouterr().out
    with pytest.raises(BridgeError):                                      # the code is single use
        cli._cmd_relay_join(argparse.Namespace(config=str(cfg), code_file=str(code)))
