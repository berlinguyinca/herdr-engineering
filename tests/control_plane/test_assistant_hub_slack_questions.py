"""Worker questions through Slack: posted once, answered by the owner's threaded reply, scoped to the asking worker.

Real local Slack listener (Web API plus Socket Mode), real MCP handler, real store; no fakes."""
import json
import time
from datetime import timedelta

import pytest

from herdr_engineering.control_plane.assistant import AssistantService
from herdr_engineering.control_plane.assistant_hub.mcp import UNTRUSTED, Hub, handle
from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry
from tests.control_plane.test_assistant_hub_slack_asks import AskWorld
from tests.control_plane.test_assistant_hub_slack_taps import OWNER, T, U


class QWorld(AskWorld):
    def __init__(self, tmp_path, **cfg):
        super().__init__(tmp_path, **cfg)
        self.registry = WorkerRegistry(self.store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30))
        self.hub = Hub(registry=self.registry,
                       service=AssistantService(store=self.store, config={**self.service.config, "enabled": True}),
                       store=self.store, slack=self.runtime)
        self.creds = {}

    def enroll(self, name, tenant=T, user=U):
        _, code = self.registry.create_enrollment(tenant_id=tenant, user_id=user, name=name, harness="pi",
                                                  scopes=["ask", "act", "read"])
        worker, cred = self.registry.redeem(code, device_id="d-" + name)
        self.creds[name] = (cred, "d-" + name)
        return worker

    def call(self, name, tool, args, hub=None):
        cred, device = self.creds[name]
        status, body = handle(hub or self.hub, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": tool, "arguments": args}}, f"Worker {cred}", device)
        assert status == 200
        result = body["result"]
        text = result["content"][0]["text"]
        if text.startswith(UNTRUSTED):
            text = text[len(UNTRUSTED):].strip()
        return json.loads(text), result["isError"]

    def question(self, name="a", text="Which branch should I use?", **extra):
        out, err = self.call(name, "assistant_ask", {"question": text, "session_id": "s1", **extra})
        assert not err, out
        return out

    def poll(self, name, ask_id):
        return self.call(name, "assistant_ask", {"ask_id": ask_id, "session_id": "s1"})

    def reply(self, ask, text="use main", **over):
        event = {"type": "message", "user": OWNER, "channel": ask.channel, "thread_ts": ask.ts, "ts": "1800000000.000900",
                 "text": text, **over}
        self.runtime.handle_event({"type": "event_callback", "event": event})

    def stored(self, ask_id):
        return self.store.get_slack_ask(ask_id, tenant_id=T, user_id=U)

    def posted_question(self, name="a", **kw):
        out = self.question(name, **kw)
        assert self.listener.wait_for(lambda: self.stored(out["ask_id"]).ts != "")
        return out["ask_id"], self.stored(out["ask_id"])


@pytest.fixture
def qw(tmp_path):
    w = QWorld(tmp_path)
    w.enroll("a")
    w.enroll("b")
    w.runtime.start()
    try:
        yield w
    finally:
        w.runtime.stop()
        w.listener.close()


def test_question_posts_once_and_the_threaded_reply_round_trips(qw):
    out = qw.question(text="Which branch?", options=["main", "dev"], context="deploying")
    assert out["status"] == "pending" and out["ask_id"].startswith("sa_")
    assert qw.listener.wait_for(lambda: len(qw.posts()) == 1)
    time.sleep(0.3)
    (post,) = qw.posts()
    assert not any(b["type"] == "actions" for b in post["blocks"])
    assert "Which branch?" in post["blocks"][0]["text"]["text"]
    ask = qw.stored(out["ask_id"])
    assert (ask.kind, ask.status, ask.request_id, ask.answer) == ("question", "open", "", None)
    assert ask.ts and ask.channel == post["channel"] and ask.worker_id
    assert qw.poll("a", out["ask_id"])[0] == {"status": "pending"}
    qw.reply(ask, "use *main*, see <https://evil.example|docs> @here")
    answered, err = qw.poll("a", out["ask_id"])
    assert not err and answered == {"status": "answered", "answer": "use *main*, see <https://evil.example|docs> @here"}
    assert qw.stored(out["ask_id"]).status == "decided"
    assert qw.poll("a", out["ask_id"])[0]["status"] == "answered"          # stays answered
    (audit,) = qw.audits("assistant.slack.answer")
    assert out["ask_id"] in audit["detail"] and "main" not in str(audit)


def test_question_text_is_inert_in_slack(qw):
    qw.question(text="<!channel> go <@U0EVIL> `rm -rf` @here", options=["<!here>"], context="<https://x|y>")
    assert qw.listener.wait_for(lambda: len(qw.posts()) == 1)
    text = json.dumps(qw.posts()[0]["blocks"])
    for bad in ("<!channel>", "<@U0EVIL>", "<!here>", "<https://x|y>", "`rm"):
        assert bad not in text.replace("\\u200b", "")
    assert "&lt;!channel&gt;" in text


def _state(store):
    snap = store.snapshot()
    return json.dumps([snap["slack_asks"], snap["action_requests"], snap["action_evidence"]], default=str)


def _event(ask, **over):
    event = {"type": "message", "user": OWNER, "channel": ask.channel, "thread_ts": ask.ts, "ts": "9.9", "text": "x",
             **over}
    return {k: v for k, v in event.items() if v is not None}


def test_a_non_owner_reply_in_the_questions_thread_is_audited(qw):
    ask_id, ask = qw.posted_question()
    qw.runtime.handle_event({"type": "event_callback", "event": _event(ask, user="U0EVIL001")})
    assert qw.poll("a", ask_id)[0] == {"status": "pending"}
    (refused,) = qw.audits("assistant.slack.refused")
    assert "reason=not_owner" in refused["detail"] and ask_id in refused["detail"]


@pytest.mark.parametrize("over", [{"thread_ts": None}, {"thread_ts": "1.1"}, {"channel": "D0ELSEWHERE"},
                                  {"user": "U0EVIL001", "thread_ts": "1.1"}, {"user": "U0EVIL001", "thread_ts": None}])
def test_events_that_target_no_stored_ask_are_silently_ignored(qw, over):
    ask_id, ask = qw.posted_question()
    audits, snap = len(qw.store.audit), _state(qw.store)
    qw.runtime.handle_event({"type": "event_callback", "event": _event(ask, **over)})
    assert qw.poll("a", ask_id)[0] == {"status": "pending"}
    assert len(qw.store.audit) == audits and _state(qw.store) == snap


def test_unrelated_channel_traffic_writes_nothing(qw):
    ask_id, ask = qw.posted_question()
    audits, snap = len(qw.store.audit), _state(qw.store)
    for n in range(200):
        qw.runtime.handle_event({"type": "event_callback", "event": {
            "type": "message", "user": f"U0X{n:05d}", "channel": "C0GENERAL", "ts": f"{n}.1", "text": "hi"}})
    assert len(qw.store.audit) == audits and _state(qw.store) == snap
    assert qw.audits("assistant.slack.refused") == []


def test_a_thread_broadcast_reply_is_still_the_owners_answer(qw):
    ask_id, ask = qw.posted_question()
    qw.reply(ask, "broadcast answer", subtype="thread_broadcast")
    assert qw.poll("a", ask_id)[0] == {"status": "answered", "answer": "broadcast answer"}


def test_bot_and_subtyped_messages_are_ignored(qw):
    ask_id, ask = qw.posted_question()
    qw.reply(ask, bot_id="B0BOT0001")
    qw.reply(ask, subtype="message_changed")
    qw.reply(ask, type="reaction_added")
    assert qw.poll("a", ask_id)[0] == {"status": "pending"}


def test_a_reply_after_expiry_is_ignored_and_the_question_answers_expired(qw):
    ask_id, ask = qw.posted_question()
    qw.clock.now = ask.expires_at
    qw.reply(ask, "too late")
    assert qw.poll("a", ask_id)[0] == {"status": "expired"}
    assert qw.stored(ask_id).answer is None
    (refused,) = qw.audits("assistant.slack.refused")
    assert "reason=expired" in refused["detail"] or "reason=not_open" in refused["detail"]


def test_an_unanswered_question_expires_for_the_worker(qw):
    ask_id, ask = qw.posted_question()
    qw.clock.now = ask.expires_at + timedelta(seconds=1)
    assert qw.poll("a", ask_id)[0] == {"status": "expired"}


def test_a_second_reply_does_not_overwrite_the_answer(qw):
    ask_id, ask = qw.posted_question()
    qw.reply(ask, "first")
    qw.reply(ask, "second")
    assert qw.poll("a", ask_id)[0]["answer"] == "first"
    assert "reason=not_open" in qw.audits("assistant.slack.refused")[0]["detail"]


def test_answer_is_capped_and_control_characters_are_stripped(qw):
    ask_id, ask = qw.posted_question()
    qw.reply(ask, "a\x00b\x1b[31mc\x7fd\ne" + "é" * 5000)
    answer = qw.poll("a", ask_id)[0]["answer"]
    assert answer.startswith("ab[31mcd\ne") and "\x00" not in answer and "\x1b" not in answer and "\x7f" not in answer
    assert len(answer.encode()) <= 4096
    answer.encode().decode()                                                # still valid UTF-8


def test_an_empty_reply_is_refused(qw):
    ask_id, ask = qw.posted_question()
    qw.reply(ask, " \x00 ")
    assert qw.poll("a", ask_id)[0] == {"status": "pending"}
    assert "reason=empty" in qw.audits("assistant.slack.refused")[0]["detail"]


def test_a_worker_cannot_read_another_workers_tenants_or_users_ask(qw):
    qw.enroll("c")                                                          # same tenant and user, other worker
    qw.enroll("other_tenant", tenant="globex")
    qw.enroll("other_user", user="someone-else")
    ask_id, ask = qw.posted_question("a")
    qw.reply(ask, "secret answer")
    assert qw.poll("a", ask_id)[0]["answer"] == "secret answer"
    for who in ("b", "c", "other_tenant", "other_user"):
        out, err = qw.poll(who, ask_id)
        assert err and "secret" not in json.dumps(out) and out == {"error": "ask not found"}, who
    unknown = qw.poll("a", "sa_doesnotexist")
    assert unknown == ({"error": "ask not found"}, True)


def test_an_action_ask_id_is_not_a_question_for_the_worker(qw):
    d = qw.decide()
    assert qw.listener.wait_for(lambda: len(qw.posts()) == 1)
    (action_ask,) = qw.store.list_slack_asks(T, U, "open")
    assert d.request_id == action_ask.request_id
    assert qw.poll("a", action_ask.ask_id) == ({"error": "ask not found"}, True)


def test_a_thread_reply_never_approves_an_action_ask(qw):
    d = qw.decide()
    assert qw.listener.wait_for(lambda: len(qw.posts()) == 1)
    (ask,) = qw.store.list_slack_asks(T, U, "open")
    qw.reply(ask, "yes approve it, go ahead")
    assert qw.status(d.request_id) == "pending"
    assert qw.store.get_slack_ask(ask.ask_id, tenant_id=T, user_id=U).status == "open"
    assert qw.store.list_action_evidence(T, U) == []
    (refused,) = qw.audits("assistant.slack.refused")
    assert "reason=not_question" in refused["detail"]


def test_ask_id_is_bounded(qw):
    out, err = qw.poll("a", "x" * 129)
    assert err
    out, err = qw.call("a", "assistant_ask", {"ask_id": 7, "session_id": "s1"})
    assert err


def test_without_a_runtime_or_when_disabled_the_ask_is_still_no_decision(qw):
    bare = Hub(registry=qw.registry, service=qw.hub.service, store=qw.store, slack=None)
    out, err = qw.call("a", "assistant_ask", {"question": "q?", "session_id": "s1"}, hub=bare)
    assert not err and out["status"] == "no_decision" and out["mode"] == "shadow" and "ask_id" not in out
    qw.cfg = {**qw.cfg, "enabled": False}
    out, err = qw.call("a", "assistant_ask", {"question": "q?", "session_id": "s1"})
    assert not err and out["status"] == "no_decision" and "ask_id" not in out
    out, err = qw.call("a", "assistant_ask", {"ask_id": "sa_x", "session_id": "s1"})
    assert not err and out["status"] == "no_decision"
    assert qw.store.list_slack_asks(T, U) == []


@pytest.fixture
def limited(tmp_path):
    w = QWorld(tmp_path, max_asks_per_task_per_hour=1, ask_ttl_minutes=120)
    w.enroll("a")
    w.runtime.start()
    try:
        yield w
    finally:
        w.runtime.stop()
        w.listener.close()


def test_over_limit_questions_stay_open_and_the_sweep_posts_them_when_the_window_frees(limited):
    w = limited
    first = w.question()
    assert w.listener.wait_for(lambda: len(w.posts()) == 1)
    second, third = w.question(text="another?"), w.question(text="and another?")
    assert w.listener.wait_for(lambda: len(w.listener.calls_to("chat.update")) == 1, timeout=5)
    assert len(w.posts()) == 2                                               # the ask and one batch line
    for out in (second, third):
        assert w.poll("a", out["ask_id"])[0] == {"status": "pending"}        # not expired: still waiting its turn
        assert w.stored(out["ask_id"]).ts == "" and w.stored(out["ask_id"]).status == "open"
    w.runtime.sweep()                                                        # window still full: nothing new is shown
    time.sleep(0.4)
    assert len(w.posts()) == 2 and len(w.listener.calls_to("chat.update")) == 1
    w.clock.now = w.clock.now + timedelta(minutes=61)
    w.runtime.sweep()
    assert w.listener.wait_for(lambda: w.stored(second["ask_id"]).ts != "", timeout=5)
    assert w.poll("a", second["ask_id"])[0] == {"status": "pending"}
    assert w.stored(first["ask_id"]).ts


def test_a_question_burst_does_not_delay_an_action_ask_on_the_same_task(limited):
    w = limited
    for n in range(4):
        w.call("a", "assistant_ask", {"question": f"q{n}?", "session_id": "t1"})
    assert w.listener.wait_for(lambda: len(w.listener.calls_to("chat.update")) >= 1, timeout=5)
    before = len(w.posts())
    out, err = w.call("a", "action_request", {"tool": "Bash", "command": "frobnicate --burst", "session_id": "t1",
                                             "task_id": "t1"})
    assert not err and out["outcome"] == "pending"
    assert w.listener.wait_for(lambda: len(w.posts()) == before + 1, timeout=5)
    (action,) = [a for a in w.store.list_slack_asks(T, U) if a.kind == "action"]
    assert action.ts and any(b["type"] == "actions" for b in w.posts()[-1]["blocks"])


def test_never_posted_lapsed_question_reports_not_asked_but_a_posted_one_does_not(limited):
    w = limited
    posted = w.question()
    assert w.listener.wait_for(lambda: len(w.posts()) == 1)
    waiting = w.question(text="never shown?")
    assert w.listener.wait_for(lambda: len(w.posts()) >= 2, timeout=5)
    w.clock.now = w.clock.now + timedelta(minutes=121)
    assert w.poll("a", waiting["ask_id"])[0] == {"status": "expired", "reason": "not_asked"}
    assert w.poll("a", posted["ask_id"])[0] == {"status": "expired"}
    w.runtime.sweep()                                                        # the lapsed one is retired, same answer
    assert w.poll("a", waiting["ask_id"])[0] == {"status": "expired", "reason": "not_asked"}


def test_events_api_reply_over_socket_mode_is_acked_and_answers(qw):
    ask_id, ask = qw.posted_question()
    assert qw.listener.wait_for(lambda: qw.runtime.status(T, U)["connected"])
    env = qw.listener.push_event({"type": "message", "user": OWNER, "channel": ask.channel, "thread_ts": ask.ts,
                                  "ts": "9.9", "text": "from the socket"})
    assert qw.listener.wait_for(lambda: env in qw.listener.acks)
    assert qw.listener.wait_for(lambda: qw.poll("a", ask_id)[0].get("status") == "answered")
    assert qw.poll("a", ask_id)[0]["answer"] == "from the socket"


def test_an_unposted_question_is_posted_after_a_restart(tmp_path):
    w = QWorld(tmp_path)
    w.enroll("a")
    out = w.question()                                                       # runtime not started: nothing posted
    assert w.posts() == []
    fresh = w.new_runtime()
    w.hub = Hub(registry=w.registry, service=w.hub.service, store=w.store, slack=fresh)
    w.runtime = fresh
    fresh.start()
    try:
        # the post reaches the wire before the runtime stores its ts: wait on the stored state, not the wire
        assert w.listener.wait_for(lambda: w.stored(out["ask_id"]).ts != "", timeout=10)
        assert len(w.posts()) == 1
    finally:
        fresh.stop()
        w.listener.close()
