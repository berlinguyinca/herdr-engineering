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
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction
from herdr_engineering.control_plane.assistant_hub.mcp import Hub, handle
from herdr_engineering.control_plane.assistant_hub.policy import decide
from herdr_engineering.control_plane.assistant_hub.slack import SlackRuntime, slack_text
from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_assistant_hub_slack_taps import APP, BOT, NOW, T, U, World

OPERATOR, VIEWER = "ask-op-key", "ask-viewer-key"


class AskWorld(World):
    def __init__(self, tmp_path, **cfg):
        super().__init__(tmp_path)
        self.cfg = {**self.cfg, **cfg}
        self.runtime = SlackRuntime(self.store, self.service, lambda: self.cfg, self.clock,
                                    min_backoff=0.05, max_backoff=0.2, ping_interval=0.3)
        self.n = 0

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


@pytest.mark.parametrize("failure", ["500", "reject"])
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
