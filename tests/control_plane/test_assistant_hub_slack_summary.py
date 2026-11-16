"""End-of-work Slack task summaries: one per (worker, task), from a final report or an idle timer.

Real hub, real MCP handler, real policy and judge wire path, real local Slack listener; no fakes."""
import json
import time
from dataclasses import replace
from datetime import timedelta

import pytest

from herdr_engineering.control_plane.assistant import (
    AssistantService,
    AssistantStore,
    _slack_config,
)
from herdr_engineering.control_plane.assistant_hub.policy import record_owner_decision
from herdr_engineering.control_plane.assistant_hub.slack import SlackRuntime
from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry
from tests.control_plane.slack_support import SlackListener
from tests.control_plane.test_assistant_hub_action_request import ACT, NOW, act, grant_for, make_hub
from tests.control_plane.test_assistant_hub_mcp import body_of, call, enrol
from tests.control_plane.test_assistant_hub_slack_taps import APP, BOT, OWNER, Clock

T, U = "t1", "u1"
HOOK = "<!channel> <@U0EVIL> `rm -rf` @here <https://evil.example|docs>"


class SumWorld:
    def __init__(self, tmp_path, **cfg):
        self.listener = SlackListener(BOT, APP)
        bot, app = tmp_path / "bot", tmp_path / "app"
        bot.write_text(BOT + "\n")
        app.write_text(APP + "\n")
        for f in (bot, app):
            f.chmod(0o600)
        self.cfg = _slack_config({"enabled": True, "base_url": self.listener.base_url, "bot_token_file": str(bot),
                                  "app_token_file": str(app), "owner_user_id": OWNER, **cfg})
        self.clock = Clock()
        self.clock.now = NOW
        base = make_hub()
        self.store = base.store
        self.hub = replace(base, event_rate_limit=10_000)
        self.runtime = self.new_runtime()
        self.hub = replace(self.hub, slack=self.runtime)
        self.auths: dict[str, tuple] = {}

    def new_runtime(self, store=None):
        return SlackRuntime(store or self.store, self.hub.service, lambda: self.cfg, self.clock,
                            min_backoff=0.05, max_backoff=0.2, ping_interval=0.3, reoffer_backoff=0.1,
                            summary_interval=0.1)

    def worker(self, name, tenant=T, user=U):
        self.auths[name] = enrol(self.hub, tenant=tenant, user=user, device="d-" + name, scopes=ACT)
        return self.auths[name][0]

    def act(self, name, **over):
        return body_of(act(self.hub, self.auths[name], **over))

    def report(self, name, text="done", task_id="t1", final=None, kind="outcome", **extra):
        args = {"kind": kind, "text": text, "session_id": "s1", **extra}
        if task_id is not None:
            args["task_id"] = task_id
        if final is not None:
            args["final"] = final
        return call(self.hub, self.auths[name], "report", args)

    def owner_decides(self, request_id, approve):
        record_owner_decision(self.store, self.hub.service.config["grants"], NOW, T, U, request_id, approve, U)

    def posts(self):
        return self.listener.calls_to("chat.postMessage")

    def summaries(self):
        return [p for p in self.posts() if str(p.get("text", "")).startswith("Task summary")]

    def summary_body(self):
        (post,) = self.summaries()
        return "\n".join(b["text"]["text"] for b in post["blocks"] if b["type"] == "section")

    def summary_raw(self):
        return "".join(r.decode() for m, r in self.listener.bodies if m == "chat.postMessage" and b"Task summary" in r)

    def audits(self, action):
        return [e for e in self.store.audit if e["action"] == action]

    def close(self):
        self.runtime.stop()
        self.hub.judge.recorder.stop()
        self.listener.close()


@pytest.fixture
def sw(tmp_path):
    w = SumWorld(tmp_path)
    w.worker("a")
    w.runtime.start()
    try:
        yield w
    finally:
        w.close()


def _busy_task(w):
    """Worker a on task t1: 2 grant-authorized, 1 one-shot, 1 owner-denied, 1 vetoed.  Noise on t2, worker b, tenant 2."""
    w.worker("b")
    other_tenant = SumWorld.worker(w, "c", tenant="t2", user="u9")
    grant_for(w.hub, w.auths["a"][0])
    grant_for(w.hub, w.auths["b"][0])
    grant_for(w.hub, other_tenant)
    for _ in range(2):
        assert w.act("a")["source"] == "grant"
    pending = w.act("a", command="git push origin dev")
    assert pending["outcome"] == "pending"
    w.owner_decides(pending["request_id"], True)
    assert w.act("a", command="git push origin dev")["source"] == "owner"          # the one-shot is spent
    refused = w.act("a", command="git push origin staging")
    w.owner_decides(refused["request_id"], False)
    assert w.act("a", command="git push --force origin main")["source"] == "veto"
    # noise that must not be counted for (a, t1)
    assert w.act("a", task_id="t2")["source"] == "grant"
    assert w.act("a", task_id="t2", command="git push --force origin dev")["source"] == "veto"
    assert w.act("b")["source"] == "grant"
    assert w.act("b", command="git push --force origin main")["source"] == "veto"
    assert call(w.hub, w.auths["c"], "action_request", {"tool": "Bash", "command": "git push --force origin main",
                                                         "repo": "acme/repo", "session_id": "s1",
                                                         "task_id": "t1"})["isError"] is False


def test_final_report_posts_exactly_one_summary_with_correct_counts(sw):
    _busy_task(sw)
    out = sw.report("a", text="all shipped", final=True)
    assert out["isError"] is False and body_of(out)["status"] == "recorded"
    assert sw.listener.wait_for(lambda: len(sw.summaries()) == 1)
    time.sleep(0.4)
    body = sw.summary_body()
    assert len(sw.summaries()) == 1
    assert "Authorized under grants: at least 2" in body
    assert "One-shot approvals used: at least 1" in body
    assert "Denied by you: at least 1" in body
    assert "Vetoed: at least 1" in body
    assert "all shipped" in body
    raw = sw.summary_raw()
    assert "git push" not in raw and "origin" not in raw          # counts and the report text only, never commands
    (ask,) = [a for a in sw.store.list_slack_asks(T, U) if a.kind == "summary" and a.status == "decided"]
    assert (ask.task_id, ask.status, ask.ts != "") == ("t1", "decided", True)
    (audit,) = sw.audits("assistant.slack.summary_posted")
    assert ask.ask_id in audit["detail"] and "all shipped" not in str(audit)


def test_repeated_final_reports_and_a_restart_do_not_repeat_the_summary(sw):
    sw.report("a", final=True, text="first")
    assert sw.listener.wait_for(lambda: len(sw.summaries()) == 1)
    for _ in range(3):
        assert sw.report("a", final=True, text="again")["isError"] is False
    time.sleep(0.5)
    assert len(sw.summaries()) == 1
    # a restart over a restored snapshot: new store, new runtime, same listener
    restored = AssistantStore()
    restored.restore(json.loads(json.dumps(sw.store.snapshot(), default=str)))
    sw.runtime.stop()
    hub2 = replace(sw.hub, store=restored, service=AssistantService(store=restored, config=sw.hub.service.config),
                   registry=replace_registry(sw.hub, restored))
    rt2 = SlackRuntime(restored, hub2.service, lambda: sw.cfg, sw.clock, min_backoff=0.05, max_backoff=0.2,
                       ping_interval=0.3, reoffer_backoff=0.1, summary_interval=0.1)
    hub2 = replace(hub2, slack=rt2)
    rt2.start()
    try:
        call(hub2, sw.auths["a"], "report", {"kind": "outcome", "text": "after restart", "session_id": "s1",
                                             "task_id": "t1", "final": True})
        sw.clock.now = NOW + timedelta(hours=2)                    # and the idle timer must not fire either
        time.sleep(0.8)
        assert len(sw.summaries()) == 1
    finally:
        rt2.stop()


def replace_registry(hub, store):
    return WorkerRegistry(store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30), now=lambda: NOW)


def test_idle_task_gets_one_summary_and_a_later_final_report_adds_none(sw):
    sw.report("a", text="halfway", final=False)
    sw.act("a", command="git push --force origin main")
    time.sleep(0.5)
    assert sw.summaries() == []
    sw.clock.now = NOW + timedelta(minutes=29)
    time.sleep(0.5)
    assert sw.summaries() == []
    sw.clock.now = NOW + timedelta(minutes=31)
    assert sw.listener.wait_for(lambda: len(sw.summaries()) == 1)
    assert "Vetoed: at least 1" in sw.summary_body() and "halfway" in sw.summary_body()
    sw.report("a", text="late", final=True)
    sw.clock.now = NOW + timedelta(hours=3)
    time.sleep(0.8)
    assert len(sw.summaries()) == 1


def test_activity_pushes_the_idle_deadline_back(sw):
    sw.report("a", text="one")
    sw.clock.now = NOW + timedelta(minutes=20)
    sw.report("a", text="two")
    sw.clock.now = NOW + timedelta(minutes=40)           # 20 minutes since the last event: not idle yet
    time.sleep(0.6)
    assert sw.summaries() == []
    sw.clock.now = NOW + timedelta(minutes=51)
    assert sw.listener.wait_for(lambda: len(sw.summaries()) == 1)


def test_a_chatty_worker_posts_no_summary_and_nothing_else(sw):
    for i in range(40):
        assert sw.report("a", text=f"progress {i}", final=False)["isError"] is False
    time.sleep(0.6)
    assert sw.posts() == [] and sw.runtime.queued() == 0
    assert len([a for a in sw.store.list_slack_asks(T, U) if a.kind == "summary"]) == 1     # one tracker, not forty


def test_report_without_the_new_arguments_behaves_as_before(sw):
    out = call(sw.hub, sw.auths["a"], "report", {"kind": "decision", "text": "chose X", "session_id": "s1",
                                                 "payload": {"k": "v"}})
    assert out["isError"] is False and body_of(out)["status"] == "recorded"
    (event,) = [e for e in sw.store.worker_events.values() if e.kind == "report"]
    assert event.payload == {"kind": "decision", "text": "chose X", "payload": {"k": "v"}}
    assert sw.store.list_slack_asks(T, U) == []
    time.sleep(0.3)
    assert sw.posts() == []


def test_new_arguments_are_recorded_in_the_event_payload(sw):
    sw.report("a", text="x", final=True)
    (event,) = [e for e in sw.store.worker_events.values() if e.kind == "report"]
    assert event.payload["task_id"] == "t1" and event.payload["final"] is True


@pytest.mark.parametrize("extra", [{"bogus": 1}, {"final": "yes", "task_id": "t1"}, {"task_id": "x" * 129},
                                   {"task_id": ""}, {"task_id": 7}, {"final": True}])
def test_unknown_or_invalid_arguments_are_rejected(sw, extra):
    args = {"kind": "outcome", "text": "x", "session_id": "s1", **extra}
    out = call(sw.hub, sw.auths["a"], "report", args)
    assert out["isError"] is True
    assert [e for e in sw.store.worker_events.values() if e.kind == "report"] == []


def test_schema_lists_the_new_arguments_and_still_closes_the_object(sw):
    from herdr_engineering.control_plane.assistant_hub.mcp import _TOOLS
    schema = _TOOLS["report"][2]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["task_id"]["maxLength"] == 128 and schema["properties"]["final"] == {"type": "boolean"}


def test_worker_report_text_reaches_slack_inert(sw):
    sw.report("a", text=HOOK, final=True)
    assert sw.listener.wait_for(lambda: len(sw.summaries()) == 1)
    raw = sw.summary_raw()
    for bad in ("<!channel>", "<@U0EVIL>", "<https://evil.example|docs>", "`rm", "@here"):
        assert bad not in raw
    assert "&lt;!channel&gt;" in raw


def test_report_text_is_capped_and_secrets_are_not_forwarded(sw):
    sw.report("a", text="hcw_" + "A" * 40 + " " + "y" * 1900, final=True)
    assert sw.listener.wait_for(lambda: len(sw.summaries()) == 1)
    body = sw.summary_body()
    assert "hcw_AAAA" not in body and len(body) < 1500


def test_tasks_and_workers_get_their_own_summaries(sw):
    sw.worker("b")
    sw.report("a", task_id="t1", final=True, text="a1")
    sw.report("a", task_id="t2", final=True, text="a2")
    sw.report("b", task_id="t1", final=True, text="b1")
    assert sw.listener.wait_for(lambda: len(sw.summaries()) == 3)
    time.sleep(0.4)
    assert len(sw.summaries()) == 3


def test_summary_idle_minutes_is_configurable(tmp_path):
    w = SumWorld(tmp_path, summary_idle_minutes=5)
    w.worker("a")
    w.runtime.start()
    try:
        w.report("a", text="x")
        w.clock.now = NOW + timedelta(minutes=6)
        assert w.listener.wait_for(lambda: len(w.summaries()) == 1)
    finally:
        w.close()


def test_slack_off_or_absent_posts_nothing_and_stores_nothing(tmp_path):
    w = SumWorld(tmp_path, enabled=False, bot_token_file="", app_token_file="", owner_user_id="")
    w.worker("a")
    try:
        assert w.report("a", final=True)["isError"] is False
        bare = replace(w.hub, slack=None)
        out = call(bare, w.auths["a"], "report", {"kind": "outcome", "session_id": "s1", "task_id": "t1", "final": True})
        assert out["isError"] is False
        assert w.store.list_slack_asks(T, U) == [] and w.posts() == []
    finally:
        w.close()


@pytest.mark.parametrize("value", [1, 30, 1440])
def test_config_accepts_summary_idle_minutes(value):
    assert _slack_config({"summary_idle_minutes": value})["summary_idle_minutes"] == value


def test_config_default_is_thirty():
    assert _slack_config({})["summary_idle_minutes"] == 30


@pytest.mark.parametrize("value", [0, 1441, -1, True, "30", 2.5, None])
def test_config_rejects_bad_summary_idle_minutes(value):
    with pytest.raises(ValueError, match="summary_idle_minutes"):
        _slack_config({"summary_idle_minutes": value})


def test_a_failed_post_is_retried_by_the_sweep_and_still_posts_once(tmp_path):
    w = SumWorld(tmp_path)
    w.worker("a")
    w.listener.fail_posts[:] = ["reject"]                      # channel_not_found: permanent
    w.runtime.start()
    try:
        w.report("a", final=True, text="done")
        assert w.listener.wait_for(lambda: len(w.audits("assistant.slack.ask_failed")) == 1, timeout=5)
        assert w.listener.wait_for(lambda: len(w.summaries()) == 1, timeout=8)
        time.sleep(0.5)
        assert len(w.summaries()) == 1
        (ask,) = [a for a in w.store.list_slack_asks(T, U) if a.kind == "summary"]
        assert ask.status == "decided" and ask.ts
    finally:
        w.close()


def test_the_summary_counts_apart_from_asks_and_questions(sw):
    sw.report("a", final=True)
    assert sw.listener.wait_for(lambda: len(sw.summaries()) == 1)
    keys = list(sw.runtime._posted)
    assert any(k[-1] == "summary" for k in keys)
