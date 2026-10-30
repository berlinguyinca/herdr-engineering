"""Read-only Gmail + Calendar: refresh-token access, the allow-list, read-only scopes, and no credential in any error."""
import base64
import json
import os
import stat
import urllib.error
import urllib.parse

import pytest

from herdr_engineering.control_plane import google_workspace as gw
from herdr_engineering.control_plane.assistant import (
    AssistantError,
    AssistantService,
    assistant_config,
)

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
        self.requests, self.fail = [], {}
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
            return Resp({"access_token": "ya29.access", "expires_in": 3600})
        path = urllib.parse.urlparse(url).path
        if path.endswith("/messages"):
            return Resp({"messages": [{"id": "m1"}, {"id": "../bad"}]})
        if "/messages/" in path:
            return Resp(self.msgs[path.rsplit("/", 1)[1]])
        if "/events" in path:
            return Resp({"items": [{"id": "e1", "summary": "Standup", "start": {"dateTime": "2026-10-31T09:00:00-07:00"},
                                    "end": {"dateTime": "2026-10-31T09:15:00-07:00"}, "attendees": [{}, {}], "status": "confirmed"}]})
        raise AssertionError(url)


@pytest.fixture()
def files(tmp_path):
    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": SECRET}}))
    token = tmp_path / "token.json"
    gw.write_token_file(str(token), {"refresh_token": REFRESH, "scopes": list(gw.SCOPES)})
    return str(client), str(token)


def ws(files, users=("default/u1",), **kw):
    google = Google()
    cfg = {"enabled": True, "client_secret_file": files[0], "token_file": files[1], "users": list(users)}
    return gw.GoogleWorkspace(cfg, opener=google, **kw), google


def test_search_read_and_events_return_trimmed_untrusted_data(files):
    w, g = ws(files)
    out = w.run("default", "u1", "search_email", {"query": "from:alice", "max_results": 3})
    assert out["untrusted_content"] is True
    assert out["result"] == [{"id": "m1", "from": "Alice <a@x.org>", "subject": "Lunch", "date": "Fri", "snippet": "snip"}]
    mail = w.run("default", "u1", "read_email", {"message_id": "m1"})["result"]
    assert mail["body"] == "Hello from the body" and mail["subject"] == "Lunch" and mail["truncated"] is False
    [ev] = w.run("default", "u1", "list_calendar_events", {"time_min": "2026-10-31"})["result"]
    assert ev["summary"] == "Standup" and ev["attendees"] == 2
    assert all(r[0] in ("GET", "POST") for r in g.requests)
    assert sum(1 for r in g.requests if r[1] == gw.TOKEN_URL) == 1       # the access token is cached


def test_only_listed_users_may_read_and_it_can_be_off(files):
    w, _ = ws(files)
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "someone-else", "search_email", {"query": "x"})
    assert exc.value.reason == "not_enabled_for_user"
    w.config["enabled"] = False
    with pytest.raises(gw.GoogleError):
        w.run("default", "u1", "search_email", {"query": "x"})


def test_a_token_with_any_write_scope_is_never_used(files):
    gw.write_token_file(files[1], {"refresh_token": REFRESH, "scopes": [*gw.SCOPES, "https://mail.google.com/"]})
    w, g = ws(files)
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "u1", "search_email", {"query": "x"})
    assert exc.value.reason == "unexpected_scope" and g.requests == []


def test_failures_have_fixed_reasons_and_never_carry_a_credential(files):
    w, g = ws(files)
    g.fail["/messages"] = 403
    with pytest.raises(gw.GoogleError) as exc:
        w.run("default", "u1", "search_email", {"query": "x"})
    assert exc.value.reason == "forbidden"
    for secret in (REFRESH, SECRET, "ya29.access"):
        assert secret not in repr(exc.value) and secret not in str(exc.value)
    os.unlink(files[1])
    w2, _ = ws(files)
    with pytest.raises(gw.GoogleError) as exc2:
        w2.run("default", "u1", "list_calendar_events", {})
    assert exc2.value.reason == "not_connected"


def test_ids_and_times_are_validated(files):
    w, _ = ws(files)
    for bad in ("../x", "a b", "", None, 5):
        with pytest.raises(gw.GoogleError):
            w.read_email(bad)
    with pytest.raises(gw.GoogleError):
        w.list_events(time_min="tomorrow; drop")


def test_config_validation():
    assert gw.google_config(None)["enabled"] is False
    with pytest.raises(ValueError, match="needs client_secret_file"):
        gw.google_config({"enabled": True})
    with pytest.raises(ValueError, match="unknown keys"):
        gw.google_config({"enabled": False, "scope": "x"})
    with pytest.raises(ValueError, match="users"):
        gw.google_config({"users": ["no-slash"]})


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


def test_the_assistant_tools_dispatch_and_report_fixed_reasons(files):
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    a = svc.create_assistant(tenant_id="default", user_id="u1", name="A")
    with pytest.raises(AssistantError, match="not enabled"):
        svc.execute_tool(tenant_id="default", user_id="u1", assistant_id=a.assistant_id, tool_id="search_email",
                         payload={"query": "x"})
    w, _ = ws(files)
    svc.google_read = w.run
    out = svc.execute_tool(tenant_id="default", user_id="u1", assistant_id=a.assistant_id, tool_id="search_email",
                           payload={"query": "x"})
    assert out["result"][0]["subject"] == "Lunch"
    w.config["users"] = []
    with pytest.raises(AssistantError, match="not_enabled_for_user"):
        svc.execute_tool(tenant_id="default", user_id="u1", assistant_id=a.assistant_id, tool_id="search_email",
                         payload={"query": "x"})
