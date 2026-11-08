"""Security review round 1 of the public surface: Expect, request-body encodings, the per-share cap race, blob
verification cost, the protocol-error hook, claim bookkeeping, logs and cancellation. Real server, real sockets."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import socket
import threading
import time
import zlib
from pathlib import Path

import aiohttp
import asyncpg
import pytest
from aiohttp import web

from herdr_artifact_host import public_server
from herdr_artifact_host.annotation_anchor import annotation_settings
from herdr_artifact_host.blobs import BlobStore
from herdr_artifact_host.library import LibraryStore
from herdr_artifact_host.public_server import Claim, build_app
from herdr_artifact_host.roles import Role, routes_for
from tests.artifact_host.seed import Host, default_config, make_sync_dir, running_host
from tests.artifact_host.test_public_annotations import Session, rows
from tests.artifact_host.test_public_lab import lab
from tests.artifact_host.test_public_security import raw_exchange


def strip_date(wire: bytes) -> bytes:
    return b"\r\n".join(
        line for line in wire.split(b"\r\n") if not line.lower().startswith(b"date:")
    )


def reference(host: Host) -> bytes:
    return strip_date(raw_exchange(host, b"GET /nothing HTTP/1.1\r\nHost: x\r\n\r\n"))


# ----------------------------------------------------------------------------- I1: Expect


@pytest.mark.parametrize("target", ["/nothing", "/healthz", "/s/x/", "PUBLIC"])
@pytest.mark.parametrize(
    "expect", [b"100-continue", b"bogus", b"100-CONTINUE", b"", b"<script>alert(1)</script>"]
)
def test_expect_header_never_bypasses_the_uniform_404(
    host: Host, target: str, expect: bytes
) -> None:
    if target == "PUBLIC":
        target = f"/s/{host.seed_share().token}/f/index.html"
    wire = strip_date(
        raw_exchange(
            host,
            b"GET " + target.encode() + b" HTTP/1.1\r\nHost: x\r\nExpect: " + expect + b"\r\n\r\n",
        )
    )
    assert wire == reference(host)
    assert b"100 Continue" not in wire and b"417" not in wire
    assert b"aiohttp" not in wire and b"Python" not in wire


def test_expect_with_a_body_gets_one_uniform_404_and_no_second_request(host: Host) -> None:
    second = b"GET /nothing HTTP/1.1\r\nHost: x\r\n\r\n"
    wire = raw_exchange(
        host,
        b"POST /s/x/y HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\nContent-Length: %d\r\n\r\n"
        % len(second)
        + second,
    )
    assert wire.count(b"HTTP/1.") == 1 and b"100 Continue" not in wire
    assert strip_date(wire) == reference(host)


def test_no_response_carries_a_framework_server_banner(host: Host) -> None:
    s = host.seed_share()
    for path in (f"/s/{s.token}/f/index.html", "/nothing", "/healthz", f"/s/{s.token}/f/nope"):
        r = host.get(path)
        assert r.header("Server") == "herdr-share", path
    wire = raw_exchange(host, b"GARBAGE\r\n\r\n")
    assert b"aiohttp" not in wire and b"Python" not in wire


# ----------------------------------------------------------------------------- I2: request-body encodings


def _peak_rss_kib() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1])
    raise RuntimeError("no VmHWM")


def _reset_peak() -> bool:
    try:
        Path("/proc/self/clear_refs").write_text("5")
        return True
    except OSError:
        return False


@pytest.mark.parametrize("encoding", ["deflate", "gzip", "br", "identity", "zstd", "x-unknown"])
def test_any_request_content_encoding_is_the_uniform_404(host: Host, encoding: str) -> None:
    s = host.seed_share()
    for path in (f"/s/{s.token}/f/index.html", "/nothing", "/healthz"):
        wire = strip_date(
            raw_exchange(
                host,
                b"GET "
                + path.encode()
                + b" HTTP/1.1\r\nHost: x\r\nContent-Encoding: "
                + encoding.encode()
                + b"\r\n\r\n",
            )
        )
        assert wire == reference(host), (path, encoding)


def test_the_server_never_inflates_request_bodies(host: Host) -> None:
    kwargs = host.server._runner.server._kwargs  # type: ignore[union-attr]
    assert kwargs.get("auto_decompress") is False
    if not _reset_peak():
        pytest.skip("cannot reset the peak RSS counter")
    comp = zlib.compressobj(9)
    bomb = (
        b"".join(comp.compress(bytes(1 << 20)) for _ in range(400)) + comp.flush()
    )  # 400 MiB inflated
    assert len(bomb) < 2 << 20
    _reset_peak()
    before = _peak_rss_kib()
    wire = strip_date(
        raw_exchange(
            host,
            b"POST /s/x/y HTTP/1.1\r\nHost: x\r\nContent-Encoding: deflate\r\nContent-Length: %d\r\n\r\n"
            % len(bomb)
            + bomb,
        )
    )
    assert wire == reference(host)
    assert _peak_rss_kib() - before < 150 * 1024


# ----------------------------------------------------------------------------- I3: per-share cap race


def test_concurrent_authors_cannot_overshoot_the_per_share_cap(
    migrated_dsn: str, tmp_path: Path
) -> None:
    emails = tuple(f"user{i}@lab.example" for i in range(12))
    limits = annotation_settings({"max_per_share": 2})
    with running_host(migrated_dsn, tmp_path, {"annotations": limits}) as h:
        s = lab(h, emails=emails)
        sessions = [Session(h, s, f"user{i}", e) for i, e in enumerate(emails)]
        barrier = threading.Barrier(len(sessions))

        def go(sess: Session) -> int:
            barrier.wait()
            return sess.create("x").status

        with concurrent.futures.ThreadPoolExecutor(len(sessions)) as pool:
            statuses = sorted(pool.map(go, sessions))
        assert statuses == [201, 201] + [429] * 10
        assert len(rows(h, s)) == 2


# ----------------------------------------------------------------------------- I4: blob verification cost


def big_share(host: Host):
    data = bytes(range(256)) * 16384  # 4 MiB
    return host.seed_share({"index.html": b"<html><body>x</body></html>", "big.bin": data}), data


def blob_path(host: Host, data: bytes) -> Path:
    import hashlib

    sha = hashlib.sha256(data).hexdigest()
    return host.blob_root / "sha256" / sha[:2] / sha[2:4] / sha


def test_head_range_and_304_do_not_rehash_a_verified_blob(host: Host) -> None:
    s, data = big_share(host)
    blobs = host.surface.blobs
    url = f"/s/{s.token}/f/big.bin"
    full = host.get(url)
    assert full.status == 200 and full.body == data
    base = blobs.verifications
    etag = full.header("ETag") or ""
    assert host.request("HEAD", url).status == 200
    assert host.get(url, {"Range": "bytes=0-0"}).status == 206
    assert host.get(url, {"Range": "bytes=-5"}).status == 206
    assert host.get(url, {"If-None-Match": etag}).status == 304
    assert blobs.verifications == base
    # a full 200 body is always verified (cost proportional to the bytes served)
    assert host.get(url).status == 200
    assert blobs.verifications == base + 1


def test_first_sight_is_always_verified(host: Host) -> None:
    s, _ = big_share(host)
    blobs = host.surface.blobs
    base = blobs.verifications
    assert host.request("HEAD", f"/s/{s.token}/f/big.bin").status == 200
    assert blobs.verifications == base + 1


def test_a_blob_corrupted_after_caching_is_still_detected(host: Host) -> None:
    s, data = big_share(host)
    url = f"/s/{s.token}/f/big.bin"
    assert host.get(url, {"Range": "bytes=0-0"}).status == 206
    assert host.get(url, {"Range": "bytes=0-0"}).status == 206  # cached
    path = blob_path(host, data)
    st = path.stat()
    with path.open(
        "r+b"
    ) as fh:  # same size, then restore the mtime: only the inode change time betrays it
        fh.seek(7)
        fh.write(b"\xff\xfe")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    for headers in ({"Range": "bytes=0-0"}, {}):
        r = host.get(url, headers)
        assert r.status == 404 and r.body == b"Not found\n"


def test_a_blob_replaced_by_a_new_inode_is_detected(host: Host) -> None:
    s, data = big_share(host)
    url = f"/s/{s.token}/f/big.bin"
    assert host.request("HEAD", url).status == 200
    path = blob_path(host, data)
    bad = bytearray(data)
    bad[0] ^= 1
    replacement = path.with_name("replacement")
    replacement.write_bytes(bytes(bad))
    os.replace(replacement, path)
    assert host.request("HEAD", url).status == 404
    assert host.get(url, {"Range": "bytes=0-0"}).status == 404


def test_blob_work_does_not_depend_on_the_default_executor(host: Host) -> None:
    s, data = big_share(host)
    loop = host.loop
    assert loop is not None
    release = threading.Event()
    started = threading.Event()

    async def clog() -> None:
        async def blocker() -> None:
            await asyncio.get_running_loop().run_in_executor(None, release.wait, 60)

        tasks = [asyncio.ensure_future(blocker()) for _ in range(128)]
        started.set()
        await asyncio.gather(*tasks)

    fut = asyncio.run_coroutine_threadsafe(clog(), loop)
    try:
        assert started.wait(10)
        r = host.get(f"/s/{s.token}/f/big.bin", {"Range": "bytes=0-9"})
        assert r.status == 206 and r.body == data[:10]
        assert host.get(f"/s/{s.token}/f/big.bin").body == data
    finally:
        release.set()
        fut.result(30)


# ----------------------------------------------------------------------------- M1: the protocol hook fails closed


def test_the_uniform_protocol_error_hook_is_installed(host: Host) -> None:
    runner = host.server._runner  # type: ignore[union-attr]
    assert isinstance(runner.server, public_server._UniformServer)
    public_server.assert_uniform_hook(runner.server)


def test_a_plain_aiohttp_server_fails_the_hook_check() -> None:
    async def go() -> None:
        runner = web.AppRunner(web.Application())
        await runner.setup()
        try:
            with pytest.raises(RuntimeError):
                public_server.assert_uniform_hook(runner.server)  # type: ignore[arg-type]
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_the_aiohttp_version_is_checked() -> None:
    assert public_server.aiohttp_version_ok(aiohttp.__version__)
    assert public_server.aiohttp_version_ok("3.13.5")
    for other in (
        "3.13.4",
        "3.13.6",
        "3.14.0",
        "4.0.0",
        "3.12.15",
        "3.13",
        "3.13.5rc1",
        "garbage",
        "2.9.0",
    ):
        assert not public_server.aiohttp_version_ok(other), (
            other
        )  # exactly the pinned release, nothing else
    assert aiohttp.__version__ == "3.13.5"  # the exact pin the manifests carry


def test_manifests_pin_the_same_aiohttp() -> None:
    root = Path(__file__).resolve().parents[2]
    for manifest in (root / "pyproject.toml", root / "deploy/artifact-host/pyproject.toml"):
        assert "aiohttp==3.13.5" in manifest.read_text(), manifest


# ----------------------------------------------------------------------------- M2: the HTTP version set is exact


def _status_line(wire: bytes) -> bytes:
    return wire.split(b"\r\n", 1)[0]


def test_the_status_line_version_set_is_exactly_documented(host: Host) -> None:
    """Every failure is byte-identical apart from the version in the status line, which follows the request line:
    HTTP/1.1 answers 1.1, HTTP/1.0 answers 1.0, a version-less or HTTP/0.9 request line is answered as 0.9, and every request
    aiohttp cannot parse is answered as 1.0.  This is the complete accepted set; the Connection: close header is
    always present, so the version never changes framing."""
    ref = strip_date(raw_exchange(host, b"GET /nothing HTTP/1.1\r\nHost: x\r\n\r\n"))
    assert _status_line(ref) == b"HTTP/1.1 404 Not Found"
    tail = ref.split(b"\r\n", 1)[1]
    cases = {
        b"GET /nothing HTTP/1.0\r\n\r\n": b"HTTP/1.0 404 Not Found",
        b"GET /nothing HTTP/0.9\r\n\r\n": b"HTTP/0.9 404 Not Found",
        b"GET /nothing\r\n\r\n": b"HTTP/0.9 404 Not Found",
        b"GARBAGE\r\n\r\n": b"HTTP/1.0 404 Not Found",
        b"GET /\x00 HTTP/1.1\r\nHost: x\r\n\r\n": b"HTTP/1.0 404 Not Found",
        b"GET /nothing HTTP/2.0\r\nHost: x\r\n\r\n": b"HTTP/2.0 404 Not Found",
        b"GET /nothing HTTP/1.2\r\nHost: x\r\n\r\n": b"HTTP/1.0 404 Not Found",
    }
    for request, line in cases.items():
        wire = strip_date(raw_exchange(host, request))
        assert _status_line(wire) == line, request
        assert wire.split(b"\r\n", 1)[1] == tail, request


# ----------------------------------------------------------------------------- M3: claims bookkeeping


def test_a_refused_claim_is_not_remembered_and_logs_no_event(host: Host) -> None:
    s = host.seed_share()
    surface = host.surface
    loop = host.loop
    assert loop is not None

    def run(claim: Claim) -> None:
        asyncio.run_coroutine_threadsafe(surface._claimed(s.share_id, claim), loop).result(10)

    run(
        Claim(identity="", email="a@b.example", session="sess-1")
    )  # the library refuses an empty member
    assert surface._claims_seen == set()
    assert host.events(s.share_id, "claimed") == []
    assert host.rows("SELECT 1 FROM share_claims WHERE share_id=$1", s.share_id) == []
    run(
        Claim(identity="alice", email="a@b.example", session="sess-1")
    )  # the same session, now valid
    assert len(host.events(s.share_id, "claimed")) == 1
    assert len(host.rows("SELECT 1 FROM share_claims WHERE share_id=$1", s.share_id)) == 1


# ----------------------------------------------------------------------------- M4: no tracebacks in logs


def test_internal_errors_log_no_traceback(host: Host, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    s = host.seed_share()
    host.execute("ALTER TABLE shares RENAME TO shares_gone")
    try:
        r = host.get(f"/s/{s.token}/f/index.html")
    finally:
        host.execute("ALTER TABLE shares_gone RENAME TO shares")
    assert r.status == 404 and r.body == b"Not found\n"
    assert any("internal_error" in rec.getMessage() for rec in caplog.records)
    for rec in caplog.records:
        assert rec.exc_info is None and rec.exc_text is None
        assert s.token not in rec.getMessage() and s.token not in str(rec.args)


# ----------------------------------------------------------------------------- M5: cancellation propagates


def test_a_cancelled_stream_is_not_swallowed(migrated_dsn: str, tmp_path: Path) -> None:
    big = tmp_path / "big"
    with big.open("wb") as fh:
        fh.truncate(512 << 20)
    outcome: dict[str, str] = {}

    async def go() -> None:
        pool = await asyncpg.create_pool(migrated_dsn, min_size=1, max_size=2)
        try:
            sync = make_sync_dir(tmp_path / "sync")
            app = build_app(
                default_config(None, sync), pool, BlobStore(tmp_path / "blobs"), LibraryStore(pool)
            )
            surface = app[public_server.SURFACE]
            done = asyncio.Event()

            async def route(request: web.Request) -> web.StreamResponse:
                handle = big.open("rb")
                try:
                    resp = await surface.stream(
                        request,
                        200,
                        [("Content-Type", "application/octet-stream")],
                        handle,
                        0,
                        512 << 20,
                    )
                    outcome["result"] = "completed"
                    return resp
                except asyncio.CancelledError:
                    outcome["result"] = "cancelled"
                    raise
                finally:
                    handle.close()
                    done.set()

            inner = web.Application()
            inner.router.add_get("/big", route)
            runner = web.AppRunner(
                inner, handler_cancellation=True, access_log=None, handle_signals=False
            )
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /big HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            await reader.read(1024)
            sock = writer.get_extra_info("socket")
            sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00"
            )
            writer.close()
            await asyncio.wait_for(done.wait(), 20)
            await runner.cleanup()
        finally:
            await pool.close()

    asyncio.run(go())
    assert outcome["result"] == "cancelled"


# ----------------------------------------------------------------------------- N3: /healthz has its own loopback listener


def lan_address() -> str | None:
    """A local non-loopback IPv4 address, or None on a machine that only has loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # no packet is sent
            addr = probe.getsockname()[0]
        except OSError:
            return None
    return None if addr.startswith("127.") else addr


def test_healthz_is_answered_on_the_health_listener(host: Host) -> None:
    assert host.health_port and host.health_port != host.port
    r = host.health_get("/healthz")
    assert r.status == 200 and r.body == b"ok\n"


def test_the_public_listener_has_no_healthz(host: Host) -> None:
    ref = host.get("/nothing").shape()
    assert host.get("/healthz").shape() == ref
    assert host.health_get("/nothing", port=host.port).shape() == ref
    assert "GET /healthz" not in routes_for(Role.PUBLIC)
    assert routes_for(Role.HEALTH) == {"GET /healthz"}


def test_the_health_listener_answers_nothing_else_and_refuses_framing(host: Host) -> None:
    ref = host.get("/nothing").shape()
    for path in ("/", "/nothing", "/s/anything/f/x", "/healthz/", "/internal/healthz"):
        assert host.health_get(path).shape() == ref, path
    assert host.health_get("/healthz", {"Expect": "100-continue"}).shape() == ref
    assert host.health_get("/healthz", {"Content-Encoding": "gzip"}).shape() == ref
    assert host.health_get("/healthz").status == 200


def test_the_health_listener_is_bound_to_loopback_even_when_the_public_one_is_wide(
    migrated_dsn: str, tmp_path: Path
) -> None:
    addr = lan_address()
    if addr is None:
        pytest.skip("no non-loopback address on this machine")
    from herdr_artifact_host.share_http import DEFAULT_STATIC_DIR

    sync = make_sync_dir(tmp_path / "sync")
    wide = Host(migrated_dsn, tmp_path / "blobs", DEFAULT_STATIC_DIR, sync, bind="0.0.0.0")
    wide.start(default_config(DEFAULT_STATIC_DIR, sync))
    try:
        assert wide.health_get("/healthz").status == 200  # over 127.0.0.1
        with socket.socket() as probe:
            probe.settimeout(5)
            assert probe.connect_ex((addr, wide.health_port)) != 0  # nothing listens there
        with socket.socket() as probe:
            probe.settimeout(5)
            assert probe.connect_ex((addr, wide.port)) == 0  # the public listener is wide
    finally:
        wide.stop()


def test_loopback_peer_check_is_gone() -> None:
    assert not hasattr(public_server, "is_loopback_peer")


# ----------------------------------------------------------------------------- N4: the startup check is behavioural


def test_the_startup_check_accepts_the_real_server(host: Host) -> None:
    asyncio.run(public_server.verify_uniform_listener(host.port))


def test_the_startup_check_fails_closed_on_a_plain_aiohttp_server() -> None:
    async def go() -> None:
        runner = web.AppRunner(web.Application())  # aiohttp's own 400 text for unparseable requests
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            with pytest.raises(RuntimeError, match="uniform"):
                await public_server.verify_uniform_listener(port)
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_the_startup_check_fails_closed_when_nothing_answers() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(RuntimeError, match="uniform"):
        asyncio.run(public_server.verify_uniform_listener(port))


def test_the_private_attributes_eof_received_relies_on_are_required() -> None:
    with pytest.raises(RuntimeError, match="_current_request"):
        public_server.require_handler_internals(object())


def test_a_real_handler_has_the_private_attributes() -> None:
    async def go() -> None:
        runner = public_server._UniformRunner(web.Application())
        await runner.setup()
        try:
            public_server.require_handler_internals(runner.server())  # type: ignore[misc]
        finally:
            await runner.cleanup()

    asyncio.run(go())


# ----------------------------------------------------------------------------- M7: half-close must not lose the response


def half_closed(host: Host, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", host.port), timeout=10) as sock:
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while data := sock.recv(65536):
            chunks.append(data)
    return b"".join(chunks)


def test_half_closed_clients_still_get_full_answers_for_known_shares(host: Host) -> None:
    s = host.seed_share()
    gone = host.seed_share(state="revoked")
    cases = {
        "file": (
            f"GET /s/{s.token}/f/notes.txt HTTP/1.1\r\nHost: x\r\n\r\n",
            b"200 OK",
            b"plain notes",
        ),
        "injected html": (
            f"GET /s/{s.token}/f/index.html HTTP/1.1\r\nHost: x\r\n\r\n",
            b"200 OK",
            b"hello",
        ),
        "range": (
            f"GET /s/{s.token}/f/notes.txt HTTP/1.1\r\nHost: x\r\nRange: bytes=0-4\r\n\r\n",
            b"206 Partial Content",
            b"plain",
        ),
        "head": (f"HEAD /s/{s.token}/f/notes.txt HTTP/1.1\r\nHost: x\r\n\r\n", b"200 OK", b""),
        "revoked share": (
            f"GET /s/{gone.token}/f/notes.txt HTTP/1.1\r\nHost: x\r\n\r\n",
            b"404 Not Found",
            b"Not found\n",
        ),
        "missing file": (
            f"GET /s/{s.token}/f/ghost HTTP/1.1\r\nHost: x\r\n\r\n",
            b"404 Not Found",
            b"Not found\n",
        ),
        "healthz (not on the public port)": (
            "GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n",
            b"404 Not Found",
            b"Not found\n",
        ),
        "protocol error": (
            f"GET /s/{s.token}/f/notes.txt HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
            b"404 Not Found",
            b"Not found\n",
        ),
    }
    for name, (request, status, tail) in cases.items():
        out = half_closed(host, request.encode())
        assert out.split(b"\r\n", 1)[0].endswith(status), (name, out[:120])
        assert tail in out.split(b"\r\n\r\n", 1)[1] if tail else True, name
        assert out.count(b"HTTP/1.") == 1, name


def test_half_closed_lab_post_is_answered_and_written(host: Host) -> None:
    s = lab(host)
    a = Session(host, s)
    body = json.dumps(
        {"anchor": {"kind": "document", "file": "index.html"}, "comment": "half"}
    ).encode()
    head = f"POST {a.base} HTTP/1.1\r\nHost: x\r\n" + "".join(
        f"{k}: {v}\r\n" for k, v in a.headers().items()
    )
    out = half_closed(host, head.encode() + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    assert out.startswith(b"HTTP/1.1 201 ")
    assert [r["body"] for r in rows(host, s)] == ["half"]


def test_an_idle_half_close_just_closes(host: Host) -> None:
    with socket.create_connection(("127.0.0.1", host.port), timeout=10) as sock:
        sock.shutdown(socket.SHUT_WR)
        assert sock.recv(10) == b""  # closed promptly, not held open until the keep-alive timeout


# ----------------------------------------------------------------------------- M8: HTML over the injection limit


def test_html_over_the_limit_is_the_uniform_404_with_an_explicit_reason(host: Host) -> None:
    """Every host share is a member link and so carries the "Shared by" label, exactly as member links did in the
    control plane (``share_public.py``: ``if size > MODE_SWITCH_MAX_BYTES: if bar: deny``): a labelled page is never
    served unlabelled, and HTML over 2 MiB cannot be labelled.  The event says why; other types still stream."""
    big = b"<html><body>" + b"<p>x</p>" * 300_000 + b"</body></html>"
    assert len(big) > public_server.MODE_SWITCH_MAX_BYTES
    s = host.seed_share({"index.html": big, "data.bin": big})
    r = host.get(f"/s/{s.token}/f/index.html")
    assert r.status == 404 and r.body == b"Not found\n"
    reasons = [json.loads(e["detail"])["reason"] for e in host.events(s.share_id, "denied")]
    assert reasons == ["html_too_large"]
    ok = host.get(f"/s/{s.token}/f/data.bin")
    assert ok.status == 200 and ok.body == big


# ----------------------------------------------------------------------------- N5: advisory lock namespaces


def hold_lock(host: Host, namespace: int, key: str, seconds: float) -> threading.Thread:
    """Hold ``pg_advisory_xact_lock(namespace, hashtext(key))`` in a real transaction for ``seconds``."""
    held = threading.Event()

    def run() -> None:
        async def body(pool: asyncpg.Pool) -> None:
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1, hashtext($2))", namespace, key)
                held.set()
                await asyncio.sleep(seconds)

        host.db(body)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert held.wait(10)
    return t


def timed_create(a: Session) -> tuple[float, int]:
    start = time.monotonic()
    status = a.create().status
    return time.monotonic() - start, status


def test_share_cap_and_author_locks_use_distinct_two_argument_namespaces(host: Host) -> None:
    from herdr_artifact_host.db import LOCK_NS_AUTHOR, LOCK_NS_SHARE_CAP

    assert LOCK_NS_AUTHOR != LOCK_NS_SHARE_CAP
    s = lab(host)
    a = Session(host, s)
    assert a.create().status == 201
    identity = rows(host, s)[0]["author_identity"]
    # the share-cap lock of THIS share blocks a create for it ...
    t = hold_lock(host, LOCK_NS_SHARE_CAP, s.share_id, 1.5)
    waited, status = timed_create(a)
    t.join()
    assert status == 201 and waited >= 1.0
    # ... and the very same key text in the author namespace (or a different share's key) does not
    t = hold_lock(host, LOCK_NS_AUTHOR, s.share_id, 1.5)
    waited, status = timed_create(a)
    assert status == 201 and waited < 1.0
    t.join()
    t = hold_lock(host, LOCK_NS_SHARE_CAP, "some-other-share", 1.5)
    waited, status = timed_create(a)
    assert status == 201 and waited < 1.0
    t.join()
    # the author lock (identity key) blocks that author in its own namespace
    t = hold_lock(host, LOCK_NS_AUTHOR, identity, 1.5)
    waited, status = timed_create(a)
    t.join()
    assert status == 201 and waited >= 1.0


# ----------------------------------------------------------------------------- N1: hashing cannot starve the surface

HUGE = 160 * 1024 * 1024


def huge_share(host: Host):
    data = b"\xa5" * HUGE
    return host.seed_share({"index.html": b"<html><body>x</body></html>", "big.bin": data})


def fire_and_close(host: Host, path: str) -> None:
    """The reviewer's probe: connect, send a full GET, close at once."""
    try:
        with socket.create_connection(("127.0.0.1", host.port), timeout=5) as sock:
            sock.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    except OSError:
        pass


def test_a_connect_get_close_flood_of_a_huge_blob_does_not_starve_small_requests(
    host: Host,
) -> None:
    s = huge_share(host)
    big, small = f"/s/{s.token}/f/big.bin", f"/s/{s.token}/f/index.html"
    assert host.get(small).status == 200
    stop = threading.Event()

    def hammer() -> None:
        while not stop.is_set():
            fire_and_close(host, big)

    threads = [threading.Thread(target=hammer, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    try:
        time.sleep(0.5)  # the flood is in full swing
        slowest = 0.0
        for _ in range(15):
            start = time.monotonic()
            r = host.get(small)
            slowest = max(slowest, time.monotonic() - start)
            assert r.status == 200
        assert slowest < 1.0, f"a tiny GET took {slowest:.2f}s while a huge blob was being hammered"
        health_start = time.monotonic()
        assert host.health_get("/healthz").status == 200
        assert time.monotonic() - health_start < 1.0
    finally:
        stop.set()
        for t in threads:
            t.join(10)


def read_status_and_head(sock: socket.socket) -> bytes:
    sock.settimeout(30)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    if head.startswith(b"HTTP/1.1 503"):  # small body: read it fully
        length = int(head.lower().split(b"content-length: ")[1].split(b"\r\n")[0])
        while len(body) < length:
            body += sock.recv(4096)
        return head + b"\r\n\r\n" + body
    return data


def test_concurrent_full_blob_verifications_are_capped_and_saturation_is_a_503(host: Host) -> None:
    from herdr_artifact_host.share_http import UNAVAILABLE_BODY

    s = huge_share(host)
    big = f"/s/{s.token}/f/big.bin"
    cap = public_server.HASH_CONCURRENCY
    socks = [socket.create_connection(("127.0.0.1", host.port), timeout=30) for _ in range(cap + 4)]
    try:
        for sock in socks:
            sock.sendall(f"GET {big} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        heads = [read_status_and_head(sock) for sock in socks]
        busy = [h for h in heads if h.startswith(b"HTTP/1.1 503")]
        served = [h for h in heads if h.startswith(b"HTTP/1.1 200")]
        assert len(busy) >= 1 and len(busy) + len(served) == len(socks)
        assert len(served) <= cap + 1  # the cap, plus one that arrived after a slot freed
        for h in busy:
            assert b"Retry-After: 5" in h
            assert h.split(b"\r\n\r\n", 1)[1] == UNAVAILABLE_BODY
        # the 503 is only reachable for a token that resolved; an unknown token is still the uniform 404
        assert host.get("/s/unknowntoken/f/big.bin").shape() == host.get("/nothing").shape()
    finally:
        for sock in socks:
            sock.close()
    # slots are released: the next full read is served
    assert host.get(f"/s/{s.token}/f/index.html").status == 200
