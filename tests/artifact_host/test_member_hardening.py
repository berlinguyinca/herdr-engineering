"""Fix round 1 for the member surface: every finding of the security review, over real HTTP and real Postgres."""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from urllib.parse import quote

from herdr_artifact_host import member_session as ms
from herdr_artifact_host.member_api import CHUNK_BYTES, MemberLimits
from tests.artifact_host.member_support import (
    ORIGIN,
    call,
    create,
    exchange,
    publish_files,
    published,
    sha,
    sign_in,
    upload,
)
from tests.artifact_host.seed import MEMBER_SECRET, Host, access, ident, running_host

# ---------------------------------------------------------------- helpers


def run_headers(name: str, data: bytes, whole: bytes | None = None) -> dict[str, str]:
    whole = data if whole is None else whole
    return {
        "X-Name": quote(name),
        "X-Size-Bytes": str(len(whole)),
        "X-File-SHA256": sha(whole),
        "X-Run-SHA256": sha(data),
        "Content-Type": "application/octet-stream",
    }


def upload_id(host: Host, s: str, aid: str, name: str, data: bytes) -> str:
    spec = {"name": name, "size_bytes": len(data), "file_sha256": sha(data)}
    return call(host, s, "POST", f"/artifacts/{aid}/uploads/status", spec).json()["upload_id"]


def stage_run(host: Host, s: str, aid: str, name: str, whole: bytes, part: bytes | None = None):
    part = whole[:CHUNK_BYTES] if part is None else part
    uid = upload_id(host, s, aid, name, whole)
    return call(
        host,
        s,
        "PUT",
        f"/artifacts/{aid}/uploads/{uid}/runs/0",
        part,
        headers=run_headers(name, part, whole),
    )


def staged_files(host: Host) -> list[Path]:
    root = host.blob_root / "tmp" / "up"
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def blob_files(host: Host) -> list[Path]:
    return [p for p in (host.blob_root / "sha256").rglob("*") if p.is_file()]


def limited(migrated_dsn: str, tmp_path: Path, **limits: object):
    return running_host(migrated_dsn, tmp_path, {"member_limits": MemberLimits(**limits)})


# ---------------------------------------------------------------- I1


def test_approve_is_refused_while_sharing_is_disabled(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s)
    made = call(
        host, s, "POST", f"/artifacts/{aid}/shares", {"version_id": vid, "audience": "public"}
    ).json()
    host.execute("UPDATE member_usage SET sharing_disabled=true WHERE member_id='alice'")
    path = f"/artifacts/{aid}/shares/{made['share_id']}"
    r = call(host, s, "POST", f"{path}/approve", {"confirm": True})
    assert r.status == 403 and r.json()["error"] == "sharing_disabled"
    assert host.rows("SELECT state FROM shares WHERE share_id=$1", made["share_id"])[0][
        "state"
    ] == ("pending")
    # revoke and the event log stay open so the member can still clean up
    assert call(host, s, "GET", f"{path}/events").status == 200
    assert call(host, s, "POST", f"{path}/revoke").status == 200


# ---------------------------------------------------------------- I2


def test_junk_exchanges_from_one_client_do_not_block_another(host: Host) -> None:
    junk = {
        **ORIGIN,
        "Authorization": "Bearer junk",
        "X-Id-Token": "junk",
        "X-Nonce": "n",
        "X-Forwarded-For": "10.1.1.1",
    }
    codes = [host.request("POST", "/api/member/session", junk, b"").status for _ in range(30)]
    assert codes[0] == 401 and codes[-1] == 429
    assert exchange(host, "bob", forwarded="10.2.2.2").status == 200
    assert (
        exchange(host, "bob", forwarded="10.1.1.1").status == 429
    )  # the noisy client stays blocked


def test_successful_exchanges_are_not_counted(host: Host) -> None:
    assert all(exchange(host, "bob", forwarded="10.3.3.3").status == 200 for _ in range(30))


# ---------------------------------------------------------------- I3


def test_staging_is_capped_per_artifact_and_per_member(migrated_dsn, tmp_path) -> None:
    with limited(
        migrated_dsn, tmp_path, staging_bytes_per_member=1000, staging_bytes_per_artifact=600
    ) as host:
        s = sign_in(host)
        a, b, c = (create(host, s) for _ in range(3))
        assert stage_run(host, s, a, "x.bin", b"1" * 500).status == 200
        over = stage_run(host, s, a, "y.bin", b"2" * 500)  # 1000 on one artifact, cap 600
        assert over.status == 413 and over.json()["error"] == "staging_full"
        assert stage_run(host, s, b, "x.bin", b"3" * 500).status == 200
        over = stage_run(host, s, c, "x.bin", b"4" * 500)  # 1500 for the member, cap 1000
        assert over.status == 413 and over.json()["error"] == "staging_full"
        # sending the same run again replaces its own bytes instead of counting twice
        assert stage_run(host, s, a, "x.bin", b"1" * 500).status == 200
        # another member has their own allowance
        other = sign_in(host, "bob")
        assert stage_run(host, other, create(host, other), "x.bin", b"5" * 500).status == 200


def test_staging_cap_is_decided_before_the_body_is_read(migrated_dsn, tmp_path) -> None:
    with limited(migrated_dsn, tmp_path, staging_bytes_per_artifact=100) as host:
        s = sign_in(host)
        aid = create(host, s)
        whole = b"z" * 400
        uid = upload_id(host, s, aid, "x.bin", whole)
        headers = {
            "Authorization": f"Member {s}",
            **ORIGIN,
            **run_headers("x.bin", whole),
            "Content-Length": "400",
        }
        # headers only: the 413 arrives although not one body byte was sent
        conn = socket.create_connection(("127.0.0.1", host.port), timeout=5)
        try:
            lines = [f"PUT /api/member/artifacts/{aid}/uploads/{uid}/runs/0 HTTP/1.1", "Host: x"]
            lines += [f"{k}: {v}" for k, v in headers.items()]
            conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            assert conn.recv(4096).startswith(b"HTTP/1.1 413")
        finally:
            conn.close()


def test_delete_and_discard_remove_staged_bytes(host: Host) -> None:
    s = sign_in(host)
    aid = create(host, s)
    assert stage_run(host, s, aid, "x.bin", b"1" * 300).status == 200
    assert staged_files(host)
    assert call(host, s, "POST", f"/artifacts/{aid}/delete", {"confirm": True}).status == 200
    assert staged_files(host) == []
    host.execute(
        "INSERT INTO artifacts (artifact_id, owner, intake) VALUES ('art_pend0000000000000001','alice','pending')"
    )
    assert stage_run(host, s, "art_pend0000000000000001", "y.bin", b"2" * 300).status == 200
    assert staged_files(host)
    assert call(host, s, "POST", "/pending/art_pend0000000000000001/discard").status == 200
    assert staged_files(host) == []


def test_sweep_removes_only_stale_staging(host: Host) -> None:
    s = sign_in(host)
    old, fresh = create(host, s), create(host, s)
    assert stage_run(host, s, old, "x.bin", b"1" * 300).status == 200
    assert stage_run(host, s, fresh, "x.bin", b"2" * 300).status == 200
    stale = [p for p in staged_files(host) if old in p.parts]
    assert stale
    past = time.time() - 7200
    for p in stale:
        os.utime(p, (past, past))
        os.utime(p.parent, (past, past))
    removed = host.surface.member.svc.sweep_staging(3600)
    assert removed == 1
    remaining = staged_files(host)
    assert remaining and all(old not in p.parts for p in remaining)
    assert host.surface.member.svc.sweep_staging(3600) == 0


# ---------------------------------------------------------------- I4


def test_publish_past_quota_or_size_leaves_no_blob_files(host: Host) -> None:
    s = sign_in(host)
    host.execute(
        "INSERT INTO member_usage (member_id, max_member_bytes, max_artifact_bytes) VALUES ('alice', 10, 8) "
        "ON CONFLICT (member_id) DO UPDATE SET max_member_bytes=10, max_artifact_bytes=8"
    )
    aid = create(host, s)
    assert publish_files(host, s, aid, {"a.txt": b"x" * 9}).json()["error"] == "quota"  # 9 > 8
    host.execute("UPDATE member_usage SET max_artifact_bytes=100 WHERE member_id='alice'")
    assert publish_files(host, s, aid, {"a.txt": b"y" * 11}).json()["error"] == "quota"  # 11 > 10
    assert blob_files(host) == []


def test_failed_transaction_removes_the_blobs_it_wrote(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s, {"index.html": b"<html>kept</html>"})
    kept = blob_files(host)
    assert len(kept) == 1
    # stale base: the blob is written (the check is inside the transaction) and must be cleaned up again
    r = publish_files(host, s, aid, {"index.html": b"<html>new</html>"}, base=None)
    assert r.status == 409 and r.json()["error"] == "stale_base"
    assert blob_files(host) == kept
    # a blob another artifact references is never removed by a failed publish of the same content
    other = create(host, s)
    r = publish_files(host, s, other, {"index.html": b"<html>kept</html>"}, base="ver_wrong")
    assert r.status == 409
    assert blob_files(host) == kept


# ---------------------------------------------------------------- I5


def trickle(host: Host, s: str, aid: str, name: str, whole: bytes) -> socket.socket:
    uid = upload_id(host, s, aid, name, whole)
    headers = {
        "Authorization": f"Member {s}",
        **ORIGIN,
        **run_headers(name, whole),
        "Content-Length": str(len(whole)),
    }
    conn = socket.create_connection(("127.0.0.1", host.port), timeout=10)
    lines = [f"PUT /api/member/artifacts/{aid}/uploads/{uid}/runs/0 HTTP/1.1", "Host: x"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + whole[:5])  # then silence
    return conn


def test_a_trickling_upload_releases_its_slot_after_the_deadline(migrated_dsn, tmp_path) -> None:
    with limited(migrated_dsn, tmp_path, body_deadline_seconds=1.0, uploads_per_member=2) as host:
        s = sign_in(host)
        aid = create(host, s)
        held = [trickle(host, s, aid, f"t{i}.bin", bytes([i]) * 100) for i in range(2)]
        try:
            time.sleep(0.4)
            busy = stage_run(host, s, aid, "ok.bin", b"fine" * 10)
            assert busy.status == 429 and busy.json()["error"] == "busy"
            other = sign_in(host, "bob")  # slots are per member
            assert stage_run(host, other, create(host, other), "ok.bin", b"fine").status == 200
            for conn in held:  # the deadline ends the request
                assert conn.recv(4096).startswith(b"HTTP/1.1 408")
            assert stage_run(host, s, aid, "ok.bin", b"fine" * 10).status == 200
        finally:
            for conn in held:
                conn.close()


# ---------------------------------------------------------------- I6


def test_missing_members_json_fails_closed_by_default(migrated_dsn, tmp_path) -> None:
    with running_host(migrated_dsn, tmp_path, {"member_group_sync_required": True}) as host:
        assert exchange(host, "alice").status == 401
        (host.sync_dir / "members.json").write_text(
            json.dumps({"updated": time.time(), "groups": {"local.dev": ["bob"]}})
        )
        r = exchange(host, "bob")  # (alice's refusal is cached for a minute)
        assert r.status == 200
        # the full session length applies when the directory vouches for the member
        assert r.json()["expires_at"] > time.time() + 3000
        old = ms.mint_session(MEMBER_SECRET, "carol", time.time() + 600)
        assert call(host, old, "GET", "/me").status == 401


def test_default_config_requires_the_sync() -> None:
    from herdr_artifact_host.public_server import PublicConfig

    assert PublicConfig().member_group_sync_required is True


def test_claim_fallback_caps_the_session_at_fifteen_minutes(host: Host) -> None:
    r = exchange(host, "alice")  # the test host opted into the fallback (no members.json)
    assert r.status == 200
    assert r.json()["expires_at"] <= time.time() + 15 * 60 + 2


# ---------------------------------------------------------------- M2


def test_abort_rederives_the_upload_id(host: Host) -> None:
    s = sign_in(host)
    aid = create(host, s)
    whole = b"q" * 300
    uid = upload_id(host, s, aid, "x.bin", whole)
    assert stage_run(host, s, aid, "x.bin", whole).status == 200
    spec = {"name": "x.bin", "size_bytes": 300, "file_sha256": sha(whole)}
    wrong = {**spec, "name": "other.bin"}
    r = call(host, s, "POST", f"/artifacts/{aid}/uploads/{uid}/abort", wrong)
    assert r.status == 400 and staged_files(host)
    assert call(host, s, "POST", f"/artifacts/{aid}/uploads/{uid}/abort").status == 400  # no spec
    ok = call(host, s, "POST", f"/artifacts/{aid}/uploads/{uid}/abort", spec)
    assert ok.status == 200 and staged_files(host) == []


# ---------------------------------------------------------------- M3


def test_recorded_identity_expires_with_the_session(host: Host) -> None:
    s = sign_in(host)
    assert call(host, s, "GET", "/me").json()["email"] == "alice@lab.example"
    [row] = host.rows("SELECT identity_expires_at FROM member_usage WHERE member_id='alice'")
    assert row["identity_expires_at"] is not None
    host.execute(
        "UPDATE member_usage SET identity_expires_at = now() - interval '1 second' WHERE member_id='alice'"
    )
    assert call(host, s, "GET", "/me").json()["email"] == ""
    sign_in(host)  # a fresh sign-in vouches again
    assert call(host, s, "GET", "/me").json()["email"] == "alice@lab.example"


# ---------------------------------------------------------------- M4


def test_signed_out_token_is_rejected(host: Host) -> None:
    s = sign_in(host)
    other = sign_in(host, "bob")
    assert call(host, s, "GET", "/me").status == 200
    assert call(host, s, "POST", "/signout").status == 200
    assert call(host, s, "GET", "/me").status == 401
    assert call(host, other, "GET", "/me").status == 200  # only that member
    # a session without an issue time cannot be proven newer than the sign-out
    legacy = ms.mint_session(MEMBER_SECRET, "alice", time.time() + 600)
    assert call(host, legacy, "GET", "/me").status == 401
    time.sleep(0.01)
    fresh = sign_in(host)
    assert call(host, fresh, "GET", "/me").status == 200


# ---------------------------------------------------------------- M5


def test_content_is_streamed_with_a_concurrency_limit(migrated_dsn, tmp_path) -> None:
    with limited(migrated_dsn, tmp_path, content_streams=1) as host:
        s = sign_in(host)
        big = os.urandom(CHUNK_BYTES) * 34  # 17 MiB: more than the socket buffers hold
        aid = create(host, s)
        spec = upload(host, s, aid, "big.bin", big)
        assert (
            call(
                host,
                s,
                "POST",
                f"/artifacts/{aid}/publish",
                {"base_version_id": None, "files": [spec]},
            ).status
            == 200
        )
        stalled = socket.create_connection(("127.0.0.1", host.port), timeout=10)
        try:
            stalled.sendall(
                f"GET /api/member/artifacts/{aid}/content/big.bin HTTP/1.1\r\nHost: x\r\n"
                f"Authorization: Member {s}\r\n\r\n".encode()
            )
            deadline, status = time.time() + 5, 200
            while status == 200 and time.time() < deadline:
                time.sleep(0.2)
                status = call(host, s, "GET", f"/artifacts/{aid}/content/big.bin").status
            assert status == 503
        finally:
            stalled.close()
        time.sleep(0.5)
        r = call(host, s, "GET", f"/artifacts/{aid}/content/big.bin")
        assert r.status == 200 and r.body == big and r.header("Content-Length") == str(len(big))


# ---------------------------------------------------------------- M7


def test_a_capped_group_claim_is_not_recorded(host: Host) -> None:
    groups = ("local.dev",) + tuple(f"g{i}" for i in range(70))
    assert exchange(host, "alice", groups=groups).status == 200
    assert host.rows("SELECT groups FROM member_usage WHERE member_id='alice'")[0]["groups"] == []
    assert exchange(host, "bob", groups=("local.dev", "lab")).status == 200
    assert set(host.rows("SELECT groups FROM member_usage WHERE member_id='bob'")[0]["groups"]) == {
        "local.dev",
        "lab",
    }


def test_approve_honours_the_idempotency_key(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s)
    sid = call(
        host, s, "POST", f"/artifacts/{aid}/shares", {"version_id": vid, "audience": "public"}
    ).json()["share_id"]
    path = f"/artifacts/{aid}/shares/{sid}/approve"
    assert (
        call(host, s, "POST", path, {"confirm": True, "idempotency_key": "key-12345678"}).status
        == 200
    )
    again = call(host, s, "POST", path, {"confirm": True, "idempotency_key": "key-12345678"})
    assert again.status == 200 and again.json()["state"] == "live"
    assert (
        call(host, s, "POST", path, {"confirm": True, "idempotency_key": "key-other-123"}).status
        == 409
    )
    assert call(host, s, "POST", path, {"confirm": True}).status == 409
    assert call(host, s, "POST", path, {"confirm": True, "idempotency_key": "x"}).status == 400
    assert [e["kind"] for e in host.events(sid)].count("approved") == 1


# ---------------------------------------------------------------- M8


def test_the_id_token_nonce_must_match(host: Host) -> None:
    assert exchange(host, "alice", nonce="abc", sent_nonce="abc").status == 200
    assert exchange(host, "alice", nonce="abc", sent_nonce="other").status == 401
    plain = {
        **ORIGIN,
        "Authorization": f"Bearer {access('alice', groups=('local.dev',))}",
        "X-Id-Token": ident("alice", email="alice@lab.example"),  # no nonce claim
    }
    assert (
        host.request("POST", "/api/member/session", {**plain, "X-Nonce": "abc"}, b"").status == 401
    )
    assert host.request("POST", "/api/member/session", plain, b"").status == 401  # no header
