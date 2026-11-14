# ruff: noqa: F811  (imported pytest fixtures are used as test arguments)
"""The dashboard library API: a read-only window onto the artifact host, over real HTTP and a real PostgreSQL.

No doubles.  The artifact host's internal listener (``InternalServer``, the production aiohttp app) runs on a real port
over the host test database; the dashboard is a real ``ControlPlaneServer`` whose ``library`` is the production
``ArtifactHostClient``.  The misbehaving-host tests use a real stdlib HTTP server that sends deliberately unsafe
answers (what is under test is that the dashboard refuses them).
"""
from __future__ import annotations

import asyncio
import http.client
import http.server
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgpack
import pytest

from herdr_artifact_host.blobs import BlobStore
from herdr_artifact_host.internal_server import (
    InternalConfig,
    InternalServer,
    build_app,
    key_digest,
)
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_host_client import ArtifactHostClient
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.artifact_host.conftest import (  # noqa: F401
    host,
    host_dsn,
    host_dsn_factory,
    host_postgres_server,
    migrated_dsn,
)
from tests.artifact_host.seed import FILES, Host

HOST_KEY = "dash-read-key-" + "x" * 32
KEYS = {"viewer-key": "viewer", "operator-key": "operator", "ingest-key": "ingest", "agent-key": "agent",
        "reviewer-key": "reviewer"}
CSP = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"
ROOT = Path(__file__).resolve().parents[2]
TABLES = ("artifacts", "artifact_versions", "shares", "share_events", "library_items", "library_folders", "member_usage")


def _boot_host(host: Host) -> InternalServer:
    async def boot() -> InternalServer:
        server = InternalServer(build_app(InternalConfig(key_digest(HOST_KEY), key_failures_per_minute=10_000),
                                          host._pool, BlobStore(host.blob_root)))
        await server.start()
        return server

    assert host.loop is not None
    return asyncio.run_coroutine_threadsafe(boot(), host.loop).result(30)


def _stop_host(host: Host, server: InternalServer) -> None:
    assert host.loop is not None
    asyncio.run_coroutine_threadsafe(server.stop(), host.loop).result(30)


def _get(port: int, path: str, key: str | None = "operator-key", method: str = "GET", body: bytes | None = None,
         headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    sent = dict(headers or {})
    if key:
        sent["X-API-Key"] = key
    try:
        conn.request(method, path, body=body, headers=sent)
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


@dataclass
class Dash:
    port: int
    host: Host
    internal: InternalServer
    stopped: bool = False

    def req(self, path: str, **kw: Any) -> tuple[int, dict[str, str], bytes]:
        return _get(self.port, path, **kw)

    def json(self, path: str, **kw: Any) -> Any:
        status, _, body = self.req(path, **kw)
        assert status == 200, (path, status, body)
        return json.loads(body)

    def host_json(self, path: str) -> Any:
        """The host's own JSON answer for ``path`` (what the dashboard must repeat)."""
        r = self.host.request("GET", path, {"Authorization": f"Bearer {HOST_KEY}", "Accept": "application/json"},
                              port=self.internal.port)
        assert r.status == 200, (path, r.status)
        return r.json()

    def counts(self) -> dict[str, int]:
        return {t: self.host.rows(f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in TABLES}


def _dashboard(client: ArtifactHostClient | None) -> ControlPlaneServer:
    server = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, api_keys=KEYS, library=client,
                                web_root=ROOT / "packages")
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    return server


@pytest.fixture
def dash(host: Host) -> Iterator[Dash]:
    internal = _boot_host(host)
    server = _dashboard(ArtifactHostClient(f"http://127.0.0.1:{internal.port}", HOST_KEY, timeout=3))
    state = Dash(server.port, host, internal)
    try:
        yield state
    finally:
        server.shutdown()
        if not state.stopped:
            _stop_host(host, internal)


def _seed(host: Host, n: int = 3) -> list[Any]:
    seeded = [host.seed_share() for _ in range(n)]
    for i, s in enumerate(seeded):
        host.execute("UPDATE artifacts SET display_name=$2, current_version_id=$3 WHERE artifact_id=$1",
                     s.artifact_id, f"Report {i}", s.version_id)
    return seeded


# ------------------------------------------------------------------------------------------ rows equal the host's feed

def test_library_rows_equal_the_hosts_feed(dash: Dash) -> None:
    _seed(dash.host)
    for query in ("", "?limit=2", "?limit=2&offset=2", "?q=report%201", "?sources=mine,pending"):
        assert dash.json(f"/api/v1/library{query}") == dash.host_json(f"/v1/feed{query}"), query
    assert len(dash.json("/api/v1/library")["items"]) == 3
    assert dash.json("/api/v1/library/folders") == dash.host_json("/v1/folders")


def test_item_versions_and_shares_equal_the_hosts(dash: Dash) -> None:
    aid = _seed(dash.host)[0].artifact_id
    assert dash.json(f"/api/v1/library/items/{aid}") == dash.host_json(f"/v1/items/{aid}")
    assert dash.json(f"/api/v1/library/items/{aid}/versions") == dash.host_json(f"/v1/items/{aid}/versions")
    assert dash.json(f"/api/v1/library/shares?artifact_id={aid}") == dash.host_json(f"/v1/shares?artifact_id={aid}")
    shares = dash.json(f"/api/v1/library/shares?artifact_id={aid}")["shares"]
    assert shares and all("token" not in s and "token_hash" not in s for s in shares)


def test_the_dashboard_answers_json_while_the_wire_to_the_host_is_msgpack(dash: Dash) -> None:
    _seed(dash.host, 1)
    status, headers, body = dash.req("/api/v1/library")
    assert status == 200 and headers["content-type"].startswith("application/json")
    packed = dash.host.request("GET", "/v1/feed", {"Authorization": f"Bearer {HOST_KEY}"}, port=dash.internal.port)
    assert packed.header("Content-Type") == "application/msgpack"
    assert msgpack.unpackb(packed.body, raw=False) == json.loads(body)


def test_bad_parameters_and_unknown_ids_keep_the_hosts_status(dash: Dash) -> None:
    _seed(dash.host, 1)
    assert dash.req("/api/v1/library?limit=abc")[0] == 400
    assert dash.req("/api/v1/library?sources=bogus")[0] == 400
    assert dash.req("/api/v1/library?nope=1")[0] == 400
    assert dash.req("/api/v1/library/items/artifact_missing")[0] == 404
    assert dash.req("/api/v1/library/items/%2e%2e")[0] == 404
    assert dash.req("/api/v1/library/unknown")[0] == 404


def test_the_library_needs_a_dashboard_key(dash: Dash) -> None:
    assert dash.req("/api/v1/library", key=None)[0] == 401
    assert dash.req("/api/v1/library", key="wrong")[0] == 401
    assert dash.req("/api/v1/library", key="operator-key")[0] == 200


@pytest.mark.parametrize("key,allowed", [("operator-key", True), ("viewer-key", False), ("ingest-key", False),
                                         ("agent-key", False), ("reviewer-key", False)])
def test_only_the_operator_role_reads_the_library(dash: Dash, key: str, allowed: bool) -> None:
    s = _seed(dash.host, 1)[0]
    for path in ("/api/v1/library", "/api/v1/library/folders", "/api/v1/library/shares",
                 f"/api/v1/library/items/{s.artifact_id}", f"/api/v1/library/items/{s.artifact_id}/versions",
                 f"/api/v1/library/content/{s.artifact_id}/{s.version_id}/index.html"):
        status, headers, body = dash.req(path, key=key)
        if allowed:
            assert status == 200, (path, status)
        else:
            assert status == 403, (key, path, status)
            assert "content-security-policy" not in headers and b"Report" not in body


def test_the_host_key_never_reaches_the_browser(dash: Dash) -> None:
    s = _seed(dash.host, 1)[0]
    paths = ["/api/v1/library", "/api/v1/library/folders", f"/api/v1/library/items/{s.artifact_id}",
             f"/api/v1/library/content/{s.artifact_id}/{s.version_id}/index.html", "/api/v1/library/nope"]
    for path in paths:
        _, headers, body = dash.req(path)
        blob = (json.dumps(headers) + body.decode("latin-1")).lower()
        assert HOST_KEY.lower() not in blob and "bearer" not in blob, path


# ------------------------------------------------------------------------------------------ no write route

WRITE_PATHS = ["/api/v1/library", "/api/v1/library/folders", "/api/v1/library/folders/fld_aaaaaaaaaaaaaaaaaaaaaaaa",
               "/api/v1/library/items/artifact_x", "/api/v1/library/items/artifact_x/versions",
               "/api/v1/library/pending/artifact_x/accept", "/api/v1/library/items/artifact_x/folders",
               "/api/v1/library/shares", "/api/v1/library/content/a/v/index.html", "/api/v1/library/anything"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_no_library_write_route_exists_and_nothing_changes(dash: Dash, method: str) -> None:
    _seed(dash.host)
    before = dash.counts()
    for path in WRITE_PATHS:
        for key in ("viewer-key", "operator-key"):
            status, headers, _ = dash.req(path, method=method, key=key, body=b'{"name":"x"}',
                                          headers={"Content-Type": "application/json"})
            # POST: 405 from the library itself (a viewer is refused 403 first); the other verbs are never routed to the
            # library at all, so they get the server's flat 501 like any other resource that does not take them
            assert status in {403, 405, 501}, (method, path, key, status)
            if status == 405:
                assert headers.get("allow") == "GET"
            if key == "operator-key":
                assert status == (405 if method == "POST" else 501), (method, path, status)
    assert dash.counts() == before


def test_the_dashboard_routes_no_library_verb_but_get_and_post_to_the_library_module() -> None:
    text = (ROOT / "herdr_engineering/control_plane/api.py").read_text()
    assert '"/api/v1/library/"' not in text and '| {"library"}' not in text and '{"assistant-apps", "library"}' not in text


def test_the_library_modules_have_no_write_handler_and_the_client_only_gets() -> None:
    text = (ROOT / "herdr_engineering/control_plane/artifact_library_api.py").read_text()
    for forbidden in ("accept_pending", "discard_pending", "create_folder", "rename_folder", "delete_folder", "file_item",
                      "unfile", "do_POST", "do_PUT", "do_DELETE"):
        assert forbidden not in text, forbidden
    client = (ROOT / "herdr_engineering/control_plane/artifact_host_client.py").read_text()
    assert '"GET"' in client
    for verb in ('"POST"', '"PUT"', '"DELETE"', '"PATCH"'):
        assert verb not in client


# ------------------------------------------------------------------------------------------ host down

def test_host_down_is_a_library_503_and_the_rest_of_the_dashboard_still_works(dash: Dash) -> None:
    _seed(dash.host, 1)
    assert dash.req("/api/v1/library")[0] == 200
    _stop_host(dash.host, dash.internal)
    dash.stopped = True
    for path in ("/api/v1/library", "/api/v1/library/folders", "/api/v1/library/items/artifact_x",
                 "/api/v1/library/items/artifact_x/versions", "/api/v1/library/shares",
                 "/api/v1/library/content/artifact_x/version_x/index.html"):
        status, _, body = dash.req(path)
        assert status == 503, path
        assert json.loads(body)["error"] == "library_unavailable"
        assert HOST_KEY not in body.decode()
    assert dash.req("/api/v1/artifacts")[0] == 200
    assert dash.req("/index.html")[0] == 200


def test_a_library_that_is_not_configured_is_a_503_too() -> None:
    server = _dashboard(None)
    try:
        assert _get(server.port, "/api/v1/library")[0] == 503
        assert _get(server.port, "/api/v1/artifacts")[0] == 200
    finally:
        server.shutdown()


def test_a_host_that_refuses_the_dashboards_key_is_a_503(host: Host) -> None:
    internal = _boot_host(host)
    server = _dashboard(ArtifactHostClient(f"http://127.0.0.1:{internal.port}", "wrong-key-" + "y" * 32, timeout=3))
    try:
        status, _, body = _get(server.port, "/api/v1/library")
        assert status == 503 and b"library_unavailable" in body
    finally:
        server.shutdown()
        _stop_host(host, internal)


# ------------------------------------------------------------------------------------------ the content proxy

def test_content_is_proxied_on_the_dashboard_origin_with_the_hosts_security_headers(dash: Dash) -> None:
    s = _seed(dash.host, 1)[0]
    tail = f"{s.artifact_id}/{s.version_id}/index.html"
    direct = dash.host.request("GET", f"/v1/content/{tail}", {"Authorization": f"Bearer {HOST_KEY}"},
                               port=dash.internal.port)
    assert direct.status == 200
    status, headers, body = dash.req(f"/api/v1/library/content/{tail}")
    assert status == 200 and body == FILES["index.html"] == direct.body
    for name in ("Content-Security-Policy", "X-Content-Type-Options", "Content-Type"):
        assert headers[name.lower()] == direct.header(name), name
    assert "sandbox" in headers["content-security-policy"].lower()
    assert "allow-same-origin" not in headers["content-security-policy"].lower()
    assert headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in headers and "www-authenticate" not in headers
    assert headers["cache-control"] == "no-store"
    nested = dash.req(f"/api/v1/library/content/{s.artifact_id}/{s.version_id}/data/big.bin")
    assert nested[0] == 200 and nested[2] == FILES["data/big.bin"]
    assert nested[1]["content-type"] == "application/octet-stream"


def test_content_needs_the_dashboard_key_and_forwards_nothing_from_the_browser(dash: Dash) -> None:
    s = _seed(dash.host, 1)[0]
    path = f"/api/v1/library/content/{s.artifact_id}/{s.version_id}/index.html"
    assert dash.req(path, key=None)[0] == 401
    status, headers, _ = dash.req(path, headers={"Cookie": "a=b", "Authorization": f"Bearer {HOST_KEY}"})
    assert status == 200 and "set-cookie" not in headers


@pytest.mark.parametrize("tail", ["../index.html", "data/../index.html", "%2e%2e/index.html", "..%2findex.html",
                                  "%5c..%5cindex.html", "index.html%00", "nope.html", "data/",
                                  "%2Fetc%2Fpasswd", "./index.html", "a//b"])
def test_content_paths_that_are_not_clean_manifest_paths_are_404(dash: Dash, tail: str) -> None:
    s = _seed(dash.host, 1)[0]
    status, _, body = dash.req(f"/api/v1/library/content/{s.artifact_id}/{s.version_id}/{tail}")
    assert status == 404 and b"root:" not in body, tail


@pytest.mark.parametrize("ids", [("..", "v"), ("a", ".."), ("a%2fb", "v"), ("a%20b", "v"), ("a" * 200, "v"), ("-a", "v"),
                                 ("a;b", "v"), ("%00", "v")])
def test_content_ids_are_validated_before_anything_is_sent(dash: Dash, ids: tuple[str, str]) -> None:
    assert dash.req(f"/api/v1/library/content/{ids[0]}/{ids[1]}/index.html")[0] == 404


def test_content_of_another_version_or_a_purged_one_is_404(dash: Dash) -> None:
    a, b = _seed(dash.host, 2)
    assert dash.req(f"/api/v1/library/content/{a.artifact_id}/{b.version_id}/index.html")[0] == 404
    dash.host.execute("UPDATE artifact_versions SET state='purged' WHERE version_id=$1", a.version_id)
    assert dash.req(f"/api/v1/library/content/{a.artifact_id}/{a.version_id}/index.html")[0] == 404


class Rogue:
    """A real HTTP server that answers every request with a configurable (unsafe) answer and logs what it received."""

    def __init__(self, headers: list[tuple[str, str]], body: bytes = b"<script>alert(1)</script>",
                 trickle: float | None = None) -> None:
        self.seen: list[tuple[str, dict[str, str]]] = []
        rogue = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                rogue.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                self.send_response(200)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if trickle is None:
                    self.wfile.write(body)
                    return
                try:
                    for i in range(len(body)):          # one byte at a time: each read is "in time", the whole is not
                        self.wfile.write(body[i:i + 1])
                        self.wfile.flush()
                        time.sleep(trickle)
                except OSError:
                    pass

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def rogue_factory() -> Iterator[Any]:
    made: list[tuple[Rogue, ControlPlaneServer]] = []

    def make(headers: list[tuple[str, str]], body: bytes = b"<script>alert(1)</script>", trickle: float | None = None,
             **client: Any) -> tuple[Rogue, int]:
        rogue = Rogue(headers, body, trickle)
        server = _dashboard(ArtifactHostClient(rogue.url, HOST_KEY, timeout=3, **client))
        made.append((rogue, server))
        return rogue, server.port

    yield make
    for rogue, server in made:
        server.shutdown()
        rogue.close()


GOOD = [("Content-Type", "text/html; charset=utf-8"), ("X-Content-Type-Options", "nosniff"),
        ("Content-Security-Policy", CSP)]
PATH = "/api/v1/library/content/art_1/ver_1/index.html"


@pytest.mark.parametrize("headers", [
    [("Content-Type", "text/html"), ("X-Content-Type-Options", "nosniff")],                                  # no CSP
    [("Content-Type", "text/html"), ("X-Content-Type-Options", "nosniff"),
     ("Content-Security-Policy", "default-src 'none'")],                                                      # no sandbox
    [("Content-Type", "text/html"), ("X-Content-Type-Options", "nosniff"),
     ("Content-Security-Policy", "sandbox allow-same-origin allow-scripts; default-src 'none'")],            # same-origin
    [("Content-Type", "text/html"), ("X-Content-Type-Options", "nosniff"),
     ("Content-Security-Policy", "sandbox allow-scripts; default-src 'none'")],                              # scripts
    [("Content-Type", "text/html"), ("X-Content-Type-Options", "nosniff"),
     ("Content-Security-Policy", "sandbox allow-popups-to-escape-sandbox; default-src 'none'")],             # any allow-*
    [("Content-Type", "text/html"), ("X-Content-Type-Options", "nosniff"),
     ("Content-Security-Policy", "sandbox; default-src *")],                                                  # open default-src
    [("Content-Type", "text/html"), ("X-Content-Type-Options", "nosniff"),
     ("Content-Security-Policy", "sandbox; img-src data:")],                                                  # no default-src
    [("Content-Type", "text/html"), ("Content-Security-Policy", CSP)],                                       # no nosniff
    [("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", CSP)],                               # no type
])
def test_a_host_response_without_the_sandbox_policy_is_refused_502(rogue_factory, headers) -> None:
    rogue, port = rogue_factory(headers)
    status, out, body = _get(port, PATH)
    assert status == 502 and b"alert(1)" not in body
    assert "content-security-policy" not in out
    assert len(rogue.seen) == 1


def test_hop_by_hop_cookie_and_auth_headers_from_the_host_are_stripped(rogue_factory) -> None:
    extra = [("Set-Cookie", "sid=1; Path=/"), ("WWW-Authenticate", "Basic"), ("Proxy-Authenticate", "x"),
             ("Keep-Alive", "timeout=5"), ("X-Powered-By", "rogue"), ("Link", "<http://evil>; rel=preload"),
             ("Access-Control-Allow-Origin", "*")]
    rogue, port = rogue_factory(GOOD + extra)
    status, headers, body = _get(port, PATH, headers={"Cookie": "a=b"})
    assert status == 200 and body == b"<script>alert(1)</script>"
    for name in ("set-cookie", "www-authenticate", "proxy-authenticate", "keep-alive", "x-powered-by", "link",
                 "access-control-allow-origin"):
        assert name not in headers, name
    assert headers["content-security-policy"] == CSP and headers["x-content-type-options"] == "nosniff"
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert [p for p, _ in rogue.seen] == ["/v1/content/art_1/ver_1/index.html"]
    sent = rogue.seen[0][1]
    assert sent["authorization"] == f"Bearer {HOST_KEY}"
    assert "cookie" not in sent and "x-api-key" not in sent


@pytest.mark.parametrize("bad", ["Content-Type", "Content-Security-Policy"])
def test_a_forwarded_header_value_with_a_line_break_is_refused_502(rogue_factory, bad: str) -> None:
    folded = {"Content-Type": "text/html;\r\n charset=x", "Content-Security-Policy": CSP + ";\r\n x-evil: 1"}[bad]
    headers = [(n, folded if n == bad else v) for n, v in GOOD]
    rogue, port = rogue_factory(headers)
    status, out, body = _get(port, PATH)
    assert status == 502 and b"alert(1)" not in body and "content-security-policy" not in out


@pytest.mark.parametrize("answer", [msgpack.packb({"items": [b"\xff\x00"]}), msgpack.packb({"x": msgpack.ExtType(5, b"ab")}),
                                    msgpack.packb({b"k": 1}), b"\xc1"])
def test_a_host_answer_json_cannot_carry_is_a_503(rogue_factory, answer: bytes) -> None:
    rogue, port = rogue_factory([("Content-Type", "application/msgpack")], answer)
    for path in ("/api/v1/library", "/api/v1/library/folders", "/api/v1/library/items/art_1"):
        status, _, body = _get(port, path)
        assert status == 503 and json.loads(body)["error"] == "library_unavailable", (path, status)


def test_a_host_that_trickles_the_content_is_cut_off_at_the_total_deadline(rogue_factory) -> None:
    rogue, port = rogue_factory(GOOD, b"x" * 400, trickle=0.1, content_deadline=0.6)
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    got = b""
    try:
        conn.request("GET", PATH, headers={"X-API-Key": "operator-key"})
        response = conn.getresponse()
        assert response.status == 200
        try:
            got = response.read()
        except http.client.IncompleteRead as exc:
            got = exc.partial
    finally:
        conn.close()
    assert len(got) < 400 and time.monotonic() - started < 6


def test_the_proxy_is_not_an_open_proxy(rogue_factory) -> None:
    hostile = ["/api/v1/library/content/art_1/ver_1/http://evil.example/x",
               "/api/v1/library/content/art_1/ver_1//evil.example/x",
               "/api/v1/library/content/art_1/ver_1/..%2f..%2fv1/feed",
               "/api/v1/library/content/art_1/ver_1/%2e%2e/%2e%2e/v1/feed",
               "/api/v1/library/content/..%2fv1%2ffeed/ver_1/index.html",
               "/api/v1/library/content/art_1/ver_1/a%5cb"]
    rogue, port = rogue_factory(GOOD)
    for path in hostile:
        assert _get(port, path)[0] == 404, path
    for requested, _ in rogue.seen:       # whatever did reach the target is a content path under the validated ids
        assert requested.startswith("/v1/content/art_1/ver_1/"), requested
        assert ".." not in requested and "\\" not in requested
    # an "absolute URL" path piece is one escaped segment of the path, never a different target
    assert all("evil.example" not in p.split("/v1/content/art_1/ver_1/", 1)[1].split("/", 1)[0] for p, _ in rogue.seen)


# ------------------------------------------------------------------------------------------ pages

def test_the_library_scripts_are_served_and_the_item_route_exists(dash: Dash) -> None:
    for name in ("library-view.js", "library-frame.js"):
        status, _, body = dash.req("/" + name)
        assert status == 200 and b"export" in body, name
    assert "'/library/:id': viewLibraryItem" in (ROOT / "packages/app.js").read_text()


def test_the_dashboards_own_artifacts_page_does_not_list_members_items(dash: Dash) -> None:
    _seed(dash.host)
    status, _, body = dash.req("/api/v1/artifacts")
    assert status == 200 and b"Report 0" not in body


# ------------------------------------------------------------------------------------------ the host key stays out of logs

def test_the_host_key_never_appears_in_logs_or_the_clients_repr(rogue_factory, dash: Dash, host: Host, caplog) -> None:
    import logging
    caplog.set_level(logging.DEBUG)
    client = ArtifactHostClient("http://127.0.0.1:1", HOST_KEY, timeout=1)
    assert HOST_KEY not in repr(client) and HOST_KEY not in str(client) and "dash-read" not in repr(client)
    _stop_host(dash.host, dash.internal)                  # host down: the warning path
    dash.stopped = True
    dash.req("/api/v1/library")
    dash.req("/api/v1/library/content/a/v/index.html")
    _, port = rogue_factory([("Content-Type", "text/html")])             # unsafe content: the refusal path
    _get(port, PATH)
    _, port = rogue_factory([("Content-Type", "application/msgpack")], msgpack.packb({"x": b"\xff"}))
    _get(port, "/api/v1/library")
    for call in (client.feed, client.folders, client.shares):
        with pytest.raises(Exception) as caught:
            call()
        assert HOST_KEY not in str(caught.value) and HOST_KEY not in repr(caught.value)
    assert caplog.records and HOST_KEY not in caplog.text
    assert all(HOST_KEY not in str(r.args) and HOST_KEY not in r.getMessage() for r in caplog.records)
