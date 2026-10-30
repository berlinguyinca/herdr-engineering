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
    assert pol.allows("ANN@x.org")                                         # case-sensitive
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


def test_person_a_tool_call_cannot_use_person_b_tokens(store):
    w, g = ws(store)
    w.run("default", "u1", "search_email", {"query": "x"})
    w.run("default", "u2", "search_email", {"query": "x"})
    bearers = [r[2] for r in g.requests if r[0] == "GET"]
    assert "Bearer ya29.access." + REFRESH in bearers and "Bearer ya29.access.2//other-refresh" in bearers
    u1 = [r[2] for r in g.requests[: len(g.requests) // 2] if r[0] == "GET"]
    assert all("other-refresh" not in b for b in u1)
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "u3", "search_email", {"query": "x"})
    assert exc.value.reason == "not_connected"
