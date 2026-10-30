"""Read-only Gmail + Calendar: refresh-token access, the allow-list, read-only scopes, and no credential in any error."""
import base64
import io
import json
import os
import stat
import urllib.error
import urllib.parse

import pytest

from herdr_engineering.control_plane import google_workspace as gw
from herdr_engineering.control_plane.app_credentials import CredentialStore, SecretBox
from herdr_engineering.control_plane.assistant import (
    AssistantError,
    AssistantService,
    assistant_config,
)
from herdr_engineering.control_plane.google_oauth import SCOPES

REFRESH = "1//refresh-token-value"
SECRET = "client-secret-value"


class Resp:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.data


class Google:
    """A fake Google: token endpoint + Gmail + Calendar, recording every request."""

    def __init__(self):
        self.requests, self.fail, self.revoked, self.primary = [], {}, False, "ann@x.org"
        self.busy = {}      # calendar id -> [(start, end)] reported by freeBusy
        b64 = base64.urlsafe_b64encode(b"Hello from the body").decode()
        self.msgs = {"m1": {"id": "m1", "snippet": "snip", "payload": {
            "headers": [{"name": "From", "value": "Alice <a@x.org>"}, {"name": "Subject", "value": "Lunch"},
                        {"name": "Date", "value": "Fri"}, {"name": "To", "value": "me"}],
            "mimeType": "multipart/alternative", "parts": [{"mimeType": "text/html", "body": {"data": "PGI+"}},
                                                           {"mimeType": "text/plain", "body": {"data": b64}}]}}}

    def __call__(self, req, timeout):
        url = req.full_url
        self.requests.append((req.get_method(), url, req.get_header("Authorization"), req.data))
        for needle, code in self.fail.items():
            if needle in url:
                raise urllib.error.HTTPError(url, code, "x", {}, None)
        if url == gw.TOKEN_URL:
            if self.revoked:
                raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"invalid_grant","error_description":"Token has been revoked ' + REFRESH.encode() + b'"}'))
            who = urllib.parse.parse_qs(req.data.decode())["refresh_token"][0]
            return Resp({"access_token": "ya29.access." + who, "expires_in": 3600})
        path = urllib.parse.urlparse(url).path
        if url == gw.FREEBUSY_URL:
            ids = [i["id"] for i in json.loads(req.data)["items"]]
            return Resp({"calendars": {i: {"busy": [{"start": a, "end": b} for a, b in self.busy.get(i, [])]} for i in ids}})
        if path.endswith("/messages"):
            return Resp({"messages": [{"id": "m1"}, {"id": "../bad"}]})
        if "/messages/" in path:
            return Resp(self.msgs[path.rsplit("/", 1)[1]])
        if path.endswith("/calendarList"):
            return Resp({"items": [{"id": "ann@x.org", "summary": "Ann", "primary": True}, {"id": "team@x.org", "summary": "Team"},
                                   {"id": "secret@x.org", "summary": "Private"}]})
        if path.endswith("/calendars/primary"):
            return Resp({"id": self.primary})
        if "/events/" in path:
            return Resp({"id": "e1", "summary": "One", "start": {"date": "2026-10-31"}, "end": {"date": "2026-11-01"}})
        if "/events" in path:
            return Resp({"items": [{"id": "e1", "summary": "Standup", "start": {"dateTime": "2026-10-31T09:00:00-07:00"},
                                    "end": {"dateTime": "2026-10-31T09:15:00-07:00"}, "attendees": [{}, {}], "status": "confirmed"}]})
        raise AssertionError(url)


@pytest.fixture()
def store(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    st = CredentialStore(SecretBox(str(key)))
    st.put_client("google", "cid.apps.googleusercontent.com", SECRET)
    st.put_connection("default", "u1", "google", account="u1@x.org", scopes=list(SCOPES), refresh_token=REFRESH)
    return st


def ws(store, blocked=None, **kw):
    google = Google()
    return gw.GoogleWorkspace(store, blocked_fn=lambda t, u: list((blocked or {}).get((t, u), [])), opener=google, **kw), google


def test_search_read_and_events_return_trimmed_untrusted_data(store):
    w, g = ws(store)
    out = w.run("default", "u1", "search_email", {"query": "from:alice", "max_results": 3})
    assert out["untrusted_content"] is True
    assert out["result"] == [{"id": "m1", "from": "Alice <a@x.org>", "subject": "Lunch", "date": "Fri", "snippet": "snip"}]
    mail = w.run("default", "u1", "read_email", {"message_id": "m1"})["result"]
    assert mail["body"] == "Hello from the body" and mail["subject"] == "Lunch" and mail["truncated"] is False
    [ev] = w.run("default", "u1", "list_calendar_events", {"time_min": "2026-10-31"})["result"]
    assert ev["summary"] == "Standup" and ev["attendees"] == 2
    # reads are GET; the only POSTs are the token endpoint and freeBusy (a read that takes its query as a body)
    assert all(r[0] == "GET" or (r[0] == "POST" and r[1] in (gw.TOKEN_URL, gw.FREEBUSY_URL)) for r in g.requests)
    assert sum(1 for r in g.requests if r[1] == gw.TOKEN_URL) == 1       # the access token is cached


def test_a_person_without_a_connection_or_needing_attention_gets_fixed_reasons(store):
    w, g = ws(store)
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "someone-else", "search_email", {"query": "x"})
    assert exc.value.reason == "not_connected" and g.requests == []
    store.set_status("default", "u1", "google", "needs_attention")
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "u1", "search_email", {"query": "x"})
    assert exc.value.reason == "needs_attention" and g.requests == []


def test_failures_have_fixed_reasons_and_never_carry_a_credential(store):
    w, g = ws(store)
    g.fail["/messages"] = 403
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "u1", "search_email", {"query": "x"})
    assert exc.value.reason == "forbidden"
    for secret in (REFRESH, SECRET, "ya29.access"):
        assert secret not in repr(exc.value) and secret not in str(exc.value)


def test_ids_and_times_are_validated(store):
    w, _ = ws(store)
    for bad in ("../x", "a b", "", None, 5):
        with pytest.raises(gw.GoogleError):
            w.read_email("default", "u1", bad)
    with pytest.raises(gw.GoogleError):
        w.list_events("default", "u1", time_min="tomorrow; drop")


def test_authorization_url_asks_for_read_only_scopes_with_pkce():
    state, verifier, challenge = gw.new_pkce()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(gw.authorization_url("cid", "http://127.0.0.1:8765/", state, challenge)).query)
    assert set(q["scope"][0].split()) == set(gw.SCOPES) and all(s.endswith(".readonly") for s in gw.SCOPES)
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"] and q["state"] == [state]
    assert verifier not in json.dumps(q)


def test_exchange_stores_a_private_token_and_refuses_wider_scopes(tmp_path):
    class Tokens:
        def __init__(self, scope):
            self.scope = scope

        def __call__(self, req, timeout):
            return Resp({"refresh_token": REFRESH, "scope": self.scope, "access_token": "a"})
    rec = gw.exchange_code(("cid", SECRET), "code", "ver", "http://127.0.0.1:8765/", Tokens(" ".join(gw.SCOPES)))
    path = tmp_path / "t.json"
    gw.write_token_file(str(path), rec)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with pytest.raises(gw.GoogleError) as exc:
        gw.exchange_code(("cid", SECRET), "code", "ver", "http://127.0.0.1:8765/", Tokens("https://mail.google.com/"))
    assert exc.value.reason == "unexpected_scope"


def test_the_assistant_tools_dispatch_and_report_fixed_reasons(store):
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    a = svc.create_assistant(tenant_id="default", user_id="u1", name="A")
    with pytest.raises(AssistantError, match="not enabled"):
        svc.execute_tool(tenant_id="default", user_id="u1", assistant_id=a.assistant_id, tool_id="search_email",
                         payload={"query": "x"})
    w, _ = ws(store)
    svc.google_read = w.run
    out = svc.execute_tool(tenant_id="default", user_id="u1", assistant_id=a.assistant_id, tool_id="search_email",
                           payload={"query": "x"})
    assert out["result"][0]["subject"] == "Lunch"
    store.delete_connection("default", "u1", "google")
    with pytest.raises(AssistantError, match="not connected"):
        svc.execute_tool(tenant_id="default", user_id="u1", assistant_id=a.assistant_id, tool_id="search_email",
                         payload={"query": "x"})


WIN = {"time_min": "2026-11-02T08:00:00Z", "time_max": "2026-11-02T18:00:00Z"}


def free(w, minutes=30, **win):
    return w.run("default", "u1", "find_free_time", {"duration_minutes": minutes, **(win or WIN)})


def test_free_time_ignores_blocked_calendars(store):
    w, g = ws(store, blocked={("default", "u1"): ["secret@x.org"]})
    g.busy = {"ann@x.org": [("2026-11-02T09:00:00Z", "2026-11-02T10:00:00Z")],
              "secret@x.org": [("2026-11-02T12:00:00Z", "2026-11-02T16:00:00Z")]}     # must not reduce free time
    out = free(w)
    assert "untrusted_content" not in out
    assert out["slots"] == [{"start": "2026-11-02T08:00:00Z", "end": "2026-11-02T09:00:00Z"},
                            {"start": "2026-11-02T10:00:00Z", "end": "2026-11-02T18:00:00Z"}]
    posts = [r for r in g.requests if r[1] == gw.FREEBUSY_URL]
    assert len(posts) == 1 and sorted(i["id"] for i in json.loads(posts[0][3])["items"]) == ["ann@x.org", "team@x.org"]
    assert all(b"secret@x.org" not in (r[3] or b"") for r in g.requests)


def test_free_time_returns_at_most_five_slots_inside_the_window(store):
    w, g = ws(store)
    # busy every 30 min from 09:00 for 10 minutes, one overlapping/unsorted entry, and one spilling past the window
    g.busy = {"ann@x.org": [(f"2026-11-02T{9 + i // 2:02d}:{30 * (i % 2):02d}:00Z", f"2026-11-02T{9 + i // 2:02d}:{30 * (i % 2) + 10:02d}:00Z")
                            for i in range(12)][::-1] + [("2026-11-02T17:50:00Z", "2026-11-02T19:00:00Z")]}
    slots = free(w, 15)["slots"]
    assert len(slots) == 5 and slots == sorted(slots, key=lambda s: s["start"])
    assert slots[0] == {"start": "2026-11-02T08:00:00Z", "end": "2026-11-02T09:00:00Z"}
    assert all("2026-11-02T08:00:00Z" <= s["start"] < s["end"] <= "2026-11-02T18:00:00Z" for s in slots)
    assert [s["end"] for s in free(w, 25)["slots"]] == [
        "2026-11-02T09:00:00Z", "2026-11-02T17:50:00Z"]      # 20-minute gaps are too short for 25
    assert all(g_["start"] < g_["end"] for g_ in free(w, 480, time_min="2026-11-02T00:00:00Z", time_max="2026-11-03T00:00:00Z")["slots"])


def test_free_time_with_every_calendar_blocked_is_empty_and_asks_google_nothing(store):
    w, g = ws(store, blocked={("default", "u1"): ["primary", "team@x.org", "secret@x.org"]})
    assert free(w) == {"ok": True, "slots": []}
    assert not [r for r in g.requests if r[1] == gw.FREEBUSY_URL]


def test_free_time_primary_alias_block_hides_the_real_primary_id(store):
    w, g = ws(store, blocked={("default", "u1"): ["primary"]})
    g.busy = {"ann@x.org": [("2026-11-02T09:00:00Z", "2026-11-02T17:00:00Z")]}
    assert free(w)["slots"] == [{"start": "2026-11-02T08:00:00Z", "end": "2026-11-02T18:00:00Z"}]
    assert all(b"ann@x.org" not in (r[3] or b"") for r in g.requests)


@pytest.mark.parametrize("minutes,win", [
    (4, WIN), (481, WIN), (True, WIN), ("30", WIN), (30, {"time_min": "garbage", "time_max": "2026-11-02T18:00:00Z"}),
    (30, {"time_min": "2026-11-02T18:00:00Z", "time_max": "2026-11-02T08:00:00Z"}),
    (30, {"time_min": "2026-11-02T08:00:00Z", "time_max": "2026-11-02T08:00:00Z"}),
    (30, {"time_min": "2026-11-01T00:00:00Z", "time_max": "2026-11-15T00:00:01Z"}),
    (30, {"time_min": "2026-11-01"}), (30, {"time_min": 5, "time_max": 6})])
def test_free_time_window_and_duration_are_validated(store, minutes, win):
    w, g = ws(store)
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "u1", "find_free_time", {"duration_minutes": minutes, **win})
    assert exc.value.reason == "bad_request" and g.requests == []
    assert free(w, 30, time_min="2026-11-01T00:00:00Z", time_max="2026-11-15T00:00:00Z")["slots"]      # exactly 14 days is fine
