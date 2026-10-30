# ruff: noqa: F811  (pytest fixtures are imported by name from the collaboration suites)
"""The external-bridge client and relay against a real control plane, with a fake Telegram."""
import json
import threading

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.external_bridge import config as bcfg
from herdr_engineering.external_bridge.client import BridgeClient, BridgeError
from herdr_engineering.external_bridge.relay import Relay
from herdr_engineering.external_bridge.telegram import CHUNK, Telegram
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


class FakeApi:
    def __init__(self, updates):
        self.updates, self.sent, self.calls = updates, [], []

    def __call__(self, token, method, params, timeout):
        self.calls.append((method, params))
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        self.sent.append((params["chat_id"], params["text"]))
        return {}


def update(uid, chat, text, user="dana"):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text, "from": {"username": user}}}


def test_join_stores_a_private_state_file_and_refuses_a_bad_code(world):
    client = join(world)
    assert (world.tmp / "state.json").stat().st_mode & 0o777 == 0o600
    assert client.room_id == world.room and client.can_post
    with pytest.raises(BridgeError):
        BridgeClient.join(world.url, "hcj_" + "x" * 32, world.tmp / "other.json")
    with pytest.raises(BridgeError, match="https"):
        BridgeClient.join("http://example.com", "hcj_x", world.tmp / "o.json")


def test_telegram_messages_reach_the_room_and_room_chat_reaches_telegram(world):
    client = join(world)
    api = FakeApi([update(1, 42, "hello room"), update(2, 999, "from a stranger chat")])
    relay = Relay(client, Telegram("tok", [42], api=api))
    relay.step(0)
    texts = [e["text"] for e in world.s.read_events(world.alice, world.room, 0, 50)["events"] if e["event_type"] == "message"]
    assert texts == ["[telegram] dana: hello room"]                      # the stranger chat is dropped
    world.s.post_message(world.alice, world.room, {"text": "hi from alice", "idempotency_key": "a1"})
    relay.step(0)
    assert api.sent == [(42, "user:alice: hi from alice")]               # its own post was not echoed back
    relay.step(0)
    assert len(api.sent) == 1


def test_a_retried_provider_message_is_not_posted_twice_and_long_text_is_chunked(world):
    client = join(world)
    api = FakeApi([update(7, 42, "once")])
    relay = Relay(client, Telegram("tok", [42], api=api))
    relay.step(0)
    client.post("[telegram] dana: once", key="telegram-7")                # the same idempotency key
    posts = [e for e in world.s.read_events(world.alice, world.room, 0, 50)["events"] if e["event_type"] == "message"]
    assert len(posts) == 1
    world.s.post_message(world.alice, world.room, {"text": "x" * (CHUNK + 10), "idempotency_key": "long"})
    relay.step(0)
    assert [len(t) for _, t in api.sent[-2:]] == [CHUNK, len("user:alice: ") + CHUNK + 10 - CHUNK]


def test_a_read_only_invite_drops_inbound_and_a_revoked_one_stops_the_relay(world):
    client = join(world, actions=("read",))
    api = FakeApi([update(1, 42, "nobody listens")])
    relay = Relay(client, Telegram("tok", [42], api=api))
    relay.step(0)
    assert not [e for e in world.s.read_events(world.alice, world.room, 0, 50)["events"] if e["event_type"] == "message"]
    with pytest.raises(BridgeError, match="read-only"):
        client.post("x", "k")
    world.s.revoke_external(world.alice, world.room, client.agent_id)
    with pytest.raises(BridgeError) as e:
        relay.step(0)
    assert e.value.status in (401, 403)


def test_presence_makes_the_agent_online_and_rotation_replaces_the_secret(world):
    client = join(world)
    Relay(client, Telegram("tok", [42], api=FakeApi([]))).step(0)
    mine = next(a for a in world.s.list_my_agents(world.alice)["agents"] if a["agent_id"] == client.agent_id)
    assert mine["online"] is True
    old = client.state["credential"]
    assert client.rotate_if_needed(every=0) and client.state["credential"] != old
    assert json.loads((world.tmp / "state.json").read_text())["credential"] == client.state["credential"]
    client.heartbeat("idle", "still works after rotation")


def test_config_is_yaml_only_with_secret_files(tmp_path):
    tok = tmp_path / "tok"
    tok.write_text("123:abc\n")
    tok.chmod(0o644)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"control_plane_url: https://x.example\nstate_file: {tmp_path}/s.json\n"
                   f"telegram: {{token_file: {tok}, chat_ids: [42]}}\n")
    with pytest.raises(ValueError, match="chmod 600"):
        bcfg.build_adapter(bcfg.load(cfg))
    tok.chmod(0o600)
    assert bcfg.build_adapter(bcfg.load(cfg)).name == "telegram"
    for bad in ("control_plane_url: x\nstate_file: y\n", "foo: 1\n", "control_plane_url: x\nstate_file: y\ntelegram: {chat_ids: []}\n"):
        cfg.write_text(bad)
        with pytest.raises(ValueError):
            bcfg.build_adapter(bcfg.load(cfg))
