"""Approve and remember: the owner's tap approves, then tries to activate the earned grant through the real gates.

No gate is relaxed: when a gate refuses, the approval stays recorded, the owner is told why in the thread, and the
remember intent is kept for the next qualifying approval.  Real listener, real judge runtime over a real socket."""
from dataclasses import replace
from datetime import timedelta

import pytest

from herdr_engineering.control_plane.assistant import _grants_config
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction, derive
from herdr_engineering.control_plane.assistant_hub.policy import ActivationRefused, decide
from tests.control_plane.judge_support import (
    SAFE,
    WireListener,
    judge_cfg,
    make_runtime,
    satisfy_gate,
    success_body,
    write_key,
)
from tests.control_plane.test_assistant_hub_slack_asks import AskWorld
from tests.control_plane.test_assistant_hub_slack_taps import OWNER, T0, T, U

PUSH = "git push origin main"
RISK = derive(RawAction(tool="Bash", command=PUSH, repo="acme/repo-a")).key.risk_class.value
TINY = {"required_approvals": {"local_reversible": 3, "remote_reversible": 3, "outward": 3},
        "min_sessions": 3, "min_tasks": 2}


class RWorld(AskWorld):
    def __init__(self, tmp_path, *, shadow_days=1):
        super().__init__(tmp_path)
        grants = _grants_config({"shadow_started_at": T0.isoformat(), "shadow_days": shadow_days, **TINY})
        self.service.config["grants"] = grants
        self.judge_listener = WireListener(success_body(SAFE))
        self.judge = make_runtime(self.store, judge_cfg(f"http://127.0.0.1:{self.judge_listener.port}",
                                                        write_key(tmp_path), backend_confirmed=True), self.clock)
        self.runtime.stop()
        self.runtime = self.new_runtime(judge=self.judge)         # not started: taps and thread posts only
        self.cycle = 0

    def pending(self, session, task):
        d = decide(self.store, self.service.config["grants"], self.worker,
                   RawAction(tool="Bash", command=PUSH, repo="acme/repo-a"), session, task, self.clock.now,
                   judge=self.judge)
        assert d.outcome == "pending", d
        return d.request_id

    def consume(self, session, task):
        d = decide(self.store, self.service.config["grants"], self.worker,
                   RawAction(tool="Bash", command=PUSH, repo="acme/repo-a"), session, task, self.clock.now,
                   judge=self.judge)
        assert (d.outcome, d.source) == ("authorized", "owner"), d

    def approve(self, action="ask_approve", *, consume=True):
        """One owner decision on a fresh request; the session/task spread meets the diversity floor."""
        self.cycle += 1
        session, task = f"s{self.cycle}", f"t{1 + self.cycle % 2}"
        ask = self.ask(self.pending(session, task), created_at=self.clock.now,
                       expires_at=self.clock.now + timedelta(minutes=15))
        self.tap(ask, action)
        if consume:
            self.consume(session, task)
        return ask

    def foreign_approval(self, *, worker_id="w1", tenant=T, user=U, cmd=PUSH):
        """An owner_slack approval for some other worker, key, tenant or user."""
        worker = replace(self.worker, worker_id=worker_id, tenant_id=tenant, user_id=user)
        d = decide(self.store, self.service.config["grants"], worker,
                   RawAction(tool="Bash", command=cmd, repo="acme/repo-a"), "sf", "tf", self.clock.now, judge=self.judge)
        assert d.outcome == "pending", d
        ask = self.ask(d.request_id, tenant_id=tenant, user_id=user, created_at=self.clock.now,
                       expires_at=self.clock.now + timedelta(minutes=15))
        self.tap(ask)
        assert self.store.get_action_request(d.request_id, tenant_id=tenant, user_id=user).status == "approved"
        return ask

    def thread(self, ask):
        return [c for c in self.listener.calls_to("chat.postMessage") if c.get("thread_ts") == ask.ts]

    def grants(self):
        return self.store.list_action_grants(T, U)

    def stored(self, ask):
        return self.store.get_slack_ask(ask.ask_id, tenant_id=T, user_id=U)


@pytest.fixture
def rw(tmp_path):
    w = RWorld(tmp_path)
    try:
        yield w
    finally:
        w.runtime.stop()
        w.listener.close()
        w.judge_listener._stop.set()


def test_remember_with_every_gate_satisfied_activates_the_grant_and_the_next_request_rides_it(rw):
    satisfy_gate(rw.store, rw.judge, RISK, T, U)
    rw.approve()
    rw.approve()
    ask = rw.approve("ask_remember", consume=False)
    (grant,) = rw.grants()
    assert grant.state.value == "active" and grant.confirmed_by == f"slack:{OWNER}"
    assert rw.stored(ask).remember is True
    (said,) = rw.thread(ask)
    assert "remember" in said["text"].lower() and "thread_ts" in said
    (act,) = rw.audits("assistant.grant.activated")
    assert act["actor"] == f"slack:{OWNER}"
    rw.consume("s9", "t9")                                   # the tap's own one-shot approval is still spent first
    d = decide(rw.store, rw.service.config["grants"], rw.worker,
               RawAction(tool="Bash", command=PUSH, repo="acme/repo-a"), "s10", "t10", rw.clock.now, judge=rw.judge)
    assert (d.outcome, d.source, d.grant_id) == ("authorized", "grant", grant.grant_id)


def test_a_plain_approve_never_activates_anything(rw):
    satisfy_gate(rw.store, rw.judge, RISK, T, U)
    for _ in range(3):
        rw.approve()
    (grant,) = rw.grants()
    assert grant.state.value == "proposed" and rw.audits("assistant.grant.activated") == []


def test_remember_in_shadow_records_the_approval_says_why_and_activates_on_the_next_approval(tmp_path):
    w = RWorld(tmp_path, shadow_days=14)
    try:
        satisfy_gate(w.store, w.judge, RISK, T, U)
        w.approve()
        w.approve()
        ask = w.approve("ask_remember", consume=False)
        assert w.status(ask.request_id) == "approved"                       # the approval is recorded regardless
        assert w.grants() == []
        (said,) = w.thread(ask)
        assert "shadow" in said["text"].lower()
        assert w.stored(ask).remember is True
        w.consume("s9", "t9")
        w.clock.now = T0 + timedelta(days=15)                              # the shadow period is over
        w.approve()                                                         # any later owner_slack approval retries
        (grant,) = w.grants()
        assert grant.state.value == "active" and grant.confirmed_by == f"slack:{OWNER}"
        assert any("remember" in c["text"].lower() and "now" in c["text"].lower() for c in w.thread(ask)[1:])
        assert len(w.audits("assistant.grant.activated")) == 1
        assert w.stored(ask).activation == "done"                           # the wish is spent: nothing left to retry
    finally:
        w.runtime.stop()
        w.listener.close()
        w.judge_listener._stop.set()


def test_remember_with_an_unsatisfied_judge_gate_says_what_is_missing_and_keeps_the_intent(rw):
    rw.approve()
    rw.approve()
    ask = rw.approve("ask_remember", consume=False)
    (grant,) = rw.grants()
    assert grant.state.value == "proposed" and rw.status(ask.request_id) == "approved"
    (said,) = rw.thread(ask)
    assert "labeled" in said["text"].lower() and "insufficient_labels" not in said["text"]
    assert rw.audits("assistant.grant.activate_refused")[0]["detail"].startswith("insufficient_labels")
    rw.consume("s9", "t9")
    satisfy_gate(rw.store, rw.judge, RISK, T, U)
    rw.approve()
    assert rw.grants()[0].state.value == "active"


def test_remember_before_enough_approvals_waits_for_them(rw):
    ask = rw.approve("ask_remember")
    assert rw.grants() == [] and "approved enough" in rw.thread(ask)[0]["text"].lower()
    satisfy_gate(rw.store, rw.judge, RISK, T, U)
    rw.approve()
    assert rw.grants() == []                                                # 2 of 3 so far, intent still waiting
    rw.approve()
    assert rw.grants()[0].state.value == "active"                           # the third proposes, the kept intent activates


def test_a_plain_approve_without_remember_intent_leaves_a_proposal_alone(rw):
    satisfy_gate(rw.store, rw.judge, RISK, T, U)
    for _ in range(4):
        rw.approve()
    assert rw.grants()[0].state.value == "proposed" and rw.thread(rw.ask(rw.pending("sx", "t1"))) == []


@pytest.mark.parametrize("code", ["still_in_shadow", "insufficient_labels", "judge_disabled", "backend_unconfirmed",
                                  "insufficient_diversity", "no_discrimination", "fn_bound_exceeded", "stale_report",
                                  "judge_error", "not_proposed", "confirmation_missing", "something_new"])
def test_every_refusal_code_has_a_plain_sentence(code):
    from herdr_engineering.control_plane.assistant_hub.slack import refusal_sentence
    sentence = refusal_sentence(ActivationRefused(code, "x"))
    assert sentence and code not in sentence and "_" not in sentence


@pytest.mark.parametrize("other", [{"worker_id": "w2"}, {"cmd": "git push origin dev"}, {"tenant": "globex"},
                                   {"user": "someone-else"}])
def test_a_kept_wish_does_not_fire_on_an_approval_for_another_worker_key_tenant_or_user(tmp_path, other):
    w = RWorld(tmp_path, shadow_days=14)
    try:
        satisfy_gate(w.store, w.judge, RISK, T, U)
        w.approve()
        w.approve()
        wish = w.approve("ask_remember", consume=False)                    # refused in shadow: the wish is kept
        assert w.stored(wish).activation == "pending"
        w.clock.now = T0 + timedelta(days=15)                              # now it WOULD activate if it fired
        w.foreign_approval(**other)
        assert w.stored(wish).activation == "pending"
        assert w.grants() == [] and w.audits("assistant.grant.activated") == []
        assert len(w.thread(wish)) == 1                                    # nothing new said in its thread
        w.approve()                                                        # the right worker and key does fire it
        assert w.grants()[0].state.value == "active" and w.stored(wish).activation == "done"
    finally:
        w.runtime.stop()
        w.listener.close()
        w.judge_listener._stop.set()
