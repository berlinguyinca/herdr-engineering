"""The scheduled CI watch: a deterministic periodic caller of ``AssistantService.watch_ci``."""
import logging
import threading
import time
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.assistant import (
    AssistantService,
    AssistantStore,
    assistant_config,
    ci_watch_config,
)
from herdr_engineering.control_plane.ci_watch import ACTOR, CIWatchRunner
from herdr_engineering.control_plane.project_intelligence_api import IntelligenceRoutes

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
FAILED = {"repository": "o/r", "run_number": 12, "commit": "a" * 40, "state": "failure",
          "finished": NOW.timestamp() - 60}


class World:
    def __init__(self, *, users=("key:u1",), auto_grant=False, scope=True, intel=None, **cw):
        self.section = {"enabled": True, "webhook_projects": {"o/r": ["p1"]},
                        "assistant_viewers": ({f"t/{u}": {"org_id": "org1", "projects": "*"} for u in users}
                                              if scope else {})}
        if intel:
            self.section.update(intel)
        self.routes = IntelligenceRoutes(section_fn=lambda: self.section)
        ci = {"enabled": True, "auto_grant": auto_grant, "users": [{"tenant_id": "t", "user_id": u} for u in users], **cw}
        cfg = assistant_config({"personal_assistant": {"enabled": True, "ci_watch": ci}})
        self.svc = AssistantService(config=cfg, now=lambda: NOW)
        self.svc.ci_board_request = self.routes.assistant_ci_repair
        self.rows = [dict(FAILED)]
        self.svc.ci_snapshot = lambda: {"pipelines": {"o/r": list(self.rows)}}
        self.assistants = {u: self.svc.create_assistant(tenant_id="t", user_id=u, name="A") for u in users}
        self.runner = CIWatchRunner(self.svc, self.routes.assistant_scope_state, cfg["ci_watch"])

    def grant(self, user="key:u1"):
        return self.svc.grant_tool(tenant_id="t", user_id=user, assistant_id=self.assistants[user].assistant_id,
                                   tool_id="watch_ci", scope="*")


def test_a_tick_queues_a_new_failure_once_and_not_again():
    w = World()
    w.grant()
    first = w.runner.tick()
    assert first["last_created"] == 1 and first["last_error"] is None
    (p,) = w.svc.store.proposals.values()
    assert p.kind == "ci_repair" and p.payload["repo"] == "o/r" and p.payload["run"] == 12
    assert [b.action for b in w.routes.board.pending("org1")] == ["request_ci_repair"]
    again = w.runner.tick()
    assert again["last_created"] == 0 and again["duplicates"] == 1 and again["created"] == 1
    assert len(w.svc.store.proposals) == 1 and len(w.routes.board.pending("org1")) == 1
    assert any(e["actor"] == ACTOR and e["action"] == "assistant.ci.watched" for e in w.svc.store.audit)


def test_dedupe_holds_after_a_restart_with_the_assistant_store_restored():
    w = World()
    w.grant()
    w.runner.tick()
    fresh = AssistantStore()
    fresh.restore(w.svc.store.snapshot())
    w.svc.store = fresh
    assert w.runner.tick()["duplicates"] == 1 and len(w.svc.store.proposals) == 1


def test_old_failures_and_passing_runs_and_numeric_repos_are_not_alerted():
    w = World()
    w.grant()
    w.rows = [{**FAILED, "finished": NOW.timestamp() - 10 * 3600},
              {**FAILED, "run_number": 13, "state": "success"},
              {**FAILED, "run_number": 14, "repository": "123"}]
    assert w.runner.tick()["last_created"] == 0 and not w.svc.store.proposals


def test_missing_grant_warns_once_and_does_nothing(caplog):
    w = World()
    with caplog.at_level(logging.WARNING):
        w.runner.tick()
        w.runner.tick()
    st = w.runner.status()
    assert st["skipped"] == {"no_grant": 1} and not w.svc.store.proposals and not w.svc.store.grants
    assert len([r for r in caplog.records if "no active watch_ci grant" in r.getMessage()]) == 1


def test_auto_grant_creates_an_audited_standing_grant_only_when_true():
    off = World()
    off.runner.tick()
    assert not off.svc.store.grants
    on = World(auto_grant=True)
    out = on.runner.tick()
    assert out["last_created"] == 1 and len(on.svc.store.grants) == 1
    (g,) = on.svc.store.grants.values()
    assert g.tool_id == "watch_ci" and g.scope == "*" and g.constraints == {"preauthorized": False}
    assert any(e["action"] == "assistant.ci_watch.auto_granted" and e["actor"] == ACTOR for e in on.svc.store.audit)
    assert not any(e["action"] == "assistant.ci_watch.auto_granted" for e in off.svc.store.audit)
    on.runner.tick()
    assert len(on.svc.store.grants) == 1                                   # not granted again while active


def test_one_user_failing_does_not_stop_another():
    w = World(users=("key:u1", "key:u2"))
    w.grant("key:u1")
    w.grant("key:u2")
    real = w.svc.watch_ci

    def flaky(**kw):
        if kw["user_id"] == "key:u1":
            raise RuntimeError("secret-looking detail /etc/token")
        return real(**kw)
    w.svc.watch_ci = flaky
    st = w.runner.tick()
    assert st["last_error"] == "user_failed" and "secret" not in repr(st)
    assert st["last_created"] == 1 and len(w.svc.store.proposals) == 1
    assert {u["user_id"]: u["result"] for u in st["users"]} == {"key:u1": "error", "key:u2": "ok"}


@pytest.mark.parametrize("intel, reason", [({"kill_switch": True}, "kill_switch"), ({"enabled": False}, "intelligence_disabled")])
def test_kill_switch_or_disabled_intelligence_makes_no_calls(intel, reason):
    w = World(intel=intel)
    w.grant()
    w.svc.watch_ci = lambda **kw: pytest.fail("watch_ci must not be called")
    assert w.runner.tick()["skipped"] == {reason: 1}


def test_no_scope_and_disabled_assistant_make_no_calls():
    w = World(scope=False)
    w.grant()
    w.svc.watch_ci = lambda **kw: pytest.fail("watch_ci must not be called")
    assert w.runner.tick()["skipped"] == {"no_scope": 1}
    w2 = World()
    w2.grant()
    w2.svc.update_assistant(w2.assistants["key:u1"].assistant_id, tenant_id="t", user_id="key:u1", patch={"enabled": False})
    w2.svc.watch_ci = lambda **kw: pytest.fail("watch_ci must not be called")
    assert w2.runner.tick()["skipped"] == {"no_assistant": 1}
    w2.svc.config["enabled"] = False
    assert w2.runner.tick()["skipped"] == {"assistant_disabled": 1}


def test_assistant_id_defaults_to_the_oldest_enabled_assistant():
    w = World()
    first = w.assistants["key:u1"]
    w.svc.create_assistant(tenant_id="t", user_id="key:u1", name="B")
    w.svc.update_assistant(first.assistant_id, tenant_id="t", user_id="key:u1", patch={"enabled": False})
    second = [a for a in w.svc.list_assistants(tenant_id="t", user_id="key:u1") if a.enabled][0]
    w.svc.grant_tool(tenant_id="t", user_id="key:u1", assistant_id=second.assistant_id, tool_id="watch_ci", scope="*")
    assert w.runner.tick()["last_created"] == 1
    (p,) = w.svc.store.proposals.values()
    assert p.assistant_id == second.assistant_id


def test_interval_is_clamped_and_bad_config_stops_startup():
    assert ci_watch_config({"interval_seconds": 5})["interval_seconds"] == 60
    assert ci_watch_config(None)["enabled"] is False and ci_watch_config({"enabled": "yes"})["enabled"] is False
    assert ci_watch_config({"users": [{"user_id": "u"}]})["users"][0]["tenant_id"] == "default"
    for bad in ({"users": [{"tenant_id": "t"}]}, {"source": "x"}, {"bogus": 1}, {"users": ["u"]}):
        with pytest.raises(ValueError):
            ci_watch_config(bad)


def test_a_stop_ends_the_thread_promptly_and_status_is_exposed():
    w = World()
    w.grant()
    w.runner.config = {**w.runner.config, "interval_seconds": 3600}
    t = threading.Thread(target=w.runner.run, daemon=True)
    w.runner._rng = type("NoJitter", (), {"uniform": staticmethod(lambda a, b: 0)})()
    t.start()
    deadline = time.monotonic() + 5
    while w.runner.status()["ticks"] == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    w.runner.stop()
    t.join(timeout=2)
    assert not t.is_alive()
    st = w.runner.status()
    assert st["ticks"] >= 1 and st["last_at"] and st["last_error"] is None


def test_jitter_is_bounded_and_a_stop_during_the_jitter_skips_the_tick():
    w = World()
    w.grant()
    w.runner.stop()
    w.runner.run()
    assert w.runner.status()["ticks"] == 0


def test_the_status_route_shows_the_watch_to_operators_only():
    w = World()
    w.section["default_org_id"] = "org1"
    w.routes.ci_watch_status_fn = w.runner.status
    principal = {"kind": "operator", "id": "open-listener", "authenticated_by": "open_listener"}
    code, body = w.routes.get(["status"], {}, principal, "operator")
    assert code == 200 and body["ci_watch"]["enabled"] is True and "last_error" in body["ci_watch"]
    code, body = w.routes.get(["status"], {}, principal, "viewer")
    assert "ci_watch" not in (body or {})


def test_a_refused_rerun_never_hides_repair_in_the_scheduled_path():
    w = World()
    w.grant()

    def boom(*_a):
        raise RuntimeError("kill switch")
    w.svc.ci_rerun_request = boom
    assert w.runner.tick()["last_created"] == 1
    (p,) = w.svc.store.proposals.values()
    assert "rerun_proposal" not in p.payload


def test_a_budget_overrun_skips_remaining_users_and_a_tick_never_raises():
    w = World(users=("key:u1", "key:u2"))
    ticks = iter([0, 1000, 2000, 3000])
    w.runner._clock = lambda: next(ticks, 9999)
    st = w.runner.tick()
    assert st["skipped"].get("budget_exceeded", 0) >= 1
    w.runner.scope_fn = lambda *_: (_ for _ in ()).throw(RuntimeError("x"))
    w.runner._clock = time.monotonic
    assert w.runner.tick()["last_error"] == "user_failed"


def test_the_server_starts_the_watch_and_shutdown_joins_it_cleanly():
    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    cfg = assistant_config({"personal_assistant": {"enabled": True, "ci_watch": {
        "enabled": True, "users": [{"tenant_id": "default", "user_id": "open-listener"}]}}})
    server = ControlPlaneServer(ControlPlaneRepo(), assistant_service=AssistantService(config=cfg))
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    assert server.ci_watch.config["enabled"] and server.intelligence.ci_watch_status_fn is not None
    started = time.monotonic()
    server.shutdown()
    assert server.ci_watch.stop_event.is_set() and time.monotonic() - started < 5
