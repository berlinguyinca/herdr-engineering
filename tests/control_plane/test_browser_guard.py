"""Open mode trusts every caller as operator, so a hostile web page must not be able to act as one: every
state-changing method on every route refuses a cross-origin / cross-site browser request, a browser request with a
non-JSON body, and (on every request) a Host that is a bare name nobody configured. Agents and the CLI send no Origin
and keep working. Real HTTP server, no mocks."""
import http.client
import json
import threading

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo

JSON = {"Content-Type": "application/json"}


@pytest.fixture(scope="module")
def server():
    svr = ControlPlaneServer(ControlPlaneRepo(), collab_dashboard_hosts=["dev.lan"])
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    yield svr
    svr.shutdown()


def call(server, method, path, headers=None, body=b"{}"):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        conn.request(method, path, body=body if method != "GET" else None, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw or b"null")
        except ValueError:
            return resp.status, raw.decode(errors="replace")
    finally:
        conn.close()


def refused(status, body):
    return status in {403, 415} and any(w in json.dumps(body) for w in ("cross-", "browser requests", "host"))


# Routes that had no browser defence before: they reach agent panes, mint keys, reroute models, create handoffs.
UNGUARDED = ["/api/v1/sessions/s1/messages", "/api/v1/llm/keys", "/api/v1/llm/models", "/api/v1/handoffs",
             "/api/v1/services/retire", "/api/v1/ingest"]


@pytest.mark.parametrize("path", UNGUARDED)
def test_a_cross_origin_post_is_refused_on_every_route(server, path):
    status, body = call(server, "POST", path, {**JSON, "Origin": "https://evil.example"})
    assert status == 403 and "cross-origin" in json.dumps(body)


@pytest.mark.parametrize("path", UNGUARDED)
def test_a_cross_site_fetch_without_origin_is_refused(server, path):
    status, body = call(server, "POST", path, {**JSON, "Sec-Fetch-Site": "cross-site"})
    assert status == 403 and "cross-site" in json.dumps(body)


@pytest.mark.parametrize("origin", ["null", "ftp://127.0.0.1", "http://127.0.0.1.evil.example"])
def test_odd_origins_are_refused(server, origin):
    assert call(server, "POST", "/api/v1/llm/keys", {**JSON, "Origin": origin})[0] == 403


@pytest.mark.parametrize("path", UNGUARDED)
def test_a_browser_post_must_be_json(server, path):
    origin = f"http://127.0.0.1:{server.port}"
    for ctype in ({"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"}, {}):
        status, body = call(server, "POST", path, {**ctype, "Origin": origin})
        assert status == 415, (path, ctype, body)


@pytest.mark.parametrize("method,path", [("PUT", "/api/v1/assistant-apps/a/client"), ("PATCH", "/api/v1/assistants/a"),
                                         ("DELETE", "/api/v1/assistants/a"), ("DELETE", "/api/v1/assistant-memory/m")])
def test_put_patch_and_delete_are_guarded_too(server, method, path):
    status, body = call(server, method, path, {**JSON, "Origin": "https://evil.example"})
    assert status == 403 and "cross-origin" in json.dumps(body)


def test_the_same_origin_dashboard_still_posts(server):
    status, body = call(server, "POST", "/api/v1/llm/keys",
                        {**JSON, "Origin": f"http://127.0.0.1:{server.port}", "Sec-Fetch-Site": "same-origin"},
                        body=json.dumps({"project": "p"}).encode())
    assert status == 200, body


def test_an_origin_the_operator_configured_is_allowed(server):
    status, body = call(server, "POST", "/api/v1/llm/keys",
                        {**JSON, "Origin": "http://dev.lan", "Host": "127.0.0.1"}, body=b'{"project": "p"}')
    assert status == 200, body


@pytest.mark.parametrize("path", UNGUARDED)
def test_agents_and_the_cli_send_no_origin_and_still_work(server, path):
    # Not a browser: no Origin, no Sec-Fetch-Site. Not even a Content-Type is demanded of them.
    for headers in (JSON, {}):
        status, body = call(server, "POST", path, headers, body=b'{"project": "p"}')
        assert not refused(status, body), (path, status, body)


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8080", "attacker.dev.lan", "127.0.0.1.evil.example"])
@pytest.mark.parametrize("method,path", [("GET", "/api/v1/sessions"), ("POST", "/api/v1/llm/keys"), ("GET", "/"),
                                         ("DELETE", "/api/v1/assistants/a")])
def test_a_rebound_host_is_refused_on_every_method(server, host, method, path):
    status, body = call(server, method, path, {**JSON, "Host": host})
    assert status == 403 and "host" in json.dumps(body).lower()


@pytest.mark.parametrize("host", ["localhost", "localhost:80", "127.0.0.1:8080", "[::1]:80", "10.1.2.3", "dev.lan",
                                  "dev.lan:80", "DEV.LAN"])
def test_loopback_ips_and_configured_names_are_accepted(server, host):
    status, body = call(server, "GET", "/api/v1/sessions", {"Host": host})
    assert status == 200, body


def test_the_bound_name_is_accepted():
    svr = ControlPlaneServer(ControlPlaneRepo(), host="localhost")
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        assert call(svr, "GET", "/api/v1/sessions", {"Host": "localhost"})[0] == 200
    finally:
        svr.shutdown()


def test_the_github_webhook_is_exempt_by_name(server):
    # Authenticated by its HMAC signature, called by GitHub (no browser, any content type, its own Host).
    for headers in ({"Content-Type": "text/plain", "Host": "hooks.example"},
                    {"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://github.com"}):
        status, body = call(server, "POST", "/api/v1/intelligence/webhooks/github", headers)
        assert not refused(status, body), (status, body)
