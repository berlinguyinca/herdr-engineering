"""The database-free public share server: real sockets, frozen tmp bundles, the uniform 404, Range, headers, events."""
import hashlib
import http.client
import json
import os
import socket
import threading
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane import share_public
from herdr_engineering.control_plane.artifact_shares import ShareStore
from herdr_engineering.control_plane.share_bundle import (
    BundleLimits,
    build_bundle,
    remove_state,
    write_state,
)
from herdr_engineering.control_plane.share_public import ShareConfig, load_state, make_share_server

CSP = ("sandbox allow-scripts allow-downloads; default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
       "worker-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'")
EXPOSE = "Content-Range, Content-Length, Accept-Ranges, ETag"

TOKEN = "A" * 20 + "b-_" + "9" * 20
assert len(TOKEN) == 43
OTHER_TOKEN = "Z" * 43

BODY = bytes(range(256)) * 40  # 10240 bytes
FILES = {
    "index.html": b"<!doctype html><title>t</title><p>hello</p>",
    "data/table.parquet": BODY,
    "doc.pdf": b"%PDF-1.4\n%fake\n",
    "pic.svg": b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
    "empty.txt": b"",
    "my file é.txt": b"spaced",
}


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _iso(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat()


class Env:
    def __init__(self, tmp_path, *, audience="public", expires_in=86400.0, files=FILES, entry=None, config_extra=None,
                 state_extra=None):
        self.root = tmp_path / "shares"
        self.events = tmp_path / "events" / "events.jsonl"
        self.events.parent.mkdir()
        self.clock = [1_900_000_000.0]
        self.share_id = "shr-1"
        index = build_bundle(self.root / self.share_id,
                             [(n, _sha(b), len(b), [b]) for n, b in files.items()],
                             entry=entry, limits=BundleLimits(1 << 24, 100))
        self.token_hash = _sha(TOKEN.encode())
        self.state = {
            "share_id": self.share_id, "audience": audience, "state": "live",
            "expires_at": None if expires_in is None else _iso(self.clock[0] + expires_in),
            "allow_emails": [], "allow_groups": [], "entry": index.entry, "title": "t",
            "files": [{"name": f.name, "sha256": f.sha256, "size": f.size, "content_type": f.content_type}
                      for f in index.files]}
        self.state.update(state_extra or {})
        write_state(self.root, self.token_hash, self.state)
        self.config = ShareConfig(shares_root=self.root, events_path=self.events, **(config_extra or {}))
        self.server = make_share_server(self.config, host="127.0.0.1", port=0, now=lambda: self.clock[0])
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_accept_encoding=True)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            conn.endheaders(body)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
        finally:
            conn.close()

    def raw(self, data: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(data)
            out = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except (TimeoutError, ConnectionResetError):
                    break
                if not chunk:
                    break
                out += chunk
            return out

    def event_lines(self):
        if not self.events.exists():
            return []
        return self.events.read_bytes().splitlines()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def f(path):
    return "/s/%s/f/%s" % (TOKEN, path)


def _not_found(env):
    status, headers, body = env.request("GET", "/nothing-here")
    assert status == 404
    return body


# ----------------------------------------------------------------------------- basic serving

def test_public_share_serves_entry_redirect_and_files(env):
    status, headers, body = env.request("GET", "/s/%s/" % TOKEN)
    assert status == 302
    assert headers["location"] == "f/index.html"
    assert headers["access-control-allow-origin"] == "*"
    status, headers, body = env.request("GET", f("index.html"))
    assert status == 200 and body == FILES["index.html"]
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert headers["content-length"] == str(len(FILES["index.html"]))
    status, _, body = env.request("GET", f("data/table.parquet"))
    assert status == 200 and body == BODY
    status, _, body = env.request("GET", f("my%20file%20%C3%A9.txt"))
    assert status == 200 and body == b"spaced"
    status, _, body = env.request("GET", f("index.html") + "?v=2")
    assert status == 200 and body == FILES["index.html"]
    status, headers, _ = env.request("HEAD", "/s/%s/" % TOKEN)
    assert status == 302 and headers["location"] == "f/index.html"


def test_redirect_url_encodes_the_entry(tmp_path):
    e = Env(tmp_path, files={"my report#1.html": b"<p>x</p>"})
    try:
        status, headers, _ = e.request("GET", "/s/%s/" % TOKEN)
        assert status == 302 and headers["location"] == "f/my%20report%231.html"
        status, _, body = e.request("GET", "/s/%s/%s" % (TOKEN, headers["location"]))
        assert status == 200 and body == b"<p>x</p>"
    finally:
        e.close()


# ----------------------------------------------------------------------------- the uniform 404

TRAVERSALS = [
    "../index.html", "..%2findex.html", "%2e%2e/index.html", "%2E%2E%2Findex.html", "data/../index.html",
    "data/%2e%2e/index.html", "%252e%252e/index.html", "data%2ftable.parquet", "data%2Ftable.parquet",
    "data%5ctable.parquet", "data\\table.parquet", "index.html%00", "index.html%00.png", "./index.html",
    "data//table.parquet", "/index.html", "%c0%ae%c0%ae/index.html", "%c0%afindex.html", "%ff", "%",
    "%zz", "INDEX.HTML", "Index.html", "x" * 5000, "a/" * 600, "state.json", "../by-token/%s/state.json" % ("0" * 64),
    "../../shr-1/files/index.html", "data", "data/", "", "nope.html", "index.html/", "index.html/.",
]


def test_every_failure_is_the_same_404_body(env, tmp_path):
    reference = _not_found(env)
    assert reference
    cases = []
    cases.append(("GET", "/s/%s/f/index.html" % OTHER_TOKEN))           # unknown token
    cases.append(("GET", "/s/%s/f/index.html" % (TOKEN[:-1])))           # malformed (short)
    cases.append(("GET", "/s/%s/f/index.html" % (TOKEN + "x")))          # malformed (long)
    cases.append(("GET", "/s/%s/f/index.html" % (TOKEN[:-1] + "%41")))   # token must not be percent-decoded
    cases.append(("GET", "/s/%s/f/index.html" % (TOKEN[:-1] + "!")))
    cases.append(("GET", "/s/%s" % TOKEN))
    cases.append(("GET", "/s/%s/x" % TOKEN))
    cases.append(("GET", "/s/"))
    cases.append(("GET", "/"))
    cases.append(("GET", "/S/%s/f/index.html" % TOKEN))
    cases.extend(("GET", f(p)) for p in TRAVERSALS)
    cases.extend((m, f("index.html")) for m in ("POST", "PUT", "DELETE", "PATCH", "TRACE", "FOO"))
    for method, path in cases:
        status, headers, body = env.request(method, path)
        assert status == 404, (method, path, status)
        assert body == reference, (method, path)
        assert headers["access-control-allow-origin"] == "*", path
        assert headers["access-control-expose-headers"] == EXPOSE
        assert headers["connection"] == "close"
        assert headers["x-content-type-options"] == "nosniff"
    # raw-socket forms http.client refuses to send
    for raw_path in (f("index.html\\").encode(), f("data\\table.parquet").encode(), f("index.html").encode() + b"\x00",
                     f("").encode() + b"\xe9.txt", f("my file \xc3\xa9.txt").encode("latin-1")):
        out = env.raw(b"GET " + raw_path + b" HTTP/1.1\r\nHost: x\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 404 "), (raw_path, out[:80])
        assert out.endswith(reference)
    # over-long request line (stdlib 414) and HTTP/0.9 are the uniform 404 too
    out = env.raw(b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\nHost: x\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 404 ") and out.endswith(reference)
    out = env.raw(b"GET /s/" + TOKEN.encode() + b"/\r\n\r\n")
    assert b"404" in out.split(b"\r\n", 1)[0] and out.endswith(reference)
    out = env.raw(b"GARBAGE\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 404 ") and out.endswith(reference)
    # a body is never read: the response arrives without the server waiting for it, and the connection closes
    out = env.raw(b"POST " + f("index.html").encode() + b" HTTP/1.1\r\nHost: x\r\nContent-Length: 100000\r\n"
                  b"Expect: 100-continue\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 404 ") and b"100 Continue" not in out and out.endswith(reference)
    # revoked (state removed), expired, not live, lab audience
    env.clock[0] += 86400 * 2
    assert env.request("GET", f("index.html"))[2] == reference
    assert env.request("GET", "/s/%s/" % TOKEN)[0] == 404
    env.clock[0] -= 86400 * 2
    write_state(env.root, env.token_hash, dict(env.state, state="revoked"))
    assert env.request("GET", f("index.html"))[2] == reference
    write_state(env.root, env.token_hash, dict(env.state, audience="lab"))
    status, _, body = env.request("GET", f("index.html"))
    assert status == 404 and body == reference
    assert env.request("GET", "/s/%s/" % TOKEN)[2] == reference
    remove_state(env.root, env.token_hash)
    assert env.request("GET", f("index.html"))[2] == reference


def test_file_missing_on_disk_or_wrong_size_is_404(env):
    reference = _not_found(env)
    (env.root / "shr-1" / "files" / "index.html").write_bytes(b"short")
    assert env.request("GET", f("index.html"))[2] == reference
    (env.root / "shr-1" / "files" / "doc.pdf").unlink()
    assert env.request("GET", f("doc.pdf"))[2] == reference


def test_bad_share_id_in_state_is_404(env):
    reference = _not_found(env)
    for bad in ("..", "by-token", "a/b", "", None, 5):
        write_state(env.root, env.token_hash, dict(env.state, share_id=bad))
        assert env.request("GET", f("index.html"))[2] == reference


def test_symlink_in_bundle_is_not_followed(env):
    reference = _not_found(env)
    target = env.root / "shr-1" / "files" / "index.html"
    target.unlink()
    target.symlink_to("/etc/hostname")
    status, _, body = env.request("GET", f("index.html"))
    assert status == 404 and body == reference
    # an intermediate directory replaced by a symlink is not followed either
    data = env.root / "shr-1" / "files" / "data"
    elsewhere = env.root.parent / "elsewhere"
    data.rename(elsewhere)
    data.symlink_to(elsewhere)
    status, _, body = env.request("GET", f("data/table.parquet"))
    assert status == 404 and body == reference
    # the share directory itself, too
    share_dir = env.root / "shr-1"
    moved = env.root.parent / "moved-share"
    share_dir.rename(moved)
    share_dir.symlink_to(moved)
    assert env.request("GET", f("pic.svg"))[2] == reference


def test_symlinked_state_file_is_not_followed(env):
    reference = _not_found(env)
    state = env.root / "by-token" / env.token_hash / "state.json"
    real = env.root.parent / "real-state.json"
    state.rename(real)
    state.symlink_to(real)
    assert env.request("GET", f("index.html"))[2] == reference


# ----------------------------------------------------------------------------- Range

def test_range_matrix(env):
    size = len(BODY)
    etag = '"%s"' % _sha(BODY)
    p = f("data/table.parquet")
    status, h, body = env.request("GET", p, {"Range": "bytes=0-99"})
    assert status == 206 and body == BODY[:100]
    assert h["content-range"] == "bytes 0-99/%d" % size and h["content-length"] == "100"
    assert h["accept-ranges"] == "bytes"
    status, h, body = env.request("GET", p, {"Range": "bytes=-8"})          # suffix
    assert status == 206 and body == BODY[-8:] and h["content-range"] == "bytes %d-%d/%d" % (size - 8, size - 1, size)
    status, h, body = env.request("GET", p, {"Range": "bytes=-999999"})     # suffix longer than the file
    assert status == 206 and body == BODY and h["content-range"] == "bytes 0-%d/%d" % (size - 1, size)
    status, h, body = env.request("GET", p, {"Range": "bytes=10000-"})      # open-ended
    assert status == 206 and body == BODY[10000:] and h["content-length"] == str(size - 10000)
    status, h, body = env.request("GET", p, {"Range": "bytes=10000-999999"})  # end clamped
    assert status == 206 and body == BODY[10000:]
    status, h, body = env.request("GET", p, {"Range": "bytes=%d-" % size})  # past EOF
    assert status == 416 and h["content-range"] == "bytes */%d" % size
    assert h["access-control-allow-origin"] == "*"
    status, h, body = env.request("GET", p, {"Range": "bytes=-0"})
    assert status == 416
    # multi-range: satisfiable -> full 200; all unsatisfiable -> 416
    status, h, body = env.request("GET", p, {"Range": "bytes=0-1,5-6"})
    assert status == 200 and body == BODY and "content-range" not in h
    status, h, body = env.request("GET", p, {"Range": "bytes=%d-,%d-" % (size, size + 5)})
    assert status == 416 and h["content-range"] == "bytes */%d" % size
    # syntactically invalid ranges are ignored (full 200)
    for bad in ("bytes=5-1", "items=0-1", "bytes=a-b", "bytes=", "bytes=0-1,x"):
        status, h, body = env.request("GET", p, {"Range": bad})
        assert status == 200 and body == BODY, bad
    # If-Range: matching ETag honours Range, mismatched or a date serves the full body
    status, h, body = env.request("GET", p, {"Range": "bytes=0-3", "If-Range": etag})
    assert status == 206 and body == BODY[:4]
    status, h, body = env.request("GET", p, {"Range": "bytes=0-3", "If-Range": '"deadbeef"'})
    assert status == 200 and body == BODY
    status, h, body = env.request("GET", p, {"Range": "bytes=0-3", "If-Range": "W/" + etag})
    assert status == 200 and body == BODY
    status, h, body = env.request("GET", p, {"Range": "bytes=0-3", "If-Range": "Wed, 21 Oct 2015 07:28:00 GMT"})
    assert status == 200 and body == BODY
    # HEAD: same headers, no body
    status, h, body = env.request("HEAD", p)
    assert status == 200 and body == b"" and h["content-length"] == str(size) and h["etag"] == etag
    status, h, body = env.request("HEAD", p, {"Range": "bytes=-8"})
    assert status == 206 and body == b"" and h["content-length"] == "8"
    # zero-byte file
    status, h, body = env.request("GET", f("empty.txt"))
    assert status == 200 and body == b"" and h["content-length"] == "0"
    status, h, body = env.request("GET", f("empty.txt"), {"Range": "bytes=0-"})
    assert status == 416 and h["content-range"] == "bytes */0"
    status, h, body = env.request("GET", f("empty.txt"), {"Range": "bytes=-5"})
    assert status == 416


def test_keep_alive_survives_several_range_requests(env):
    conn = http.client.HTTPConnection("127.0.0.1", env.port, timeout=10)
    try:
        for start in (0, 100, 5000):
            conn.request("GET", f("data/table.parquet"), headers={"Range": "bytes=%d-%d" % (start, start + 9)})
            resp = conn.getresponse()
            assert resp.status == 206 and resp.read() == BODY[start:start + 10]
    finally:
        conn.close()


def test_large_file_streams_in_chunks_and_tolerates_disconnect(tmp_path):
    big = os.urandom(3 * 1024 * 1024 + 17)
    e = Env(tmp_path, files={"index.html": b"x", "big.bin": big})
    try:
        status, h, body = e.request("GET", f("big.bin"))
        assert status == 200 and body == big and h["content-length"] == str(len(big))
        # a client that disconnects mid-body does not take the server down
        with socket.create_connection(("127.0.0.1", e.port), timeout=10) as s:
            s.sendall(b"GET " + f("big.bin").encode() + b" HTTP/1.1\r\nHost: x\r\n\r\n")
            s.recv(1024)
        status, _, body = e.request("GET", f("big.bin"), {"Range": "bytes=-17"})
        assert status == 206 and body == big[-17:]
    finally:
        e.close()


# ----------------------------------------------------------------------------- headers

def _assert_cors(h):
    assert h["access-control-allow-origin"] == "*"
    assert h["access-control-expose-headers"] == EXPOSE


def test_headers_exact(env):
    status, h, _ = env.request("GET", f("index.html"))
    assert status == 200
    assert h["content-security-policy"] == CSP
    assert h["x-content-type-options"] == "nosniff"
    assert h["cache-control"] == "private, no-cache"
    assert h["referrer-policy"] == "no-referrer"
    assert h["x-robots-tag"] == "noindex"
    assert h["etag"] == '"%s"' % _sha(FILES["index.html"])
    assert h["accept-ranges"] == "bytes"
    assert "content-disposition" not in h
    assert h["server"] == "herdr-share"
    _assert_cors(h)
    for name in ("pic.svg", "data/table.parquet", "empty.txt"):
        status, h, _ = env.request("GET", f(name))
        assert status == 200 and h["content-security-policy"] == CSP, name
    status, h, _ = env.request("GET", f("pic.svg"))
    assert h["content-type"] == "image/svg+xml"
    status, h, _ = env.request("GET", f("doc.pdf"))
    assert status == 200
    assert h["content-type"] == "application/pdf"
    assert h["content-disposition"] == "inline"
    assert h["x-content-type-options"] == "nosniff"
    assert h["content-security-policy"] == "frame-ancestors 'none'"
    _assert_cors(h)
    status, h, _ = env.request("GET", f("data/table.parquet"), {"Range": "bytes=0-1"})
    assert status == 206 and h["content-security-policy"] == CSP
    _assert_cors(h)
    status, h, _ = env.request("GET", f("index.html"), {"If-None-Match": '"%s"' % _sha(FILES["index.html"])})
    assert status == 304
    _assert_cors(h)
    status, h, _ = env.request("GET", f("missing"))
    assert status == 404
    _assert_cors(h)
    # OPTIONS preflight
    for path in (f("data/table.parquet"), f("anything"), "/s/%s/" % TOKEN):
        status, h, body = env.request("OPTIONS", path, {"Origin": "null", "Access-Control-Request-Method": "GET",
                                                        "Access-Control-Request-Headers": "range"})
        assert status == 204 and body == b""
        assert h["access-control-allow-methods"] == "GET, HEAD, OPTIONS"
        assert h["access-control-allow-headers"] == "Range, If-Range, If-None-Match, If-Match"
        assert h["access-control-max-age"] == "600"
        _assert_cors(h)
    # preflight outside /s/ is the uniform 404
    status, h, body = env.request("OPTIONS", "/elsewhere")
    assert status == 404


FETCH_DIRECTIVES = ("default-src", "script-src", "style-src", "img-src", "font-src", "connect-src", "worker-src")


def _directives(policy):
    out = {}
    for part in policy.split(";"):
        name, *sources = part.split()
        out[name] = sources
    return out


def _assert_token_scoped(policy, origin, token):
    """Every fetch directive is the plain one plus exactly ``<origin>/s/<token>/``; no other host anywhere."""
    plain, scoped = _directives(CSP), _directives(policy)
    assert set(scoped) == set(plain)
    source = f"{origin}/s/{token}/"
    for name, sources in scoped.items():
        if name in FETCH_DIRECTIVES:
            assert sources[0] == "'self'" and sources[1] == source and sources[2:] == plain[name][1:], name
        else:
            assert sources == plain[name], name
    hosts = [src for srcs in scoped.values() for src in srcs if "://" in src]
    assert set(hosts) == {source}


def test_csp_names_only_its_own_token_prefix(tmp_path):
    e = Env(tmp_path, config_extra={"public_url": "https://Share.Example/"})
    try:
        write_state(e.root, _sha(OTHER_TOKEN.encode()), e.state)          # a second link to the same bundle
        for path, headers in ((f("index.html"), None), (f("data/table.parquet"), {"Range": "bytes=-4"}),
                              (f("pic.svg"), None), (f("empty.txt"), None)):
            status, h, _ = e.request("GET", path, headers)
            assert status in (200, 206), path
            _assert_token_scoped(h["content-security-policy"], "https://share.example", TOKEN)
            assert OTHER_TOKEN not in h["content-security-policy"]
        status, h, _ = e.request("GET", f"/s/{OTHER_TOKEN}/f/index.html")
        assert status == 200
        _assert_token_scoped(h["content-security-policy"], "https://share.example", OTHER_TOKEN)
        assert TOKEN not in h["content-security-policy"]
        status, h, _ = e.request("HEAD", f("index.html"), {"Host": "evil.example"})   # Host is never trusted
        assert "evil" not in h["content-security-policy"]
        _assert_token_scoped(h["content-security-policy"], "https://share.example", TOKEN)
        # PDFs keep their own minimal policy; the redirect, the preflight and every 404 carry no token source
        status, h, _ = e.request("GET", f("doc.pdf"))
        assert status == 200 and h["content-security-policy"] == "frame-ancestors 'none'"
        status, h, _ = e.request("GET", f"/s/{TOKEN}/")
        assert status == 302 and "content-security-policy" not in h
        status, h, _ = e.request("OPTIONS", f("index.html"))
        assert status == 204 and "content-security-policy" not in h
        status, h, _ = e.request("GET", f("missing"))
        assert status == 404 and h["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'; sandbox"
    finally:
        e.close()


@pytest.mark.parametrize("public_url", [None, "", "http://share.example", "https://share.example/base/",
                                        "https://share.example/?q", "https://user@share.example", "not a url", 42])
def test_csp_without_a_bare_https_public_origin_keeps_self_only(tmp_path, public_url):
    e = Env(tmp_path, config_extra={"public_url": public_url})
    try:
        status, h, _ = e.request("GET", f("index.html"), {"Host": "evil.example"})
        assert status == 200 and h["content-security-policy"] == CSP
    finally:
        e.close()


def test_sandbox_csp_refuses_a_malformed_token():
    assert share_public.sandbox_csp("https://share.example", "short") == CSP
    assert share_public.sandbox_csp("https://share.example", TOKEN[:-1] + ";") == CSP
    assert share_public.sandbox_csp("https://share.example", None) == CSP
    assert share_public.sandbox_csp(None, TOKEN) == CSP
    _assert_token_scoped(share_public.sandbox_csp("https://share.example", TOKEN), "https://share.example", TOKEN)


def test_etag_304(env):
    etag = '"%s"' % _sha(BODY)
    p = f("data/table.parquet")
    for value in (etag, "W/" + etag, '"x", ' + etag, "*"):
        status, h, body = env.request("GET", p, {"If-None-Match": value})
        assert status == 304 and body == b"", value
        assert h["etag"] == etag
    status, h, body = env.request("HEAD", p, {"If-None-Match": etag})
    assert status == 304
    status, h, body = env.request("GET", p, {"If-None-Match": '"other"'})
    assert status == 200 and body == BODY
    # If-None-Match wins over Range
    status, h, body = env.request("GET", p, {"If-None-Match": etag, "Range": "bytes=0-1"})
    assert status == 304
    # If-Match: mismatch -> 412, match -> normal
    status, h, body = env.request("GET", p, {"If-Match": '"other"'})
    assert status == 412
    _assert_cors(h)
    status, h, body = env.request("GET", p, {"If-Match": etag, "Range": "bytes=0-1"})
    assert status == 206 and body == BODY[:2]


# ----------------------------------------------------------------------------- revoke / expiry

def test_revoke_takes_effect_on_next_request(env):
    conn = http.client.HTTPConnection("127.0.0.1", env.port, timeout=10)
    try:
        conn.request("GET", f("index.html"))
        resp = conn.getresponse()
        assert resp.status == 200 and resp.read() == FILES["index.html"]
        (env.root / "by-token" / env.token_hash / "state.json").unlink()
        conn.request("GET", f("index.html"))      # same keep-alive connection
        resp = conn.getresponse()
        assert resp.status == 404
        resp.read()
    finally:
        conn.close()
    # republished with a changed file list: the cache re-stats and sees the change
    write_state(env.root, env.token_hash, dict(env.state, files=[x for x in env.state["files"] if x["name"] != "pic.svg"]))
    assert env.request("GET", f("index.html"))[0] == 200
    assert env.request("GET", f("pic.svg"))[0] == 404


def test_expiry_uses_injected_clock(env):
    expires = env.clock[0] + 86400
    assert env.request("GET", f("index.html"))[0] == 200
    env.clock[0] = expires - 1
    assert env.request("GET", f("index.html"))[0] == 200
    env.clock[0] = expires
    assert env.request("GET", f("index.html"))[0] == 404
    env.clock[0] = expires - 1
    assert env.request("GET", f("index.html"))[0] == 200


def test_load_state_rules(env):
    now = env.clock[0]
    assert load_state(env.root, TOKEN, now)["share_id"] == "shr-1"
    assert load_state(env.root, TOKEN, now + 86400) is None
    assert load_state(env.root, TOKEN + "\n", now) is None
    assert load_state(env.root, TOKEN[:-1], now) is None
    assert load_state(env.root, "../" + TOKEN[3:], now) is None
    assert load_state(env.root, OTHER_TOKEN, now) is None
    for patch in ({"state": "pending"}, {"expires_at": "not a date"}, {"expires_at": None}, {"files": "x"},
                  {"entry": 5}):
        write_state(env.root, env.token_hash, dict(env.state, **patch))
        assert load_state(env.root, TOKEN, now) is None, patch
    # lab shares may have no expiry
    write_state(env.root, env.token_hash, dict(env.state, audience="lab", expires_at=None))
    assert load_state(env.root, TOKEN, now)["audience"] == "lab"
    # tz-naive expiry is read as UTC
    naive = datetime.fromtimestamp(now + 10, UTC).replace(tzinfo=None).isoformat()
    write_state(env.root, env.token_hash, dict(env.state, expires_at=naive))
    assert load_state(env.root, TOKEN, now) is not None
    assert load_state(env.root, TOKEN, now + 10) is None
    (env.root / "by-token" / env.token_hash / "state.json").write_text("{not json")
    assert load_state(env.root, TOKEN, now) is None


# ----------------------------------------------------------------------------- events

def test_events_appended_not_tokens(env, capfd):
    env.request("GET", "/s/%s/" % TOKEN)
    env.request("GET", f("index.html"))
    env.request("GET", f("data/table.parquet"), {"Range": "bytes=0-1"})
    env.request("GET", f("missing.html"))
    env.request("GET", f("missing.html%00"))
    env.request("GET", "/s/%s/f/index.html" % OTHER_TOKEN)
    lines = env.event_lines()
    assert lines
    parsed = [ShareStore._parse_line(raw) for raw in lines]
    assert all(p is not None for p in parsed), lines
    kinds = [p[1] for p in parsed]
    assert "opened" in kinds and "denied" in kinds
    for share_id, kind, actor, at, detail in parsed:
        assert share_id == "shr-1" and actor and at
    opened = [json.loads(raw) for raw in lines if json.loads(raw)["kind"] == "opened"]
    assert {"share_id", "kind", "actor", "at"} <= set(opened[0])
    denied = [json.loads(raw) for raw in lines if json.loads(raw)["kind"] == "denied"]
    assert denied[0]["detail"]["reason"] == "not_in_bundle"
    assert "missing" not in json.dumps(denied)  # attacker-chosen paths are never recorded
    text = env.events.read_bytes()
    captured = capfd.readouterr()
    for secret in (TOKEN, OTHER_TOKEN, env.token_hash, _sha(OTHER_TOKEN.encode())):
        assert secret.encode() not in text
        assert secret not in captured.out and secret not in captured.err
    # denied events are rate-limited per reason
    before = len(env.event_lines())
    for _ in range(200):
        env.request("GET", f("nope.html"))
    denied_now = len(env.event_lines()) - before
    assert 0 < denied_now <= share_public.DENIED_PER_MINUTE
    # a minute later the limiter has room again
    env.clock[0] += 61
    env.request("GET", f("nope.html"))
    assert len(env.event_lines()) == before + denied_now + 1


def test_events_file_cap_drops_and_counts(env, monkeypatch):
    monkeypatch.setattr(share_public, "EVENTS_MAX_BYTES", 600)
    for i in range(20):
        env.clock[0] += 1
        env.request("GET", f("index.html"))
    size = env.events.stat().st_size
    assert 0 < size <= 600
    assert env.server.events.dropped > 0
    assert all(ShareStore._parse_line(raw) for raw in env.event_lines())


def test_errors_never_log_the_token(env, capfd, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom " + TOKEN)
    monkeypatch.setattr(share_public, "_check_state", boom)  # an internal failure while handling a request
    status, h, body = env.request("GET", "/s/%s/" % TOKEN)
    assert status == 404 and h["access-control-allow-origin"] == "*" and body == share_public.NOT_FOUND_BODY
    monkeypatch.undo()
    env.server.events.append = boom  # an event-write failure still serves the file
    status, h, body = env.request("GET", f("index.html"))
    assert status == 200 and body == FILES["index.html"]
    out = env.raw(b"GET /s/" + TOKEN.encode() + b"/ HTTP/1.1\r\n" + b"X-Many: y\r\n" * 120 + b"\r\n")  # stdlib 431
    assert out.startswith(b"HTTP/1.1 404 ") and out.endswith(share_public.NOT_FOUND_BODY)
    captured = capfd.readouterr()
    assert TOKEN not in captured.out and TOKEN not in captured.err


def test_state_index_is_built_once_per_state_file(env, monkeypatch):
    calls = []
    real = share_public._build_index

    def counting(files):
        calls.append(1)
        return real(files)
    monkeypatch.setattr(share_public, "_build_index", counting)
    share_public._state_cache.clear()
    for _ in range(5):
        status, _, _ = env.request("GET", f("data/table.parquet"), {"Range": "bytes=0-9"})
        assert status == 206
    assert len(calls) == 1
    # a republish (new inode) is seen by the next request and rebuilds once
    write_state(env.root, env.token_hash, dict(env.state, files=[x for x in env.state["files"] if x["name"] != "pic.svg"]))
    assert env.request("GET", f("pic.svg"))[0] == 404
    assert env.request("GET", f("index.html"))[0] == 200
    assert len(calls) == 2
    # the per-request state is a copy: mutating it does not touch the cache
    state = load_state(env.root, TOKEN, env.clock[0])
    state["entry"] = "mutated"
    assert load_state(env.root, TOKEN, env.clock[0])["entry"] == "index.html"


# ----------------------------------------------------------------------------- fix round 1

SMUGGLE_SECOND = b"GET /s/%s/f/index.html HTTP/1.1\r\nHost: x\r\n\r\n" % TOKEN.encode()


@pytest.mark.parametrize("framing", [
    b"Content-Length: 0\r\nContent-Length: %d\r\n\r\n" % len(SMUGGLE_SECOND),
    b"Transfer-Encoding:\r\nTransfer-Encoding: chunked\r\n\r\n",
    b"Transfer-Encoding: chunked\r\n\r\n",
    b"Content-Length: 5\r\n\r\nhello",
    b"Content-Length: %d\r\nExpect: 100-continue\r\n\r\n" % len(SMUGGLE_SECOND),
    b"Content-Length: 00\r\n\r\n",
    b"Content-Length:  0 \r\nContent-Length: 0\r\n\r\n",
])
def test_body_framing_never_yields_a_second_request(env, framing):
    first = b"GET " + f("index.html").encode() + b" HTTP/1.1\r\nHost: x\r\n" + framing
    out = env.raw(first + SMUGGLE_SECOND)   # raw() reads until the server closes
    assert out.count(b"HTTP/1.1 ") == 1, out[:300]
    assert out.startswith(b"HTTP/1.1 200 ")
    assert b"\r\nConnection: close\r\n" in out.split(b"\r\n\r\n", 1)[0] + b"\r\n"
    assert b"100 Continue" not in out


def test_single_zero_content_length_keeps_alive(env):
    out = env.raw(b"GET " + f("index.html").encode() + b" HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
                  + SMUGGLE_SECOND.replace(b"\r\n\r\n", b"\r\nConnection: close\r\n\r\n"))
    assert out.count(b"HTTP/1.1 200 ") == 2


def test_events_file_is_world_readable_despite_umask(env):
    old = os.umask(0o027)
    try:
        env.request("GET", f("index.html"))
    finally:
        os.umask(old)
    assert env.events.exists()
    assert env.events.stat().st_mode & 0o777 == 0o644


def test_existing_0640_events_file_is_made_0644(env):
    env.events.write_bytes(b"")
    os.chmod(env.events, 0o640)
    env.request("GET", f("index.html"))
    assert env.events.stat().st_mode & 0o777 == 0o644
    assert env.event_lines()


def test_denied_budget_is_per_share(env):
    other = {"index.html": b"<p>b</p>"}
    build_bundle(env.root / "shr-2", [(n, _sha(b), len(b), [b]) for n, b in other.items()],
                 entry=None, limits=BundleLimits(1 << 20, 10))
    write_state(env.root, _sha(OTHER_TOKEN.encode()), dict(
        env.state, share_id="shr-2", entry="index.html",
        files=[{"name": "index.html", "sha256": _sha(other["index.html"]), "size": len(other["index.html"]),
                "content_type": "text/html; charset=utf-8"}]))
    for _ in range(share_public.DENIED_PER_MINUTE + 10):
        env.request("GET", f("%2e%2e"))            # share A floods bad_path
    env.request("GET", "/s/%s/f/%%2e%%2e" % OTHER_TOKEN)
    denied = [json.loads(raw) for raw in env.event_lines() if json.loads(raw)["kind"] == "denied"]
    assert sum(1 for d in denied if d["share_id"] == "shr-1") == share_public.DENIED_PER_MINUTE
    assert [d["detail"]["reason"] for d in denied if d["share_id"] == "shr-2"] == ["bad_path"]


@pytest.mark.parametrize("defect", [b"Broken header line\r\n", b" folded-continuation-first\r\n", b"X:\r\n:no-name\r\n"])
def test_malformed_header_lines_close_the_connection(env, defect):
    """A malformed header line makes the stdlib parser stop early, hiding a later Content-Length from the body check:
    the request still gets one response and the connection closes, so the hidden body is never parsed as a request."""
    first = (b"GET " + f("index.html").encode() + b" HTTP/1.1\r\nHost: x\r\n" + defect
             + b"Content-Length: %d\r\n\r\n" % len(SMUGGLE_SECOND))
    out = env.raw(first + SMUGGLE_SECOND)
    assert out.count(b"HTTP/1.1 ") == 1, out[:300]
    assert b"\r\nConnection: close\r\n" in out.split(b"\r\n\r\n", 1)[0] + b"\r\n"
