"""Unit tests for the Herdr Personal Assistant domain + policy service.

Covers the blocking security/privacy controls from the spec bundle
(docs/specs/rakazo-personal-assistant-<ts>/, 05-security-and-privacy.md and
09-tests-and-acceptance.md acceptance A1-A4):
- feature flag off-by-default and inertness (A1)
- deny-by-default tools + approval gates (A2)
- user/tenant isolation of threads, memory, runs (A3, A4)
- grant expiry / revocation blocking future actions
- approval expiry fail-closed
- idempotency of tool actions
- memory delete / export propagation
"""
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from herdr_engineering.control_plane.assistant import (
    ApprovalRequired,
    AssistantError,
    AssistantNotFound,
    AssistantService,
    AssistantStore,
    AssistantUnavailable,
    Connector,
    HttpModelRunnerAdapter,
    MCPConnectorAdapter,
    OpenApiConnectorAdapter,
    PostgresStoreBackend,
    RateLimited,
    assistant_config,
    redact_secrets,
)


def make_service(**kwargs):
    cfg = assistant_config({"personal_assistant": {"enabled": True}})
    cfg.update(kwargs)
    return AssistantService(config=cfg)


# ---------------------------------------------------------------------------
# A1 — feature flag off by default; inert when disabled
# ---------------------------------------------------------------------------

def test_flag_off_by_default():
    cfg = assistant_config({})
    assert cfg["enabled"] is False
    svc = AssistantService(config=cfg)
    with pytest.raises(AssistantUnavailable):
        svc.list_assistants(tenant_id="t1", user_id="u1")
    with pytest.raises(AssistantUnavailable):
        svc.create_assistant(tenant_id="t1", user_id="u1", name="x")


def test_disabled_service_creates_no_data():
    svc = AssistantService(config=assistant_config({}))
    with pytest.raises(AssistantUnavailable):
        svc.create_assistant(tenant_id="t1", user_id="u1", name="x")
    assert svc.store.assistants == {}
    assert svc.store.runs == {}
    assert svc.store.audit == []


# ---------------------------------------------------------------------------
# assistant CRUD + isolation
# ---------------------------------------------------------------------------

def test_create_and_get_assistant():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    got = svc.get_assistant(a.assistant_id, tenant_id="t1", user_id="u1")
    assert got.name == "Helper"
    assert got.status == "ready"
    assert got.enabled is True


def test_cross_user_assistant_hidden():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    with pytest.raises((KeyError, AssistantNotFound)):
        svc.get_assistant(a.assistant_id, tenant_id="t1", user_id="u2")


def test_cross_tenant_assistant_hidden():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    with pytest.raises((KeyError, AssistantNotFound)):
        svc.get_assistant(a.assistant_id, tenant_id="t2", user_id="u1")


def test_list_only_own_assistants():
    svc = make_service()
    svc.create_assistant(tenant_id="t1", user_id="u1", name="A1")
    svc.create_assistant(tenant_id="t1", user_id="u2", name="A2")
    svc.create_assistant(tenant_id="t2", user_id="u1", name="A3")
    own = svc.list_assistants(tenant_id="t1", user_id="u1")
    assert [a.name for a in own] == ["A1"]


def test_update_and_archive():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    svc.update_assistant(a.assistant_id, tenant_id="t1", user_id="u1",
                         patch={"name": "Renamed", "enabled": False})
    got = svc.get_assistant(a.assistant_id, tenant_id="t1", user_id="u1")
    assert got.name == "Renamed"
    assert got.enabled is False
    assert got.status == "disabled"
    svc.archive_assistant(a.assistant_id, tenant_id="t1", user_id="u1")
    assert svc.get_assistant(a.assistant_id, tenant_id="t1", user_id="u1").enabled is False


# ---------------------------------------------------------------------------
# threads + messages; disabled assistant cannot chat (A1)
# ---------------------------------------------------------------------------

def test_send_message_requires_enabled_assistant():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper", enabled=False)
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    with pytest.raises(Exception):
        svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                         thread_id=t.thread_id, content="hello")


def test_chat_produces_run_and_reply():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hello there")
    assert run.state == "completed"
    messages = svc.store.list_messages(t.thread_id, tenant_id="t1", user_id="u1")
    roles = [m.role for m in messages]
    assert roles == ["user", "assistant"]
    assert "hello there" in messages[-1].content


def test_thread_isolated_by_user():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    with pytest.raises((KeyError, AssistantNotFound)):
        svc.get_thread(t.thread_id, tenant_id="t1", user_id="u2")


def test_cancel_run():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hi")
    with pytest.raises(Exception):
        # already completed -> not cancellable
        svc.cancel_run(run.run_id, tenant_id="t1", user_id="u1")


# ---------------------------------------------------------------------------
# grants / deny-by-default / approvals (A2)
# ---------------------------------------------------------------------------

def test_unknown_tool_rejected():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hi")
    with pytest.raises(Exception):
        svc.request_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                                tool_id="nope", scope="*", run_id=run.run_id,
                                idempotency_key="k1", request_payload={}, actor="u1")


def test_deny_by_default_when_no_grant():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hi")
    action = svc.request_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                                     tool_id="browse", scope="*", run_id=run.run_id,
                                     idempotency_key="k1", request_payload={}, actor="u1")
    assert action.status == "denied"
    assert any(e["action"] == "assistant.tool.denied" for e in svc.store.audit)


def test_normal_tool_with_grant_authorized():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    svc.grant_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                   tool_id="browse", scope="*")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hi")
    action = svc.request_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                                     tool_id="browse", scope="*", run_id=run.run_id,
                                     idempotency_key="k1", request_payload={}, actor="u1")
    assert action.status in ("authorized", "approved")


def test_sensitive_tool_requires_approval():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    svc.grant_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                   tool_id="send_message", scope="*")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hi")
    with pytest.raises(ApprovalRequired) as ei:
        svc.request_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                                tool_id="send_message", scope="*", run_id=run.run_id,
                                idempotency_key="k1", request_payload={}, actor="u1")
    approvals = svc.list_approvals(tenant_id="t1", user_id="u1", state="pending")
    assert len(approvals) == 1
    assert approvals[0].approval_id == ei.value.approval_id


def test_sensitive_tool_preauthorized_skips_approval():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    svc.grant_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                   tool_id="send_message", scope="*", preauthorized=True)
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hi")
    action = svc.request_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                                     tool_id="send_message", scope="*", run_id=run.run_id,
                                     idempotency_key="k1", request_payload={}, actor="u1")
    assert action.status == "approved"


def test_approval_flow_approve():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    svc.grant_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                   tool_id="send_message", scope="*")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hi")
    with pytest.raises(ApprovalRequired) as ei:
        svc.request_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                                tool_id="send_message", scope="*", run_id=run.run_id,
                                idempotency_key="k1", request_payload={}, actor="u1")
    approval = svc.decide_approval(ei.value.approval_id, tenant_id="t1", user_id="u1",
                                   decision="approved", actor="u1")
    assert approval.decision == "approved"
    action = svc.store.get_action(approval.action_id)
    assert action.status == "approved"


def test_approval_deny_blocks():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    svc.grant_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                   tool_id="send_message", scope="*")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hi")
    with pytest.raises(ApprovalRequired) as ei:
        svc.request_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                                tool_id="send_message", scope="*", run_id=run.run_id,
                                idempotency_key="k1", request_payload={}, actor="u1")
    approval = svc.decide_approval(ei.value.approval_id, tenant_id="t1", user_id="u1",
                                   decision="denied", actor="u1")
    assert approval.decision == "denied"
    assert svc.store.get_action(approval.action_id).status == "denied"


def test_approval_expired_fails_closed():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    svc.grant_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                   tool_id="send_message", scope="*")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hi")
    with pytest.raises(ApprovalRequired) as ei:
        svc.request_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                                tool_id="send_message", scope="*", run_id=run.run_id,
                                idempotency_key="k1", request_payload={}, actor="u1")
    with patch.object(svc, "now", (lambda real: lambda: real() + timedelta(hours=2))(svc.now)):
        with pytest.raises(Exception):
            svc.decide_approval(ei.value.approval_id, tenant_id="t1", user_id="u1",
                                decision="approved", actor="u1")


def test_revoked_grant_blocks_future_action():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    g = svc.grant_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                       tool_id="browse", scope="*")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hi")
    svc.revoke_grant(g.grant_id, tenant_id="t1", user_id="u1")
    action = svc.request_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                                     tool_id="browse", scope="*", run_id=run.run_id,
                                     idempotency_key="k2", request_payload={}, actor="u1")
    assert action.status == "denied"


def test_expired_grant_blocks():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    svc.grant_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                   tool_id="browse", scope="*", ttl_seconds=1)
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hi")
    with patch.object(svc, "now", (lambda real: lambda: real() + timedelta(days=1))(svc.now)):
        action = svc.request_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                                         tool_id="browse", scope="*", run_id=run.run_id,
                                         idempotency_key="k3", request_payload={}, actor="u1")
        assert action.status == "denied"


def test_idempotent_action_deduplicated():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    svc.grant_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                   tool_id="browse", scope="*")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hi")
    first = svc.request_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                                    tool_id="browse", scope="*", run_id=run.run_id,
                                    idempotency_key="same", request_payload={"url": "https://x"}, actor="u1")
    second = svc.request_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                                     tool_id="browse", scope="*", run_id=run.run_id,
                                     idempotency_key="same", request_payload={"url": "https://x"}, actor="u1")
    assert second.action_id == first.action_id
    assert len([a2 for a2 in svc.store.actions.values() if a2.run_id == run.run_id]) == 1


# ---------------------------------------------------------------------------
# memory (A4)
# ---------------------------------------------------------------------------

def test_memory_stored_and_searched():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    svc.store_memory(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                     content="User prefers concise answers in German", source_ref="chat")
    hits = svc.search_memory(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                             query="German answers")
    assert len(hits) == 1
    assert hits[0].revision == 1


def test_memory_isolated_across_users():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    svc.store_memory(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                     content="secret fact about u1")
    # user B sees nothing, even on the same assistant id shape
    hits = svc.search_memory(tenant_id="t1", user_id="u2", assistant_id=a.assistant_id,
                             query="secret")
    assert hits == []


def test_memory_forget_and_export():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    m = svc.store_memory(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                         content="fact to forget", source_ref="chat")
    exp = svc.export_memory(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    assert exp["count"] == 1
    svc.forget_memory(m.memory_id, tenant_id="t1", user_id="u1")
    assert svc.export_memory(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)["count"] == 0


def test_export_isolated_by_assistant():
    svc = make_service()
    a1 = svc.create_assistant(tenant_id="t1", user_id="u1", name="A1")
    a2 = svc.create_assistant(tenant_id="t1", user_id="u1", name="A2")
    svc.store_memory(tenant_id="t1", user_id="u1", assistant_id=a1.assistant_id,
                     content="a1 secret", source_ref="chat")
    assert svc.export_memory(tenant_id="t1", user_id="u1", assistant_id=a2.assistant_id)["count"] == 0


def test_diagnostics():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    d = svc.diagnostics(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    assert d["assistant_id"] == a.assistant_id
    assert d["runner"] == "LocalRunnerAdapter"
    assert d["enabled"] is True


# ---------------------------------------------------------------------------
# P3 — durable routines (timezone, quiet hours, idempotent trigger, dedupe)
# ---------------------------------------------------------------------------

def test_create_interval_routine_computes_next_at():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    r = svc.create_routine(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           name="Briefing", trigger_type="interval",
                           trigger_spec={"every": "minutes", "amount": 15},
                           timezone="UTC")
    assert r.enabled is True
    assert r.next_at is not None
    assert r.last_result == ""


def test_daily_routine_respects_timezone_and_day():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    # 2026-10-27 is a Tuesday. Daily 09:00 Mon..Fri.
    base = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    r = svc.create_routine(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           name="Daily", trigger_type="daily",
                           trigger_spec={"time": "09:00", "days": ["mon", "tue", "wed", "thu", "fri"]},
                           timezone="UTC")
    with patch.object(svc, "now", (lambda real: lambda: real())(svc.now)):
        r.next_at = svc._compute_next(base, r)
    # next 09:00 on the next weekday after Tue 12:00 = Wed 09:00
    assert r.next_at.date().isoformat() == "2026-10-28"
    assert r.next_at.hour == 9


def test_quiet_hours_defer_trigger():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    r = svc.create_routine(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           name="Quiet", trigger_type="interval",
                           trigger_spec={"every": "hours", "amount": 1},
                           timezone="UTC", quiet_hours={"start": "22:00", "end": "07:00"})
    # 23:00 is inside quiet hours -> next candidate is 08:00 next day
    base = datetime(2026, 10, 27, 23, 0, tzinfo=UTC)
    nxt = svc._compute_next(base, r)
    # 07:00 is the first interval tick outside the 22:00-07:00 quiet window
    assert nxt.hour == 7
    assert nxt.date().isoformat() == "2026-10-28"


def test_in_quiet_hours_wrap_midnight():
    assert AssistantService._in_quiet_hours(datetime(2026, 1, 1, 23, 0), {"start": "22:00", "end": "07:00"}) is True
    assert AssistantService._in_quiet_hours(datetime(2026, 1, 1, 3, 0), {"start": "22:00", "end": "07:00"}) is True
    assert AssistantService._in_quiet_hours(datetime(2026, 1, 1, 12, 0), {"start": "22:00", "end": "07:00"}) is False


def test_trigger_is_idempotent_per_bucket():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    base = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
    svc.now = lambda: base  # fixed clock so next_at is deterministic
    r = svc.create_routine(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           name="R", trigger_type="interval",
                           trigger_spec={"every": "minutes", "amount": 10})
    fire_at = base + timedelta(minutes=10)  # == next_at
    fired = svc.trigger(r.routine_id, tenant_id="t1", user_id="u1", now=fire_at)
    assert fired.last_result == "triggered"
    assert fired.last_run_at == fire_at
    assert svc.store.routine_fired(r.routine_id, "2026-10-27T10:10") is True
    # a duplicate delivery of the SAME scheduled bucket must not re-fire
    stale = svc.store.get_routine(r.routine_id, tenant_id="t1", user_id="u1")
    stale.next_at = fire_at  # simulate a duplicate delivery reading the stale schedule
    dup = svc.trigger(r.routine_id, tenant_id="t1", user_id="u1", now=fire_at)
    assert dup.last_result == "skipped: already fired for this schedule bucket"


def test_due_routines_only_enabled_and_due():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    svc.create_routine(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id, name="Due",
                       trigger_spec={"every": "minutes", "amount": 1})
    svc.create_routine(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id, name="Off",
                       trigger_spec={"every": "minutes", "amount": 1}, enabled=False)
    now = svc.now() + timedelta(minutes=5)
    due = svc.due_routines(tenant_id="t1", user_id="u1", now=now)
    assert len(due) == 1
    assert due[0].name == "Due"


def test_routines_isolated_by_user():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    r = svc.create_routine(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id, name="R")
    with pytest.raises((KeyError, AssistantNotFound)):
        svc.get_routine(r.routine_id, tenant_id="t1", user_id="u2")


# ---------------------------------------------------------------------------
# P3 — memory correction (revision + provenance) and approvals
# ---------------------------------------------------------------------------

def test_memory_correct_bumps_revision():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    m = svc.store_memory(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                         content="User likes coffee", source_ref="chat")
    rev_before = m.revision
    corrected = svc.correct_memory(m.memory_id, tenant_id="t1", user_id="u1",
                                   new_content="User prefers tea")
    assert corrected.revision == rev_before + 1
    assert corrected.content == "User prefers tea"
    assert corrected.source_ref == "correction"
    hits = svc.search_memory(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                             query="tea")
    assert len(hits) == 1
    assert hits[0].revision == corrected.revision


# ---------------------------------------------------------------------------
# P4 — connectors, computer-tool sandbox policy, delegation
# ---------------------------------------------------------------------------

def test_register_connector_requires_allowlist():
    svc = make_service()
    with pytest.raises(AssistantError):
        svc.register_connector(tenant_id="t1", user_id="u1", name="X", base_url="https://x.example.com", auth={})
    c = svc.register_connector(tenant_id="t1", user_id="u1", name="X", base_url="https://x.example.com",
                               auth={"_allowed_origins": ["x.example.com"], "token": "secret"})
    assert c.enabled is True
    d = c.as_dict()
    assert "token" not in d and d["auth_present"] is True


def test_connector_isolated_by_user():
    svc = make_service()
    c = svc.register_connector(tenant_id="t1", user_id="u1", name="X", base_url="https://x.example.com",
                               auth={"_allowed_origins": ["x.example.com"]})
    with pytest.raises((KeyError, AssistantNotFound)):
        svc.get_connector(c.connector_id, tenant_id="t1", user_id="u2")


def test_connector_request_deny_by_default_ssrf():
    svc = make_service()
    c = svc.register_connector(tenant_id="t1", user_id="u1", name="X", base_url="https://x.example.com",
                               auth={"_allowed_origins": ["x.example.com"]})
    # SSRF attempt: an absolute-URL path must be rejected because its host is
    # not in the connector's operator allowlist
    r = svc.connector_request(tenant_id="t1", user_id="u1", connector_id=c.connector_id,
                              path="https://evil.example.net/steal")
    assert r["ok"] is False
    assert "allowlisted" in r["error"]
    assert svc.store.get_connector(c.connector_id, tenant_id="t1", user_id="u1").as_dict().get("token") is None


def test_register_connector_rejects_http():
    svc = make_service()
    with pytest.raises(AssistantError):
        svc.register_connector(tenant_id="t1", user_id="u1", name="X",
                               base_url="http://x.example.com",
                               auth={"_allowed_origins": ["x.example.com"]})
    # an http allowlist origin is also rejected
    with pytest.raises(AssistantError):
        svc.register_connector(tenant_id="t1", user_id="u1", name="X",
                               base_url="https://x.example.com",
                               auth={"_allowed_origins": ["http://x.example.com"]})


def test_computer_policy_deny_by_default():
    svc = make_service()
    assert svc.evaluate_computer_policy(tool="browse", target="https://evil.example.com")["allowed"] is False
    assert svc.evaluate_computer_policy(tool="run_shell", target="rm -rf /")["allowed"] is False
    assert svc.evaluate_computer_policy(tool="unknown", target="x")["allowed"] is False


def test_browse_tool_policy_gated():
    svc = make_service()
    res = svc.browse_tool(tenant_id="t1", user_id="u1", url="https://example.com")
    assert res["denied"] is True and "policy" in res["reason"]


def test_run_shell_tool_deny_by_default():
    svc = make_service()
    res = svc.run_shell_tool(tenant_id="t1", user_id="u1", command="ls /tmp")
    assert res["denied"] is True


def test_delegate_creates_queued_request():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    d = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                     task="Add a health endpoint", target_system="pi")
    assert d.status == "queued"  # never fabricates completion
    got = svc.get_delegation(d.delegation_id, tenant_id="t1", user_id="u1")
    assert got.artifact_refs == []


def test_delegation_artifact_and_cancel():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    d = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id, task="t")
    d = svc.register_delegation_artifact(d.delegation_id, tenant_id="t1", user_id="u1",
                                         artifact_ref="artifact://mission/MSN-1")
    assert d.status == "running" and "artifact://mission/MSN-1" in d.artifact_refs
    d = svc.cancel_delegation(d.delegation_id, tenant_id="t1", user_id="u1")
    assert d.status == "cancelled"


def test_execute_tool_delegate_dispatch():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    res = svc.execute_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           tool_id="delegate", payload={"task": "T"}, actor="u1")
    assert res["ok"] is True and res["delegation"]["status"] == "queued"


# ---------------------------------------------------------------------------
# P6 — hardening: secrets redaction, validation, isolation, rate limiting
# ---------------------------------------------------------------------------

class _FailingAdapter:
    """A connector adapter whose transport error echoes its URL (to prove the
    error output is redacted)."""
    def request(self, connector, method, path, body, timeout):
        return {"ok": False, "error": f"connector call failed: {connector.base_url}{path}"}


def test_audit_log_never_contains_secrets():
    svc = make_service()
    svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    c = svc.register_connector(tenant_id="t1", user_id="u1", name="X",
                               base_url="https://x.example.com",
                               auth={"_allowed_origins": ["x.example.com"], "token": "supersecret123"})
    # a request that fails must not leak the token into the audit detail
    svc.connector_request(tenant_id="t1", user_id="u1", connector_id=c.connector_id,
                          path="https://evil.example.net/x")
    joined = "\n".join(f"{e['action']} {e['detail']}" for e in svc.store.audit)
    assert "supersecret123" not in joined
    assert "Authorization" not in joined
    assert redact_secrets("Authorization: Bearer abcdef123456") == "Authorization: Bearer ***REDACTED***"


def test_connector_error_output_redacted():
    svc = make_service()
    c = svc.register_connector(tenant_id="t1", user_id="u1", name="X",
                               base_url="https://x.example.com",
                               auth={"_allowed_origins": ["x.example.com"], "token": "tok_abc123"})
    # force a transport failure that echoes the URL including the token
    svc._adapter_for = lambda kind: _FailingAdapter()
    r = svc.connector_request(tenant_id="t1", user_id="u1", connector_id=c.connector_id,
                              path="?token=tok_abc123")
    assert "tok_abc123" not in r.get("error", "")


def test_register_connector_requires_https_allowlist():
    svc = make_service()
    with pytest.raises(AssistantError):
        svc.register_connector(tenant_id="t1", user_id="u1", name="X",
                               base_url="http://x.example.com",
                               auth={"_allowed_origins": ["x.example.com"]})


def test_invalid_timezone_rejected():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    with pytest.raises(AssistantError):
        svc.create_routine(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           name="R", timezone="Not/AZone")


def test_p4_entities_isolated_across_users():
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    c = svc.register_connector(tenant_id="t1", user_id="u1", name="X", base_url="https://x.example.com",
                               auth={"_allowed_origins": ["x.example.com"]})
    r = svc.create_routine(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id, name="R")
    d = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id, task="T")
    for access in (lambda: svc.get_connector(c.connector_id, tenant_id="t1", user_id="u2"),
                   lambda: svc.get_routine(r.routine_id, tenant_id="t1", user_id="u2"),
                   lambda: svc.get_delegation(d.delegation_id, tenant_id="t1", user_id="u2")):
        with pytest.raises((KeyError, AssistantNotFound)):
            access()


def test_rate_limit_enforced():
    svc = make_service()
    svc.config["max_requests_per_minute"] = 3
    for _ in range(3):
        svc.check_rate_limit(key="k1")  # ok
    with pytest.raises(RateLimited):
        svc.check_rate_limit(key="k1")
    # a different key is unaffected
    svc.check_rate_limit(key="k2")


def test_rate_limit_resets_next_window():
    svc = make_service()
    svc.config["max_requests_per_minute"] = 1
    svc.check_rate_limit(key="k")
    with pytest.raises(RateLimited):
        svc.check_rate_limit(key="k")
    # advance into a new minute window -> fresh count
    svc.now = lambda: datetime(2026, 10, 28, 0, 1, tzinfo=UTC)
    svc.check_rate_limit(key="k")


# ---------------------------------------------------------------------------
# Gap-closing: durable store (R1), real model runner (R2), MCP/OpenAPI
# adapters, real browse/shell executors
# ---------------------------------------------------------------------------

def test_durable_store_survives_restart(tmp_path):
    path = tmp_path / "pa.json"
    store = AssistantStore(persist_path=str(path))
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}),
                           store=store)
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                     thread_id=t.thread_id, content="hi")
    # simulate a process restart: a fresh store loaded from the same file
    store2 = AssistantStore(persist_path=str(path))
    store2.load_persisted()
    svc2 = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}),
                            store=store2)
    got = svc2.get_assistant(a.assistant_id, tenant_id="t1", user_id="u1")
    assert got.name == "Helper"
    assert len(store2.runs) == 1  # the run survived


def test_durable_snapshot_roundtrip(tmp_path):
    path = tmp_path / "pa.json"
    store = AssistantStore(persist_path=str(path))
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}),
                           store=store)
    svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    svc.register_connector(tenant_id="t1", user_id="u1", name="X",
                           base_url="https://x.example.com",
                           auth={"_allowed_origins": ["x.example.com"], "token": "tok_1"})
    snap = store.snapshot()
    assert path.exists()  # persist happened on mutation
    store3 = AssistantStore()
    store3.restore(snap)
    assert len(store3.assistants) == 1
    assert len(store3.connectors) == 1
    # credentials are not lost in storage but auth is opaque here
    assert store3.connectors[next(iter(store3.connectors))].auth["token"] == "tok_1"


def test_http_model_runner_uses_openai_endpoint():
    store = AssistantStore()
    seen = {}
    def fake_urlopen(endpoint, payload, timeout):
        seen["endpoint"] = endpoint; seen["payload"] = payload
        return {"choices": [{"message": {"content": "model answer"}}]}
    runner = HttpModelRunnerAdapter(store, endpoint="http://llm:8088/v1/chat/completions",
                                    model="deepseek_v4.1", urlopen=fake_urlopen)
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}),
                           store=store)
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hello")
    msgs = store.list_messages(t.thread_id, tenant_id="t1", user_id="u1")
    out = runner.execute(run, msgs)
    assert out == "model answer"
    assert seen["endpoint"] == "http://llm:8088/v1/chat/completions"
    assert seen["payload"]["model"] == "deepseek_v4.1"
    assert seen["payload"]["messages"][0]["content"] == "hello"  # the user turn


def test_http_model_runner_degrades_on_failure():
    store = AssistantStore()
    runner = HttpModelRunnerAdapter(store, endpoint="https://model.example.com/v1",
                                    model="deepseek_v4.1",
                                    urlopen=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = runner.execute(_FakeRun(), [])
    assert "unavailable" in out


class _FakeRun:
    assistant_id = "asst_x"
    tenant_id = "t1"
    user_id = "u1"


def test_mcp_adapter_wraps_jsonrpc():
    seen = {}
    def fake_urlopen(url, method, headers, body, timeout):
        seen["body"] = body; seen["headers"] = headers; return {"status": 200, "body": "{}"}
    c = Connector(connector_id="c", tenant_id="t", user_id="u", name="m", kind="mcp",
                  base_url="https://m.example.com",
                  auth={"_allowed_origins": ["m.example.com"], "_mcp_tool": "weather"})
    adapter = MCPConnectorAdapter(urlopen=fake_urlopen)
    res = adapter.request(c, "POST", "/", {"q": "x"}, 10)
    assert res["ok"] is True
    assert seen["body"]["jsonrpc"] == "2.0"
    assert seen["body"]["method"] == "tools/call"
    assert seen["body"]["params"]["name"] == "weather"
    assert seen["body"]["params"]["arguments"] == {"q": "x"}


def test_openapi_adapter_uses_operation_templating():
    seen = {}
    def fake_urlopen(url, method, headers, body, timeout):
        seen["method"] = method; seen["url"] = url; return {"status": 200, "body": "{}"}
    c = Connector(connector_id="c", tenant_id="t", user_id="u", name="o", kind="openapi",
                  base_url="https://o.example.com",
                  auth={"_allowed_origins": ["o.example.com"], "_openapi_operation": "GET /v1/weather"})
    adapter = OpenApiConnectorAdapter(urlopen=fake_urlopen)
    adapter.request(c, "", "", {}, 10)
    assert seen["method"] == "GET"
    assert seen["url"] == "https://o.example.com/v1/weather"


def test_browse_executes_when_policy_allows(tmp_path):
    cfg = assistant_config({"personal_assistant": {"enabled": True,
                                                   "computer": {"allow_browser": True,
                                                                "allowed_hosts": ["example.com"]}}})
    svc = AssistantService(config=cfg)
    seen = {}
    def fake_urlopen(url, method, headers, body, timeout):
        seen["url"] = url; return {"status": 200, "body": "<html>ok</html>"}
    svc._transport = fake_urlopen
    res = svc.browse_tool(tenant_id="t1", user_id="u1", url="https://example.com/page")
    assert res["ok"] is True
    assert "<html>ok</html>" in res["body"]


def test_browse_denied_outside_allowlist():
    cfg = assistant_config({"personal_assistant": {"enabled": True,
                                                   "computer": {"allow_browser": True,
                                                                "allowed_hosts": ["example.com"]}}})
    svc = AssistantService(config=cfg)
    res = svc.browse_tool(tenant_id="t1", user_id="u1", url="https://evil.example.net/x")
    assert res["denied"] is True


def test_run_shell_executes_when_allowlisted():
    cfg = assistant_config({"personal_assistant": {"enabled": True,
                                                   "computer": {"allow_shell": True,
                                                                "allowed_commands": ["/bin/echo"]}}})
    svc = AssistantService(config=cfg)
    svc._shell_runner = lambda command, timeout: {"exit_code": 0, "stdout": "hi", "stderr": ""}
    res = svc.run_shell_tool(tenant_id="t1", user_id="u1", command="/bin/echo hi")
    assert res["ok"] is True
    assert res["stdout"] == "hi"


def test_run_shell_denied_when_not_allowlisted():
    cfg = assistant_config({"personal_assistant": {"enabled": True,
                                                   "computer": {"allow_shell": True,
                                                                "allowed_commands": ["/bin/echo"]}}})
    svc = AssistantService(config=cfg)
    res = svc.run_shell_tool(tenant_id="t1", user_id="u1", command="/bin/rm -rf /")
    assert res["denied"] is True
    assert "not allowlisted" in res["reason"]


def test_service_builds_http_runner_when_endpoint_configured():
    cfg = assistant_config({"personal_assistant": {"enabled": True,
                                                   "inference": {"endpoint": "https://m.example.com/v1"}}})
    svc = AssistantService(config=cfg)
    assert isinstance(svc.runner, HttpModelRunnerAdapter)


# ---------------------------------------------------------------------------
# Postgres store backend (utilize herdr_pa)
# ---------------------------------------------------------------------------

def _make_pg_fake(bucket):
    """Return a psycopg2 connect() that shares an in-memory json 'table'."""
    class _Mem:
        def __init__(self):
            self._rows = [(bucket.get("data", "{}"),)]
            self.executed = []
        def execute(self, sql, params=None):
            self.executed.append((sql, params))
            if sql.startswith("INSERT"):
                bucket["data"] = params[1]
            elif sql.startswith("SELECT"):
                self._rows = [(bucket.get("data", "{}"),)]
        def fetchone(self):
            return self._rows[0] if bucket.get("data", "{}") != "{}" else None
        def __enter__(self): return self
        def __exit__(self, *a): return False
    class _Conn:
        def cursor(self): return _Mem()
        def commit(self): pass
        def close(self): pass
    return lambda: _Conn()


def test_postgres_backend_persist_and_load():
    bucket = {}
    backend = PostgresStoreBackend("postgresql://u:p@h:5432/db", connect=_make_pg_fake(bucket))
    backend.persist({"assistants": [{"assistant_id": "a1"}]})
    snap = backend.load()
    assert snap == {"assistants": [{"assistant_id": "a1"}]}
    assert backend.load() == {"assistants": [{"assistant_id": "a1"}]}


def test_postgres_backend_persist_never_raises():
    def connect():
        raise RuntimeError("db down")
    backend = PostgresStoreBackend("postgresql://u:p@h:5432/db", connect=connect)
    backend.persist({"x": 1})  # must not raise
    assert backend.load() is None


def test_store_with_pg_backend_survives_restart():
    bucket = {}
    backend = PostgresStoreBackend("postgresql://u:p@h:5432/db", connect=_make_pg_fake(bucket))
    store = AssistantStore(persist_backend=backend)
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}), store=store)
    svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    store2 = AssistantStore(persist_backend=PostgresStoreBackend("postgresql://u:p@h:5432/db",
                                                                 connect=_make_pg_fake(bucket)))
    store2.load_persisted()
    assert next(iter(store2.assistants.values())).name == "Helper"


@pytest.mark.integration
def test_postgres_backend_real_roundtrip():
    """Requires a live Postgres with the pa_store_snapshot table (PA_PG_DSN)."""
    dsn = os.environ.get("PA_PG_DSN")
    if not dsn:
        pytest.skip("PA_PG_DSN not set")
    backend = PostgresStoreBackend(dsn)
    store = AssistantStore(persist_backend=backend)
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}), store=store)
    svc.create_assistant(tenant_id="t9", user_id="u9", name="ITest")
    fresh = AssistantStore(persist_backend=PostgresStoreBackend(dsn))
    fresh.load_persisted()
    assert any(x.name == "ITest" for x in fresh.assistants.values())
