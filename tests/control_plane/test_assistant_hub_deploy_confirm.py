"""Owner flow for a standing deploy grant: REST asks, ONLY a verified Slack Confirm tap creates it, REST revokes.

Real API server, real Slack listener (Web API plus Socket Mode), real judge runtime, real store."""
import hashlib
import json
import threading
import urllib.error
import urllib.request
from datetime import timedelta

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.assistant_hub.actiongrants import GrantState
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction
from herdr_engineering.control_plane.assistant_hub.policy import current_judge_version, decide
from herdr_engineering.control_plane.assistant_hub.slack import SlackRuntime
from herdr_engineering.control_plane.assistant_hub.slack_asks import button_value
from herdr_engineering.control_plane.assistant_hub.workers import Worker
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.judge_support import (
    SAFE,
    WireListener,
    judge_cfg,
    make_runtime,
    satisfy_gate,
    success_body,
    write_key,
)
from tests.control_plane.test_assistant_hub_slack_taps import APP, BOT, NOW, OWNER, T0, T, World

OPERATOR, VIEWER = "dep-op-key", "dep-viewer-key"
PID = "key:" + hashlib.sha256(OPERATOR.encode()).hexdigest()[:12]
CMD = "deploy/control-plane/deploy.sh"
REPO = "berlinguyinca/herdr-engineering"
SHA = "0123456789abcdef0123456789abcdef01234567"
DEPLOY = {"projects": {"herdr-engineering": {"repo": REPO, "scopes": {
    "control-plane": {"command": CMD, "mode": "auto"}, "other": {"command": "deploy/other/run.sh"}}}}}
PATH = "/api/v1/assistant-deploys/herdr-engineering/control-plane/auto"


def call(base, path, key=None, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(base + path, method=method, data=data,
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


class DeployWorld(World):
    def __init__(self, tmp_path, acknowledged_sink, *, shadow_days=1):
        super().__init__(tmp_path)
        self.svc = AssistantService(store=self.store, now=self.clock, config=assistant_config({"personal_assistant": {
            "enabled": True, "tenancy": {"principals": {PID: T}}, "hub": {"tenant": T},
            "grants": {"shadow_started_at": T0.isoformat(), "shadow_days": shadow_days},
            "deploy": DEPLOY}}))
        self.service = self.svc
        self.judge_listener = WireListener(success_body(SAFE))
        self.jcfg = judge_cfg(f"http://127.0.0.1:{self.judge_listener.port}", write_key(tmp_path),
                              backend_confirmed=True)
        self.judge = make_runtime(self.store, self.jcfg, self.clock)
        self.runtime = SlackRuntime(self.store, self.svc, lambda: self.cfg, self.clock, judge=self.judge,
                                    min_backoff=0.05, max_backoff=0.2, ping_interval=0.3)
        self.worker = Worker(worker_id="w1", tenant_id=T, user_id=PID, name="n", harness="claude", device_id="d",
                             secret_digest="x", status="active", created_at=T0, expires_at=T0 + timedelta(days=365))
        self.store.workers["w1"] = self.worker
        self.server = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=self.svc,
                                         api_keys={OPERATOR: "operator", VIEWER: "viewer"},
                                         assistant_slack=self.runtime, assistant_judge=self.judge)
        threading.Thread(target=self.server.serve, daemon=True).start()
        self.server.wait_until_ready()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def gate(self):
        satisfy_gate(self.store, self.judge, "outward", T, PID)

    def ask_auto(self, worker="w1", path=PATH):
        return call(self.base, path, OPERATOR, "POST", {"worker_id": worker, "confirm": True})

    def deploy_asks(self):
        return [a for a in self.store.list_slack_asks(T, PID) if a.kind == "deploy"]

    def posts(self):
        return self.listener.calls_to("chat.postMessage")

    def grants(self):
        return self.store.list_action_grants(T, PID)

    def request_ask(self):
        status, body = self.ask_auto()
        assert status == 202, body
        (ask,) = [a for a in self.deploy_asks() if a.status == "open"]
        return ask

    def deploy_decision(self, sha=SHA):
        return decide(self.store, self.svc.config["grants"], self.worker,
                      RawAction(tool="Bash", command=f"{CMD} --ref {sha}", repo=REPO), "s1", "t1", self.clock.now,
                      judge=self.judge, deploy_cfg=self.svc.config["deploy"])

    def refusals(self, reason):
        return [e for e in self.audits("assistant.slack.refused") if f"reason={reason}" in e["detail"]]


@pytest.fixture
def dw(tmp_path, acknowledged_sink):
    w = DeployWorld(tmp_path, acknowledged_sink)
    try:
        yield w
    finally:
        w.server.shutdown()
        w.runtime.stop()
        w.listener.close()
        w.judge_listener.close()


# ------------------------------------------------------------------------------------- the ask
def test_the_route_posts_one_confirmation_and_creates_no_grant(dw):
    dw.gate()
    status, body = dw.ask_auto()
    assert status == 202 and body["status"] == "awaiting_confirmation" and body["ask_id"]
    (post,) = dw.posts()
    text = post["blocks"][0]["text"]["text"]
    for needle in ("herdr-engineering", "control-plane", REPO, CMD, "w1", "auto"):
        assert needle in text, needle
    (actions,) = [b for b in post["blocks"] if b["type"] == "actions"]
    assert [(b["text"]["text"], b["action_id"]) for b in actions["elements"]] == [
        ("Confirm", "deploy_confirm"), ("Cancel", "deploy_cancel")]
    (ask,) = dw.deploy_asks()
    assert {b["value"] for b in actions["elements"]} == {button_value(ask)}
    assert (ask.kind, ask.status, ask.worker_id, ask.project, ask.scope) == (
        "deploy", "open", "w1", "herdr-engineering", "control-plane")
    assert ask.ts and ask.channel == post["channel"] and ask.expires_at == NOW + timedelta(minutes=15)
    assert dw.grants() == []                                                  # a REST call alone creates nothing
    (posted,) = dw.audits("assistant.slack.ask_posted")
    assert ask.ask_id in posted["detail"] and BOT not in str(posted) and CMD not in str(posted)


def test_the_message_is_inert_and_carries_no_token(dw):
    dw.ask_auto()
    raw = b"".join(r for m, r in dw.listener.bodies if m == "chat.postMessage").decode()
    assert BOT not in raw and APP not in raw and "<!channel>" not in raw


def test_a_second_request_supersedes_the_first_ask(dw):
    dw.gate()
    first = dw.request_ask()
    dw.ask_auto()
    second = [a for a in dw.deploy_asks() if a.status == "open"]
    assert [a.ask_id for a in second] != [first.ask_id] and len(second) == 1
    dw.tap(first, "deploy_confirm")
    assert dw.grants() == [] and dw.refusals("not_open")
    dw.tap(second[0], "deploy_confirm")
    assert len(dw.grants()) == 1


@pytest.mark.parametrize("path, body, code", [
    ("/api/v1/assistant-deploys/nope/control-plane/auto", {"worker_id": "w1", "confirm": True}, 404),
    ("/api/v1/assistant-deploys/herdr-engineering/nope/auto", {"worker_id": "w1", "confirm": True}, 404),
    ("/api/v1/assistant-deploys/herdr-engineering/control-plane/auto", {"worker_id": "ghost", "confirm": True}, 404),
    (PATH, {"worker_id": "w1"}, 400),
    (PATH, {"worker_id": "w1", "confirm": False}, 400),
    (PATH, {"worker_id": "w1", "confirm": "true"}, 400),
    (PATH, {"confirm": True}, 400),
    (PATH, {"worker_id": 5, "confirm": True}, 400),
    (PATH, {"worker_id": "w1", "confirm": True, "extra": 1}, 400),
    (PATH, {"worker_id": "w1/../x", "confirm": True}, 400),
    ("/api/v1/assistant-deploys/herdr-engineering/control-plane/other", {"worker_id": "w1", "confirm": True}, 404),
    ("/api/v1/assistant-deploys/herdr-engineering/auto", {"worker_id": "w1", "confirm": True}, 404),
])
def test_bad_requests_post_and_create_nothing(dw, path, body, code):
    assert call(dw.base, path, OPERATOR, "POST", body)[0] == code
    assert dw.posts() == [] and dw.deploy_asks() == [] and dw.grants() == []


def test_another_users_worker_is_not_found(dw):
    dw.store.workers["w9"] = Worker(worker_id="w9", tenant_id=T, user_id="someone-else", name="n", harness="c",
                                    device_id="d", secret_digest="x", status="active", created_at=T0,
                                    expires_at=T0 + timedelta(days=365))
    dw.store.workers["w8"] = Worker(worker_id="w8", tenant_id="other", user_id=PID, name="n", harness="c",
                                    device_id="d", secret_digest="x", status="active", created_at=T0,
                                    expires_at=T0 + timedelta(days=365))
    dw.store.workers["w7"] = Worker(worker_id="w7", tenant_id=T, user_id=PID, name="n", harness="c", device_id="d",
                                    secret_digest="x", status="revoked", created_at=T0,
                                    expires_at=T0 + timedelta(days=365))
    for wid in ("w9", "w8", "w7"):
        assert dw.ask_auto(wid)[0] == 404, wid
    assert dw.posts() == []


def test_auth_is_operator_only_and_origin_checked(dw):
    body = {"worker_id": "w1", "confirm": True}
    assert call(dw.base, PATH, None, "POST", body)[0] in (401, 403)
    assert call(dw.base, PATH, VIEWER, "POST", body)[0] == 403
    assert call(dw.base, "/api/v1/assistant-deploys", None)[0] in (401, 403)
    assert call(dw.base, "/api/v1/assistant-deploys", VIEWER)[0] == 403
    assert call(dw.base, PATH + "?worker_id=w1", VIEWER, "DELETE")[0] == 403
    req = urllib.request.Request(dw.base + PATH, method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-API-Key": OPERATOR,
                                          "Origin": "https://evil.example"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 403
    assert dw.posts() == [] and dw.grants() == []


def test_slack_off_or_unreachable_creates_nothing(dw):
    dw.cfg = {**dw.cfg, "enabled": False}
    status, body = dw.ask_auto()
    assert status == 503 and dw.posts() == [] and dw.deploy_asks() == []
    good = dw.cfg
    dw.cfg = {**good, "enabled": True, "base_url": "http://127.0.0.1:1/api"}
    status, body = dw.ask_auto()
    assert status == 502 and dw.grants() == []
    assert all(a.status != "open" for a in dw.deploy_asks())                  # nothing left that a tap could use


def test_no_slack_runtime_at_all_is_unavailable(tmp_path, acknowledged_sink):
    svc = AssistantService(config=assistant_config({"personal_assistant": {
        "enabled": True, "tenancy": {"principals": {PID: T}}, "deploy": DEPLOY}}))
    svc.store.workers["w1"] = Worker(worker_id="w1", tenant_id=T, user_id=PID, name="n", harness="c", device_id="d",
                                     secret_digest="x", status="active", created_at=T0,
                                     expires_at=T0 + timedelta(days=365))
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=svc,
                             api_keys={OPERATOR: "operator"})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        status, _ = call(f"http://127.0.0.1:{svr.port}", PATH, OPERATOR, "POST", {"worker_id": "w1", "confirm": True})
        assert status == 503 and svc.store.list_action_grants(T, PID) == []
    finally:
        svr.shutdown()


# ------------------------------------------------------------------------------------- the tap
def test_only_the_owners_confirm_tap_creates_the_standing_grant(dw):
    dw.gate()
    ask = dw.request_ask()
    dw.tap(ask, "deploy_confirm")
    (grant,) = dw.grants()
    assert grant.state is GrantState.ACTIVE and grant.source == "owner_standing"
    assert grant.confirmed_by == f"slack:{OWNER}" and grant.worker_id == "w1" and grant.verb == "deploy.prod"
    assert grant.resource_scope == REPO and grant.activated_at == NOW
    assert grant.judge_version == current_judge_version(dw.judge)
    stored = dw.store.get_slack_ask(ask.ask_id, tenant_id=T, user_id=PID)
    assert stored.status == "decided"
    (update,) = dw.listener.calls_to("chat.update")
    assert not any(b.get("type") == "actions" for b in update["blocks"]) and "onfirmed" in update["text"]
    (act,) = dw.audits("assistant.grant.activated")
    assert act["actor"] == f"slack:{OWNER}" and SHA not in str(act)
    (dec,) = dw.audits("assistant.slack.decision")
    assert dec["actor"] == f"slack:{OWNER}" and "deploy_confirm" in dec["detail"] and ask.ask_id in dec["detail"]


def test_a_second_confirm_tap_creates_nothing_more(dw):
    dw.gate()
    ask = dw.request_ask()
    dw.tap(ask, "deploy_confirm")
    dw.tap(ask, "deploy_confirm")
    assert len(dw.grants()) == 1 and len(dw.audits("assistant.grant.activated")) == 1
    assert dw.refusals("not_open") and len(dw.listener.calls_to("chat.update")) == 1


def test_cancel_creates_nothing_and_ends_the_ask(dw):
    dw.gate()
    ask = dw.request_ask()
    dw.tap(ask, "deploy_cancel")
    assert dw.grants() == [] and dw.store.get_slack_ask(ask.ask_id, tenant_id=T, user_id=PID).status == "decided"
    assert "ancel" in dw.listener.calls_to("chat.update")[0]["text"]
    dw.tap(ask, "deploy_confirm")
    assert dw.grants() == [] and dw.refusals("not_open")


def test_an_expired_ask_creates_nothing(dw):
    dw.gate()
    ask = dw.request_ask()
    dw.clock.now = ask.expires_at
    dw.tap(ask, "deploy_confirm")
    assert dw.grants() == [] and dw.refusals("expired")


@pytest.mark.parametrize("how", ["non_owner", "wrong_nonce", "wrong_channel", "wrong_ts", "edited", "bad_value",
                                 "approve_action", "remember_action", "unknown_action"])
def test_a_tap_that_does_not_verify_creates_nothing(dw, how):
    dw.gate()
    ask = dw.request_ask()
    kw = {"non_owner": {"user": "U0INTRUDER"}, "wrong_nonce": {"value": f"{ask.ask_id}:nope"},
          "wrong_channel": {"channel": "C0OTHER"}, "wrong_ts": {"ts": "9.9"}, "edited": {"edited": {"ts": "1"}},
          "bad_value": {"value": "garbage"}, "approve_action": {}, "remember_action": {}, "unknown_action": {}}[how]
    action = {"approve_action": "ask_approve", "remember_action": "ask_remember",
              "unknown_action": "deploy_all"}.get(how, "deploy_confirm")
    dw.tap(ask, action, **kw)
    assert dw.grants() == [], how
    assert dw.store.get_slack_ask(ask.ask_id, tenant_id=T, user_id=PID).status == "open"
    assert dw.audits("assistant.slack.refused")
    dw.tap(ask, "deploy_confirm")                                              # the right tap still works afterwards
    assert len(dw.grants()) == 1


def test_a_deploy_button_on_an_action_ask_decides_nothing(dw):
    dw.gate()
    request_id = dw.request("frobnicate --x")
    action_ask = dw.ask(request_id)
    dw.tap(action_ask, "deploy_confirm")
    assert dw.grants() == []
    assert dw.store.get_action_request(request_id, tenant_id=T, user_id=PID).status == "pending"
    assert dw.refusals("no_ask")


def test_a_thread_reply_on_a_deploy_ask_creates_nothing(dw):
    dw.gate()
    ask = dw.request_ask()
    for text in ("yes", "confirm", "deploy_confirm"):
        dw.runtime.handle_event({"type": "event_callback", "event": {
            "type": "message", "user": OWNER, "channel": ask.channel, "ts": "50.1", "thread_ts": ask.ts, "text": text}})
    assert dw.grants() == [] and dw.store.get_slack_ask(ask.ask_id, tenant_id=T, user_id=PID).status == "open"
    assert dw.refusals("not_question")


def test_the_tap_works_over_the_real_socket(dw):
    dw.gate()
    dw.runtime.start()
    assert dw.listener.wait_for(lambda: dw.listener.ws_alive == 1)
    ask = dw.request_ask()
    dw.listener.push_interactive({"type": "block_actions", "user": {"id": OWNER}, "channel": {"id": ask.channel},
                                  "message": {"ts": ask.ts},
                                  "actions": [{"action_id": "deploy_confirm", "value": button_value(ask)}]})
    assert dw.listener.wait_for(lambda: len(dw.grants()) == 1)


# ------------------------------------------------------------------------------------- refusals at the tap
def test_a_judge_gate_refusal_is_reported_in_the_thread_and_creates_nothing(dw):
    ask = dw.request_ask()                                  # the judge gate is not satisfied
    dw.tap(ask, "deploy_confirm")
    assert dw.grants() == []
    (said,) = [c for c in dw.posts() if c.get("thread_ts") == ask.ts]
    assert "labeled" in said["text"] and "insufficient" not in said["text"]
    (refused,) = dw.audits("assistant.grant.activate_refused")
    assert "insufficient_labels" in refused["detail"] and refused["actor"] == f"slack:{OWNER}"
    assert dw.audits("assistant.grant.activated") == []
    assert dw.store.get_slack_ask(ask.ask_id, tenant_id=T, user_id=PID).status == "decided"


def test_unconfirmed_backend_is_reported_in_plain_words(dw):
    dw.gate()
    dw.jcfg["backend_confirmed"] = False
    ask = dw.request_ask()
    dw.tap(ask, "deploy_confirm")
    (said,) = [c for c in dw.posts() if c.get("thread_ts") == ask.ts]
    assert "real model" in said["text"] and dw.grants() == []


def test_during_shadow_the_confirm_tap_is_refused_with_still_in_shadow(tmp_path, acknowledged_sink):
    w = DeployWorld(tmp_path, acknowledged_sink, shadow_days=14)
    try:
        w.gate()
        ask = w.request_ask()
        w.tap(ask, "deploy_confirm")
        assert w.grants() == []
        (refused,) = w.audits("assistant.grant.activate_refused")
        assert "still_in_shadow" in refused["detail"]
        (said,) = [c for c in w.posts() if c.get("thread_ts") == ask.ts]
        assert "shadow" in said["text"].lower()
    finally:
        w.server.shutdown()
        w.runtime.stop()
        w.listener.close()
        w.judge_listener.close()


def test_a_scope_removed_from_the_config_before_the_tap_creates_nothing(dw):
    dw.gate()
    ask = dw.request_ask()
    dw.svc.config["deploy"] = {"projects": {}}
    dw.tap(ask, "deploy_confirm")
    assert dw.grants() == []
    (said,) = [c for c in dw.posts() if c.get("thread_ts") == ask.ts]
    assert "configured" in said["text"]


def test_a_changed_repo_or_command_in_the_config_creates_nothing(dw):
    dw.gate()
    ask = dw.request_ask()
    moved = json.loads(json.dumps(DEPLOY))
    moved["projects"]["herdr-engineering"]["repo"] = "acme/moved"
    dw.svc.config["deploy"] = moved
    dw.tap(ask, "deploy_confirm")
    assert dw.grants() == []


def test_a_worker_revoked_before_the_tap_creates_nothing(dw):
    dw.gate()
    ask = dw.request_ask()
    dw.store.workers["w1"].status = "revoked"
    dw.tap(ask, "deploy_confirm")
    assert dw.grants() == []


# ------------------------------------------------------------------------------------- use, list, revoke
def test_after_confirmation_a_deploy_is_pending_on_preconditions_not_authorized(dw):
    dw.gate()
    assert dw.deploy_decision().source == "owner"
    dw.tap(dw.request_ask(), "deploy_confirm")
    d = dw.deploy_decision("fedcba9876543210fedcba9876543210fedcba98")
    assert (d.outcome, d.source) == ("pending", "deploy_preconditions")


def test_delete_revokes_and_the_next_request_is_pending_owner(dw):
    dw.gate()
    dw.tap(dw.request_ask(), "deploy_confirm")
    assert dw.deploy_decision().source == "deploy_preconditions"
    status, body = call(dw.base, PATH + "?worker_id=w1", OPERATOR, "DELETE")
    assert status == 200 and body["revoked"] == 1
    (grant,) = dw.grants()
    assert grant.state is GrantState.REVOKED
    assert dw.deploy_decision().source == "owner"
    assert dw.audits("assistant.grant.revoked")
    assert call(dw.base, PATH + "?worker_id=w1", OPERATOR, "DELETE")[0] == 404          # nothing left to revoke


@pytest.mark.parametrize("query", ["", "?worker_id=", "?worker_id=ghost", "?worker_id=w1&x=1", "?worker_id=a/b"])
def test_delete_needs_a_known_worker(dw, query):
    status, _ = call(dw.base, PATH + query, OPERATOR, "DELETE")
    assert status in (400, 404)


def test_delete_ends_a_pending_ask_too(dw):
    dw.gate()
    ask = dw.request_ask()
    call(dw.base, PATH + "?worker_id=w1", OPERATOR, "DELETE")
    dw.tap(ask, "deploy_confirm")
    assert dw.grants() == []


def test_delete_only_touches_that_worker_and_scope(dw):
    dw.gate()
    dw.store.workers["w2"] = Worker(worker_id="w2", tenant_id=T, user_id=PID, name="n", harness="c", device_id="d",
                                    secret_digest="x", status="active", created_at=T0,
                                    expires_at=T0 + timedelta(days=365))
    dw.tap(dw.request_ask(), "deploy_confirm")
    status, _ = dw.ask_auto("w2")
    (ask2,) = [a for a in dw.deploy_asks() if a.status == "open"]
    dw.tap(ask2, "deploy_confirm")
    assert len(dw.grants()) == 2
    call(dw.base, PATH + "?worker_id=w2", OPERATOR, "DELETE")
    states = {g.worker_id: g.state for g in dw.grants()}
    assert states == {"w1": GrantState.ACTIVE, "w2": GrantState.REVOKED}


def test_get_lists_projects_scopes_and_whether_a_standing_grant_is_live(dw):
    dw.gate()
    status, before = call(dw.base, "/api/v1/assistant-deploys", OPERATOR)
    assert status == 200
    (proj,) = before["projects"]
    assert (proj["project"], proj["repo"]) == ("herdr-engineering", REPO)
    scopes = {s["scope"]: s for s in proj["scopes"]}
    assert scopes["control-plane"]["mode"] == "auto" and scopes["control-plane"]["command"] == CMD
    assert scopes["control-plane"]["require"] == ["on_main", "ci_green", "descends_from_deployed"]
    assert scopes["other"]["mode"] == "ask"
    assert all(s["standing_grant"] is False and s["workers"] == [] for s in proj["scopes"])
    dw.tap(dw.request_ask(), "deploy_confirm")
    _, after = call(dw.base, "/api/v1/assistant-deploys", OPERATOR)
    scopes = {s["scope"]: s for s in after["projects"][0]["scopes"]}
    assert scopes["control-plane"]["standing_grant"] is True and scopes["control-plane"]["workers"] == ["w1"]
    assert scopes["other"]["standing_grant"] is False
    blob = json.dumps(after)
    assert BOT not in blob and APP not in blob and "token" not in blob and "key_file" not in blob
    call(dw.base, PATH + "?worker_id=w1", OPERATOR, "DELETE")
    _, gone = call(dw.base, "/api/v1/assistant-deploys", OPERATOR)
    assert {s["scope"]: s["standing_grant"] for s in gone["projects"][0]["scopes"]}["control-plane"] is False


def test_the_list_is_empty_without_a_deploy_config(tmp_path, acknowledged_sink):
    svc = AssistantService(config=assistant_config({"personal_assistant": {
        "enabled": True, "tenancy": {"principals": {PID: T}}}}))
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=svc,
                             api_keys={OPERATOR: "operator"})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        assert call(f"http://127.0.0.1:{svr.port}", "/api/v1/assistant-deploys", OPERATOR) == (200, {"projects": []})
    finally:
        svr.shutdown()


def test_unrelated_methods_and_paths_are_not_found(dw):
    assert call(dw.base, PATH, OPERATOR, "GET")[0] == 404
    assert call(dw.base, "/api/v1/assistant-deploys", OPERATOR, "POST")[0] == 404
    assert call(dw.base, "/api/v1/assistant-deploys/herdr-engineering", OPERATOR)[0] == 404
    assert dw.grants() == [] and dw.posts() == []
