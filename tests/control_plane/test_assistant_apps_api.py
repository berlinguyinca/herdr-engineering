"""HTTP routes for the assistant's app connections (/api/v1/assistant-apps)."""
import http.client
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import (
    AssistantService,
    apps_config,
    assistant_config,
)
from herdr_engineering.control_plane.google_oauth import SCOPES
from herdr_engineering.control_plane.repo import ControlPlaneRepo

KEY = "apps-test-operator-key"
OTHER = "apps-test-other-key"
VIEWER = "apps-test-viewer-key"
SECRET = "client-secret-xyz"
BASE = "/api/v1/assistant-apps"


class Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_google(req, timeout):
    if req.full_url.endswith("/token"):
        return Resp({"refresh_token": "r-tok", "access_token": "a-tok", "scope": " ".join(SCOPES)})
    if req.full_url.endswith("/profile"):
        return Resp({"emailAddress": "ann@example.com"})
    return Resp({})


def _serve(sink, apps):
    cfg = assistant_config({"personal_assistant": {"enabled": True, **({"apps": apps} if apps is not None else {})}})
    svr = ControlPlaneServer(ControlPlaneRepo(sink=sink), assistant_service=AssistantService(config=cfg),
                             api_keys={KEY: "operator", OTHER: "operator", VIEWER: "viewer"})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr


def call(svr, path, body=None, method="GET", key=KEY):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(f"http://127.0.0.1:{svr.port}{path}", method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def raw_get(svr, path):
    conn = http.client.HTTPConnection("127.0.0.1", svr.port)
    conn.request("GET", path)
    r = conn.getresponse()
    r.read()
    return r.status, r.getheader("Location")


@pytest.fixture
def ready(tmp_path, acknowledged_sink):
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    svr = _serve(acknowledged_sink, {"key_file": str(key), "redirect_base": "https://cp.example"})
    svr.apps._open = fake_google
    yield svr
    svr.shutdown()


@pytest.fixture
def unready(tmp_path, acknowledged_sink):
    svr = _serve(acknowledged_sink, {"key_file": str(tmp_path / "missing"), "redirect_base": "https://cp.example"})
    yield svr
    svr.shutdown()


def test_apps_config_rejects_unknown_keys_and_bad_values():
    assert apps_config(None)["present"] is False
    assert apps_config({"key_file": "/k", "redirect_base": "https://h/"})["redirect_base"] == "https://h"
    for bad in ({"nope": 1}, {"redirect_base": "ftp://x"}, {"key_file": ""}, "str"):
        with pytest.raises(ValueError):
            apps_config(bad)


def test_list_reports_not_ready_without_a_key_file(unready):
    status, body = call(unready, BASE)
    assert status == 200 and body["ready"] is False and body["providers"] == []
    for method, path, payload in (("PUT", "/google/client", {"client_id": "a", "client_secret": "b"}),
                                  ("POST", "/google/connect", {}), ("DELETE", "/google", None),
                                  ("PUT", "/google/blocked-calendars", {"ids": ["x"]})):
        assert call(unready, BASE + path, payload, method) == (409, {"error": "not_set_up"})


def test_list_not_ready_when_block_absent(tmp_path, acknowledged_sink):
    svr = _serve(acknowledged_sink, None)
    try:
        assert call(svr, BASE)[1]["ready"] is False
    finally:
        svr.shutdown()


def test_put_client_stores_secret_and_responses_never_echo_it(ready):
    status, body = call(ready, BASE + "/google/client", {"client_id": "cid.apps", "client_secret": SECRET}, "PUT")
    assert (status, body) == (200, {"ok": True})
    status, listing = call(ready, BASE)
    assert status == 200 and listing["ready"] is True
    assert listing["redirect_uri"] == "https://cp.example/api/v1/assistant-apps/google/callback"
    assert SECRET not in json.dumps(listing)
    assert any(p["client_configured"] for p in listing["providers"] if p["provider"] == "google")
    status, body = call(ready, BASE + "/google/client", {"client_id": "", "client_secret": SECRET}, "PUT")
    assert status == 400 and SECRET not in json.dumps(body)


def test_connect_returns_a_google_url_for_the_calling_person(ready):
    assert call(ready, BASE + "/google/connect", {}, "POST") == (409, {"error": "no_client"})
    call(ready, BASE + "/google/client", {"client_id": "cid.apps", "client_secret": SECRET}, "PUT")
    status, body = call(ready, BASE + "/google/connect", {}, "POST")
    assert status == 200 and body["url"].startswith("https://accounts.google.com/")
    assert SECRET not in body["url"]
    assert call(ready, BASE + "/google/connect", {}, "POST", key=None)[0] == 401


def test_callback_redirects_and_a_bad_state_redirects_with_error_not_500(ready):
    call(ready, BASE + "/google/client", {"client_id": "cid.apps", "client_secret": SECRET}, "PUT")
    cb = BASE + "/google/callback"
    assert raw_get(ready, cb) == (302, "/#/personal-assistant?error=bad_state")
    assert raw_get(ready, cb + "?state=nope&code=abc") == (302, "/#/personal-assistant?error=bad_state")
    url = call(ready, BASE + "/google/connect", {}, "POST")[1]["url"]
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
    assert raw_get(ready, cb + f"?state={state}&code=thecode") == (302, "/#/personal-assistant?connected=google")
    google = next(p for p in call(ready, BASE)[1]["providers"] if p["id"] == "gmail")
    assert google["connected"] is True and google["account"] == "ann@example.com"
    # the person who started it, not the caller (the callback has no caller), got the connection
    other = next(p for p in call(ready, BASE, key=OTHER)[1]["providers"] if p["id"] == "gmail")
    assert other["connected"] is False
    # state is single use
    assert raw_get(ready, cb + f"?state={state}&code=thecode")[1].endswith("error=bad_state")
    # provider-side denial: fixed reason only
    url = call(ready, BASE + "/google/connect", {}, "POST")[1]["url"]
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
    assert raw_get(ready, cb + f"?state={state}&error=access_denied%20secret") == (
        302, "/#/personal-assistant?error=denied")


def test_callback_when_not_set_up_still_redirects(unready):
    assert raw_get(unready, BASE + "/google/callback?state=x&code=y") == (
        302, "/#/personal-assistant?error=not_set_up")


def test_callback_needs_no_assistant_scope_but_connect_does(ready):
    assert raw_get(ready, BASE + "/google/callback?state=x")[0] == 302
    assert call(ready, BASE, key=None)[0] == 401
    assert call(ready, BASE + "/google/connect", {}, "POST", key=None)[0] == 401
    assert call(ready, BASE + "/google", None, "DELETE", key=None)[0] == 401
    assert call(ready, BASE + "/google/blocked-calendars", key=None)[0] == 401
    assert call(ready, BASE + "/google/client", {"client_id": "a", "client_secret": "b"}, "PUT", key=None)[0] == 401
    assert call(ready, BASE + "/google/callbackx", key=None)[0] == 401      # only the exact path is exempt


def test_blocked_calendars_are_per_person_and_validated(ready):
    path = BASE + "/google/blocked-calendars"
    assert call(ready, path, {"ids": ["a@x", "b@x"]}, "PUT") == (200, {"ids": ["a@x", "b@x"]})
    assert call(ready, path)[1] == {"ids": ["a@x", "b@x"]}
    assert call(ready, BASE)[1]["blocked_calendars"] == ["a@x", "b@x"]
    assert call(ready, path, key=OTHER)[1] == {"ids": []}
    assert call(ready, path, {"ids": "nope"}, "PUT")[0] == 400
    assert call(ready, path, {"ids": [1]}, "PUT")[0] == 400


def test_disconnect_and_unknown_routes(ready):
    assert call(ready, BASE + "/google", None, "DELETE") == (200, {"ok": True})
    assert call(ready, BASE + "/nope/connect", {}, "POST")[0] == 404
    assert call(ready, BASE + "/google/nope")[0] == 404


def _raw(svr, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", svr.port)
    h = {"X-API-Key": KEY, "Content-Type": "application/json", **(headers or {})}
    conn.request(method, path, body=body, headers=h)
    r = conn.getresponse()
    r.read()
    return r.status


def test_put_is_501_outside_assistant_apps(ready):
    assert _raw(ready, "PUT", "/api/v1/assistant-memory/x", body=b"{}") == 501
    assert _raw(ready, "PUT", "/api/v1/collab/rooms/r1/profile", body=b"{}") == 501
    assert _raw(ready, "PUT", "/api/v1/assistant-apps", body=b"{}") == 501


def test_cross_origin_and_wrong_content_type_mutations_are_refused(ready):
    evil = {"Origin": "http://evil.example"}
    assert _raw(ready, "PUT", BASE + "/google/client", evil, b'{"client_id":"a","client_secret":"b"}') == 403
    assert _raw(ready, "DELETE", BASE + "/google", evil) == 403
    assert _raw(ready, "POST", BASE + "/google/connect", evil, b"{}") == 403
    assert _raw(ready, "PUT", BASE + "/google/client", {"Content-Type": "text/plain"}, b"{}") == 415


def test_viewer_key_cannot_mutate(ready):
    for method, path, payload in (("PUT", "/google/client", {"client_id": "a", "client_secret": "b"}),
                                  ("POST", "/google/connect", {}), ("DELETE", "/google", None),
                                  ("PUT", "/google/blocked-calendars", {"ids": ["x"]})):
        assert call(ready, BASE + path, payload, method, key=VIEWER)[0] == 403


def test_a_second_key_cannot_touch_another_persons_connection(ready):
    call(ready, BASE + "/google/client", {"client_id": "cid.apps", "client_secret": SECRET}, "PUT")
    url = call(ready, BASE + "/google/connect", {}, "POST")[1]["url"]
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
    raw_get(ready, BASE + f"/google/callback?state={state}&code=c")
    call(ready, BASE + "/google/blocked-calendars", {"ids": ["mine"]}, "PUT")
    assert call(ready, BASE + "/google", None, "DELETE", key=OTHER)[0] == 200      # deletes OTHER's (nonexistent) own
    call(ready, BASE + "/google/blocked-calendars", {"ids": ["theirs"]}, "PUT", key=OTHER)
    mine = next(p for p in call(ready, BASE)[1]["providers"] if p["id"] == "gmail")
    assert mine["connected"] is True
    assert call(ready, BASE + "/google/blocked-calendars")[1] == {"ids": ["mine"]}


def test_callback_variants_stay_safe(ready):
    cb = BASE + "/google/callback"
    for path in (cb + "/", cb + "/?state=x", cb + "?state=x&state=y&code=", cb + "?code=1&x=%00"):
        assert raw_get(ready, path) == (302, "/#/personal-assistant?error=bad_state")


def test_calendars_route_lists_blocked_ones_flagged_for_the_owner_only(ready):
    assert call(ready, BASE + "/google/calendars") == (409, {"error": "not_connected"})
    call(ready, BASE + "/google/client", {"client_id": "cid.apps", "client_secret": SECRET}, "PUT")
    url = call(ready, BASE + "/google/connect", {}, "POST")[1]["url"]
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
    raw_get(ready, BASE + f"/google/callback?state={state}&code=c")

    from tests.control_plane.test_google_workspace import Google
    fake = Google()
    fake.primary = "ann@x.org"
    ready.google._open = fake
    call(ready, BASE + "/google/blocked-calendars", {"ids": ["secret@x.org"]}, "PUT")
    status, body = call(ready, BASE + "/google/calendars")
    assert status == 200
    assert {c["id"]: c["blocked"] for c in body["calendars"]} == {"ann@x.org": False, "team@x.org": False, "secret@x.org": True}
    assert SECRET not in json.dumps(body)
    # the model-facing listing still omits the blocked calendar, and the assistant tool entry point has no way to this one
    assert call(ready, BASE + "/google/calendars", key=None)[0] == 401
    assert call(ready, BASE + "/google/calendars", key=OTHER)[0] == 409       # another person has no connection
