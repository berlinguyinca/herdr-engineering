"""Cross-cutting security properties of the public surface: uniform failures on the wire, quiet logs, static assets."""

from __future__ import annotations

import logging
import socket

import pytest

from herdr_artifact_host.roles import Role, routes_for
from tests.artifact_host.seed import Host, access, ident
from tests.artifact_host.test_public_lab import lab, landing_session, sign_in


def raw_exchange(host: Host, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", host.port), timeout=10) as sock:
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while data := sock.recv(65536):
            chunks.append(data)
    return b"".join(chunks)


def normalise(wire: bytes) -> bytes:
    """Drop the Date header only.  The status-line version is NOT normalised: it is pinned exactly by
    test_public_hardening.test_the_status_line_version_set_is_exactly_documented."""
    return b"\r\n".join(
        line for line in wire.split(b"\r\n") if not line.lower().startswith(b"date:")
    )


def test_protocol_level_errors_match_the_uniform_404(host: Host) -> None:
    ref = normalise(raw_exchange(host, b"GET /nothing HTTP/1.1\r\nHost: x\r\n\r\n"))
    assert ref.startswith(b"HTTP/1.1 404") and ref.endswith(b"Not found\n")
    body = ref.split(b"\r\n", 1)[1]
    for bad in (
        b"GET /s/x HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        b"GARBAGE\r\n\r\n",
        b"GET /\x00 HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET /s/" + b"a" * 70000 + b" HTTP/1.1\r\nHost: x\r\n\r\n",
    ):
        wire = normalise(raw_exchange(host, bad))
        assert wire.startswith((b"HTTP/1.1 404", b"HTTP/1.0 404")), wire[:120]
        assert wire.split(b"\r\n", 1)[1] == body


def test_routes_are_public_only() -> None:
    public, internal = routes_for(Role.PUBLIC), routes_for(Role.INTERNAL)
    assert not public & internal
    assert "POST /s/{token}/session" in public and "GET /s/callback" in public
    assert not any("/s/" in r for r in internal)


def test_logs_never_carry_tokens_sessions_or_csrf(
    host: Host, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    s = lab(host)
    r = sign_in(host, s)
    sess = landing_session(r)
    host.get(f"/s/{s.token}/k/{sess}/f/missing.txt")
    host.get(f"/s/{s.token}/k/{sess}x/f/notes.txt")
    sign_in(host, s, idt=ident(email="mallory@evil.example"))
    pub = host.seed_share()
    host.get(f"/s/{pub.token}/f/nope")
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    for secret in (s.token, sess, pub.token, access(), "eyJ"):
        assert secret not in text
    for e in host.rows("SELECT detail::text AS d FROM share_events"):
        for secret in (s.token, sess, pub.token, "eyJ"):
            assert secret not in e["d"]


def test_denied_events_are_rate_limited(host: Host) -> None:
    s = host.seed_share()
    for _ in range(40):
        host.get(f"/s/{s.token}/f/nope")
    assert len(host.events(s.share_id, "denied")) <= 20


def test_static_assets(host: Host) -> None:
    assert host.get("/s/static/config.json").status == 200
    for path in ("/s/static/share.css", "/s/static/share.js", "/s/static/annotate.js"):
        r = host.get(path)
        assert r.status == 200 and r.header("X-Content-Type-Options") == "nosniff", path
    assert host.get("/s/static/fonts/nope.woff2").status == 404
    assert host.request("POST", "/s/static/share.js", {"Content-Length": "0"}).status == 404
    r = host.get("/s/static/vendor/pdfjs/pdf.min.mjs")
    if r.status == 200:
        assert (
            r.header("ETag")
            and host.get(
                "/s/static/vendor/pdfjs/pdf.min.mjs", {"If-None-Match": r.header("ETag") or ""}
            ).status
            == 304
        )


def test_large_html_label_failure_is_closed(host: Host) -> None:
    pad = b"<p>x</p>" * 400_000
    s = host.seed_share({"index.html": b"<html><body>" + pad + b"</body></html>"})
    assert (
        host.get(f"/s/{s.token}/f/index.html").status == 404
    )  # a labelled page is never served unlabelled


def test_unlabellable_html_is_not_served_unlabelled(host: Host) -> None:
    s = host.seed_share({"index.html": b"\xff\xfe<\x00h\x00t\x00m\x00l\x00>\x00"})
    assert host.get(f"/s/{s.token}/f/index.html").status == 404


def test_sharer_name_falls_back_to_member_then_generic(host: Host) -> None:
    s = host.seed_share(sharer=None)
    r = host.get(f"/s/{s.token}/f/index.html")
    assert r.status == 200 and b"Shared by owner_" in r.body
    host.execute("UPDATE shares SET created_by=NULL WHERE share_id=$1", s.share_id)
    assert b"Shared by a lab member" in host.get(f"/s/{s.token}/f/index.html").body


def test_uniform_404_has_one_connection_header(host: Host) -> None:
    wire = raw_exchange(host, b"GET /nothing HTTP/1.1\r\nHost: x\r\n\r\n").lower()
    assert wire.count(b"\r\nconnection:") == 1
