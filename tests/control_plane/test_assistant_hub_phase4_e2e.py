"""Phase 4 end to end, over real sockets only.

One hub (real MCP handler, real policy, real judge runtime over a raw-socket listener), one real ``ControlPlaneServer``
for the owner's REST ask, one real Slack runtime speaking Socket Mode to a real local WebSocket listener, and real local
HTTP listeners for GitHub (with the real installation-token exchange), Woodpecker and the health endpoint.  The owner's
taps and thread replies are pushed over the Socket Mode link.  No mocks, no fakes, no guessed sleeps: every wait is on
observable state.  Nothing here touches the real ~/.claude, ~/.codex, ~/.pi or ~/.config/herdr.

NOT covered: a real Slack workspace, real GitHub / Woodpecker, or a real herdr-engineering deploy (see
docs/operations/assistant-hub.md, "Not verified")."""
import hashlib
import json
import threading
import urllib.request
from datetime import timedelta

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import (
    AssistantService,
    AssistantStore,
    _slack_config,
    assistant_config,
)
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction, derive
from herdr_engineering.control_plane.assistant_hub.mcp import Hub
from herdr_engineering.control_plane.assistant_hub.preconditions import HealthClient
from herdr_engineering.control_plane.assistant_hub.slack import SlackRuntime
from herdr_engineering.control_plane.assistant_hub.slack_asks import button_value
from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry
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
from tests.control_plane.slack_support import SlackListener
from tests.control_plane.test_assistant_hub_deploy_grants import CMD, REPO, SHA
from tests.control_plane.test_assistant_hub_mcp import body_of, call
from tests.control_plane.test_assistant_hub_preconditions import OLD, Stack
from tests.control_plane.test_assistant_hub_slack_taps import APP, BOT, NOW, OWNER, T0, Clock

TENANT = "acme"
OPERATOR = "p4-e2e-operator-key"
PID = "key:" + hashlib.sha256(OPERATOR.encode()).hexdigest()[:12]
ACT = ("ask", "report", "read", "act")
PUSH = "git push origin main"
RISK = derive(RawAction(tool="Bash", command=PUSH, repo="acme/repo")).key.risk_class.value
TINY = {"required_approvals": {"local_reversible": 3, "remote_reversible": 3, "outward": 3},
        "min_sessions": 3, "min_tasks": 2}
DEPLOY_PATH = "/api/v1/assistant-deploys/herdr-engineering/control-plane/auto"


class Phase4World:
    def __init__(self, tmp_path, acknowledged_sink) -> None:
        self.slack = SlackListener(BOT, APP)
        bot, app = tmp_path / "bot", tmp_path / "app"
        bot.write_text(BOT + "\n")
        app.write_text(APP + "\n")
        for f in (bot, app):
            f.chmod(0o600)
        self.slack_cfg = _slack_config({"enabled": True, "base_url": self.slack.base_url, "bot_token_file": str(bot),
                                        "app_token_file": str(app), "owner_user_id": OWNER})
        self.stack = Stack(tmp_path)                      # GitHub + Woodpecker + health listeners, real clients
        self.clock = Clock()
        self.clock.now = NOW
        self.store = AssistantStore()
        scope = {"command": CMD, "mode": "auto", "health_url": self.stack.health_url}
        self.config = assistant_config({"personal_assistant": {
            "enabled": True, "tenancy": {"principals": {PID: TENANT}}, "hub": {"tenant": TENANT},
            "grants": {"shadow_started_at": T0.isoformat(), "shadow_days": 1, **TINY},
            "deploy": {"projects": {"herdr-engineering": {"repo": REPO, "scopes": {"control-plane": scope}}}}}})
        self.service = AssistantService(store=self.store, now=self.clock, config=self.config)
        self.judge_listener = WireListener(success_body(SAFE))
        self.judge = make_runtime(self.store, judge_cfg(f"http://127.0.0.1:{self.judge_listener.port}",
                                                        write_key(tmp_path), backend_confirmed=True), self.clock)
        clients = self.stack.clients()
        self.runtime = SlackRuntime(self.store, self.service, lambda: self.slack_cfg, self.clock, judge=self.judge,
                                    min_backoff=0.05, max_backoff=0.2, ping_interval=0.3, reoffer_backoff=0.1,
                                    summary_interval=0.1, check_interval=0.05, health=HealthClient(timeout=5),
                                    preconditions=clients)
        registry = WorkerRegistry(self.store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30),
                                  now=self.clock)
        self.hub = Hub(registry=registry, service=self.service, store=self.store, judge=self.judge,
                       slack=self.runtime, preconditions=clients, event_rate_limit=10_000)
        _, code = registry.create_enrollment(tenant_id=TENANT, user_id=PID, name="box", harness="pi", scopes=ACT)
        worker, cred = registry.redeem(code, device_id="d1")
        self.worker, self.auth = worker, (worker, f"Worker {cred}", "d1")
        self.server = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=self.service,
                                         api_keys={OPERATOR: "operator"}, assistant_slack=self.runtime,
                                         assistant_judge=self.judge)
        threading.Thread(target=self.server.serve, daemon=True).start()
        self.server.wait_until_ready()
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.runtime.start()
        assert self.slack.wait_for(lambda: self.slack.ws_alive == 1)

    # -- worker side (MCP) ---------------------------------------------------------------------------
    def act(self, command: str, repo: str = "acme/repo", task: str = "t1", session: str = "s1") -> dict:
        out = call(self.hub, self.auth, "action_request", {"tool": "Bash", "command": command, "repo": repo,
                                                           "session_id": session, "task_id": task})
        assert out["isError"] is False, out
        return body_of(out)

    def deploy(self) -> dict:
        return self.act(f"{CMD} --ref {SHA}", repo=REPO)

    def ask_question(self, text: str) -> dict:
        out = call(self.hub, self.auth, "assistant_ask", {"question": text, "session_id": "s1"})
        assert out["isError"] is False, out
        return body_of(out)

    def poll_question(self, ask_id: str) -> dict:
        return body_of(call(self.hub, self.auth, "assistant_ask", {"ask_id": ask_id, "session_id": "s1"}))

    def report(self, text: str, task: str, final: bool = True) -> None:
        out = call(self.hub, self.auth, "report", {"kind": "outcome", "text": text, "session_id": "s1",
                                                   "task_id": task, "final": final})
        assert out["isError"] is False, out

    # -- owner side (Slack over the real socket) --------------------------------------------------------
    def asks(self, **match):
        return [a for a in self.store.list_slack_asks(TENANT, PID)
                if all(getattr(a, k) == v for k, v in match.items())]

    def posted_ask(self, **match):
        """The ask that has really been posted to Slack (its ts is stored only after the post succeeded)."""
        assert self.slack.wait_for(lambda: any(a.ts for a in self.asks(**match)), timeout=10), match
        return [a for a in self.asks(**match) if a.ts][-1]

    def tap(self, ask, action: str) -> str:
        return self.slack.push_interactive({"type": "block_actions", "user": {"id": OWNER},
                                            "channel": {"id": ask.channel}, "message": {"ts": ask.ts},
                                            "actions": [{"action_id": action, "value": button_value(ask)}]})

    def request_status(self, request_id: str) -> str:
        return self.store.get_action_request(request_id, tenant_id=TENANT, user_id=PID).status

    def rest_ask_auto(self):
        req = urllib.request.Request(self.base + DEPLOY_PATH, method="POST",
                                     data=json.dumps({"worker_id": self.worker.worker_id, "confirm": True}).encode(),
                                     headers={"Content-Type": "application/json", "X-API-Key": OPERATOR})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())

    def grant_standing_deploy(self) -> None:
        """The owner's way in: REST ask -> Slack message -> Confirm tap over Socket Mode.  Only the tap creates it."""
        satisfy_gate(self.store, self.judge, "outward", TENANT, PID)
        status, body = self.rest_ask_auto()
        assert status == 202 and body["status"] == "awaiting_confirmation"
        assert self.store.list_action_grants(TENANT, PID) == []              # asking granted nothing
        ask = self.posted_ask(kind="deploy", status="open")
        env = self.tap(ask, "deploy_confirm")
        assert self.slack.wait_for(lambda: len(self.store.list_action_grants(TENANT, PID)) == 1, timeout=10)
        assert self.slack.wait_for(lambda: env in self.slack.acks, timeout=10)

    def approve_cycle(self, n: int, action: str = "ask_approve"):
        """One owner decision on a fresh request; the session/task spread meets the diversity floor.  The one-shot
        approval is spent right away by asking again."""
        session, task = f"s{n}", f"t{1 + n % 2}"
        first = self.act(PUSH, task=task, session=session)
        assert first["outcome"] == "pending"
        ask = self.posted_ask(request_id=first["request_id"])
        self.tap(ask, action)
        assert self.slack.wait_for(lambda: self.request_status(first["request_id"]) == "approved", timeout=10)
        assert self.slack.wait_for(lambda: self.stored_ask(ask).status == "decided", timeout=10)
        spent = self.act(PUSH, task=task, session=session)
        assert (spent["outcome"], spent["source"]) == ("authorized", "owner")
        return ask

    def stored_ask(self, ask):
        return self.store.get_slack_ask(ask.ask_id, tenant_id=TENANT, user_id=PID)

    def thread_said(self, ask) -> list[str]:
        return [str(c.get("text", "")) for c in self.posts() if c.get("thread_ts") == ask.ts]

    def posts(self) -> list[dict]:
        return self.slack.calls_to("chat.postMessage")

    def outcome_posts(self) -> list[str]:
        return [str(p.get("text", "")) + json.dumps(p.get("blocks", [])) for p in self.posts()
                if "Deploy" in str(p.get("text", ""))]

    def close(self) -> None:
        self.server.shutdown()
        self.runtime.stop()
        self.judge.recorder.stop()
        self.slack.close()
        self.judge_listener.close()
        self.stack.close()


@pytest.fixture
def world(tmp_path, acknowledged_sink):
    w = Phase4World(tmp_path, acknowledged_sink)
    try:
        yield w
    finally:
        w.close()


# ------------------------------------------------------------------------------------------------ approvals
def test_an_unknown_command_asks_in_slack_and_one_tap_authorizes_exactly_once(world):
    first = world.act("frobnicate --all")
    assert first["outcome"] == "pending"
    ask = world.posted_ask(request_id=first["request_id"])
    assert world.request_status(first["request_id"]) == "pending"           # nothing is decided by the post
    env = world.tap(ask, "ask_approve")
    assert world.slack.wait_for(lambda: world.request_status(first["request_id"]) == "approved", timeout=10)
    assert world.slack.wait_for(lambda: env in world.slack.acks, timeout=10)
    row = world.store.get_action_request(first["request_id"], tenant_id=TENANT, user_id=PID)
    assert row.decided_by == f"slack:{OWNER}"
    assert [e.provenance for e in world.store.list_action_evidence(TENANT, PID)] == ["owner_slack"]
    again = world.act("frobnicate --all")
    assert (again["outcome"], again["source"]) == ("authorized", "owner")
    spent = world.act("frobnicate --all")                                   # one-shot: the approval was used up
    assert spent["outcome"] == "pending" and spent["request_id"] != first["request_id"]


def test_a_question_is_answered_by_a_threaded_reply_from_the_owner(world):
    out = world.ask_question("Which branch should I deploy from?")
    assert out["status"] == "pending"
    ask = world.posted_ask(ask_id=out["ask_id"])
    assert world.poll_question(out["ask_id"]) == {"status": "pending"}
    env = world.slack.push_event({"type": "message", "user": "U0STRANGER1", "channel": ask.channel,
                                  "thread_ts": ask.ts, "ts": "1800000000.000100", "text": "use dev"})
    assert world.slack.wait_for(lambda: env in world.slack.acks, timeout=10)
    assert world.poll_question(out["ask_id"]) == {"status": "pending"}      # a stranger's reply answers nothing
    world.slack.push_event({"type": "message", "user": OWNER, "channel": ask.channel, "thread_ts": ask.ts,
                            "ts": "1800000000.000200", "text": "use main"})
    assert world.slack.wait_for(lambda: world.poll_question(out["ask_id"]).get("status") == "answered", timeout=10)
    assert world.poll_question(out["ask_id"])["answer"] == "use main"


# ------------------------------------------------------------------------------------------------ deploys
def test_green_deploy_with_a_standing_grant_is_authorized_verified_and_summarized(world):
    world.grant_standing_deploy()
    decision = world.deploy()
    assert (decision["outcome"], decision["source"]) == ("authorized", "grant")
    (check,) = world.store.list_deploy_checks(TENANT, PID)
    assert check.status == "awaiting" and check.sha == SHA
    world.stack.r.deployed = SHA                                            # the new version starts serving
    assert world.slack.wait_for(lambda: world.store.get_deploy_check(
        check.check_id, tenant_id=TENANT, user_id=PID).target_seen_at.year > 1970)      # seen once: only provisional
    world.clock.now = NOW + timedelta(minutes=11)                           # ... healthy after a whole window
    assert world.slack.wait_for(lambda: len(world.outcome_posts()) == 1, timeout=10)
    assert "Deploy healthy" in world.outcome_posts()[0] and SHA[:12] in world.outcome_posts()[0]
    assert world.store.get_deploy_check(check.check_id, tenant_id=TENANT, user_id=PID).status == "healthy"
    world.report("deployed", "t1")
    assert world.slack.wait_for(lambda: any(str(p.get("text", "")).startswith("Task summary") for p in world.posts()),
                                timeout=10)
    (summary,) = [p for p in world.posts() if str(p.get("text", "")).startswith("Task summary")]
    body = "\n".join(b["text"]["text"] for b in summary["blocks"] if b["type"] == "section")
    assert f"herdr-engineering/control-plane {SHA[:12]}: healthy" in body


def test_a_deploy_that_never_shows_the_new_version_is_not_reported_healthy(world):
    world.grant_standing_deploy()
    assert world.deploy()["outcome"] == "authorized"
    (check,) = world.store.list_deploy_checks(TENANT, PID)
    world.stack.r.deployed = OLD                                            # the old version keeps serving
    world.stack.r.compare = "behind"                                        # ... and it is not newer than the target
    world.clock.now = NOW + timedelta(minutes=11)                           # the deploy window has passed
    assert world.slack.wait_for(lambda: len(world.outcome_posts()) == 1, timeout=10)
    text = world.outcome_posts()[0]
    assert "Deploy not live" in text and "cause unknown" in text and "healthy" not in text.lower()
    assert world.store.get_deploy_check(check.check_id, tenant_id=TENANT, user_id=PID).status == "rolled_back"


def test_red_ci_keeps_the_deploy_pending_and_asks_the_owner(world):
    world.grant_standing_deploy()
    world.stack.r.status_state = "failure"
    decision = world.deploy()
    assert (decision["outcome"], decision["source"]) == ("pending", "deploy_preconditions")
    assert "failed ci_green" in decision["reason"]
    assert world.store.list_deploy_checks(TENANT, PID) == []
    ask = world.posted_ask(request_id=decision["request_id"])
    (post,) = [p for p in world.posts() if any(
        e.get("value") == button_value(ask) for b in p["blocks"] if b["type"] == "actions" for e in b["elements"])]
    assert post["channel"] == ask.channel
    assert world.request_status(ask.request_id) == "pending"                # the owner has not been bypassed


def test_mode_flipped_to_ask_refuses_the_standing_grant(world):
    world.grant_standing_deploy()
    scope = world.service.config["deploy"]["projects"]["herdr-engineering"]["scopes"]["control-plane"]
    scope["mode"] = "ask"                                                   # the owner's kill switch, read live
    decision = world.deploy()
    assert (decision["outcome"], decision["source"]) == ("pending", "deploy_mode")
    assert world.store.list_deploy_checks(TENANT, PID) == []
    assert len(world.store.list_action_grants(TENANT, PID)) == 1            # the grant is kept, just ignored
    scope["mode"] = "auto"
    assert world.deploy()["outcome"] == "authorized"                        # and works again once auto is back


# ------------------------------------------------------------------------------------------------ remember
def test_approve_and_remember_with_the_gate_satisfied_lets_the_next_request_through_without_slack(world):
    satisfy_gate(world.store, world.judge, RISK, TENANT, PID)
    world.approve_cycle(1)
    world.approve_cycle(2)
    ask = world.approve_cycle(3, "ask_remember")
    assert world.slack.wait_for(lambda: any(g.state.value == "active" for g in
                                            world.store.list_action_grants(TENANT, PID)), timeout=10)
    (grant,) = world.store.list_action_grants(TENANT, PID)
    assert grant.confirmed_by == f"slack:{OWNER}" and world.stored_ask(ask).remember is True
    assert world.slack.wait_for(lambda: any("remember" in t.lower() for t in world.thread_said(ask)), timeout=10)
    asks_before = len(world.asks(kind="action"))
    later = world.act(PUSH, task="t9", session="s9")
    assert (later["outcome"], later["source"], later["grant_id"]) == ("authorized", "grant", grant.grant_id)
    assert len(world.asks(kind="action")) == asks_before                                   # nobody was asked in Slack


def test_approve_and_remember_with_the_gate_unsatisfied_tells_the_owner_and_keeps_the_intent(world):
    world.approve_cycle(1)
    world.approve_cycle(2)
    ask = world.approve_cycle(3, "ask_remember")                             # the judge gate has no labels
    assert world.slack.wait_for(lambda: any("labeled" in t.lower() for t in world.thread_said(ask)), timeout=10)
    (grant,) = world.store.list_action_grants(TENANT, PID)
    assert grant.state.value == "proposed"
    assert world.stored_ask(ask).remember is True and world.stored_ask(ask).activation == "pending"
    assert not any("insufficient_labels" in t for t in world.thread_said(ask))
    satisfy_gate(world.store, world.judge, RISK, TENANT, PID)
    world.approve_cycle(4)                                                    # the next qualifying approval retries
    assert world.slack.wait_for(lambda: world.store.list_action_grants(TENANT, PID)[0].state.value == "active",
                                timeout=10)
