"""Ported lab and annotation-view tests: fail-closed configuration, session budgets, session basis, annotation edge
cases and the annotation view's markup, CSP and hostile content.  Real server, real Postgres, real sockets."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import threading
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from herdr_artifact_host.share_http import DEFAULT_STATIC_DIR, SESSION_GLOBAL_PER_MINUTE
from herdr_artifact_host.share_session import (
    csrf_token,
    mint_session,
    write_cookie_name,
    write_cookie_value,
)
from tests.artifact_host.seed import (
    OTHER_KEY,
    PUBLIC_URL,
    SECRET,
    Host,
    Seeded,
    access,
    default_config,
    ident,
    jwks_doc,
    make_sync_dir,
    running_host,
)
from tests.artifact_host.test_public_annotations import Session, rows
from tests.artifact_host.test_public_lab import lab, landing_session, sign_in

CLOSE_TEMPLATE = "</" + "template>"
CLOSE_STYLE = "</" + "style>"
FRAME_OPEN = "<" + "iframe src=a></" + "iframe>"
FRAME_RE = re.compile(r"<iframe", re.I)
HOSTILE_NAME = "we&ird 'q' <x>.pdf"
HOSTILE = (
    b"<html><head><style>:host{position:fixed!important;inset:0;z-index:2147483647}</style></head><body>"
    b"<script>window.x=1</script>" + FRAME_OPEN.encode() + b'<img src="pic.png" onerror="alert(1)">'
    b'<a href="javascript:alert(1)">js</a><a href="page2.html">next</a>'
    b'<div id="annotate-panel" name="herdr-view-data"><a id="herdr-view-data" name="herdr-annotate-csrf">c</a></div>'
    b"<p>" + CLOSE_TEMPLATE.encode() + b"<b>escaped?</b></p>"
    b"<style>p{color:red}" + CLOSE_TEMPLATE.encode() + b"<b id=pwn>x</b></style>"
    b"<style>p{color:blue}" + CLOSE_STYLE.encode() + b"<b id=pwn2>y</b></body></html>"
)
VIEW_FILES: dict[str, bytes] = {
    "index.html": HOSTILE,
    "page2.html": b"<html><body><p>two</p></body></html>",
    "pic.png": b"\x89PNG\r\n\x1a\n" + bytes(range(64)),
    "notes.txt": b"hello <script>alert(1)</script> & more",
    "doc.pdf": b"%PDF-1.4\n%fake\n",
    HOSTILE_NAME: b"%PDF-1.4\n%fake\n",
}


@contextmanager
def started(
    dsn: str, tmp: Path, sync: Path | None, static: Path | None, **over: object
) -> Iterator[Host]:
    """A host over an explicit sync dir and static dir (``running_host`` always makes its own)."""
    host = Host(dsn, tmp / "blobs", DEFAULT_STATIC_DIR, tmp / "unused-sync")
    host.start(default_config(static, sync, **over))  # type: ignore[arg-type]
    try:
        yield host
    finally:
        host.stop()


# ----------------------------------------------------------------------------- fail closed


@pytest.mark.parametrize(
    "over",
    [
        {"signing_secret": b""},
        {"signing_secret": b"short"},
        {"pool_id": None},
        {"pool_id": "bogus"},
        {"web_client_id": None},
        {"cognito_domain": None},
        {"cognito_domain": "http://auth.lab.example"},
        {"cognito_domain": "https://auth.lab.example/path?x"},
        {"public_url": None},
        {"sync_dir": None},
    ],
)
def test_lab_unconfigured_fails_closed(migrated_dsn: str, tmp_path: Path, over: dict) -> None:
    sync = None if "sync_dir" in over else make_sync_dir(tmp_path / "sync")
    rest = {k: v for k, v in over.items() if k != "sync_dir"}
    with started(migrated_dsn, tmp_path / "run", sync, DEFAULT_STATIC_DIR, **rest) as h:
        s = lab(h)
        ref = h.get("/nothing-here").shape()
        good = mint_session(
            SECRET,
            s.share_id,
            "alice",
            int(time.time()) + 100,
            email="alice@lab.example",
            basis="e",
        )
        for path in (
            f"/s/{s.token}/",
            "/s/callback",
            "/s/static/share.js",
            "/s/static/config.json",
            f"/s/{s.token}/k/{good}/f/index.html",
        ):
            assert h.get(path).shape() == ref, path
        assert sign_in(h, s).shape() == ref


@pytest.mark.parametrize(
    "jwks_text",
    [None, "not json", json.dumps({"keys": []}), json.dumps(jwks_doc(OTHER_KEY))],
)
def test_missing_or_wrong_jwks_fails_closed(
    migrated_dsn: str, tmp_path: Path, jwks_text: str | None
) -> None:
    sync = make_sync_dir(tmp_path / "sync")
    if jwks_text is None:
        (sync / "jwks.json").unlink()
    else:
        (sync / "jwks.json").write_text(jwks_text)
    with started(migrated_dsn, tmp_path / "run", sync, DEFAULT_STATIC_DIR) as h:
        s = lab(h)
        r = sign_in(h, s)
        assert r.status == 404 and r.shape() == h.get("/nothing-here").shape()


def test_static_dir_extra_files_are_not_reachable(migrated_dsn: str, tmp_path: Path) -> None:
    static = tmp_path / "static"
    shutil.copytree(DEFAULT_STATIC_DIR, static)
    (static / "secret.txt").write_text("secret")
    (static / "sub").mkdir()
    sync = make_sync_dir(tmp_path / "sync")
    with started(migrated_dsn, tmp_path / "run", sync, static) as h:
        ref = h.get("/nothing-here").shape()
        for path in (
            "/s/static/secret.txt",
            "/s/static/sub",
            "/s/static/sub/",
            "/s/static/share.js/..",
        ):
            assert h.get(path).shape() == ref, path
        (static / "share.css").unlink()
        (static / "share.css").symlink_to(static / "secret.txt")
        assert h.get("/s/static/share.css").shape() == ref  # a symlink is never followed
        assert h.get("/s/static/share.js").status == 200


def test_unknown_tokens_cannot_drain_the_global_session_budget(host: Host) -> None:
    s = lab(host)
    for _ in range(SESSION_GLOBAL_PER_MINUTE + 50):
        bogus = secrets.token_urlsafe(32)
        r = host.request(
            "POST",
            f"/s/{bogus}/session",
            {"Authorization": "Bearer x", "X-Id-Token": "y", "Content-Length": "0"},
        )
        assert r.status == 404
    assert sign_in(host, s).status == 200


# ----------------------------------------------------------------------------- session basis


def test_a_forged_basis_is_an_invalid_session(host: Host) -> None:
    s = lab(host)
    exp = int(time.time()) + 600
    group = mint_session(SECRET, s.share_id, "bob", exp, email="bob@lab.example", basis="g")
    _, e, blob = group.split(".")
    forged = ".".join(("e", e, blob))
    assert host.get(f"/s/{s.token}/k/{group}/f/notes.txt").status in (200, 404)
    assert host.get(f"/s/{s.token}/k/{forged}/f/notes.txt").shape() == host.get("/nothing").shape()
    base = f"/s/{s.token}/k/{forged}/annotations"
    cookie = (
        f"{write_cookie_name(s.share_id, forged)}={write_cookie_value(SECRET, s.share_id, forged)}"
    )
    r = host.request(
        "POST",
        base,
        {
            "Origin": PUBLIC_URL,
            "X-Herdr-Annotate": csrf_token(SECRET, s.share_id, forged),
            "Content-Type": "application/json",
            "Cookie": cookie,
        },
        json.dumps({"anchor": {"kind": "document", "file": "index.html"}, "comment": "x"}).encode(),
    )
    assert r.status == 404 and rows(host, s) == []


def test_email_removal_is_honoured_even_when_the_share_has_groups(host: Host) -> None:
    s = lab(host, groups=("staff",))
    a = Session(host, s)
    assert a.create().status == 201
    host.execute("UPDATE shares SET allow_emails='{}' WHERE share_id=$1", s.share_id)
    assert (
        a.create().status == 404
    )  # alice (admitted by email) was removed even though groups exist
    host.execute(
        "UPDATE shares SET allow_emails=$2 WHERE share_id=$1", s.share_id, ["alice@lab.example"]
    )
    assert a.create().status == 201
    assert len(rows(host, s)) == 2


# ----------------------------------------------------------------------------- annotation edge cases


def test_another_sessions_csrf_token_is_refused(host: Host) -> None:
    s = lab(host, emails=("alice@lab.example", "bob@lab.example"))
    a = Session(host, s)
    bob = Session(host, s, "bob", "Bob@Lab.example")
    other_share = lab(host)
    doc = {"anchor": {"kind": "document", "file": "index.html"}, "comment": "x"}
    assert (
        a.post(a.base, doc, X_Herdr_Annotate=csrf_token(SECRET, s.share_id, bob.sess)).status == 404
    )
    assert (
        a.post(
            a.base, doc, X_Herdr_Annotate=csrf_token(SECRET, other_share.share_id, a.sess)
        ).status
        == 404
    )
    assert rows(host, s) == []
    assert a.post(a.base, doc).status == 201


def test_short_body_is_refused(host: Host) -> None:
    import socket

    s = lab(host)
    a = Session(host, s)
    body = json.dumps(
        {"anchor": {"kind": "document", "file": "index.html"}, "comment": "x"}
    ).encode()
    headers = a.headers()
    head = f"POST {a.base} HTTP/1.1\r\nHost: x\r\n" + "".join(
        f"{k}: {v}\r\n" for k, v in headers.items()
    )
    head += f"Content-Length: {len(body) + 50}\r\n\r\n"
    with socket.create_connection(("127.0.0.1", host.port), timeout=10) as conn:
        conn.sendall(head.encode() + body)
        conn.shutdown(socket.SHUT_WR)  # the promised bytes never arrive
        out = b""
        while data := conn.recv(65536):
            out += data
    assert out == b"" or out.startswith((b"HTTP/1.1 404", b"HTTP/1.0 404"))
    assert rows(host, s) == []


def test_options_and_other_methods_on_annotation_paths_are_404(host: Host) -> None:
    s = lab(host)
    a = Session(host, s)
    ref = host.get("/nothing-here").shape()
    created = a.create().json()
    ident = created.get("annotation_id") or created.get("id") or "x"
    paths = [
        a.base,
        f"{a.base}/{ident}/edit",
        f"{a.base}/{ident}/delete",
        f"/s/{s.token}/k/{a.sess}/v/notes.txt",
        f"/s/{s.token}/k/{a.sess}/a/notes.txt",
    ]
    for path in paths:
        r = host.request(
            "OPTIONS",
            path,
            {
                "Origin": "null",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-herdr-annotate",
            },
        )
        assert r.shape() == ref, path
    for method in ("HEAD", "PUT", "DELETE", "PATCH"):
        assert host.request(method, a.base).status == 404
    assert host.request("OPTIONS", f"/s/{s.token}/session", {"Origin": "null"}).status == 404
    assert (
        host.request("POST", f"/s/{s.token}/k/{a.sess}/f/index.html", a.headers(), b"{}").status
        == 404
    )
    assert host.get(a.base + "/x").status == 404


def test_missing_public_url_means_no_origin_and_no_writes(
    migrated_dsn: str, tmp_path: Path
) -> None:
    with running_host(migrated_dsn, tmp_path, {"public_url": None}) as h:
        s = lab(h)
        exp = int(time.time()) + 600
        sess = mint_session(SECRET, s.share_id, "alice", exp, email="alice@lab.example", basis="e")
        base = f"/s/{s.token}/k/{sess}/annotations"
        cookie = (
            f"{write_cookie_name(s.share_id, sess)}={write_cookie_value(SECRET, s.share_id, sess)}"
        )
        body = json.dumps(
            {"anchor": {"kind": "document", "file": "index.html"}, "comment": "x"}
        ).encode()
        for origin in (PUBLIC_URL, "null", None):
            headers = {
                "X-Herdr-Annotate": csrf_token(SECRET, s.share_id, sess),
                "Content-Type": "application/json",
                "Cookie": cookie,
            }
            if origin is not None:
                headers["Origin"] = origin
            assert h.request("POST", base, headers, body).status == 404
        assert rows(h, s) == []


# ----------------------------------------------------------------------------- annotation view


def view(host: Host, s: Seeded, sess: str, name: str, kind: str = "a"):
    return host.get(f"/s/{s.token}/k/{sess}/{kind}/{urllib.parse.quote(name)}")


@pytest.fixture
def viewed(host: Host) -> tuple[Seeded, str]:
    s = lab(host, files=VIEW_FILES)
    return s, landing_session(sign_in(host, s))


def parse_csp(csp: str) -> dict[str, list[str]]:
    return {d.split()[0]: d.split()[1:] for d in csp.split("; ")}


def split_shadow(page: str) -> tuple[str, str, str]:
    opener = '<template shadowrootmode="open">'
    assert page.count(opener) == 1 and page.count(CLOSE_TEMPLATE) == 1
    before, rest = page.split(opener)
    inside, after = rest.split(CLOSE_TEMPLATE)
    return before, inside, after


def data_of(page: str) -> dict:
    m = re.search(r'id="herdr-view-data"[^>]*>(.*?)</script>', page, re.S)
    assert m
    return json.loads(m.group(1))


def test_csp_header_by_header(host: Host, viewed: tuple[Seeded, str]) -> None:
    s, sess = viewed
    r = view(host, s, sess, "notes.txt")
    assert r.status == 200
    csp = parse_csp(r.header("Content-Security-Policy") or "")
    script = csp["script-src"]
    assert len(script) == 2 and script[0].startswith("'nonce-") and script[1] == "'strict-dynamic'"
    share = f"{PUBLIC_URL}/s/{s.token}/"
    expected = {
        "default-src": ["'none'"],
        "style-src-elem": [f"{PUBLIC_URL}/s/static/", share, "'unsafe-inline'"],
        "style-src-attr": ["'unsafe-inline'"],
        "img-src": [share, "data:"],
        "font-src": [share, f"{PUBLIC_URL}/s/static/", "data:"],
        "connect-src": [share],
        "worker-src": [f"{PUBLIC_URL}/s/static/vendor/pdfjs/"],
        "frame-src": ["'none'"],
        "child-src": ["'none'"],
        "object-src": ["'none'"],
        "base-uri": ["'none'"],
        "form-action": ["'none'"],
        "frame-ancestors": ["'none'"],
    }
    for name, value in expected.items():
        assert csp[name] == value, name
    assert set(csp) == set(expected) | {"script-src"}
    assert not any(t in ("'self'", "blob:", "*") for ds in csp.values() for t in ds)
    assert not any(
        t.startswith("http") and not t.startswith(PUBLIC_URL) for ds in csp.values() for t in ds
    )
    assert view(host, s, sess, "notes.txt").header("Content-Security-Policy") != r.header(
        "Content-Security-Policy"
    )  # a fresh nonce per response


def test_csrf_is_delivered_only_in_our_markup(host: Host, viewed: tuple[Seeded, str]) -> None:
    s, sess = viewed
    page = view(host, s, sess, "index.html").body.decode()
    token = csrf_token(SECRET, s.share_id, sess)
    before, inside, after = split_shadow(page)
    assert page.count(token) == 1 and token not in inside and token not in after
    assert re.search(rf'<meta name="herdr-annotate-csrf" content="{token}">', before)
    assert token not in json.dumps(data_of(page))
    assert "herdr-annotate-csrf" not in view(host, s, sess, "notes.txt", "v").body.decode()
    other = lab(host, files=VIEW_FILES, emails=("bob@lab.example",))
    bob = landing_session(
        sign_in(host, other, access("bob"), ident("bob", email="bob@lab.example"))
    )
    assert token not in view(host, other, bob, "notes.txt").body.decode()


def test_hostile_html_is_sanitised_and_contained_in_the_shadow_root(
    host: Host, viewed: tuple[Seeded, str]
) -> None:
    s, sess = viewed
    page = view(host, s, sess, "index.html").body.decode()
    before, inside, after = split_shadow(page)
    assert after.lstrip().startswith("</div>")
    for bad in ("<script", "onerror", "javascript:", "iframe", "window.x"):
        assert bad not in inside.lower(), bad
    assert 'href="../a/page2.html"' in inside and 'src="../f/pic.png"' in inside
    assert page.count('id="herdr-view-data"') == 2 and before.count('id="herdr-view-data"') == 1
    assert after.count('id="herdr-view-data"') == 0 and "data-annotate-panel" in after
    assert 'id="annotate-panel"' in inside and 'id="annotate-panel"' not in before + after
    assert "pwn" not in before + after
    assert "color:red" not in inside and "pwn" not in inside.replace('id="pwn2"', "")
    assert "color:blue" in inside and inside.count("<style>") == inside.count(CLOSE_STYLE) == 2
    assert ":host{position:fixed!important" in inside
    outer = before.rindex('id="herdr-viewport"')
    assert outer < before.index('id="herdr-content"') and after.lstrip().startswith("</div>")
    assert not FRAME_RE.search(page.replace(FRAME_OPEN, ""))


def test_hostile_file_name_is_escaped_everywhere(host: Host, viewed: tuple[Seeded, str]) -> None:
    s, sess = viewed
    r = view(host, s, sess, HOSTILE_NAME)
    page = r.body.decode()
    assert r.status == 200
    assert HOSTILE_NAME not in page.replace(json.dumps(HOSTILE_NAME, ensure_ascii=True)[1:-1], "")
    assert "<x>" not in page and "'q'" not in re.sub(r"<script.*?</script>", "", page, flags=re.S)
    assert data_of(page)["file"] == HOSTILE_NAME
    for href in re.findall(r'<a data-mode="[a-z]+" href="([^"]*)"', page):
        assert "<" not in href and '"' not in href and " " not in href
    assert HOSTILE_NAME not in (r.header("Content-Security-Policy") or "")


def test_render_failures_map_to_uniform_responses(host: Host) -> None:
    """RenderFailed (the output tripwire) is the uniform 404; RenderTooLarge gets the note; real isolated child.
    RenderTimeout and RenderBusy are the SIGSTOP tests below."""
    files = {
        "index.html": b"<p>i</p>",
        "tripwire.html": b'<p title="' + CLOSE_TEMPLATE.encode() + b'<img src=x onerror=1>">x</p>',
        "huge.html": b"< " * 1_000_000,
    }
    s = lab(host, files=files)
    sess = landing_session(sign_in(host, s))
    ref = host.get("/nothing-here").shape()
    r = view(host, s, sess, "tripwire.html")
    assert r.shape()[0] == 404 and r.body == ref[2]
    big = view(host, s, sess, "huge.html")
    assert big.status == 200 and b"too large to show" in big.body


# ----------------------------------------------------------------------------- RenderTimeout and RenderBusy, real child


def stop_the_sanitizer_child() -> int:
    """SIGSTOP the real isolated sanitizer child: it neither answers nor dies, so a job runs into its deadline."""
    from herdr_artifact_host import share_render

    proc = share_render._WORKER._proc
    assert proc is not None and proc.poll() is None
    os.kill(proc.pid, signal.SIGSTOP)
    return proc.pid


def render_files(count: int) -> dict[str, bytes]:
    files = {"index.html": b"<p>index</p>"}
    for i in range(count):
        files[f"p{i}.html"] = f"<p>page {i} {secrets.token_hex(4)}</p>".encode()
    return files


def test_a_stopped_sanitizer_child_is_a_render_timeout_that_answers_the_uniform_404(
    host: Host,
) -> None:
    from herdr_artifact_host import share_render

    share_render.clear_sanitize_caches()
    s = lab(host, files=render_files(3))
    sess = landing_session(sign_in(host, s))
    assert view(host, s, sess, "p0.html").status == 200  # the child is up
    pid = stop_the_sanitizer_child()
    try:
        ref = host.get("/nothing-here").shape()
        start = time.monotonic()
        r = view(host, s, sess, "p1.html")
        elapsed = time.monotonic() - start
        assert r.shape()[0] == 404 and r.body == ref[2]
        assert (
            share_render.SANITIZE_DEADLINE_SECONDS - 0.5
            < elapsed
            < share_render.SANITIZE_DEADLINE_SECONDS + 3
        )
        assert {"reason": "render_failed"} in [
            json.loads(e["detail"]) for e in host.events(s.share_id, "denied")
        ]
    finally:
        try:
            os.kill(
                pid, signal.SIGKILL
            )  # the parent already killed it on the deadline; this is just cleanup
        except ProcessLookupError:
            pass
    again = view(host, s, sess, "p2.html")  # a fresh child serves the next request
    assert again.status == 200 and b"page 2" in again.body


def test_a_full_render_queue_is_a_busy_503_and_clears_once_the_child_is_replaced(
    host: Host,
) -> None:
    from herdr_artifact_host import share_render
    from herdr_artifact_host.share_http import UNAVAILABLE_BODY

    share_render.clear_sanitize_caches()
    cap = share_render.MAX_PENDING_RENDERS
    s = lab(host, files=render_files(cap + 3))
    sess = landing_session(sign_in(host, s))
    assert view(host, s, sess, "p0.html").status == 200
    pid = stop_the_sanitizer_child()
    answers: dict[int, int] = {}

    def render(i: int) -> None:
        answers[i] = view(host, s, sess, f"p{i}.html").status

    waiting = [threading.Thread(target=render, args=(i,), daemon=True) for i in range(1, cap + 1)]
    try:
        for t in waiting:
            t.start()
        deadline = time.monotonic() + 15
        # all of them are inside the render queue (every pending slot taken), stuck behind the stopped child
        while share_render._WORKER._pending._value > 0:
            assert time.monotonic() < deadline, "the renders never filled the queue"
            time.sleep(0.02)
        busy = view(host, s, sess, f"p{cap + 1}.html")
        assert busy.status == 503 and busy.body == UNAVAILABLE_BODY
        assert busy.header("Retry-After") == "5"
        ref = host.get("/nothing-here").shape()
        assert (
            host.get(f"/s/{s.token}/k/{sess}/a/missing.html").shape() == ref
        )  # still uniform for what is not there
    finally:
        for t in waiting:
            t.join(30)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert len(answers) == cap
    after = view(host, s, sess, f"p{cap + 2}.html")  # the queue drained and a fresh child works
    assert after.status == 200
