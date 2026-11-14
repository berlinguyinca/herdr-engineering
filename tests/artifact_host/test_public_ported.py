"""Ported from the control plane's share-public tests: wire framing, exact headers, token-scoped CSP, referrer policy,
ranges and streaming. Real server, real sockets; nothing is mocked."""

from __future__ import annotations

import hashlib
import http.client
import os
import socket
from pathlib import Path

import pytest

from herdr_artifact_host.share_http import CSP
from tests.artifact_host.seed import Host, running_host

EXPOSE = "Content-Range, Content-Length, Accept-Ranges, ETag"
BODY = bytes(range(256)) * 80
FILES = {
    "index.html": b"<!doctype html><html><head><title>t</title></head><body><p>hello</p></body></html>",
    "data/table.parquet": BODY,
    "pic.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
    "doc.pdf": b"%PDF-1.4\n%fake\n",
    "empty.txt": b"",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw_exchange(host: Host, payload: bytes) -> bytes:
    """Send ``payload`` and read until the server closes (the client keeps its write side open, as a real one does)."""
    with socket.create_connection(("127.0.0.1", host.port), timeout=10) as sock:
        sock.sendall(payload)
        chunks = []
        try:
            while data := sock.recv(65536):
                chunks.append(data)
        except TimeoutError:
            pass
    return b"".join(chunks)


def share(host: Host):
    return host.seed_share(FILES)


def assert_cors(h) -> None:
    assert h.header("Access-Control-Allow-Origin") == "*"
    assert h.header("Access-Control-Expose-Headers") == EXPOSE


# ----------------------------------------------------------------------------- headers


def test_headers_exact(host: Host) -> None:
    s = share(host)
    base = f"/s/{s.token}/f/"
    r = host.get(base + "index.html")
    assert r.status == 200
    assert r.header("X-Content-Type-Options") == "nosniff"
    assert r.header("Cache-Control") == "private, no-cache"
    assert r.header("Referrer-Policy") == "no-referrer"
    assert r.header("X-Robots-Tag") == "noindex"
    assert r.header("Accept-Ranges") is None  # the injected HTML is a whole-body 200, never ranged
    assert host.get(base + "data/table.parquet").header("Accept-Ranges") == "bytes"
    assert r.header("Content-Disposition") is None
    assert r.header("Server") == "herdr-share"
    assert r.header("ETag") is not None and r.header("ETag").startswith('"')
    assert_cors(r)
    csp = r.header("Content-Security-Policy") or ""
    assert csp.startswith("sandbox allow-scripts allow-downloads; default-src 'self'")
    for name in ("pic.svg", "data/table.parquet", "empty.txt"):
        got = host.get(base + name)
        assert got.status == 200 and "sandbox" in (got.header("Content-Security-Policy") or ""), (
            name
        )
    assert host.get(base + "pic.svg").header("Content-Type") == "image/svg+xml"
    pdf = host.get(base + "doc.pdf")
    assert pdf.status == 200 and pdf.header("Content-Type") == "application/pdf"
    assert pdf.header("Content-Disposition") == "inline"
    assert pdf.header("Content-Security-Policy") == "frame-ancestors 'none'"
    assert_cors(pdf)
    part = host.get(base + "data/table.parquet", {"Range": "bytes=0-1"})
    assert part.status == 206 and "sandbox" in (part.header("Content-Security-Policy") or "")
    assert_cors(part)
    etag = host.get(base + "data/table.parquet").header("ETag")
    cond = host.get(
        base + "index.html", {"If-None-Match": host.get(base + "index.html").header("ETag") or ""}
    )
    assert cond.status == 304
    assert_cors(cond)
    assert etag
    missing = host.get(base + "missing")
    assert missing.status == 404
    assert_cors(missing)
    for path in (base + "data/table.parquet", base + "anything", f"/s/{s.token}/"):
        pre = host.request(
            "OPTIONS",
            path,
            {
                "Origin": "null",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "range",
            },
        )
        assert pre.status == 204 and pre.body == b""
        assert pre.header("Access-Control-Allow-Methods") == "GET, HEAD, OPTIONS"
        assert (
            pre.header("Access-Control-Allow-Headers") == "Range, If-Range, If-None-Match, If-Match"
        )
        assert pre.header("Access-Control-Max-Age") == "600"
        assert_cors(pre)
    assert host.request("OPTIONS", "/elsewhere").status == 404


FETCH = (
    "default-src",
    "script-src",
    "style-src",
    "img-src",
    "font-src",
    "connect-src",
    "worker-src",
)


def directives(policy: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in policy.split(";"):
        name, *sources = part.split()
        out[name] = sources
    return out


def assert_token_scoped(policy: str, origin: str, token: str) -> None:
    """Every fetch directive is the plain one plus exactly ``<origin>/s/<token>/``; no other host anywhere."""
    plain, scoped = directives(CSP), directives(policy)
    assert set(scoped) == set(plain)
    source = f"{origin}/s/{token}/"
    for name, sources in scoped.items():
        if name in FETCH:
            assert (
                sources[0] == "'self'" and sources[1] == source and sources[2:] == plain[name][1:]
            ), name
        else:
            assert sources == plain[name], name
    hosts = [src for srcs in scoped.values() for src in srcs if "://" in src]
    assert set(hosts) == {source}


def test_csp_names_only_its_own_token_prefix(migrated_dsn: str, tmp_path: Path) -> None:
    with running_host(migrated_dsn, tmp_path, {"public_url": "https://Share.Example/"}) as h:
        a, b = share(h), share(h)
        for path, headers in (
            ("index.html", None),
            ("data/table.parquet", {"Range": "bytes=-4"}),
            ("pic.svg", None),
            ("empty.txt", None),
        ):
            r = h.get(f"/s/{a.token}/f/{path}", headers)
            assert r.status in (200, 206), path
            policy = r.header("Content-Security-Policy") or ""
            assert_token_scoped(policy, "https://share.example", a.token)
            assert b.token not in policy
        r = h.get(f"/s/{b.token}/f/index.html")
        assert_token_scoped(
            r.header("Content-Security-Policy") or "", "https://share.example", b.token
        )
        assert a.token not in (r.header("Content-Security-Policy") or "")
        head = h.request(
            "HEAD", f"/s/{a.token}/f/index.html", {"Host": "evil.example"}
        )  # Host is never trusted
        assert "evil" not in (head.header("Content-Security-Policy") or "")
        assert_token_scoped(
            head.header("Content-Security-Policy") or "", "https://share.example", a.token
        )
        pdf = h.get(f"/s/{a.token}/f/doc.pdf")
        assert pdf.header("Content-Security-Policy") == "frame-ancestors 'none'"
        redirect = h.get(f"/s/{a.token}/")
        assert redirect.status == 302 and redirect.header("Content-Security-Policy") is None
        pre = h.request("OPTIONS", f"/s/{a.token}/f/index.html")
        assert pre.status == 204 and pre.header("Content-Security-Policy") is None
        miss = h.get(f"/s/{a.token}/f/missing")
        assert miss.status == 404
        assert (
            miss.header("Content-Security-Policy")
            == "default-src 'none'; frame-ancestors 'none'; sandbox"
        )


@pytest.mark.parametrize(
    "public_url",
    [
        "",
        "http://share.example",
        "https://share.example/base/",
        "https://share.example/?q",
        "https://user@share.example",
        "not a url",
    ],
)
def test_csp_without_a_bare_https_public_origin_keeps_self_only(
    migrated_dsn: str, tmp_path: Path, public_url: str
) -> None:
    with running_host(migrated_dsn, tmp_path, {"public_url": public_url}) as h:
        s = share(h)
        r = h.get(f"/s/{s.token}/f/index.html", {"Host": "evil.example"})
        assert r.status == 200 and r.header("Content-Security-Policy") == CSP


def test_every_response_forces_referrer_policy_no_referrer(host: Host) -> None:
    s = share(host)
    base = f"/s/{s.token}/f/"
    etag = host.get(base + "index.html").header("ETag") or ""
    requests = [
        ("GET", base + "index.html", {}),
        ("HEAD", base + "index.html", {}),
        ("GET", base + "data/table.parquet", {"Range": "bytes=0-9"}),
        ("GET", base + "data/table.parquet", {"Range": "bytes=999999-"}),
        ("GET", base + "index.html", {"If-None-Match": etag}),
        ("GET", base + "index.html", {"If-Match": '"other"'}),
        (
            "OPTIONS",
            base + "index.html",
            {"Origin": "https://x.example", "Access-Control-Request-Method": "GET"},
        ),
        ("GET", base + "missing.html", {}),
        ("GET", "/nothing-here", {}),
        ("GET", "/healthz", {}),
        ("GET", f"/s/{s.token}/", {}),
    ]
    seen = set()
    for method, path, headers in requests:
        r = host.request(method, path, headers)
        seen.add(r.status)
        assert r.header("Referrer-Policy") == "no-referrer", (method, path, r.status)
    assert {200, 204, 206, 302, 304, 404, 412, 416} <= seen


# ----------------------------------------------------------------------------- ranges and streaming


def test_range_matrix(host: Host) -> None:
    s = share(host)
    p = f"/s/{s.token}/f/data/table.parquet"
    size = len(BODY)
    etag = host.get(p).header("ETag") or ""
    r = host.get(p, {"Range": "bytes=0-99"})
    assert r.status == 206 and r.body == BODY[:100]
    assert r.header("Content-Range") == f"bytes 0-99/{size}" and r.header("Content-Length") == "100"
    r = host.get(p, {"Range": "bytes=-8"})
    assert r.status == 206 and r.body == BODY[-8:]
    assert r.header("Content-Range") == f"bytes {size - 8}-{size - 1}/{size}"
    r = host.get(p, {"Range": "bytes=-999999"})
    assert r.status == 206 and r.body == BODY
    r = host.get(p, {"Range": "bytes=10000-"})
    assert r.status == 206 and r.body == BODY[10000:]
    r = host.get(p, {"Range": "bytes=10000-999999"})
    assert r.status == 206 and r.body == BODY[10000:]
    r = host.get(p, {"Range": f"bytes={size}-"})
    assert r.status == 416 and r.header("Content-Range") == f"bytes */{size}"
    assert r.header("Access-Control-Allow-Origin") == "*"
    assert host.get(p, {"Range": "bytes=-0"}).status == 416
    r = host.get(p, {"Range": "bytes=0-1,5-6"})
    assert r.status == 200 and r.body == BODY and r.header("Content-Range") is None
    assert host.get(p, {"Range": f"bytes={size}-,{size + 5}-"}).status == 416
    for bad in ("bytes=5-1", "items=0-1", "bytes=a-b", "bytes=", "bytes=0-1,x"):
        r = host.get(p, {"Range": bad})
        assert r.status == 200 and r.body == BODY, bad
    r = host.get(p, {"Range": "bytes=0-3", "If-Range": etag})
    assert r.status == 206 and r.body == BODY[:4]
    for if_range in ('"deadbeef"', "W/" + etag, "Wed, 21 Oct 2015 07:28:00 GMT"):
        r = host.get(p, {"Range": "bytes=0-3", "If-Range": if_range})
        assert r.status == 200 and r.body == BODY, if_range
    r = host.request("HEAD", p)
    assert r.status == 200 and r.body == b"" and r.header("Content-Length") == str(size)
    r = host.request("HEAD", p, {"Range": "bytes=-8"})
    assert r.status == 206 and r.body == b"" and r.header("Content-Length") == "8"
    e = host.get(f"/s/{s.token}/f/empty.txt")
    assert e.status == 200 and e.body == b"" and e.header("Content-Length") == "0"
    assert host.get(f"/s/{s.token}/f/empty.txt", {"Range": "bytes=0-"}).status == 416
    assert host.get(f"/s/{s.token}/f/empty.txt", {"Range": "bytes=-5"}).status == 416


def test_keep_alive_survives_several_range_requests(host: Host) -> None:
    s = share(host)
    conn = http.client.HTTPConnection("127.0.0.1", host.port, timeout=10)
    try:
        for start in (0, 100, 5000):
            conn.request(
                "GET",
                f"/s/{s.token}/f/data/table.parquet",
                headers={"Range": f"bytes={start}-{start + 9}"},
            )
            resp = conn.getresponse()
            assert resp.status == 206 and resp.read() == BODY[start : start + 10]
    finally:
        conn.close()


def test_a_large_file_streams_and_a_vanished_client_does_not_hurt_the_server(host: Host) -> None:
    big = os.urandom(3 * 1024 * 1024 + 17)
    s = host.seed_share({"index.html": b"x", "big.bin": big})
    url = f"/s/{s.token}/f/big.bin"
    r = host.get(url)
    assert r.status == 200 and r.body == big and r.header("Content-Length") == str(len(big))
    with socket.create_connection(("127.0.0.1", host.port), timeout=10) as sock:
        sock.sendall(b"GET " + url.encode() + b" HTTP/1.1\r\nHost: x\r\n\r\n")
        sock.recv(1024)
    r = host.get(url, {"Range": "bytes=-17"})
    assert r.status == 206 and r.body == big[-17:]


# ----------------------------------------------------------------------------- wire framing

SECOND = b"GET /s/%s/f/index.html HTTP/1.1\r\nHost: x\r\n\r\n"


@pytest.mark.parametrize("n", range(7))
def test_body_framing_never_yields_a_second_request(host: Host, n: int) -> None:
    s = share(host)
    second = SECOND % s.token.encode()
    framings = [
        b"Content-Length: 0\r\nContent-Length: %d\r\n\r\n" % len(second),
        b"Transfer-Encoding:\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"Transfer-Encoding: chunked\r\n\r\n",
        b"Content-Length: 5\r\n\r\nhello",
        b"Content-Length: %d\r\nExpect: 100-continue\r\n\r\n" % len(second),
        b"Content-Length: 00\r\n\r\n",
        b"Content-Length:  0 \r\nContent-Length: 0\r\n\r\n",
    ]
    first = b"GET /s/" + s.token.encode() + b"/f/index.html HTTP/1.1\r\nHost: x\r\n" + framings[n]
    out = raw_exchange(host, first + second)
    assert out.count(b"HTTP/1.") <= 1, out[:300]
    assert b"100 Continue" not in out
    assert b"\r\nConnection: close\r\n" in out.split(b"\r\n\r\n", 1)[0] + b"\r\n"
    assert out.startswith((b"HTTP/1.1 200 ", b"HTTP/1.1 404 ", b"HTTP/1.0 404 "))


def test_a_single_zero_content_length_keeps_alive(host: Host) -> None:
    s = share(host)
    path = f"/s/{s.token}/f/index.html".encode()
    out = raw_exchange(
        host,
        b"GET " + path + b" HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
        b"GET " + path + b" HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
    )
    assert out.count(b"HTTP/1.1 200 ") == 2


@pytest.mark.parametrize(
    "defect", [b"Broken header line\r\n", b" folded-continuation-first\r\n", b"X:\r\n:no-name\r\n"]
)
def test_malformed_header_lines_close_the_connection(host: Host, defect: bytes) -> None:
    """A malformed header line must not hide a later Content-Length from the body check: at most one response and
    the connection closes, so the hidden body is never parsed as a request."""
    s = share(host)
    second = SECOND % s.token.encode()
    first = (
        b"GET /s/"
        + s.token.encode()
        + b"/f/index.html HTTP/1.1\r\nHost: x\r\n"
        + defect
        + b"Content-Length: %d\r\n\r\n" % len(second)
    )
    out = raw_exchange(host, first + second)
    assert out.count(b"HTTP/1.") <= 1, out[:300]
    assert b"\r\nConnection: close\r\n" in out.split(b"\r\n\r\n", 1)[0] + b"\r\n"
