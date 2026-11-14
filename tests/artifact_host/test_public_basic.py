"""Public links: serving from blobs, immediate revoke and expiry, uniform failures, HEAD/OPTIONS/ranges."""

from __future__ import annotations

import secrets
import shutil

from tests.artifact_host.seed import FILES, Host, sha


def url(token: str, rest: str = "") -> str:
    return f"/s/{token}/{rest}"


def test_healthz(host: Host) -> None:
    r = host.health_get("/healthz")
    assert r.status == 200 and r.body == b"ok\n"


def test_link_redirects_to_entry_and_serves_bytes(host: Host) -> None:
    s = host.seed_share()
    r = host.get(url(s.token))
    assert r.status == 302 and r.header("Location") == "f/index.html"
    r = host.get(url(s.token, "f/notes.txt"))
    assert r.status == 200 and r.body == FILES["notes.txt"]
    assert r.header("ETag") == f'"{sha(FILES["notes.txt"])}"'
    assert r.header("X-Content-Type-Options") == "nosniff"
    assert "sandbox" in (r.header("Content-Security-Policy") or "")
    assert r.header("Server") == "herdr-share"


def test_html_is_labelled_with_sharer(host: Host) -> None:
    s = host.seed_share()
    r = host.get(url(s.token, "f/index.html"))
    assert r.status == 200 and b"Dana Doe" in r.body and b"hello" in r.body


def test_no_trailing_slash_redirect_is_uniform_for_unknown_tokens(host: Host) -> None:
    known, unknown = host.seed_share(), secrets.token_urlsafe(32)
    a = host.get(f"/s/{known.token}")
    b = host.get(f"/s/{unknown}")
    assert a.status == b.status == 302
    assert a.header("Location") == f"{known.token}/" and b.header("Location") == f"{unknown}/"


def test_opened_event_for_entry_only(host: Host) -> None:
    s = host.seed_share()
    host.get(url(s.token, "f/notes.txt"))
    assert host.events(s.share_id, "opened") == []
    host.get(url(s.token, "f/index.html"))
    assert len(host.events(s.share_id, "opened")) == 1


def test_revoke_is_immediate(host: Host) -> None:
    s = host.seed_share()
    assert host.get(url(s.token, "f/notes.txt")).status == 200
    host.execute(
        "UPDATE shares SET state='revoked', revoked_at=now() WHERE share_id=$1", s.share_id
    )
    assert host.get(url(s.token, "f/notes.txt")).status == 404


def test_expiry_is_immediate(host: Host) -> None:
    s = host.seed_share()
    assert host.get(url(s.token, "f/notes.txt")).status == 200
    host.execute(
        "UPDATE shares SET expires_at = now() - interval '1 second' WHERE share_id=$1", s.share_id
    )
    assert host.get(url(s.token, "f/notes.txt")).status == 404


def test_retracted_artifact_and_unready_version_not_served(host: Host) -> None:
    s = host.seed_share()
    host.execute("UPDATE artifacts SET lifecycle='retracted' WHERE artifact_id=$1", s.artifact_id)
    assert host.get(url(s.token, "f/notes.txt")).status == 404
    t = host.seed_share(version_state="draft")
    assert host.get(url(t.token, "f/notes.txt")).status == 404
    u = host.seed_share(intake="pending")
    assert host.get(url(u.token, "f/notes.txt")).status == 404


def test_public_share_needs_expiry(host: Host) -> None:
    s = host.seed_share(expires_in=None)
    assert host.get(url(s.token, "f/notes.txt")).status == 404


def test_all_failures_are_byte_identical(host: Host) -> None:
    s = host.seed_share()
    lab = host.seed_share(audience="lab")
    revoked = host.seed_share(state="revoked")
    cases = [
        f"/s/{secrets.token_urlsafe(32)}/f/notes.txt",  # unknown token
        url(revoked.token, "f/notes.txt"),  # revoked
        url(s.token, "f/missing.txt"),  # not in bundle
        url(s.token, "f/..%2fetc"),  # bad path
        url(s.token, "f/%2e%2e/x"),  # dot segments
        url(s.token, "f/a%zz"),  # bad escape
        url(lab.token, "f/notes.txt"),  # lab files only under a session
        url(s.token, "bogus"),  # unknown route
        "/s/short/f/x",  # malformed token
        "/elsewhere",
        "/s/",
        url(s.token, "k/notasession/f/notes.txt"),
    ]
    shapes = {host.get(c).shape() for c in cases}
    assert len(shapes) == 1
    status, headers, body = next(iter(shapes))
    assert status == 404 and body == b"Not found\n"
    assert ("connection", "close") in headers


def test_reason_codes_not_in_response(host: Host) -> None:
    s = host.seed_share()
    r = host.get(url(s.token, "f/missing.txt"))
    assert s.token.encode() not in r.body and b"bundle" not in r.body
    assert host.events(s.share_id, "denied")[0]["detail"].find(s.token) == -1


def test_request_target_checks(host: Host) -> None:
    s = host.seed_share()
    long = "/s/" + "a" * 5000
    assert host.get(long).status == 404
    assert host.get(url(s.token, "f/notes.txt?x=1")).status == 200
    assert host.get(url(s.token, "f/data/../notes.txt")).status == 404
    assert host.get(url(s.token, "f//notes.txt")).status == 404
    assert host.get(url(s.token, "f/notes%2etxt")).status == 200  # one decode per segment


def test_blob_corruption_is_uniform_404(host: Host) -> None:
    s = host.seed_share()
    digest = sha(FILES["notes.txt"])
    path = next(
        p
        for p in host.blob_root.rglob("*")
        if p.is_file() and p.name.startswith(digest[:10]) or p.name == digest
    )
    path.write_bytes(b"tampered")
    bad = host.get(url(s.token, "f/notes.txt"))
    ref = host.get(url(s.token, "f/missing.txt"))
    assert bad.shape() == ref.shape()
    assert host.events(s.share_id, "denied")[0]["detail"].find("blob_corrupt") != -1


def test_missing_blob_is_uniform_404(host: Host) -> None:
    s = host.seed_share()
    shutil.rmtree(host.blob_root)
    host.blob_root.mkdir()
    assert host.get(url(s.token, "f/notes.txt")).status == 404


def test_head_has_length_and_no_body(host: Host) -> None:
    s = host.seed_share()
    r = host.request("HEAD", url(s.token, "f/notes.txt"))
    assert r.status == 200 and r.body == b""
    assert r.header("Content-Length") == str(len(FILES["notes.txt"]))


def test_conditional_requests(host: Host) -> None:
    s = host.seed_share()
    etag = f'"{sha(FILES["notes.txt"])}"'
    assert host.get(url(s.token, "f/notes.txt"), {"If-None-Match": etag}).status == 304
    assert host.get(url(s.token, "f/notes.txt"), {"If-Match": '"nope"'}).status == 412


def test_ranges(host: Host) -> None:
    s = host.seed_share()
    data = FILES["data/big.bin"]
    r = host.get(url(s.token, "f/data/big.bin"), {"Range": "bytes=10-19"})
    assert r.status == 206 and r.body == data[10:20]
    assert r.header("Content-Range") == f"bytes 10-19/{len(data)}"
    r = host.get(url(s.token, "f/data/big.bin"), {"Range": "bytes=-5"})
    assert r.status == 206 and r.body == data[-5:]
    r = host.get(url(s.token, "f/data/big.bin"), {"Range": f"bytes={len(data) + 5}-"})
    assert r.status == 416
    r = host.get(url(s.token, "f/data/big.bin"), {"Range": "bytes=0-1,5-6"})
    assert r.status == 200 and r.body == data


def test_options_preflight_and_post_refused(host: Host) -> None:
    s = host.seed_share()
    r = host.request("OPTIONS", url(s.token, "f/notes.txt"))
    assert r.status == 204 and r.header("Access-Control-Allow-Origin") == "*"
    assert host.request("POST", url(s.token, "f/notes.txt"), {"Content-Length": "0"}).status == 404
    assert host.request("DELETE", url(s.token, "f/notes.txt")).status == 404


def test_body_on_get_is_refused_and_not_read(host: Host) -> None:
    s = host.seed_share()
    r = host.request("GET", url(s.token, "f/notes.txt"), {"Content-Length": "5"}, b"hello")
    assert r.status == 200 and r.header("Connection") == "close"


def test_public_host_restriction(migrated_dsn: str, tmp_path) -> None:
    from tests.artifact_host.seed import running_host

    with running_host(migrated_dsn, tmp_path, {"public_host": "share.example"}) as h:
        s = h.seed_share()
        assert h.get(url(s.token, "f/notes.txt")).status == 404
        ok = h.get(url(s.token, "f/notes.txt"), {"Host": "share.example"})
        assert ok.status == 200
