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
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from herdr_engineering.control_plane.assistant import (
    ApprovalRequired,
    AssistantNotFound,
    AssistantService,
    AssistantUnavailable,
    assistant_config,
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
