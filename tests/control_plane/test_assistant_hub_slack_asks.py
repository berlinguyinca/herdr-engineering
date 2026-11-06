"""Owner asks in Slack: queued off the request path, posted by the runtime, inert, batched, and visible in a status route.

Everything runs against a real local Slack listener (Web API plus Socket Mode) and the real decide/mcp/api code."""
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import (
    AssistantService,
    AssistantStore,
    assistant_config,
)
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction
from herdr_engineering.control_plane.assistant_hub.mcp import Hub, handle
from herdr_engineering.control_plane.assistant_hub.policy import decide, record_owner_decision
from herdr_engineering.control_plane.assistant_hub.slack import SlackRuntime, slack_text
from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_assistant_hub_slack_taps import APP, BOT, NOW, T, U, World

OPERATOR, VIEWER = "ask-op-key", "ask-viewer-key"


class AskWorld(World):
    def __init__(self, tmp_path, **cfg):
        super().__init__(tmp_path)
        self.cfg = {**self.cfg, **cfg}
        self.runtime = self.new_runtime()
        self.n = 0

    def new_runtime(self, store=None, **kw):
        kw = {"min_backoff": 0.05, "max_backoff": 0.2, "ping_interval": 0.3, "reoffer_backoff": 0.1, **kw}
        return SlackRuntime(store or self.store, self.service, lambda: self.cfg, self.clock, **kw)

    def decide(self, cmd=None, task="t1", repo="acme/repo-a"):
        if cmd is None:
            self.n += 1
            cmd = f"frobnicate --all --run={self.n}"
        return decide(self.store, self.service.config["grants"], self.worker,
                      RawAction(tool="Bash", command=cmd, repo=repo), "s1", task, NOW,
                      notify=self.runtime.notify_pending)

    def posts(self):
        return self.listener.calls_to("chat.postMessage")

    def raw_posts(self):
        return [raw for method, raw in self.listener.bodies if method == "chat.postMessage"]


@pytest.fixture
def aw(tmp_path):
    w = AskWorld(tmp_path)
    try:
        yield w
    finally:
        w.runtime.stop()
        w.listener.close()


def buttons(post):
    (actions,) = [b for b in post["blocks"] if b["type"] == "actions"]
    return actions["elements"]


def test_new_pending_action_posts_one_message_with_three_buttons(aw):
    aw.runtime.start()
    d = aw.decide("frobnicate --all --token=hunter2-CANARY")
    assert d.outcome == "pending"
    assert aw.listener.wait_for(lambda: len(aw.posts()) == 1)
    (post,) = aw.posts()
    assert [b["text"]["text"] for b in buttons(post)] == ["Approve", "Deny", "Approve and remember"]
    assert [b["action_id"] for b in buttons(post)] == ["ask_approve", "ask_deny", "ask_remember"]
    (ask,) = aw.store.list_slack_asks(T, U, "open")
    assert {b["value"] for b in buttons(post)} == {f"{ask.ask_id}:{ask.nonce}"}
    assert ask.request_id == d.request_id and ask.ts and ask.channel == post["channel"]
    text = post["blocks"][0]["text"]["text"]
    assert "w1" in text and "acme/repo-a" in text and "owner decides" in text and "hunter2-CANARY" not in text
    (posted,) = aw.audits("assistant.slack.ask_posted")
    assert ask.ask_id in posted["detail"] and "frobnicate" not in str(posted) and BOT not in str(posted)


def test_reasking_the_same_pending_request_posts_nothing_new(aw):
    aw.runtime.start()
    first = aw.decide("frobnicate --same")
    assert aw.listener.wait_for(lambda: len(aw.posts()) == 1)
    again = aw.decide("frobnicate --same")
    assert again.request_id == first.request_id
    time.sleep(0.4)
    assert len(aw.posts()) == 1 and aw.runtime.queued() == 0


def test_a_veto_posts_nothing(aw):
    aw.runtime.start()
    d = aw.decide("GIT_SSH_COMMAND=x rm -rf /quobyte/x")
    assert d.outcome == "denied" and d.source == "veto"
    time.sleep(0.4)
    assert aw.posts() == [] and aw.runtime.queued() == 0 and aw.store.list_slack_asks(T, U) == []


def test_unreachable_slack_leaves_decide_pending_fast_and_the_ask_is_retried(aw):
    good = aw.cfg["base_url"]
    aw.cfg = {**aw.cfg, "base_url": "http://127.0.0.1:1/api"}
    aw.runtime.start()
    started = time.monotonic()
    d = aw.decide()
    assert d.outcome == "pending" and time.monotonic() - started < 0.5
    time.sleep(0.4)
    assert aw.posts() == [] and aw.runtime.queued() == 1            # kept, not lost
    aw.cfg = {**aw.cfg, "base_url": good}                           # Slack is reachable again
    assert aw.listener.wait_for(lambda: len(aw.posts()) == 1)
    assert aw.listener.wait_for(lambda: aw.runtime.queued() == 0)
    (ask,) = aw.store.list_slack_asks(T, U, "open")
    assert ask.request_id == d.request_id and ask.ts


@pytest.mark.parametrize("failure", ["500", "429:0"])
def test_slack_errors_are_retried_until_the_post_succeeds(aw, failure):
    aw.listener.fail_posts[:] = [failure, failure]
    aw.runtime.start()
    d = aw.decide()
    assert d.outcome == "pending"
    assert aw.listener.wait_for(lambda: len(aw.posts()) == 1)
    assert len(aw.raw_posts()) == 3                                 # two failed attempts, then the success
    assert len(aw.store.list_slack_asks(T, U)) == 1                 # one ask row across all attempts


def test_slow_slack_does_not_slow_decide(aw):
    aw.listener.on_request = lambda method: time.sleep(1.2) if method == "chat.postMessage" else None
    aw.runtime.start()
    started = time.monotonic()
    ds = [aw.decide() for _ in range(3)]
    assert time.monotonic() - started < 0.5 and all(d.outcome == "pending" for d in ds)
    assert aw.listener.wait_for(lambda: len(aw.posts()) == 3, timeout=10)


def test_mrkdwn_control_sequences_in_display_arrive_inert(aw):
    aw.runtime.start()
    nasty = "frobnicate '<!channel>' <@U123> '<https://x|y>' `id` ```x``` '<!here|x>' @here @channel &amp;"
    aw.decide(nasty)
    assert aw.listener.wait_for(lambda: len(aw.raw_posts()) == 1)
    (raw,) = aw.raw_posts()
    for live in (b"<!channel>", b"<@U123>", b"<https://x|y>", b"<!here|x>", b"<!here", b"@here", b"@channel"):
        assert live not in raw
    assert b"&lt;!channel&gt;" in raw and b"&lt;@" in raw and b"&lt;https://x|y&gt;" in raw
    text = json.loads(raw)["blocks"][0]["text"]["text"]
    assert text.count("`") == 8                    # only our own: the quoted worker id (2) and the fence (6)
    assert "```id" not in text and "`id`" not in text
    assert "&amp;amp;" in text


def test_a_chatty_task_is_batched_into_one_line(aw):
    aw.cfg = {**aw.cfg, "max_asks_per_task_per_hour": 2}
    aw.runtime.start()
    for _ in range(5):
        assert aw.decide(task="busy").outcome == "pending"
    assert aw.listener.wait_for(lambda: aw.runtime.queued() == 0)
    posts = aw.posts()
    assert len(posts) == 3                                          # two asks and ONE batched line
    assert sum(1 for p in posts if any(b["type"] == "actions" for b in p["blocks"])) == 2
    assert aw.listener.wait_for(lambda: any("3 more waiting" in str(u.get("text")) for u in aw.listener.calls_to("chat.update")))
    assert len(aw.audits("assistant.slack.ask_batched")) == 3
    assert aw.decide(task="other").outcome == "pending"             # another task still gets its own ask
    assert aw.listener.wait_for(lambda: len(aw.posts()) == 4)


def test_queue_is_bounded_and_drops_the_oldest(aw):
    small = SlackRuntime(aw.store, aw.service, lambda: aw.cfg, aw.clock, queue_size=2)     # never started
    rows = []
    for _ in range(3):
        d = decide(aw.store, aw.service.config["grants"], aw.worker,
                   RawAction(tool="Bash", command=f"frobnicate {len(rows)}"), "s1", "t1", NOW,
                   notify=small.notify_pending)
        rows.append(d.request_id)
    assert small.queued() == 2 and small.dropped == 1


def test_a_request_decided_before_the_post_is_not_asked(aw):
    d = aw.decide()
    from herdr_engineering.control_plane.assistant_hub.policy import record_owner_decision
    record_owner_decision(aw.store, aw.service.config["grants"], NOW, T, U, d.request_id, False, U)
    aw.runtime.start()
    assert aw.listener.wait_for(lambda: aw.runtime.queued() == 0)
    assert aw.posts() == []


def test_disabled_slack_queues_nothing(aw):
    aw.cfg = {**aw.cfg, "enabled": False}
    assert aw.decide().outcome == "pending"
    assert aw.runtime.queued() == 0


def test_mcp_action_request_posts_through_the_hub_slack_runtime(aw):
    registry = WorkerRegistry(aw.store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30))
    service = AssistantService(store=aw.store, config={**aw.service.config, "enabled": True})
    hub = Hub(registry=registry, service=service, store=aw.store, slack=aw.runtime)
    _, code = registry.create_enrollment(tenant_id=T, user_id=U, name="box", harness="pi", scopes=["act", "read"])
    worker, cred = registry.redeem(code, device_id="d1")
    aw.runtime.start()
    status, body = handle(hub, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "action_request", "arguments": {"tool": "Bash", "command": "frobnicate --mcp", "session_id": "s",
                                                "task_id": "t"}}}, f"Worker {cred}", "d1")
    assert status == 200 and "pending" in str(body) and not body["result"]["isError"]
    assert aw.listener.wait_for(lambda: len(aw.posts()) == 1)
    assert worker.worker_id in aw.posts()[0]["blocks"][0]["text"]["text"]


# -- slack_text --------------------------------------------------------------------------------

def test_slack_text_escapes_neutralizes_strips_and_truncates():
    assert slack_text("a & b <c> d") == "a &amp; b &lt;c&gt; d"
    out = slack_text("<!channel> <!here|x> <@U123> <https://x|y> @here @channel `rm`")
    assert "<" not in out and ">" not in out and "`" not in out and "@here" not in out and "@channel" not in out
    assert slack_text("a\x00b\x1b[31mc\nd\te\x7f") == "a b [31mc d e "
    long = slack_text("x" * 1000)
    assert len(long) <= 300 and long.endswith("…")
    cut = slack_text("&" * 400)
    assert len(cut) <= 300 and cut.rstrip("…").endswith(";")      # never cut inside an entity
    assert slack_text("") == ""


# -- status route ------------------------------------------------------------------------------

def call(base, path, key=None, method="GET"):
    req = urllib.request.Request(base + path, method=method, data=b"{}" if method == "POST" else None,
                                 headers={"Content-Type": "application/json", **({"X-API-Key": key} if key else {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except ValueError:
            return e.code, raw


@pytest.fixture
def api(aw, acknowledged_sink):
    import hashlib
    pid = "key:" + hashlib.sha256(OPERATOR.encode()).hexdigest()[:12]
    svc = AssistantService(store=aw.store, config=assistant_config({"personal_assistant": {
        "enabled": True, "tenancy": {"principals": {pid: T}}}}))
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=svc,
                             api_keys={OPERATOR: "operator", VIEWER: "viewer"}, assistant_slack=aw.runtime)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    yield f"http://127.0.0.1:{svr.port}", pid
    svr.shutdown()


def test_status_route_requires_operator_and_never_returns_token_text(aw, api):
    base, pid = api
    assert call(base, "/api/v1/assistant-slack")[0] in (401, 403)
    assert call(base, "/api/v1/assistant-slack", VIEWER)[0] == 403
    status, off = call(base, "/api/v1/assistant-slack", OPERATOR)
    assert status == 200 and off == {"enabled": True, "connected": False, "last_event_at": None, "open_asks": 0}
    aw.runtime.start()
    assert aw.listener.wait_for(lambda: aw.listener.ws_alive == 1)
    assert aw.listener.wait_for(lambda: call(base, "/api/v1/assistant-slack", OPERATOR)[1]["connected"] is True)
    # an ask for the caller's own scope (tenant T, user = the principal id) is counted; the tap scope is not leaked
    aw.worker = SimpleNamespace(**{**aw.worker.__dict__, "user_id": pid})
    aw.decide()
    assert aw.listener.wait_for(lambda: call(base, "/api/v1/assistant-slack", OPERATOR)[1]["open_asks"] == 1)
    status, on = call(base, "/api/v1/assistant-slack", OPERATOR)
    assert status == 200 and set(on) == {"enabled", "connected", "last_event_at", "open_asks"}
    assert on["connected"] is True and on["last_event_at"]
    blob = json.dumps(on)
    assert BOT not in blob and APP not in blob and "xoxb" not in blob and "xapp" not in blob and "token" not in blob
    assert call(base, "/api/v1/assistant-slack", OPERATOR, "POST")[0] in (404, 405)


def test_status_route_reports_off_without_a_runtime(acknowledged_sink):
    import hashlib
    pid = "key:" + hashlib.sha256(OPERATOR.encode()).hexdigest()[:12]
    svc = AssistantService(config=assistant_config({"personal_assistant": {
        "enabled": True, "tenancy": {"principals": {pid: T}}}}))
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=svc,
                             api_keys={OPERATOR: "operator"})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        assert call(f"http://127.0.0.1:{svr.port}", "/api/v1/assistant-slack", OPERATOR) == (
            200, {"enabled": False, "connected": False, "last_event_at": None, "open_asks": 0})
    finally:
        svr.shutdown()


# -- cli wiring --------------------------------------------------------------------------------

def test_cli_wiring_builds_starts_and_exposes_the_slack_runtime(aw):
    from herdr_engineering.cli import _wire_assistant_hub
    from tests.control_plane.judge_support import closed_port
    svc = AssistantService(config=assistant_config({"personal_assistant": {
        "enabled": True,
        "hub": {"enabled": True, "host": "127.0.0.1", "port": closed_port(), "allowed_networks": ["127.0.0.0/8"]},
        "slack": {k: aw.cfg[k] for k in ("enabled", "base_url", "bot_token_file", "app_token_file", "owner_user_id")}}}))
    svr = SimpleNamespace(assistant=svc, assistant_workers=None, assistant_judge=None, assistant_slack="unset")
    hub_server = _wire_assistant_hub(svr)
    try:
        assert hub_server is not None and svr.assistant_slack is hub_server.hub.slack is not None
        assert aw.listener.wait_for(lambda: aw.listener.ws_alive == 1)       # started because Slack is enabled
    finally:
        hub_server.shutdown()
        assert hub_server.hub.slack.stop() is True
    assert aw.listener.wait_for(lambda: aw.listener.ws_alive == 0)
    off = SimpleNamespace(assistant=AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}})),
                          assistant_workers="x", assistant_judge="x", assistant_slack="unset")
    assert _wire_assistant_hub(off) is None and off.assistant_slack is None


# -- fix round 1: nothing is lost, permanent errors do not block, bounded bookkeeping ----------------

def down(aw):
    good = aw.cfg["base_url"]
    aw.cfg = {**aw.cfg, "base_url": "http://127.0.0.1:1/api"}
    return good


def test_unposted_ask_survives_a_restart_and_is_posted_exactly_once(aw):
    good = down(aw)
    aw.runtime.start()
    d = aw.decide()
    time.sleep(0.3)
    assert aw.posts() == [] and aw.runtime.queued() == 1
    assert aw.runtime.stop() is True                                     # the hub restarts: the queue is gone
    restored = AssistantStore()
    restored.restore(aw.store.snapshot())
    aw.cfg = {**aw.cfg, "base_url": good}
    again = aw.new_runtime(restored)
    try:
        again.start()
        # the post reaches the wire before its ts is stored: wait on the stored state
        assert aw.listener.wait_for(lambda: any(a.ts for a in restored.list_slack_asks(T, U)), timeout=10)
        (ask,) = [a for a in restored.list_slack_asks(T, U) if a.ts]
        assert ask.request_id == d.request_id
        assert again.sweep() == 0                                        # a later sweep offers nothing again
        time.sleep(0.4)
        assert len(aw.posts()) == 1
    finally:
        again.stop()


def test_request_older_than_approval_ttl_is_not_reoffered_but_a_younger_one_is(aw):
    aw.cfg = {**aw.cfg, "ask_ttl_minutes": 1}
    good = down(aw)
    aw.runtime.start()
    old = aw.decide()
    young = aw.decide()
    aw.clock.now = NOW + timedelta(minutes=2)                            # past the ask ttl: both are dropped
    assert aw.listener.wait_for(lambda: len(aw.audits("assistant.slack.ask_failed")) == 2)
    assert aw.runtime.queued() == 0
    aw.cfg = {**aw.cfg, "base_url": good}
    aw.clock.now = NOW + timedelta(minutes=14, seconds=59)               # younger than approval_ttl (15 min)
    time.sleep(0.2)
    aw.runtime.sweep()                                                   # (the reconnect sweep may win the race)
    assert aw.listener.wait_for(lambda: len(aw.posts()) == 2)
    time.sleep(0.3)
    assert len(aw.posts()) == 2
    assert {a.request_id for a in aw.store.list_slack_asks(T, U, "open") if a.ts} == {old.request_id, young.request_id}


def test_request_past_approval_ttl_is_never_reoffered(aw):
    aw.cfg = {**aw.cfg, "ask_ttl_minutes": 1}
    good = down(aw)
    aw.runtime.start()
    aw.decide()
    aw.clock.now = NOW + timedelta(minutes=2)
    assert aw.listener.wait_for(lambda: len(aw.audits("assistant.slack.ask_failed")) == 1)
    aw.cfg = {**aw.cfg, "base_url": good}
    aw.clock.now = NOW + timedelta(minutes=16)                           # older than approval_ttl_minutes
    time.sleep(0.2)
    assert aw.runtime.sweep() == 0
    time.sleep(0.3)
    assert aw.posts() == []


def test_request_decided_by_rest_before_the_sweep_is_never_asked(aw):
    good = down(aw)
    d = aw.decide()
    record_owner_decision(aw.store, aw.service.config["grants"], NOW, T, U, d.request_id, False, U)
    aw.cfg = {**aw.cfg, "base_url": good}
    fresh = aw.new_runtime()
    assert fresh.sweep() == 0 and fresh.queued() == 0
    fresh.start()
    time.sleep(0.4)
    fresh.stop()
    assert aw.posts() == []


def test_sweep_skips_vetoed_requests_and_ones_already_asked_or_queued(aw):
    aw.decide("GIT_SSH_COMMAND=x rm -rf /quobyte/x")                     # vetoed row, never asked
    aw.decide()
    fresh = aw.new_runtime()
    aw.runtime.start()
    assert aw.listener.wait_for(lambda: len(aw.posts()) == 1)
    assert aw.runtime.stop()                    # quiesce the drain thread: it must not race `fresh.sweep()` below
    queued = aw.decide()                                                 # already queued in `fresh` below
    fresh.notify_pending(aw.store.get_action_request(queued.request_id, tenant_id=T, user_id=U), "x", "y")
    assert fresh.sweep() == 0 and fresh.queued() == 1
    assert len(aw.posts()) == 1


def test_a_permanent_error_does_not_block_the_next_ask_and_is_reoffered_later(aw):
    aw.cfg = {**aw.cfg, "ask_ttl_minutes": 120}
    aw.listener.fail_posts[:] = ["reject"]                               # channel_not_found: permanent
    aw.runtime = aw.new_runtime(sweep_interval=0.3)
    aw.runtime.start()
    first = aw.decide()
    second = aw.decide()
    def posted() -> list:
        return [a for a in aw.store.list_slack_asks(T, U, "open") if a.ts]

    # the post reaches the wire before its ts and the failure audit are stored: wait on the stored state
    assert aw.listener.wait_for(lambda: len(posted()) == 1 and len(aw.audits("assistant.slack.ask_failed")) == 1,
                                timeout=10)                                  # no wait for ask_ttl
    assert posted()[0].request_id == second.request_id
    assert len(aw.posts()) == 1
    assert aw.listener.wait_for(lambda: len(posted()) == 2, timeout=10)      # the sweep re-offers the first
    assert {a.request_id for a in posted()} == {first.request_id, second.request_id}


def test_ratelimited_waits_for_retry_after(aw):
    aw.listener.fail_posts[:] = ["429:1"]
    aw.runtime.start()
    started = time.monotonic()
    aw.decide()
    assert aw.listener.wait_for(lambda: len(aw.posts()) == 1, timeout=6)
    assert time.monotonic() - started >= 0.9
    assert len(aw.raw_posts()) == 2


def test_task_bookkeeping_is_pruned_and_capped(aw):
    aw.cfg = {**aw.cfg, "max_asks_per_task_per_hour": 1, "max_asks_per_worker_per_hour": 1000,
              "max_asks_per_hour": 10000}                                # only the per-task window is under test
    aw.runtime = aw.new_runtime(max_task_keys=5)
    aw.runtime.start()
    for i in range(12):
        aw.decide(task=f"task-{i}")
        aw.decide(task=f"task-{i}")                                      # the second of each task is batched
    assert aw.listener.wait_for(lambda: aw.runtime.queued() == 0)
    assert len(aw.runtime._posted) <= 5 and len(aw.runtime._batches) <= 5
    aw.clock.now = NOW + timedelta(hours=3)                              # everything is older than an hour
    aw.decide(task="later")
    assert aw.listener.wait_for(lambda: aw.runtime.queued() == 0)
    assert aw.listener.wait_for(lambda: set(aw.runtime._posted) == {(T, U, "w1", "later", "action"),
                                                                     (T, U, "w1", "*", "action"),
                                                                     ("*", "*", "*", "*", "action")}
                                and aw.runtime._batches == {})


def test_the_same_request_queued_twice_is_posted_once(aw):
    """The sweep can race notify_pending and queue one request twice: only one ask may ever reach Slack."""
    req = aw.request()
    row = aw.store.get_action_request(req, tenant_id=T, user_id=U)
    aw.runtime.notify_pending(row, row.display, row.reason)
    aw.runtime.notify_pending(row, row.display, row.reason)
    assert aw.runtime.queued() == 2
    aw.runtime.start()
    assert aw.listener.wait_for(lambda: aw.runtime.queued() == 0)
    time.sleep(0.3)
    assert len(aw.posts()) == 1
    assert len([a for a in aw.store.slack_asks_for_request(req) if a.ts]) == 1


def test_tap_on_an_orphan_retired_ask_is_refused_as_not_open(aw):
    req = aw.request()
    orphan = aw.ask(req, ts="", channel="")          # an unposted ask left behind by a crash
    assert aw.runtime.sweep() == 1                    # the sweep retires it and queues a fresh one
    assert aw.store.get_slack_ask(orphan.ask_id, tenant_id=T, user_id=U).status == "expired"
    aw.tap(orphan, channel="", ts="")
    assert aw.status(req) == "pending"
    (refused,) = aw.audits("assistant.slack.refused")
    assert "reason=not_open" in refused["detail"]
