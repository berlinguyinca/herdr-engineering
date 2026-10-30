"""The single-operator rule: a phone ``operator`` account shares the dashboard operator's per-person data
(Google connection, blocked calendars, reminders); every other account keeps its own. Approvals keep the real principal."""
import os
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane import google_workspace as gw
from herdr_engineering.control_plane import rakazo_bridge as rb
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import (
    AssistantError,
    AssistantService,
    assistant_config,
)
from herdr_engineering.control_plane.google_oauth import GMAIL_SCOPES, SCOPES
from herdr_engineering.control_plane.reminders import ReminderStore
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_google_workspace import REFRESH
from tests.control_plane.test_google_writes import WritableGoogle as Google
from tests.control_plane.test_rakazo_bridge import FAST, PW_A

T = "default"
OP = rb.OPEN_LISTENER_USER
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _acct(name, role):
    return {"email": f"{name}@example.com", "password_scrypt": rb.hash_password(PW_A, **FAST), "role": role}


@pytest.fixture()
def srv(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    cfg = assistant_config({"personal_assistant": {
        "enabled": True, "apps": {"key_file": str(key), "redirect_base": "https://x.example"}}})
    svc = AssistantService(config=cfg, now=lambda: NOW)
    bridge = {"enabled": True, "accounts": [_acct("op", "operator"), _acct("fam", "family")]}
    server = ControlPlaneServer(ControlPlaneRepo(), assistant_service=svc, rakazo_bridge=bridge, single_operator=True)
    google = Google()
    server.google._open = google
    server.apps.credentials.put_client("google", "cid.apps.googleusercontent.com", "secret")
    server.apps.credentials.put_connection(T, OP, "google", account="op@x.org", scopes=list(SCOPES + GMAIL_SCOPES),
                                     refresh_token=REFRESH)
    server.g = google
    # a viewer role cannot be configured, but the mapper must still refuse it if one ever appears (e.g. a Cognito pool role)
    viewer = rb.Account("viewacct", "view@example.com", "rakazo:viewacct", "viewer", server.rakazo._dummy, 0.0)
    server.rakazo._by_id["viewacct"] = viewer
    server.rakazo._by_principal[viewer.principal_id] = viewer
    by_email = {a.email: a.principal_id for a in server.rakazo.accounts}
    server.p = {k: by_email[f"{k}@example.com"] for k in ("op", "fam")} | {"view": "rakazo:viewacct"}
    return server


def read(server, user):
    return server.assistant.google_read(T, user, "search_email", {"query": "x"})


def test_canonical_person_maps_only_operator_phone_accounts(srv):
    b = srv.rakazo
    assert b.canonical_person(T, srv.p["op"]) == (T, OP)
    for user in (srv.p["fam"], srv.p["view"], "rakazo:unknown", OP, "key:u1", ""):
        assert b.canonical_person(T, user) == (T, user)


def test_cognito_accounts_never_map_even_with_operator_role(srv):
    b = srv.rakazo
    for aid, role in (("cogop", "operator"), ("cogfam", "family")):
        acct = rb.Account(aid, f"{aid}@x.org", f"rakazo:{aid}", role, b._dummy, 0.0, "cog-user")
        assert acct not in b.accounts
        b._by_id[aid] = acct
        b._by_principal[acct.principal_id] = acct
        b._cognito_users[aid] = "cog-user"
        assert b.canonical_person(T, acct.principal_id) == (T, acct.principal_id)
    # a YAML account that also signed in through Cognito is Cognito-derived: not mapped
    b._cognito_users[b.accounts[0].account_id] = "x"
    assert b.canonical_person(T, srv.p["op"]) == (T, srv.p["op"])


def test_removed_member_does_not_map(srv):
    b = srv.rakazo
    del b._by_principal[srv.p["op"]]
    assert b.canonical_person(T, srv.p["op"]) == (T, srv.p["op"])


def test_not_in_single_operator_mode_nothing_maps(srv):
    srv.single_operator = False
    assert srv.rakazo.canonical_person(T, srv.p["op"]) == (T, srv.p["op"])
    with pytest.raises(gw.GoogleError):
        read(srv, srv.p["op"])
    assert srv.g.requests == []


def test_other_tenants_never_map(srv):
    assert srv.rakazo.canonical_person("elsewhere", srv.p["op"]) == ("elsewhere", srv.p["op"])


def test_phone_operator_uses_the_dashboard_google_connection(srv):
    out = read(srv, srv.p["op"])
    assert out["result"]
    assert read(srv, OP)["result"] == out["result"]
    assert all(r[2] in (None, "Bearer ya29.access." + REFRESH) for r in srv.g.requests)


@pytest.mark.parametrize("who", ["fam", "view"])
def test_family_and_viewer_cannot_see_the_operators_connection(srv, who):
    with pytest.raises(gw.GoogleError) as exc:
        read(srv, srv.p[who])
    assert exc.value.reason == "not_connected"
    assert srv.g.requests == []


def test_unknown_rakazo_id_is_not_connected(srv):
    with pytest.raises(gw.GoogleError) as exc:
        read(srv, "rakazo:nobody")
    assert exc.value.reason == "not_connected" and srv.g.requests == []


def test_google_write_maps_for_operator_phone_only(srv):
    ev = {"summary": "Planning", "start": "2026-11-02T09:00:00-08:00", "end": "2026-11-02T09:30:00-08:00"}
    with pytest.raises(gw.GoogleError) as exc:
        srv.assistant.google_write(T, srv.p["fam"], "create_calendar_event", ev)
    assert exc.value.reason == "not_connected" and srv.g.requests == []
    srv.assistant.google_write(T, srv.p["op"], "create_calendar_event", ev)
    assert any(r[0] == "POST" and "/events" in r[1] for r in srv.g.requests)


def test_blocked_calendars_set_on_dashboard_apply_to_phone_operator(srv):
    srv.apps.set_blocked_calendars(T, OP, ["secret@x.org"])
    assert srv.apps.blocked_calendars(T, OP) == ["secret@x.org"]
    # the google workspace consults the same (canonical) key for the phone operator
    assert srv.google._blocked(*srv._canonical_person(T, srv.p["op"])) == ["secret@x.org"]
    assert srv.google._blocked(*srv._canonical_person(T, srv.p["fam"])) == []


def _tool(srv, user, tool_id, **payload):
    svc = srv.assistant
    a = svc.create_assistant(tenant_id=T, user_id=user, name="A")
    return svc.execute_tool(tenant_id=T, user_id=user, assistant_id=a.assistant_id, tool_id=tool_id, payload=payload)


def test_reminders_are_shared_between_phone_operator_and_dashboard(srv):
    srv.assistant.reminders = srv.reminders = ReminderStore(clock=lambda: NOW)
    set_ = _tool(srv, srv.p["op"], "set_reminder", text="call mum", when="in 2 hours")
    rid = set_["reminder"]["id"]
    assert [r["id"] for r in _tool(srv, OP, "list_reminders")["reminders"]] == [rid]       # dashboard sees it
    assert [r.id for r in srv.reminders.list(T, OP)] == [rid]                                # stored under open-listener
    other = _tool(srv, OP, "set_reminder", text="dash one", when="in 3 hours")["reminder"]["id"]
    assert {r["id"] for r in _tool(srv, srv.p["op"], "list_reminders")["reminders"]} == {rid, other}
    assert _tool(srv, srv.p["op"], "cancel_reminder", id=other)["cancelled"] == other      # phone cancels dashboard's


def test_family_reminders_stay_private(srv):
    srv.assistant.reminders = srv.reminders = ReminderStore(clock=lambda: NOW)
    _tool(srv, srv.p["fam"], "set_reminder", text="mine", when="in 2 hours")
    assert _tool(srv, OP, "list_reminders")["reminders"] == []
    assert _tool(srv, srv.p["op"], "list_reminders")["reminders"] == []
    assert len(_tool(srv, srv.p["fam"], "list_reminders")["reminders"]) == 1


def test_without_a_bridge_the_person_is_unchanged():
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    assert svc.canonical_person(T, "rakazo:x") == (T, "rakazo:x")
    server = ControlPlaneServer(ControlPlaneRepo(), assistant_service=svc, single_operator=True)
    assert server._canonical_person(T, "rakazo:x") == (T, "rakazo:x")


def test_approval_of_a_phone_write_records_the_phone_principal(srv):
    svc, phone = srv.assistant, srv.p["op"]
    a = svc.create_assistant(tenant_id=T, user_id=phone, name="A")
    th = svc.create_thread(tenant_id=T, user_id=phone, assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id=T, user_id=phone, assistant_id=a.assistant_id, thread_id=th.thread_id, content="hi")
    svc.grant_tool(tenant_id=T, user_id=phone, assistant_id=a.assistant_id, tool_id="create_calendar_event", scope="*")
    args = {"summary": "Planning", "start": "2026-11-02T09:00:00-08:00", "end": "2026-11-02T09:30:00-08:00"}
    assert "approval required" in svc._execute_gated(run, "create_calendar_event", args, 4000)
    appr = [x for x in svc.store.approvals.values() if x.decision == "pending"][-1]
    assert appr.user_id == phone and appr.requested_by.startswith("assistant:")       # the REAL phone principal, not open-listener
    assert srv.g.requests == []
    svc.decide_approval(appr.approval_id, tenant_id=T, user_id=phone, decision="approved", actor=phone)
    assert svc.store.approvals[appr.approval_id].actor_id == phone
    svc._execute_gated(run, "create_calendar_event", args, 4000)
    assert any(r[0] == "POST" and "/events" in r[1] for r in srv.g.requests)      # ran with the operator's connection
    assert svc.store.approvals[appr.approval_id].user_id == phone and appr.actor_id == phone
    with pytest.raises(AssistantError):
        _tool(srv, srv.p["fam"], "create_calendar_event", **args)                    # direct, ungated: family has no connection
