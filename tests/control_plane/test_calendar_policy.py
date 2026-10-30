"""Blocked calendars: one choke point for list, read, free/busy and create; generic refusal; per-person tokens."""
import os
import urllib.parse

import pytest

from herdr_engineering.control_plane import google_workspace as gw
from herdr_engineering.control_plane.app_credentials import CredentialStore, SecretBox
from herdr_engineering.control_plane.calendar_policy import CalendarPolicy
from herdr_engineering.control_plane.google_oauth import SCOPES

from .test_google_workspace import REFRESH, SECRET, ws


@pytest.fixture()
def store(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    st = CredentialStore(SecretBox(str(key)))
    st.put_client("google", "cid", SECRET)
    st.put_connection("default", "u1", "google", account="u1@x.org", scopes=list(SCOPES), refresh_token=REFRESH)
    st.put_connection("default", "u2", "google", account="u2@x.org", scopes=list(SCOPES), refresh_token="2//other-refresh")
    return st


def refuses(fn, *a, **k):
    with pytest.raises(gw.GoogleError) as exc:
        fn(*a, **k)
    assert exc.value.reason == "calendar_blocked" and str(exc.value) == "calendar_blocked"
    return exc.value


def test_primary_alias_and_real_id_are_blocked_together():
    calls = []

    def resolve():
        calls.append(1)
        return "ann@x.org"
    for blocked in ({"primary"}, {"ann@x.org"}, {" ann@x.org "}):
        pol = CalendarPolicy(blocked, resolve)
        assert not pol.allows("primary") and not pol.allows("ann@x.org") and pol.allows("team@x.org")
        refuses(pol.require, "primary")
        refuses(pol.require, "ann@x.org")
        assert [c["id"] for c in pol.filter([{"id": "ann@x.org", "primary": True}, {"id": "team@x.org"}])] == ["team@x.org"]
    pol = CalendarPolicy({"ann@x.org"}, resolve)
    assert not pol.allows("ANN@X.org") and not pol.allows(" Ann@x.org ")  # case-insensitive, trimmed
    assert len(calls) == 3                                                 # resolved once per policy, never for no blocks
    assert CalendarPolicy(set(), resolve).allows("primary") and len(calls) == 3


def test_list_free_busy_read_and_create_all_refuse_a_blocked_calendar(store):
    w, g = ws(store, blocked={("default", "u1"): ["secret@x.org", "ann@x.org"]})
    assert [c["id"] for c in w.list_calendars("default", "u1")] == ["team@x.org"]
    refuses(w.list_events, "default", "u1")                                # default calendar is primary == ann@x.org
    refuses(w.list_events, "default", "u1", calendar_id="secret@x.org")
    refuses(w.read_event, "default", "u1", "secret@x.org", "e1")
    refuses(w._calendars_allowed, "default", "u1", ["team@x.org", "primary"])      # create
    assert w._calendars_allowed("default", "u1", ["team@x.org"]) == ["team@x.org"]
    assert w.exclude_blocked("default", "u1", ["primary", "team@x.org", "secret@x.org"]) == ["team@x.org"]   # free/busy
    assert not any("/events" in r[1] for r in g.requests)                  # nothing was fetched from a blocked calendar


def test_model_supplied_blocked_id_gets_the_generic_error(store):
    w, g = ws(store, blocked={("default", "u1"): ["secret@x.org"]})
    a = refuses(w.run, "default", "u1", "list_calendar_events", {"calendar_id": "secret@x.org"})
    b = w.run("default", "u1", "list_calendar_events", {"calendar_id": "team@x.org"})
    assert b["result"]
    # an id that does not exist is not distinguishable from a blocked one by the policy: both pass or fail on the list only
    assert str(a) == "calendar_blocked" and "secret@x.org" not in str(a) and "secret@x.org" not in repr(a)
    assert any(urllib.parse.quote("team@x.org", safe="") in r[1] for r in g.requests)


def test_revoked_refresh_token_marks_needs_attention_without_leaking(store):
    w, g = ws(store)
    g.revoked = True
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "u1", "search_email", {"query": "x"})
    assert exc.value.reason == "needs_attention"
    for secret in (REFRESH, SECRET):
        assert secret not in repr(exc.value) and secret not in str(exc.value)
    assert store.get_connection("default", "u1", "google").status == "needs_attention"
    assert store.get_connection("default", "u2", "google").status == "connected"
    with pytest.raises(gw.GoogleError) as exc2:         # now refused up front, without calling Google again
        w.run("default", "u1", "search_email", {"query": "x"})
    assert exc2.value.reason == "needs_attention"


def test_case_variants_are_refused_on_list_and_read_paths(store):
    w, g = ws(store, blocked={("default", "u1"): ["Secret@X.org"]})
    refuses(w.list_events, "default", "u1", calendar_id="SECRET@x.org")
    refuses(w.read_event, "default", "u1", " secret@x.ORG ", "e1")
    assert [c["id"] for c in w.list_calendars("default", "u1")] == ["ann@x.org", "team@x.org"]
    refuses(w._calendars_allowed, "default", "u1", ["secret@X.ORG"])


def test_reconnect_with_a_different_account_reresolves_primary(store):
    w, g = ws(store, blocked={("default", "u1"): ["new@x.org"]})
    w.list_events("default", "u1")                       # primary resolves to ann@x.org: allowed, and cached
    store.put_connection("default", "u1", "google", account="new@x.org", scopes=list(SCOPES), refresh_token=REFRESH)
    g.primary = "new@x.org"
    refuses(w.list_events, "default", "u1")              # even without invalidate(): the account changed
    w2, g2 = ws(store, blocked={("default", "u1"): ["new@x.org"]})
    w2.list_events("default", "u1", calendar_id="team@x.org")
    w2.invalidate("default", "u1")
    assert ("default", "u1") not in w2._policies and ("default", "u1") not in w2._access


def test_primary_resolution_failure_fails_closed(store):
    w, g = ws(store, blocked={("default", "u1"): ["secret@x.org"]})
    g.fail["/calendars/primary"] = 500
    before = len(g.requests)
    with pytest.raises(gw.GoogleError):
        w.list_events("default", "u1")
    assert not any("/events" in r[1] for r in g.requests[before:])


def test_every_request_is_a_get_to_a_read_url_except_the_token_post(store):
    w, g = ws(store, blocked={("default", "u1"): ["secret@x.org"]})
    w.run("default", "u1", "search_email", {"query": "x"})
    w.run("default", "u1", "read_email", {"message_id": "m1"})
    w.run("default", "u1", "list_calendar_events", {})
    w.read_event("default", "u1", "team@x.org", "e1")
    w.list_calendars("default", "u1")
    for method, url, *_ in g.requests:
        assert (method == "POST" and url == gw.TOKEN_URL) or (method == "GET" and url.startswith(
            ("https://gmail.googleapis.com/gmail/v1/users/me/", "https://www.googleapis.com/calendar/v3/")))


def test_apps_service_hook_invalidates_on_disconnect(store):
    from herdr_engineering.control_plane.assistant_apps import AppsService
    apps = AppsService(store, redirect_base="https://x", opener=lambda req, t: (_ for _ in ()).throw(OSError("x")))
    w, g = ws(store)
    apps.on_change = w.invalidate
    w.run("default", "u1", "search_email", {"query": "x"})
    assert ("default", "u1") in w._access
    apps.disconnect("default", "u1", "google")
    assert ("default", "u1") not in w._access
    assert apps.credentials is store


def test_person_a_tool_call_cannot_use_person_b_tokens(store):
    w, g = ws(store)
    for user, refresh in (("u1", REFRESH), ("u2", "2//other-refresh"), ("u1", REFRESH)):
        start = len(g.requests)
        w.run("default", user, "search_email", {"query": "x"})
        gets = [r for r in g.requests[start:] if r[0] == "GET"]
        assert gets and all(r[2] == "Bearer ya29.access." + refresh for r in gets)
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "u3", "search_email", {"query": "x"})
    assert exc.value.reason == "not_connected"


def test_owner_ui_listing_flags_blocked_calendars_and_the_model_facing_ones_stay_hidden(store):
    from herdr_engineering.control_plane.assistant import TOOL_CATALOG
    w, _ = ws(store, blocked={("default", "u1"): ["secret@x.org"]})
    flagged = {c["id"]: c["blocked"] for c in w.list_calendars_for_owner_ui("default", "u1")}
    assert flagged == {"ann@x.org": False, "team@x.org": False, "secret@x.org": True}
    assert "secret@x.org" not in [c["id"] for c in w.list_calendars("default", "u1")]       # the model-facing listing omits it
    # blocking the alias `primary` blocks the real primary calendar too
    w2, _ = ws(store, blocked={("default", "u1"): ["primary"]})
    assert {c["id"]: c["blocked"] for c in w2.list_calendars_for_owner_ui("default", "u1")}["ann@x.org"] is True
    # no way from the assistant's entry point, and not a catalogued tool
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "u1", "list_calendars_for_owner_ui", {})
    assert exc.value.reason == "bad_request"
    assert "list_calendars_for_owner_ui" not in TOOL_CATALOG
