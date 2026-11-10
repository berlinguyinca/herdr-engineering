"""Slack asks store, client and verified taps against a real local Slack listener (Web API plus Socket Mode)."""
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.assistant import (
    AssistantService,
    AssistantStore,
    _grants_config,
    _slack_config,
    assistant_config,
)
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction
from herdr_engineering.control_plane.assistant_hub.policy import decide, record_owner_decision
from herdr_engineering.control_plane.assistant_hub.slack import (
    SlackClient,
    SlackError,
    SlackRuntime,
)
from herdr_engineering.control_plane.assistant_hub.slack_asks import (
    SlackAsk,
    button_value,
    new_ask_id,
    new_nonce,
)
from herdr_engineering.control_plane.assistant_hub.workers import Worker
from tests.control_plane.slack_support import SlackListener

T0 = datetime(2026, 10, 1, tzinfo=UTC)
NOW = T0 + timedelta(days=1)
T, U = "acme", "owner"
OWNER = "U0OWNER01"
BOT, APP = "xoxb-bot-secret-1", "xapp-app-secret-1"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class World:
    def __init__(self, tmp_path):
        self.listener = SlackListener(BOT, APP)
        bot, app = tmp_path / "bot", tmp_path / "app"
        bot.write_text(BOT + "\n")
        app.write_text(APP + "\n")
        for f in (bot, app):
            f.chmod(0o600)
        self.cfg = _slack_config({"enabled": True, "base_url": self.listener.base_url, "bot_token_file": str(bot),
                                  "app_token_file": str(app), "owner_user_id": OWNER})
        self.store = AssistantStore()
        grants = _grants_config({"shadow_started_at": T0.isoformat()})
        self.service = AssistantService(store=self.store, config={**assistant_config({}), "grants": grants,
                                                                  "hub": {"tenant": T}})
        self.clock = Clock()
        self.runtime = SlackRuntime(self.store, self.service, lambda: self.cfg, self.clock,
                                    min_backoff=0.05, max_backoff=0.2, ping_interval=0.3)
        self.client = SlackClient(lambda: self.cfg)
        self.worker = Worker(worker_id="w1", tenant_id=T, user_id=U, name="n", harness="claude", device_id="d",
                             secret_digest="x", status="active", created_at=T0, expires_at=T0 + timedelta(days=365))

    def request(self, cmd="git push origin main"):
        d = decide(self.store, self.service.config["grants"], self.worker,
                   RawAction(tool="Bash", command=cmd, repo="acme/repo-a"), "s1", "t1", NOW)
        assert d.outcome == "pending"
        return d.request_id

    def ask(self, request_id=None, **over):
        request_id = request_id or self.request()
        channel = self.client.open_dm(OWNER)
        ts = self.client.post_message(channel, [{"type": "section", "text": {"type": "mrkdwn", "text": "ask"}}], "ask")
        ask = SlackAsk(ask_id=new_ask_id(), tenant_id=T, user_id=U, request_id=request_id, task_id="t1",
                       nonce=new_nonce(), channel=channel, ts=ts, created_at=NOW,
                       expires_at=NOW + timedelta(minutes=15))
        ask = replace(ask, **over)
        self.store.put_slack_ask(ask)
        return ask

    def tap(self, ask, action="ask_approve", *, user=OWNER, value=None, channel=None, ts=None, **message):
        payload = {"type": "block_actions", "user": {"id": user},
                   "channel": {"id": channel if channel is not None else ask.channel},
                   "message": {"ts": ts if ts is not None else ask.ts, **message},
                   "actions": [{"action_id": action, "value": value if value is not None else button_value(ask)}]}
        self.runtime.handle_interaction(payload)
        return payload

    def status(self, request_id):
        return self.store.get_action_request(request_id, tenant_id=T, user_id=U).status

    def audits(self, action):
        return [e for e in self.store.audit if e["action"] == action]


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    try:
        yield w
    finally:
        w.runtime.stop()
        w.listener.close()


def test_valid_approve_tap_approves_and_removes_buttons(world):
    ask = world.ask()
    world.tap(ask)
    assert world.status(ask.request_id) == "approved"
    row = world.store.get_action_request(ask.request_id, tenant_id=T, user_id=U)
    assert row.decided_by == f"slack:{OWNER}"
    assert world.store.get_slack_ask(ask.ask_id, tenant_id=T, user_id=U).status == "decided"
    (update,) = world.listener.calls_to("chat.update")
    assert update["channel"] == ask.channel and update["ts"] == ask.ts
    assert not any(b.get("type") == "actions" for b in update["blocks"])
    assert "Approved" in update["text"]
    ev = world.store.list_action_evidence(T, U)
    assert [e.provenance for e in ev] == ["owner_slack"]
    (dec,) = world.audits("assistant.slack.decision")
    assert dec["actor"] == f"slack:{OWNER}" and "git push" not in str(dec) and BOT not in str(dec)


def test_deny_tap_denies(world):
    ask = world.ask()
    world.tap(ask, "ask_deny")
    assert world.status(ask.request_id) == "denied"
    assert "Denied" in world.listener.calls_to("chat.update")[0]["text"]


def test_remember_tap_approves_and_flags_the_ask(world):
    ask = world.ask()
    world.tap(ask, "ask_remember")
    assert world.status(ask.request_id) == "approved"
    assert world.store.get_slack_ask(ask.ask_id, tenant_id=T, user_id=U).remember is True


def _refused(world, ask, reason):
    assert world.status(ask.request_id) == "pending"
    assert world.store.get_slack_ask(ask.ask_id, tenant_id=T, user_id=U).status == "open"
    assert world.listener.calls_to("chat.update") == []
    assert world.store.list_action_evidence(T, U) == []
    (e,) = world.audits("assistant.slack.refused")
    assert f"reason={reason}" in e["detail"] and "git push" not in str(e) and BOT not in str(e)
    assert world.audits("assistant.slack.decision") == []


def test_wrong_user_refused(world):
    ask = world.ask()
    world.tap(ask, user="U0OTHER99")
    _refused(world, ask, "not_owner")


def test_wrong_nonce_refused(world):
    ask = world.ask()
    world.tap(ask, value=f"{ask.ask_id}:{new_nonce()}")
    _refused(world, ask, "bad_nonce")


def test_unknown_ask_refused(world):
    ask = world.ask()
    world.tap(ask, value="sa_nope:abc")
    assert world.status(ask.request_id) == "pending"
    (e,) = world.audits("assistant.slack.refused")
    assert "reason=no_ask" in e["detail"]


def test_malformed_payloads_refused(world):
    ask = world.ask()
    for payload in ({}, {"user": {"id": OWNER}}, {"type": "block_actions", "user": {"id": OWNER}, "actions": []},
                    {"type": "block_actions", "user": {"id": OWNER}, "actions": [{"action_id": "x", "value": 3}]},
                    {"type": "view_submission", "user": {"id": OWNER}}):
        world.runtime.handle_interaction(payload)
    assert world.status(ask.request_id) == "pending"
    assert len(world.audits("assistant.slack.refused")) == 5


def test_already_decided_ask_refused(world):
    ask = world.ask()
    world.tap(ask)
    world.tap(ask)
    refused = world.audits("assistant.slack.refused")
    assert [("reason=not_open" in e["detail"]) for e in refused] == [True]
    assert len(world.audits("assistant.slack.decision")) == 1


def test_expired_ask_refused(world):
    ask = world.ask()
    world.clock.now = ask.expires_at
    world.tap(ask)
    _refused(world, ask, "expired")


@pytest.mark.parametrize("over", [{"channel": "D0OTHER001"}, {"ts": "1.1"}])
def test_wrong_channel_or_ts_refused(world, over):
    ask = world.ask()
    world.tap(ask, **over)
    _refused(world, ask, "wrong_message")


def test_forwarded_or_edited_message_refused(world):
    ask = world.ask()
    world.tap(ask, edited={"user": OWNER, "ts": "9.9"})
    _refused(world, ask, "wrong_message")


def test_ask_without_posted_message_refused(world):
    ask = world.ask(channel="", ts="")
    world.tap(ask, channel="", ts="")
    _refused(world, ask, "wrong_message")


def test_tap_on_vetoed_request_refused(world):
    d = decide(world.store, world.service.config["grants"], world.worker,
               RawAction(tool="Bash", command="rm -rf /", repo="acme/repo-a"), "s1", "t1", NOW)
    assert d.outcome == "denied"
    ask = world.ask(request_id=d.request_id)
    world.tap(ask)
    assert world.status(d.request_id) == "vetoed"
    assert world.store.get_slack_ask(ask.ask_id, tenant_id=T, user_id=U).status == "open"
    (e,) = world.audits("assistant.slack.refused")
    assert "reason=vetoed" in e["detail"]


def test_tap_after_rest_decision_refused(world):
    ask = world.ask()
    record_owner_decision(world.store, world.service.config["grants"], NOW, T, U, ask.request_id, False, U)
    world.tap(ask)
    assert world.status(ask.request_id) == "denied"
    (e,) = world.audits("assistant.slack.refused")
    assert "reason=request_not_pending" in e["detail"]


def test_double_tap_decides_once_even_concurrently(world):
    ask = world.ask()
    threads = [threading.Thread(target=world.tap, args=(ask,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert world.status(ask.request_id) == "approved"
    assert len(world.audits("assistant.slack.decision")) == 1
    assert len(world.store.list_action_evidence(T, U)) == 1
    assert len(world.audits("assistant.slack.refused")) == 7
    assert len(world.listener.calls_to("chat.update")) == 1


def test_asks_survive_snapshot_restore_and_are_scoped(world):
    ask = world.ask()
    other = AssistantStore()
    other.restore(world.store.snapshot())
    assert other.get_slack_ask(ask.ask_id, tenant_id=T, user_id=U) == ask
    assert other.get_slack_ask(ask.ask_id, tenant_id="other", user_id=U) is None
    assert other.get_slack_ask(ask.ask_id, tenant_id=T, user_id="someone") is None
    assert [a.ask_id for a in other.list_slack_asks(T, U, "open")] == [ask.ask_id]
    old = world.store.snapshot()
    old.pop("slack_asks")
    fresh = AssistantStore()
    fresh.restore(old)
    assert fresh.list_slack_asks(T, U) == []


def test_ask_cap_per_tenant_evicts_decided_first():
    store = AssistantStore()
    store.max_slack_asks_per_tenant = 3
    assert AssistantStore().max_slack_asks_per_tenant == 2000
    for i in range(3):
        store.put_slack_ask(SlackAsk(ask_id=f"a{i}", tenant_id=T, user_id=U, status="open" if i != 1 else "decided"))
    store.put_slack_ask(SlackAsk(ask_id="a3", tenant_id=T, user_id=U))
    assert sorted(a.ask_id for a in store.list_slack_asks(T, U)) == ["a0", "a2", "a3"]
    with pytest.raises(ValueError):
        store.put_slack_ask(SlackAsk(ask_id="a3", tenant_id="other", user_id=U))
    snap = store.snapshot()
    small = AssistantStore()
    small.max_slack_asks_per_tenant = 2
    small.restore(snap)
    assert len(small.list_slack_asks(T, U)) == 2


def test_client_uses_live_token_and_reports_errors_without_secrets(world, tmp_path):
    ch = world.client.open_dm(OWNER)
    assert world.client.post_message(ch, [], "hi", thread_ts="1.2")
    assert world.listener.calls_to("chat.postMessage")[-1]["thread_ts"] == "1.2"
    rotated = tmp_path / "bot"
    rotated.write_text("xoxb-wrong\n")
    with pytest.raises(SlackError) as err:
        world.client.post_message(ch, [], "hi")
    assert err.value.code == "invalid_auth" and "xoxb" not in str(err.value) and "xoxb" not in repr(err.value)
    rotated.write_text(BOT + "\n")                        # rotation back applies on the next call, no restart
    world.client.post_message(ch, [], "hi")
    rotated.unlink()
    with pytest.raises(SlackError) as err:
        world.client.open_dm(OWNER)
    assert err.value.code == "token"


def test_client_io_error_is_a_short_code(world):
    world.cfg = {**world.cfg, "base_url": "http://127.0.0.1:1/api"}
    with pytest.raises(SlackError) as err:
        world.client.open_dm(OWNER)
    assert err.value.code == "io"


def _socket_payload(ask, **kw):
    return {"type": "block_actions", "user": {"id": OWNER}, "channel": {"id": ask.channel},
            "message": {"ts": ask.ts}, "actions": [{"action_id": "ask_approve", "value": button_value(ask)}], **kw}


def test_tap_over_socket_mode_is_acked_and_decides(world):
    ask = world.ask()
    world.runtime.start()
    assert world.listener.wait_for(lambda: world.listener.ws_alive == 1)
    env = world.listener.push_interactive(_socket_payload(ask))
    assert world.listener.wait_for(lambda: env in world.listener.acks)
    assert world.listener.wait_for(lambda: world.status(ask.request_id) == "approved")
    assert world.listener.wait_for(lambda: len(world.listener.calls_to("chat.update")) == 1)
    app_requests = [r for r in world.listener.requests if r["path"].endswith("apps.connections.open")]
    assert app_requests and all(r["authorization"] == f"Bearer {APP}" for r in app_requests)


def test_non_owner_tap_over_socket_is_acked_and_refused(world):
    ask = world.ask()
    world.runtime.start()
    assert world.listener.wait_for(lambda: world.listener.ws_alive == 1)
    env = world.listener.push_interactive(_socket_payload(ask, user={"id": "U0EVIL001"}))
    assert world.listener.wait_for(lambda: env in world.listener.acks)
    assert world.listener.wait_for(lambda: len(world.audits("assistant.slack.refused")) == 1)
    assert world.status(ask.request_id) == "pending"


def test_runtime_reconnects_after_a_dropped_connection(world):
    ask = world.ask()
    world.runtime.start()
    assert world.listener.wait_for(lambda: world.listener.ws_alive == 1)
    world.listener.drop_socket()
    assert world.listener.wait_for(lambda: world.listener.ws_connections >= 2 and world.listener.ws_alive == 1)
    env = world.listener.push_interactive(_socket_payload(ask))
    assert world.listener.wait_for(lambda: world.status(ask.request_id) == "approved")
    assert world.listener.wait_for(lambda: env in world.listener.acks, timeout=10)   # the ack trails the tap


def _supervisors() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == "slack-socket-mode"}


def test_runtime_survives_a_dead_endpoint_and_stop_is_idempotent(world):
    earlier = _supervisors()                               # other tests' threads are not this runtime's business
    world.cfg = {**world.cfg, "base_url": "http://127.0.0.1:1/api"}
    world.runtime.start()
    world.runtime.start()                                  # second start is a no-op
    threads = list(_supervisors() - earlier)               # exactly the supervisors THIS runtime started
    assert len(threads) == 1 and threads[0].is_alive()
    world.cfg = {**world.cfg, "base_url": world.listener.base_url}      # endpoint comes back: it connects on its own
    assert world.listener.wait_for(lambda: world.listener.ws_alive == 1)
    world.runtime.stop()
    world.runtime.stop()
    assert not threads[0].is_alive()
    assert world.listener.wait_for(lambda: world.listener.ws_alive == 0)


def test_disabled_config_connects_nothing(world):
    world.cfg = {**world.cfg, "enabled": False}
    world.runtime.start()
    world.runtime.stop()
    assert world.listener.requests == [] and world.listener.ws_connections == 0


def test_socket_mode_link_ignores_environment_proxies(world, monkeypatch):
    """The link carries the app-level token: no env proxy may see the web call or the websocket."""
    proxy = SlackListener(BOT, APP)
    try:
        for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
        for name in ("no_proxy", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
        ask = world.ask()
        world.runtime.start()
        assert world.listener.wait_for(lambda: world.listener.ws_alive == 1)
        env = world.listener.push_interactive(_socket_payload(ask))
        assert world.listener.wait_for(lambda: world.status(ask.request_id) == "approved", timeout=10)
        assert world.listener.wait_for(lambda: env in world.listener.acks, timeout=10)     # the ack may trail the tap
        assert proxy.requests == [] and proxy.ws_connections == 0 and proxy.calls == []
    finally:
        proxy.close()


def test_stop_timeout_keeps_the_handle_and_start_refuses_a_second_supervisor(world):
    world.runtime = SlackRuntime(world.store, world.service, lambda: world.cfg, world.clock,
                                 min_backoff=0.05, max_backoff=0.2, ping_interval=0.3, stop_timeout=0.2)
    earlier = _supervisors()
    gate = threading.Event()
    world.listener.on_request = lambda method: gate.wait(3) if method == "apps.connections.open" else None
    world.runtime.start()
    assert world.listener.wait_for(lambda: any(r["path"].endswith("apps.connections.open")
                                               for r in world.listener.requests))
    assert world.runtime.stop() is False                        # timed out: the supervisor is still running
    world.runtime.start()                                       # refused while the old supervisor is alive
    ours = _supervisors() - earlier
    assert len(ours) == 1                                       # this runtime has exactly one supervisor, not two
    gate.set()
    assert world.listener.wait_for(lambda: not any(t.is_alive() for t in ours), timeout=5)
    assert world.runtime.stop() is True
    world.runtime.start()                                       # the old one is gone, so a new start works
    assert world.listener.wait_for(lambda: world.listener.ws_alive == 1)
    world.runtime.stop()                                        # leave no supervisor behind for later tests
    assert world.listener.wait_for(lambda: not any(t.is_alive() for t in _supervisors() - earlier), timeout=5)


def test_tap_on_request_decided_elsewhere_closes_the_ask_and_removes_buttons(world):
    ask = world.ask()
    record_owner_decision(world.store, world.service.config["grants"], NOW, T, U, ask.request_id, False, U)
    world.tap(ask)
    assert world.store.get_slack_ask(ask.ask_id, tenant_id=T, user_id=U).status == "decided"
    (update,) = world.listener.calls_to("chat.update")
    assert update["ts"] == ask.ts and not any(b.get("type") == "actions" for b in update["blocks"])
    world.tap(ask)                                       # a second tap is refused as not_open, nothing more is sent
    assert len(world.listener.calls_to("chat.update")) == 1
