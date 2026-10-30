"""Approval-gated Google writes: a Gmail DRAFT and a Calendar event, never anything that delivers mail."""
import base64
import email
import json
import os
import urllib.error
import urllib.parse

import pytest

from herdr_engineering.control_plane import google_workspace as gw
from herdr_engineering.control_plane.app_credentials import CredentialStore, SecretBox
from herdr_engineering.control_plane.assistant import (
    EXECUTABLE_AFTER_APPROVAL,
    NEVER_PREAUTHORIZED,
    TOOL_CATALOG,
    describe_action,
    tool_sensitive,
)
from herdr_engineering.control_plane.google_oauth import SCOPES
from tests.control_plane.test_assistant_actions import approve_pending, build, grant, run_tool
from tests.control_plane.test_google_workspace import Google, Resp

DRAFT = {"to": "bob@example.com, carol@example.com", "subject": "Lunch", "body": "Shall we meet at noon?"}
EVENT = {"summary": "Planning", "start": "2026-11-02T09:00:00-08:00", "end": "2026-11-02T09:30:00-08:00"}


class WritableGoogle(Google):
    """The shared fake plus the draft and event endpoints; every write is recorded separately."""

    def __init__(self):
        super().__init__()
        self.drafts = {"r-1": {"to": "dan@example.com", "subject": "Hi", "mid": "m9"}}

    @property
    def writes(self):
        return [r for r in self.requests if r[0] in ("POST", "DELETE") and r[1] not in (gw.TOKEN_URL, gw.FREEBUSY_URL)]

    def __call__(self, req, timeout):
        url, method = req.full_url, req.get_method()
        path = urllib.parse.urlparse(url).path
        if url != gw.TOKEN_URL:
            for needle, code in self.fail.items():
                if needle in url:
                    self.requests.append((method, url, req.get_header("Authorization"), req.data))
                    raise urllib.error.HTTPError(url, code, "x", {}, None)
        if url != gw.TOKEN_URL and path.endswith("/messages/send") and method == "POST":
            self.requests.append((method, url, req.get_header("Authorization"), req.data))
            return Resp({"id": "sent1", "threadId": "t9"})
        if url != gw.TOKEN_URL and path.endswith("/drafts") and method == "POST":
            self.requests.append((method, url, req.get_header("Authorization"), req.data))
            return Resp({"id": "r-new", "message": {"id": "m1"}})
        if path.endswith("/drafts") and method == "GET":
            self.requests.append((method, url, req.get_header("Authorization"), req.data))
            return Resp({"drafts": [{"id": i} for i in self.drafts]})
        if "/drafts/" in path:
            self.requests.append((method, url, req.get_header("Authorization"), req.data))
            did = path.rsplit("/", 1)[1]
            if method == "DELETE":
                self.drafts.pop(did, None)
                return Resp({})
            d = self.drafts[did]
            return Resp({"id": did, "message": {"id": d["mid"], "snippet": "snip", "payload": {"headers": [
                {"name": "To", "value": d["to"]}, {"name": "Subject", "value": d["subject"]}]}}})
        if method == "POST" and path.endswith("/events"):
            self.requests.append((method, url, req.get_header("Authorization"), req.data))
            return Resp({"id": "ev1", "htmlLink": "https://www.google.com/calendar/event?eid=x"})
        return super().__call__(req, timeout)


@pytest.fixture()
def store(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    st = CredentialStore(SecretBox(str(key)))
    st.put_client("google", "cid.apps.googleusercontent.com", "client-secret")
    for user in ("key:u1", "key:u2"):
        st.put_connection("t", user, "google", account=f"{user[4:]}@x.org", scopes=list(SCOPES), refresh_token=f"refresh-{user[4:]}")
    return st


def make(store, blocked=None):
    g = WritableGoogle()
    return gw.GoogleWorkspace(store, blocked_fn=lambda t, u: list((blocked or {}).get((t, u), [])), opener=g), g


def wired(store, blocked=None):
    w, g = make(store, blocked)
    svc, r1, r2 = build()
    svc.google_write = w.write
    return svc, r1, r2, w, g


def body_of(req_data):
    return json.loads(req_data)


@pytest.mark.parametrize("tool", ["create_email_draft", "send_email", "create_calendar_event"])
def test_draft_and_event_tools_are_sensitive_never_preauthorized_and_run_only_after_approval(store, tool):
    assert tool in TOOL_CATALOG and tool_sensitive(tool)
    assert tool in NEVER_PREAUTHORIZED and tool in EXECUTABLE_AFTER_APPROVAL
    svc, r1, _, w, g = wired(store)
    args = EVENT if tool == "create_calendar_event" else DRAFT
    grant(svc, r1, tool)
    assert "approval required" in run_tool(svc, r1, tool, args)
    assert g.writes == []
    approve_pending(svc, r1)
    out = run_tool(svc, r1, tool, args)
    assert '"ok": true' in out and len(g.writes) == 1
    assert "refresh" not in out and "Bearer" not in out and "ya29" not in out
    assert "already ran" in run_tool(svc, r1, tool, args)      # a replay writes nothing more
    assert len(g.writes) == 1


def test_a_preauthorized_grant_for_the_write_tools_is_refused(store):
    from herdr_engineering.control_plane.assistant import AssistantForbidden
    svc, r1, _, _, _ = wired(store)
    op = {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"}
    for tool in ("create_email_draft", "send_email", "create_calendar_event"):
        with pytest.raises(AssistantForbidden):
            grant(svc, r1, tool, preauthorized=True, principal=op)


def test_unapproved_call_creates_nothing(store):
    svc, r1, _, w, g = wired(store)
    for tool, args in (("create_email_draft", DRAFT), ("send_email", DRAFT), ("create_calendar_event", EVENT)):
        assert "denied" in run_tool(svc, r1, tool, {**args, "summary" if "summary" in args else "subject": "other"})   # no grant
        grant(svc, r1, tool)
        assert "approval required" in run_tool(svc, r1, tool, args)       # grant, no approval
    assert g.requests == [] and g.writes == []                           # not even a token request was made
    # executing directly (outside the gate) with no google_write wired also writes nothing
    svc.google_write = None
    from herdr_engineering.control_plane.assistant import AssistantError
    with pytest.raises(AssistantError):
        svc.execute_tool(tenant_id="t", user_id="key:u1", assistant_id=r1.assistant_id, tool_id="create_email_draft", payload=DRAFT)
    assert g.writes == []


def test_the_approval_card_shows_the_exact_content():
    d = describe_action("create_email_draft", "create_email_draft", {**DRAFT, "body": "x" * 700})
    assert "bob@example.com, carol@example.com" in d and "Lunch" in d and "x" * 700 in d and "nothing is delivered" in d
    e = describe_action("create_calendar_event", "create_calendar_event",
                        {**EVENT, "calendar_id": "team@x.org", "location": "Room 4", "reminder_minutes": 15})
    for part in ("Planning", EVENT["start"], EVENT["end"], "team@x.org", "Room 4", "15 minutes"):
        assert part in e
    assert "primary" in describe_action("create_calendar_event", "x", EVENT)


def test_event_with_reminder_minutes_sets_a_popup_override(store):
    w, g = make(store)
    out = w.create_event("t", "key:u1", reminder_minutes=45, **EVENT)
    assert out == {"id": "ev1", "html_link": "https://www.google.com/calendar/event?eid=x"}
    (method, url, auth, data), = g.writes
    assert method == "POST" and url.endswith("/calendars/primary/events")
    ev = body_of(data)
    assert ev["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 45}]}
    assert ev["start"] == {"dateTime": EVENT["start"]} and "attendees" not in ev
    w.create_event("t", "key:u1", **EVENT)                                    # no reminder: Google's defaults apply
    assert "reminders" not in body_of(g.writes[-1][3])


@pytest.mark.parametrize("bad", [
    {"reminder_minutes": -1}, {"reminder_minutes": 40321}, {"reminder_minutes": True}, {"reminder_minutes": "5"},
    {"start": "2026-11-02T09:00:00"}, {"start": "2026-11-02"}, {"end": "2026-11-02T09:00:00-08:00"},
    {"end": "2026-11-02T08:00:00-08:00"}, {"summary": ""}, {"summary": "x" * 201}, {"summary": "a\nb"},
    {"location": "x" * 201}, {"calendar_id": ""},
])
def test_event_validation_refuses_before_any_request(store, bad):
    w, g = make(store)
    with pytest.raises(gw.GoogleError) as exc:
        w.create_event("t", "key:u1", **{**EVENT, **bad})
    assert exc.value.reason == "bad_request" and g.requests == []


def test_event_on_a_blocked_calendar_is_refused_before_any_request(store):
    w, g = make(store, {("t", "key:u1"): ["secret@x.org"]})
    with pytest.raises(gw.GoogleError) as exc:
        w.create_event("t", "key:u1", calendar_id="secret@x.org", **EVENT)
    assert exc.value.reason == "calendar_blocked"
    with pytest.raises(gw.GoogleError):
        w.create_event("t", "key:u1", calendar_id=" SECRET@x.org ", **EVENT)
    assert g.requests == [] and g.writes == []
    # the person's other calendars and their primary still work
    w.create_event("t", "key:u1", calendar_id="team@x.org", **EVENT)
    assert g.writes[-1][1].endswith("/calendars/team%40x.org/events")


def test_blocking_the_primary_blocks_the_default_calendar(store):
    w, g = make(store, {("t", "key:u1"): ["ann@x.org"]})       # the fake's primary id
    with pytest.raises(gw.GoogleError) as exc:
        w.create_event("t", "key:u1", **EVENT)
    assert exc.value.reason == "calendar_blocked" and g.writes == []


def test_only_an_approved_write_can_reach_the_one_send_function():
    names = sorted(n for n in dir(gw) + dir(gw.GoogleWorkspace) if "send" in n.lower())
    assert set(names) == {"send_message", "validate_send"}
    src = open(gw.__file__, encoding="utf-8").read()
    assert src.count("self.send_message(") == 1                              # one caller: write(), the post-approval executor
    assert src.index("self.send_message(") > src.index("def write(")
    assert src.index("self.send_message(") < src.index("def run(")           # and never inside the read entry point


def test_send_email_card_shows_the_exact_message_and_says_it_is_delivered():
    d = describe_action("send_email", "send_email", {**DRAFT, "body": "x" * 700, "thread_id": "t9"})
    assert "SEND" in d and "bob@example.com, carol@example.com" in d and "Lunch" in d and "x" * 700 in d and "reply" in d


def test_the_read_path_never_writes(store):
    w, g = make(store)
    w.run("t", "key:u1", "search_email", {"query": "x"})
    w.run("t", "key:u1", "list_calendar_events", {})
    w.list_drafts("t", "key:u1")
    assert g.writes == []
    for tool in ("create_email_draft", "send_email", "create_calendar_event", "delete_draft"):
        with pytest.raises(gw.GoogleError):
            w.run("t", "key:u1", tool, DRAFT)                              # the read entry point refuses write tools
    assert g.writes == []


def test_a_draft_is_a_gmail_draft_with_a_plain_rfc822_message(store):
    w, g = make(store)
    assert w.create_draft("t", "key:u1", **DRAFT) == {"id": "r-new"}
    (method, url, _, data), = g.writes
    assert method == "POST" and url == gw.GMAIL + "/drafts"
    raw = body_of(data)["message"]["raw"]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert msg["To"] == "bob@example.com, carol@example.com" and msg["Subject"] == "Lunch"
    assert msg.get_payload(decode=True).decode().strip() == DRAFT["body"]


@pytest.mark.parametrize("bad", [
    {"to": "bob@example.com\nBcc: evil@x.org"}, {"to": "bob@example.com\r\nX: y"}, {"subject": "hi\nBcc: evil@x.org"},
    {"subject": "hi\r"}, {"to": "not an address"}, {"to": "Bob <bob@example.com>"}, {"to": ""}, {"subject": ""},
    {"to": ",".join(f"a{i}@x.org" for i in range(11))}, {"body": ""}, {"body": "x" * 801}, {"body": 5},
])
def test_header_injection_and_malformed_drafts_are_refused_before_any_request(store, bad):
    w, g = make(store)
    with pytest.raises(gw.GoogleError) as exc:
        w.create_draft("t", "key:u1", **{**DRAFT, **bad})
    assert exc.value.reason == "bad_request" and g.requests == []


def test_invalid_content_is_refused_before_an_approval_is_requested(store):
    svc, r1, _, w, g = wired(store)
    grant(svc, r1, "create_email_draft")
    assert "denied" in run_tool(svc, r1, "create_email_draft", {**DRAFT, "subject": "a\nBcc: x@y.org"})
    assert not svc.store.approvals and g.requests == []


def test_drafts_list_and_discard_use_the_calling_persons_connection(store):
    w, g = make(store)
    [d] = w.list_drafts("t", "key:u2")
    assert d == {"id": "r-1", "to": "dan@example.com", "subject": "Hi", "snippet": "snip",
                 "gmail_url": "https://mail.google.com/mail/u/0/#drafts/m9"}
    assert w.delete_draft("t", "key:u2", "r-1") == {"id": "r-1", "discarded": True}
    mine = [r for r in g.requests if r[1] != gw.TOKEN_URL]
    assert mine and all(r[2] == "Bearer ya29.access.refresh-u2" for r in mine)
    assert any(r[0] == "DELETE" and r[1] == gw.GMAIL + "/drafts/r-1" for r in mine)
    for bad in ("../x", "a/b", "", "x" * 65, None):
        with pytest.raises(gw.GoogleError):
            w.delete_draft("t", "key:u2", bad)
    with pytest.raises(gw.GoogleError):
        w.delete_draft("t", "nobody", "r-1")                                # no connection: not_connected


def test_a_write_by_person_a_never_uses_person_bs_tokens(store):
    w, g = make(store)
    w.create_draft("t", "key:u1", **DRAFT)
    w.create_event("t", "key:u2", **EVENT)
    auths = [(r[1], r[2]) for r in g.writes]
    assert auths[0][1] == "Bearer ya29.access.refresh-u1" and auths[1][1] == "Bearer ya29.access.refresh-u2"
    assert "ya29.access.refresh-u2" not in repr(g.writes[0])


def test_the_write_method_only_reaches_allow_listed_urls(store):
    w, g = make(store)
    for method, url in (("POST", gw.GMAIL + "/messages"), ("DELETE", gw.GMAIL + "/messages/m1"), ("POST", gw.GMAIL + "/drafts/r-1"),
                        ("DELETE", gw.GMAIL + "/drafts/../messages/m1"), ("PATCH", gw.GMAIL + "/drafts")):
        with pytest.raises(gw.GoogleError):
            w._write("t", "key:u1", method, url, {})
    assert g.requests == []


def test_tool_results_never_carry_credentials(store):
    w, g = make(store)
    out = json.dumps([w.write("t", "key:u1", "create_email_draft", DRAFT), w.write("t", "key:u1", "create_calendar_event", EVENT),
                      w.list_drafts("t", "key:u1")])
    for secret in ("refresh-u1", "client-secret", "ya29", "Bearer"):
        assert secret not in out


def test_google_errors_surface_as_fixed_reasons(store):
    svc, r1, _, w, g = wired(store)
    store.delete_connection("t", "key:u1", "google")
    grant(svc, r1, "create_calendar_event")
    run_tool(svc, r1, "create_calendar_event", EVENT)
    approve_pending(svc, r1)
    out = run_tool(svc, r1, "create_calendar_event", EVENT)
    assert "not connected" in out and g.writes == []


# ---- the Drafts API ---------------------------------------------------------------------------------------------
from tests.control_plane.test_assistant_apps_api import (  # noqa: E402
    BASE,
    OTHER,
    SECRET,
    VIEWER,
    _raw,
    call,
    raw_get,
    ready,  # noqa: F401 - fixture
)


@pytest.fixture()
def connected(ready):  # noqa: F811
    call(ready, BASE + "/google/client", {"client_id": "cid.apps", "client_secret": SECRET}, "PUT")
    url = call(ready, BASE + "/google/connect", {}, "POST")[1]["url"]
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
    raw_get(ready, BASE + f"/google/callback?state={state}&code=c")
    ready.google._open = ready.google_fake = WritableGoogle()
    return ready


def test_drafts_api_lists_and_discards_for_the_calling_person(connected):
    status, body = call(connected, BASE + "/google/drafts")
    assert status == 200 and [d["id"] for d in body["drafts"]] == ["r-1"]
    assert call(connected, BASE + "/google/drafts/r-1", None, "DELETE") == (200, {"ok": True, "id": "r-1"})
    assert call(connected, BASE + "/google/drafts")[1] == {"drafts": []}
    assert call(connected, BASE + "/google/drafts/../x", None, "DELETE")[0] in (400, 404)
    assert call(connected, BASE + "/google/drafts/bad%2Fid", None, "DELETE")[0] == 400
    # a second person has no connection: nothing of the first person's is reachable
    assert call(connected, BASE + "/google/drafts", key=OTHER)[0] == 409
    assert call(connected, BASE + "/google/drafts/r-1", None, "DELETE", key=OTHER)[0] == 409


def test_drafts_api_guards_origin_role_and_method(connected):
    assert _raw(connected, "DELETE", BASE + "/google/drafts/r-1", {"Origin": "http://evil.example"}) == 403
    assert call(connected, BASE + "/google/drafts/r-1", None, "DELETE", key=VIEWER)[0] == 403
    assert call(connected, BASE + "/google/drafts", key=None)[0] == 401
    assert call(connected, BASE + "/google/drafts", {}, "POST")[0] == 404          # there is no create route over HTTP
    assert call(connected, BASE + "/google/drafts/r-1", {}, "PUT")[0] in (404, 501)
    assert call(connected, BASE + "/google/drafts", None, "DELETE")[0] == 404
    assert connected.google_fake.writes == [] or all(w[0] == "DELETE" for w in connected.google_fake.writes)


# ---- review follow-ups -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("ch", [" ", " ", "\u0085", "‮", "‏", "⁦", "\x00"])
def test_unicode_line_separators_and_bidi_controls_are_refused_everywhere(store, ch):
    w, g = make(store)
    for field in ("subject",):
        with pytest.raises(gw.GoogleError):
            w.create_draft("t", "key:u1", **{**DRAFT, field: f"hi{ch}there"})
    with pytest.raises(gw.GoogleError):
        w.create_draft("t", "key:u1", **{**DRAFT, "to": f"bob{ch}@example.com"})
    for field in ("summary", "location"):
        with pytest.raises(gw.GoogleError):
            w.create_event("t", "key:u1", **{**EVENT, field: f"a{ch}b"})
    for tool, args in (("create_email_draft", {**DRAFT, "subject": f"a{ch}b"}), ("create_calendar_event", {**EVENT, "summary": f"a{ch}b"})):
        with pytest.raises(gw.GoogleError):
            gw.validate_write(tool, args)
    assert g.requests == []


def test_ordinary_unicode_text_is_still_accepted(store):
    w, g = make(store)
    assert w.create_draft("t", "key:u1", **{**DRAFT, "subject": "Café – 日本語"}) == {"id": "r-new"}


def test_no_card_for_invalid_content_on_the_direct_tool_route_or_dry_run(store):
    from herdr_engineering.control_plane.assistant import AssistantError
    svc, r1, _, w, g = wired(store)
    grant(svc, r1, "create_email_draft")
    bad = {**DRAFT, "subject": "a b"}
    kw = dict(tenant_id=r1.tenant_id, user_id=r1.user_id, assistant_id=r1.assistant_id, tool_id="create_email_draft",
              scope="create_email_draft")
    with pytest.raises(AssistantError):
        svc.request_tool_action(run_id=r1.run_id, idempotency_key="k1", request_payload=bad, actor=r1.user_id, **kw)
    with pytest.raises(AssistantError):
        svc.dry_run_tool_action(request_payload=bad, **kw)
    assert not svc.store.approvals and not svc.store.actions and g.requests == []


def test_a_connection_without_write_scopes_maps_403_to_reconnect_needed(store):
    w, g = make(store)
    g.fail["/drafts"] = 403
    with pytest.raises(gw.GoogleError) as exc:
        w.create_draft("t", "key:u1", **DRAFT)
    assert exc.value.reason == "reconnect_needed"
    g.fail = {"/events": 403}
    with pytest.raises(gw.GoogleError) as exc:
        w.create_event("t", "key:u1", **EVENT)
    assert exc.value.reason == "reconnect_needed"
    svc, r1, _ = build()
    svc.google_write = w.write
    grant(svc, r1, "create_calendar_event")
    run_tool(svc, r1, "create_calendar_event", EVENT)
    approve_pending(svc, r1)
    assert "Reconnect Google" in run_tool(svc, r1, "create_calendar_event", EVENT)


def test_drafts_api_reports_reconnect_needed_as_409(connected):
    connected.google_fake.fail["/drafts/"] = 403
    assert call(connected, BASE + "/google/drafts/r-1", None, "DELETE") == (409, {"error": "reconnect_needed"})


def test_drafts_api_does_not_double_decode_the_id(connected):
    for path in ("/google/drafts/%252F", "/google/drafts/bad%2Fid", "/google/drafts/%2E%2E"):
        assert call(connected, BASE + path, None, "DELETE")[0] in (400, 404)
    assert not [w for w in connected.google_fake.writes if w[0] == "DELETE"]


def test_the_approval_card_does_not_redact_what_will_be_written():
    body = "token is password=hunter2hunter2 and key AKIAABCDEFGHIJKLMNOP"
    d = describe_action("create_email_draft", "x", {**DRAFT, "body": body})
    assert body in d
    assert describe_action("create_calendar_event", "x", {**EVENT, "summary": body})
    assert body in describe_action("create_calendar_event", "x", {**EVENT, "summary": body})
    assert body not in describe_action("draft_email", "x", {**DRAFT, "body": body, "subject": body})


def _wrong_key(store, tmp_path):
    other = tmp_path / "other-key"
    other.write_bytes(os.urandom(32))
    store._box = SecretBox(str(other))      # the key file was replaced: the saved client secret no longer opens


def test_a_wrong_key_file_gives_not_set_up_and_a_gated_write_does_not_stay_executing(store, tmp_path):
    svc, r1, _, w, g = wired(store)
    _wrong_key(store, tmp_path)
    for call_ in (lambda: w.run("t", "key:u1", "search_email", {"query": "x"}), lambda: w.list_drafts("t", "key:u1")):
        with pytest.raises(gw.GoogleError) as exc:
            call_()
        assert exc.value.reason == "not_set_up"
    assert g.requests == []
    grant(svc, r1, "create_email_draft")
    run_tool(svc, r1, "create_email_draft", DRAFT)
    approve_pending(svc, r1)
    out = run_tool(svc, r1, "create_email_draft", DRAFT)
    assert "not set up" in out
    assert g.writes == [] and all(a.status != "executing" for a in svc.store.actions.values())


def test_a_wrong_key_file_gives_409_not_set_up_on_the_drafts_and_calendars_routes(connected, tmp_path):
    _wrong_key(connected.apps.credentials, tmp_path)
    assert call(connected, BASE + "/google/drafts") == (409, {"error": "not_set_up"})
    assert call(connected, BASE + "/google/drafts/r-1", None, "DELETE") == (409, {"error": "not_set_up"})
    assert call(connected, BASE + "/google/calendars") == (409, {"error": "not_set_up"})
