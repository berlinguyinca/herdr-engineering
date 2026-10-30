"""Read-only Gmail + Calendar: refresh-token access, the allow-list, read-only scopes, and no credential in any error."""
import base64
import io
import json
import os
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
from herdr_engineering.control_plane.google_oauth import GMAIL_SCOPES, SCOPES

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
        self.extra, self.page, self.fb_omit, self.fb_errors, self.fb_groups = 0, 0, set(), set(), None
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
            assert len(ids) <= 50
            out = {i: ({"errors": [{"reason": "notFound"}]} if i in self.fb_errors else
                       {"busy": [{"start": a, "end": b} for a, b in self.busy.get(i, [])]}) for i in ids if i not in self.fb_omit}
            return Resp({"calendars": out, **({"groups": self.fb_groups} if self.fb_groups else {})})
        if path.endswith("/messages"):
            return Resp({"messages": [{"id": "m1"}, {"id": "../bad"}]})
        if "/messages/" in path:
            return Resp(self.msgs[path.rsplit("/", 1)[1]])
        if path.endswith("/calendarList"):
            items = [{"id": "ann@x.org", "summary": "Ann", "primary": True}, {"id": "team@x.org", "summary": "Team"},
                     {"id": "secret@x.org", "summary": "Private"}] + [{"id": f"x{i}@x.org"} for i in range(self.extra)]
            if not self.page:
                return Resp({"items": items})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            at = int(q.get("pageToken", ["0"])[0])
            more = at + self.page < len(items)
            return Resp({"items": items[at:at + self.page], **({"nextPageToken": str(at + self.page)} if more else {})})
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
    st.put_connection("default", "u1", "google", account="u1@x.org", scopes=list(SCOPES + GMAIL_SCOPES), refresh_token=REFRESH)
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


def test_the_retired_file_based_google_config_is_refused():
    with pytest.raises(ValueError, match="replaced by personal_assistant.apps"):
        assistant_config({"personal_assistant": {"enabled": True, "google": {"enabled": False}}})
    assert "google" not in assistant_config({"personal_assistant": {"enabled": True}})


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
    (30, {"time_min": "2026-11-01"}), (30, {"time_min": 5, "time_max": 6}),
    (30, {"time_min": "0001-01-01T00:00:00+23:59", "time_max": "2026-11-02T18:00:00Z"})])
def test_free_time_window_and_duration_are_validated(store, minutes, win):
    w, g = ws(store)
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "u1", "find_free_time", {"duration_minutes": minutes, **win})
    assert exc.value.reason == "bad_request" and g.requests == []


def test_free_time_accepts_exactly_fourteen_days(store):
    w, _ = ws(store)
    assert free(w, 30, time_min="2026-11-01T00:00:00Z", time_max="2026-11-15T00:00:00Z")["slots"]


def fails_unavailable(w):
    with pytest.raises(gw.GoogleError) as exc:
        free(w)
    assert exc.value.reason == "unavailable"


def test_free_time_fails_safe_when_google_cannot_vouch_for_a_calendar(store):
    w, g = ws(store)
    g.fb_errors = {"team@x.org"}
    fails_unavailable(w)
    g.fb_errors, g.fb_omit = set(), {"team@x.org"}
    fails_unavailable(w)                                     # a calendar absent from the answer is not "free"
    g.fb_omit, g.fb_groups = set(), {"g": {"errors": [{"reason": "x"}]}}
    fails_unavailable(w)
    g.fb_groups, g.busy = None, {"ann@x.org": [("garbage", "2026-11-02T10:00:00Z")]}
    fails_unavailable(w)


def test_free_time_pages_the_calendar_list_and_chunks_freebusy(store):
    w, g = ws(store, blocked={("default", "u1"): ["x7@x.org"]})
    g.extra, g.page = 120, 40
    g.busy = {"x119@x.org": [("2026-11-02T09:00:00Z", "2026-11-02T10:00:00Z")]}      # on the last page
    out = free(w)
    assert out["slots"][0] == {"start": "2026-11-02T08:00:00Z", "end": "2026-11-02T09:00:00Z"}
    lists = [r for r in g.requests if r[1].split("?")[0].endswith("/calendarList")]
    assert len(lists) == 4 and all("showHidden=true" in r[1] for r in lists)
    sizes = [len(json.loads(r[3])["items"]) for r in g.requests if r[1] == gw.FREEBUSY_URL]
    assert sizes == [50, 50, 22] and sum(sizes) == 122      # 123 calendars minus the blocked x7
    assert all(b"x7@x.org" not in (r[3] or b"") for r in g.requests)


def test_free_time_refuses_to_truncate_an_endless_calendar_list(store):
    w, g = ws(store)
    g.extra, g.page = 5000, 100                                  # 51 pages > the cap
    fails_unavailable(w)
    assert not [r for r in g.requests if r[1] == gw.FREEBUSY_URL]


def test_free_time_clamps_adjacent_outside_and_offset_intervals(store):
    w, g = ws(store)
    g.busy = {"ann@x.org": [("2026-11-02T06:00:00Z", "2026-11-02T08:00:00Z"),       # ends exactly at the window start
                            ("2026-11-02T18:00:00Z", "2026-11-02T20:00:00Z"),       # starts exactly at the window end
                            ("2026-11-02T01:00:00Z", "2026-11-02T03:00:00Z"),       # wholly before
                            ("2026-11-02T09:00:00Z", "2026-11-02T10:00:00Z"),
                            ("2026-11-02T10:00:00Z", "2026-11-02T11:00:00Z")],      # adjacent: merges, no sliver
              "team@x.org": [("2026-11-02T07:00:00Z", "2026-11-02T08:30:00Z")]}     # straddles the start
    assert free(w)["slots"] == [{"start": "2026-11-02T08:30:00Z", "end": "2026-11-02T09:00:00Z"},
                                {"start": "2026-11-02T11:00:00Z", "end": "2026-11-02T18:00:00Z"}]
    # the same window written with a -08:00 offset
    out = free(w, time_min="2026-11-02T00:00:00-08:00", time_max="2026-11-02T10:00:00-08:00")
    assert out["slots"][-1] == {"start": "2026-11-02T11:00:00Z", "end": "2026-11-02T18:00:00Z"}


def test_find_free_time_dispatches_through_the_assistant_service(store):
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    a = svc.create_assistant(tenant_id="default", user_id="u1", name="A")
    w, g = ws(store)
    svc.google_read = w.run
    payload = {"duration_minutes": 30, **WIN}
    out = svc.execute_tool(tenant_id="default", user_id="u1", assistant_id=a.assistant_id, tool_id="find_free_time", payload=payload)
    assert out["ok"] is True and out["slots"] == [{"start": "2026-11-02T08:00:00Z", "end": "2026-11-02T18:00:00Z"}]
    with pytest.raises(AssistantError):
        svc.execute_tool(tenant_id="default", user_id="u1", assistant_id=a.assistant_id, tool_id="find_free_time",
                         payload={**payload, "duration_minutes": 1})
    g.fb_errors = {"ann@x.org"}
    with pytest.raises(AssistantError, match="find free time failed: unavailable"):
        svc.execute_tool(tenant_id="default", user_id="u1", assistant_id=a.assistant_id, tool_id="find_free_time", payload=payload)


def test_a_stale_revoked_refresh_does_not_mark_a_fresh_reconnect(store):
    g = Google()
    g.revoked = True
    holder = {}

    def opener(req, timeout):
        if req.full_url == gw.TOKEN_URL:
            holder["w"].invalidate("default", "u1")       # the person reconnected while this refresh was in flight
        return g(req, timeout)
    w = gw.GoogleWorkspace(store, blocked_fn=lambda t, u: [], opener=opener)
    holder["w"] = w
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "u1", "search_email", {"query": "x"})
    assert exc.value.reason == "unavailable"
    assert store.get_connection("default", "u1", "google").status == "connected"
    g.revoked = False
    holder["w"] = w
    w.run("default", "u1", "search_email", {"query": "x"})     # and a genuine revocation afterwards still flags it
    g.revoked = True
    w.invalidate("default", "u1")
    holder["w"] = type("N", (), {"invalidate": staticmethod(lambda *a: None)})()
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "u1", "search_email", {"query": "x"})
    assert exc.value.reason == "needs_attention" and store.get_connection("default", "u1", "google").status == "needs_attention"


def test_gmail_tools_fail_before_any_request_when_gmail_was_not_granted(store):
    # The deployment requests only identity + calendar scopes (Gmail is a restricted scope): a connection like that
    # still serves the calendar, and every Gmail operation is refused locally, without touching Google.
    narrow = [x for x in SCOPES if "gmail" not in x]
    store.put_connection("default", "u2", "google", account="u2@x.org", scopes=narrow, refresh_token=REFRESH)
    w, g = ws(store)
    for tool, payload in (("search_email", {"query": "x"}), ("read_email", {"message_id": "m1"})):
        with pytest.raises(gw.GoogleError) as exc:
            w.run("default", "u2", tool, payload)
        assert exc.value.reason == "forbidden"
    for call in (lambda: w.list_drafts("default", "u2"), lambda: w.delete_draft("default", "u2", "r1"),
                 lambda: w.create_draft("default", "u2", "a@x.org", "s", "b")):
        with pytest.raises(gw.GoogleError) as exc:
            call()
        assert exc.value.reason == "forbidden"
    assert g.requests == []
    assert w.run("default", "u2", "list_calendar_events", {"time_min": "2026-10-31"})["result"]


class SendGoogle(Google):
    """The Google double, plus the one write the send path makes."""
    def __call__(self, req, timeout):
        if req.full_url == f"{gw.GMAIL}/messages/send":
            self.requests.append(("POST", req.full_url, req.data))
            return Resp({"id": "sent1", "threadId": "t9"})
        return super().__call__(req, timeout)


def test_send_email_delivers_one_validated_reply_in_its_thread(store):
    import base64
    import email
    g = SendGoogle()
    w = gw.GoogleWorkspace(store, opener=g)
    out = w.write("default", "u1", "send_email", {"to": "a@x.org, b@x.org", "subject": "Re: Lunch", "body": "Yes, noon.",
                                                  "thread_id": "t9", "in_reply_to": "<abc@mail.x.org>"})
    assert out["sent"] == {"id": "sent1", "thread_id": "t9"}
    [(method, url, data)] = [r for r in g.requests if r[1].endswith("/messages/send")]
    body = json.loads(data)
    assert body["threadId"] == "t9"
    msg = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
    assert msg["To"] == "a@x.org, b@x.org" and msg["In-Reply-To"] == "<abc@mail.x.org>" and msg["References"] == "<abc@mail.x.org>"
    assert msg.get_payload().strip() == "Yes, noon."


@pytest.mark.parametrize("bad", [
    {"to": "nobody", "subject": "s", "body": "b"},
    {"to": "a@x.org", "subject": "s\r\nBcc: evil@x.org", "body": "b"},                       # header injection
    {"to": "a@x.org", "subject": "s", "body": "b", "in_reply_to": "<a@x>\r\nBcc: evil@x.org"},
    {"to": "a@x.org", "subject": "s", "body": "b", "thread_id": "../x"},
    {"to": "a@x.org", "subject": "s", "body": "x" * 801},
])
def test_send_email_refuses_invalid_content_before_any_request(store, bad):
    g = SendGoogle()
    w = gw.GoogleWorkspace(store, opener=g)
    with pytest.raises(gw.GoogleError):
        gw.validate_write("send_email", bad)
    with pytest.raises(gw.GoogleError):
        w.write("default", "u1", "send_email", bad)
    assert g.requests == []


def test_a_read_tool_cannot_send_and_send_needs_the_gmail_scopes(store):
    narrow = [x for x in SCOPES if "gmail" not in x]
    store.put_connection("default", "u2", "google", account="u2@x.org", scopes=narrow, refresh_token=REFRESH)
    g = SendGoogle()
    w = gw.GoogleWorkspace(store, opener=g)
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "u1", "send_email", {"to": "a@x.org", "subject": "s", "body": "b"})
    assert exc.value.reason == "bad_request"
    with pytest.raises(gw.GoogleError) as exc:
        w.write("default", "u2", "send_email", {"to": "a@x.org", "subject": "s", "body": "b"})
    assert exc.value.reason == "forbidden"
    assert g.requests == []
