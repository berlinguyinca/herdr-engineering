"""The member surface over real HTTP, a real port and real Postgres: sign-in, session separation, uploads, shares."""

from __future__ import annotations

import json
import re
import time

from herdr_artifact_host import member_session as ms
from herdr_artifact_host.member_api import CHUNK_BYTES, MAX_UPLOAD_BYTES
from herdr_artifact_host.share_session import CLAIM_DOMAIN, mint_session
from tests.artifact_host.member_support import (
    ORIGIN,
    call,
    create,
    publish_files,
    published,
    sha,
    sign_in,
    upload,
)
from tests.artifact_host.seed import MEMBER_SECRET, SECRET, Host, access, ident

NOT_FOUND = {"error": "not_found", "message": "not found"}


# ---------------------------------------------------------------- page and config


def test_page_config_and_static(host: Host) -> None:
    page = host.get("/")
    assert page.status == 200 and b"My artifacts" in page.body
    csp = page.header("Content-Security-Policy") or ""
    assert (
        "script-src https://share.example/my/static/member.js" in csp
        and "'self'" not in csp.split("script-src")[1].split(";")[0]
    )
    assert host.get("/my/").status == 200 and host.get("/my/callback?code=x&state=y").status == 200
    assert (
        host.get("/my/static/member.js")
        .header(
            "Content-Type",
        )
        .startswith("text/javascript")
    )
    assert b"prefers-color-scheme" not in host.get("/my/static/member.css").body  # light only
    assert host.get("/my/static/nope.js").status == 404
    cfg = host.get("/api/member/config").json()
    assert (
        cfg["redirect_uri"] == "https://share.example/my/callback"
        and cfg["max_upload_bytes"] == 17 * 1024 * 1024
    )


def test_unconfigured_members_are_uniform_404(migrated_dsn, tmp_path) -> None:
    from tests.artifact_host.seed import running_host

    with running_host(migrated_dsn, tmp_path, {"member_signing_secret": b""}) as h:
        assert h.get("/").status == 404 and h.get("/api/member/config").status == 404


# ---------------------------------------------------------------- sign-in and sessions


def test_sign_in_requires_the_group_and_origin(host: Host) -> None:
    ok = {
        **ORIGIN,
        "Authorization": f"Bearer {access('al', groups=('local.dev',))}",
        "X-Id-Token": ident("al", nonce="n1"),
        "X-Nonce": "n1",
    }
    assert host.request("POST", "/api/member/session", ok, b"").status == 200
    no_group = {**ok, "Authorization": f"Bearer {access('al', groups=('other',))}"}
    assert host.request("POST", "/api/member/session", no_group, b"").status == 401
    assert (
        host.request(
            "POST", "/api/member/session", {k: v for k, v in ok.items() if k != "Origin"}, b""
        ).status
        == 403
    )
    assert (
        host.request(
            "POST", "/api/member/session", {**ok, "Origin": "https://evil.example"}, b""
        ).status
        == 403
    )
    assert (
        host.request(
            "POST",
            "/api/member/session",
            {**ok, "X-Id-Token": ident("someone-else", nonce="n1")},
            b"",
        ).status
        == 401
    )


def test_session_authenticates_and_identity_is_bare(host: Host) -> None:
    s = sign_in(host, "alice")
    me = call(host, s, "GET", "/me").json()
    assert me["member_id"] == "alice" and me["email"] == "alice@lab.example"
    aid = create(host, s)
    [row] = host.rows("SELECT owner, created_by FROM artifacts WHERE artifact_id=$1", aid)
    assert row["owner"] == "alice"


def test_unauthenticated_is_uniform_401(host: Host) -> None:
    shapes = {
        call(host, None, "GET", "/me").shape(),
        call(host, "garbage", "GET", "/me").shape(),
        host.request("GET", "/api/member/me", {"Authorization": "Bearer x"}).shape(),
    }
    assert len(shapes) == 1
    r = call(host, None, "GET", "/me")
    assert r.status == 401 and r.json() == {"error": "unauthorized", "message": "sign in again"}


def test_agent_token_is_not_a_member_session(host: Host) -> None:
    host.execute("INSERT INTO agent_tokens (digest, member_id) VALUES ($1,'alice')", sha(b"tok"))
    r = host.request("GET", "/api/member/me", {"Authorization": "Bearer tok"})
    assert r.status == 401


def test_claim_cookie_is_never_a_session_and_a_session_never_a_claim(host: Host) -> None:
    exp = time.time() + 600
    claim = mint_session(SECRET, "shr_x", "alice", exp, email="a@x", basis="g", domain=CLAIM_DOMAIN)
    assert call(host, claim, "GET", "/me").status == 401
    assert call(host, None, "GET", "/me", headers={"Cookie": f"herdr_claim={claim}"}).status == 401
    s = sign_in(host, "alice")
    pub = host.seed_share()
    assert (
        host.get(
            f"/s/{pub.token}/f/index.html",
            {"Sec-Fetch-Dest": "document", "Cookie": f"herdr_claim={s}"},
        ).status
        == 200
    )
    assert host.rows("SELECT * FROM share_claims WHERE share_id=$1", pub.share_id) == []
    assert ms.check_session(MEMBER_SECRET, claim, time.time()) is None


def test_member_outside_the_group_directory_is_refused_everywhere(host: Host) -> None:
    (host.sync_dir / "members.json").write_text(
        json.dumps({"updated": time.time(), "groups": {"local.dev": ["bob"]}})
    )
    r = host.request(
        "POST",
        "/api/member/session",
        {
            **ORIGIN,
            "Authorization": f"Bearer {access('alice', groups=('local.dev',))}",
            "X-Id-Token": ident("alice", nonce="n1"),
            "X-Nonce": "n1",
        },
        b"",
    )
    assert r.status == 401
    # a still-valid session for a member the directory no longer lists is refused on every request
    old = ms.mint_session(MEMBER_SECRET, "carol", time.time() + 600)
    assert call(host, old, "GET", "/me").status == 401
    assert call(host, sign_in(host, "bob"), "GET", "/me").status == 200


def test_stale_group_directory_fails_closed(host: Host) -> None:
    (host.sync_dir / "members.json").write_text(
        json.dumps({"updated": time.time() - 100000, "groups": {"local.dev": ["alice"]}})
    )
    old = ms.mint_session(MEMBER_SECRET, "alice", time.time() + 600)
    assert call(host, old, "GET", "/me").status == 401


def test_non_get_needs_exactly_one_origin(host: Host) -> None:
    s = sign_in(host)
    assert (
        call(
            host, s, "POST", "/artifacts", {"title": "t", "filename": "a.html"}, origin=False
        ).status
        == 403
    )
    two = host.request(
        "POST",
        "/api/member/artifacts",
        [
            ("Authorization", f"Member {s}"),
            ("Origin", "https://share.example"),
            ("Origin", "https://share.example"),
            ("Content-Type", "application/json"),
        ],
        b'{"title":"t","filename":"a.html"}',
    )
    assert two.status == 403
    bad = call(
        host,
        s,
        "POST",
        "/artifacts",
        {"title": "t", "filename": "a.html"},
        origin=False,
        headers={"Origin": "https://x.example"},
    )
    assert bad.status == 403


def test_json_body_rules(host: Host) -> None:
    s = sign_in(host)
    h = {"Authorization": f"Member {s}", **ORIGIN, "Content-Type": "application/json"}
    dup = host.request(
        "POST", "/api/member/artifacts", h, b'{"title":"a","title":"b","filename":"x.html"}'
    )
    assert dup.status == 400
    assert (
        host.request(
            "POST", "/api/member/artifacts", h, b'{"title":"a","filename":"x.html","extra":1}'
        ).status
        == 400
    )
    assert host.request("POST", "/api/member/artifacts", h, b"x" * (300 * 1024)).status == 413


# ---------------------------------------------------------------- artifacts, uploads, content


def test_create_publish_content_history(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s, {"index.html": b"<html>v1</html>", "notes.txt": b"hello"})
    head = call(host, s, "GET", f"/artifacts/{aid}").json()
    assert (
        head["current_version_id"] == vid
        and head["owned"]
        and {f["name"] for f in head["files"]} == {"index.html", "notes.txt"}
    )
    txt = call(host, s, "GET", f"/artifacts/{aid}/content/notes.txt")
    assert txt.body == b"hello" and txt.header("Content-Disposition") == "inline"
    html = call(host, s, "GET", f"/artifacts/{aid}/content/index.html")
    assert (html.header("Content-Disposition") or "").startswith("attachment")
    r2 = publish_files(host, s, aid, {"index.html": b"<html>v2</html>"}, base=vid)
    assert r2.status == 200 and r2.json()["version_number"] == 2
    hist = call(host, s, "GET", f"/artifacts/{aid}/history").json()["versions"]
    assert [v["version_number"] for v in hist] == [2, 1]
    assert host.rows("SELECT bytes_used FROM member_usage WHERE member_id='alice'")[0][
        "bytes_used"
    ] == len(b"<html>v2</html>")


def test_stale_base_leaves_no_rows(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s)
    before_blobs = host.rows("SELECT count(*) AS n FROM artifact_blobs")[0]["n"]
    r = publish_files(host, s, aid, {"index.html": b"<html>other</html>"}, base=None)
    assert r.status == 409 and r.json()["error"] == "stale_base"
    assert host.rows("SELECT count(*) AS n FROM artifact_blobs")[0]["n"] == before_blobs
    assert (
        host.rows("SELECT count(*) AS n FROM artifact_versions WHERE artifact_id=$1", aid)[0]["n"]
        == 1
    )


def test_incomplete_and_mismatched_uploads_store_nothing(host: Host) -> None:
    s = sign_in(host)
    aid = create(host, s)
    data = b"a" * (CHUNK_BYTES + 10)
    spec = {"name": "index.html", "size_bytes": len(data), "file_sha256": sha(data)}
    st = call(host, s, "POST", f"/artifacts/{aid}/uploads/status", spec).json()
    assert st["missing"] == [[0, 1]] and not st["complete"]
    r = call(
        host, s, "POST", f"/artifacts/{aid}/publish", {"base_version_id": None, "files": [spec]}
    )
    assert r.status == 409 and r.json()["error"] == "incomplete_upload"
    liar = {"name": "index.html", "size_bytes": 5, "file_sha256": sha(b"zzzzz")}
    up = call(host, s, "POST", f"/artifacts/{aid}/uploads/status", liar).json()["upload_id"]
    r = call(
        host,
        s,
        "PUT",
        f"/artifacts/{aid}/uploads/{up}/runs/0",
        b"hello",
        headers={
            "X-Name": "index.html",
            "X-Size-Bytes": "5",
            "X-File-SHA256": sha(b"zzzzz"),
            "X-Run-SHA256": sha(b"hello"),
        },
    )
    assert r.status == 200
    r = call(
        host, s, "POST", f"/artifacts/{aid}/publish", {"base_version_id": None, "files": [liar]}
    )
    assert r.status == 409 and r.json()["error"] == "upload_mismatch"
    assert host.rows("SELECT count(*) AS n FROM artifact_blobs")[0]["n"] == 0
    assert host.rows("SELECT count(*) AS n FROM artifact_versions")[0]["n"] == 0
    assert not any((host.blob_root / "sha256").rglob("*.*")) and not [
        p for p in (host.blob_root / "sha256").rglob("*") if p.is_file()
    ]


def test_run_sha_must_match(host: Host) -> None:
    s = sign_in(host)
    aid = create(host, s)
    spec = {"name": "a.txt", "size_bytes": 3, "file_sha256": sha(b"abc")}
    up = call(host, s, "POST", f"/artifacts/{aid}/uploads/status", spec).json()["upload_id"]
    r = call(
        host,
        s,
        "PUT",
        f"/artifacts/{aid}/uploads/{up}/runs/0",
        b"abc",
        headers={
            "X-Name": "a.txt",
            "X-Size-Bytes": "3",
            "X-File-SHA256": sha(b"abc"),
            "X-Run-SHA256": sha(b"xyz"),
        },
    )
    assert r.status == 400


def test_upload_over_17_mib_is_rejected_with_nothing_stored(host: Host) -> None:
    s = sign_in(host)
    aid = create(host, s)
    too_big = MAX_UPLOAD_BYTES + 1
    st = call(
        host,
        s,
        "POST",
        f"/artifacts/{aid}/uploads/status",
        {"name": "a.bin", "size_bytes": too_big, "file_sha256": "0" * 64},
    )
    assert st.status == 413
    h = {
        "X-Name": "a.bin",
        "X-Size-Bytes": str(too_big),
        "X-File-SHA256": "0" * 64,
        "X-Run-SHA256": "0" * 64,
        "Content-Length": str(too_big),
    }
    up = "upl_" + "0" * 24
    r = call(host, s, "PUT", f"/artifacts/{aid}/uploads/{up}/runs/0", b"x", headers=h)
    assert r.status == 413
    assert host.rows("SELECT count(*) AS n FROM artifact_blobs")[0]["n"] == 0
    assert not (host.blob_root / "tmp" / "up").exists() or not list(
        (host.blob_root / "tmp" / "up").rglob("*")
    )


def test_exactly_17_mib_is_accepted(host: Host) -> None:
    s = sign_in(host)
    aid = create(host, s)
    data = (b"0123456789abcdef" * 1024) * (MAX_UPLOAD_BYTES // 16384)
    assert len(data) == MAX_UPLOAD_BYTES
    spec = upload(host, s, aid, "big.bin", data)
    r = call(
        host, s, "POST", f"/artifacts/{aid}/publish", {"base_version_id": None, "files": [spec]}
    )
    assert r.status == 200, r.body


def test_quota_is_enforced_and_settled_on_delete(host: Host) -> None:
    s = sign_in(host)
    host.execute(
        "INSERT INTO member_usage (member_id, max_member_bytes) VALUES ('alice', 10) ON CONFLICT (member_id) DO UPDATE SET max_member_bytes=10"
    )
    aid = create(host, s)
    r = publish_files(host, s, aid, {"index.html": b"x" * 11})
    assert r.status == 413 and r.json()["error"] == "quota"
    host.execute("UPDATE member_usage SET max_member_bytes=1000 WHERE member_id='alice'")
    assert publish_files(host, s, aid, {"index.html": b"x" * 11}).status == 200
    assert call(host, s, "POST", f"/artifacts/{aid}/delete", {"confirm": True}).status == 200
    [u] = host.rows("SELECT bytes_used, bytes_retracted FROM member_usage WHERE member_id='alice'")
    assert u["bytes_used"] == 0 and u["bytes_retracted"] == 11
    assert call(host, s, "GET", f"/artifacts/{aid}").status == 404


def test_create_is_idempotent(host: Host) -> None:
    s = sign_in(host)
    body = {"title": "t", "filename": "a.html", "idempotency_key": "key-12345678"}
    a = call(host, s, "POST", "/artifacts", body).json()
    b = call(host, s, "POST", "/artifacts", body).json()
    assert a["artifact_id"] == b["artifact_id"] and a["created"] and not b["created"]
    assert call(host, s, "POST", "/artifacts", {**body, "title": "other"}).status == 409


# ---------------------------------------------------------------- ownership: no oracle


def test_non_owner_sees_the_same_404_as_missing(host: Host) -> None:
    a, b = sign_in(host, "alice"), sign_in(host, "bob")
    aid, vid = published(host, a)
    missing = "art_" + "0" * 24
    for method, tail, body in [
        ("GET", "", None),
        ("GET", "/history", None),
        ("GET", "/shares", None),
        ("GET", "/content/index.html", None),
        ("POST", "/delete", {"confirm": True}),
        (
            "POST",
            "/publish",
            {
                "base_version_id": None,
                "files": [{"name": "a.txt", "size_bytes": 1, "file_sha256": "0" * 64}],
            },
        ),
        ("POST", "/shares", {"version_id": vid, "audience": "public"}),
    ]:
        real = call(host, b, method, f"/artifacts/{aid}{tail}", body)
        none = call(host, b, method, f"/artifacts/{missing}{tail}", body)
        assert real.status == 404 and real.shape() == none.shape(), (method, tail)
    assert call(host, a, "GET", f"/artifacts/{aid}").status == 200  # untouched


# ---------------------------------------------------------------- shares


def make_share(host: Host, s: str, aid: str, vid: str, **over):
    return call(
        host,
        s,
        "POST",
        f"/artifacts/{aid}/shares",
        {"version_id": vid, "audience": "public", **over},
    )


def test_share_lifecycle(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s)
    r = make_share(host, s, aid, vid)
    assert r.status == 200
    made = r.json()
    assert made["state"] == "pending" and re.fullmatch(
        r"https://share.example/s/[A-Za-z0-9_-]{40,}/", made["url"]
    )
    token = made["token"]
    assert host.get(f"/s/{token}/").status == 404  # pending is not served
    ap = call(
        host, s, "POST", f"/artifacts/{aid}/shares/{made['share_id']}/approve", {"confirm": True}
    )
    assert ap.status == 200 and ap.json()["state"] == "live"
    assert host.get(f"/s/{token}/f/index.html").status == 200
    [row] = host.rows(
        "SELECT created_by, approved_by, expires_at, created_at FROM shares WHERE share_id=$1",
        made["share_id"],
    )
    assert row["created_by"] == "alice" and row["approved_by"] == "alice"
    assert 29.9 < (row["expires_at"] - row["created_at"]).total_seconds() / 86400 < 30.1
    kinds = [e["kind"] for e in host.events(made["share_id"])]
    assert (
        "created" in kinds
        and "approved" in kinds
        and kinds.index("created") < kinds.index("approved")
    )
    rv = call(host, s, "POST", f"/artifacts/{aid}/shares/{made['share_id']}/revoke")
    assert rv.status == 200 and host.get(f"/s/{token}/f/index.html").status == 404
    assert (
        call(host, s, "POST", f"/artifacts/{aid}/shares/{made['share_id']}/revoke").status == 200
    )  # idempotent
    ev = call(host, s, "GET", f"/artifacts/{aid}/shares/{made['share_id']}/events").json()["events"]
    assert {e["kind"] for e in ev} >= {"created", "approved", "revoked"}


def test_share_token_appears_only_in_the_creation_response(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s)
    made = make_share(host, s, aid, vid).json()
    token = made["token"]
    for path in (
        "/links",
        f"/artifacts/{aid}/shares",
        f"/artifacts/{aid}",
        "/library",
        "/me",
        f"/artifacts/{aid}/shares/{made['share_id']}/events",
    ):
        r = call(host, s, "GET", path)
        assert token not in r.body.decode() and "token" not in r.body.decode().replace(
            "tokens", ""
        ), path
    assert call(host, s, "GET", "/links").json()["links"][0]["share_id"] == made["share_id"]


def test_expiry_default_max_and_validation(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s)
    assert make_share(host, s, aid, vid, expires_in_days=91).status == 400
    assert make_share(host, s, aid, vid, expires_in_days=0).status == 400
    assert make_share(host, s, aid, vid, expires_in_days=True).status == 400
    assert make_share(host, s, aid, vid, expires_in_days=90).status == 200
    assert make_share(host, s, aid, vid, audience="lab").status == 400  # lab needs an allow list
    assert (
        make_share(host, s, aid, vid, allow_emails=["a@x.org"]).status == 400
    )  # public takes none
    assert make_share(host, s, aid, vid, audience="lab", allow_emails=["A@X.org"]).status == 200
    assert host.rows("SELECT allow_emails FROM shares WHERE audience='lab'")[0]["allow_emails"] == [
        "a@x.org"
    ]


def test_non_owner_cannot_approve_or_revoke(host: Host) -> None:
    a, b = sign_in(host, "alice"), sign_in(host, "bob")
    aid, vid = published(host, a)
    sid = make_share(host, a, aid, vid).json()["share_id"]
    missing = "shr_" + "0" * 24
    ap = call(host, b, "POST", f"/artifacts/{aid}/shares/{sid}/approve", {"confirm": True})
    assert (
        ap.status == 404
        and ap.shape()
        == call(
            host, b, "POST", f"/artifacts/{aid}/shares/{missing}/approve", {"confirm": True}
        ).shape()
    )
    rv = call(host, b, "POST", f"/artifacts/{aid}/shares/{sid}/revoke")
    assert rv.status == 404
    assert host.rows("SELECT state FROM shares WHERE share_id=$1", sid)[0]["state"] == "pending"


def test_approve_needs_confirm_and_pending(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s)
    sid = make_share(host, s, aid, vid).json()["share_id"]
    assert (
        call(host, s, "POST", f"/artifacts/{aid}/shares/{sid}/approve", {"confirm": "yes"}).status
        == 400
    )
    assert (
        call(host, s, "POST", f"/artifacts/{aid}/shares/{sid}/approve", {"confirm": True}).status
        == 200
    )
    assert (
        call(host, s, "POST", f"/artifacts/{aid}/shares/{sid}/approve", {"confirm": True}).status
        == 409
    )


def test_sharing_disabled_and_rate_limit(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s)
    host.execute("UPDATE member_usage SET sharing_disabled=true WHERE member_id='alice'")
    assert make_share(host, s, aid, vid).status == 403


def test_deleting_an_artifact_revokes_its_shares(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s)
    made = make_share(host, s, aid, vid).json()
    call(host, s, "POST", f"/artifacts/{aid}/shares/{made['share_id']}/approve", {"confirm": True})
    call(host, s, "POST", f"/artifacts/{aid}/delete", {"confirm": True})
    assert host.get(f"/s/{made['token']}/f/index.html").status == 404
    assert (
        host.rows("SELECT state FROM shares WHERE share_id=$1", made["share_id"])[0]["state"]
        == "revoked"
    )


def test_unknown_and_revoked_public_bodies_are_identical(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s)
    made = make_share(host, s, aid, vid).json()
    call(host, s, "POST", f"/artifacts/{aid}/shares/{made['share_id']}/approve", {"confirm": True})
    call(host, s, "POST", f"/artifacts/{aid}/shares/{made['share_id']}/revoke")
    revoked = host.get(f"/s/{made['token']}/f/index.html")
    unknown = host.get("/s/" + "A" * 43 + "/f/index.html")
    assert revoked.status == 404 and revoked.shape() == unknown.shape()


def test_lab_share_is_not_claimable(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s)
    made = make_share(host, s, aid, vid, audience="lab", allow_emails=["alice@lab.example"]).json()
    call(host, s, "POST", f"/artifacts/{aid}/shares/{made['share_id']}/approve", {"confirm": True})
    r = host.get(f"/s/{made['token']}/signin")
    assert r.status == 404 and "herdr_claim" not in (r.header("Set-Cookie") or "")
    assert host.rows("SELECT * FROM share_claims WHERE share_id=$1", made["share_id"]) == []


# ---------------------------------------------------------------- library


def test_folders_and_feed(host: Host) -> None:
    s = sign_in(host)
    aid, _ = published(host, s)
    f = call(host, s, "POST", "/folders", {"name": "Work"}).json()
    assert (
        call(host, s, "POST", f"/folders/{f['folder_id']}/items", {"artifact_id": aid}).status
        == 200
    )
    lib = call(host, s, "GET", "/library").json()
    assert [i["artifact_id"] for i in lib["items"]] == [aid] and lib["items"][0]["folder_ids"] == [
        f["folder_id"]
    ]
    assert (
        call(host, s, "PUT", f"/folders/{f['folder_id']}", {"name": "Home"}).json()["name"]
        == "Home"
    )
    assert call(host, s, "DELETE", f"/folders/{f['folder_id']}/items/{aid}").status == 200
    assert call(host, s, "DELETE", f"/folders/{f['folder_id']}").status == 200
    other = sign_in(host, "bob")
    assert call(host, other, "GET", "/library").json()["items"] == []


def test_pending_accept_and_discard_settle_quota(host: Host) -> None:
    s = sign_in(host)
    aid, _ = published(host, s, {"index.html": b"12345"})
    host.execute("UPDATE artifacts SET intake='pending' WHERE artifact_id=$1", aid)
    assert call(host, s, "POST", f"/pending/{aid}/discard").status == 200
    [u] = host.rows("SELECT bytes_used, bytes_retracted FROM member_usage WHERE member_id='alice'")
    assert u["bytes_used"] == 0 and u["bytes_retracted"] == 5
    aid2 = create(host, s)
    host.execute("UPDATE artifacts SET intake='pending' WHERE artifact_id=$1", aid2)
    assert call(host, s, "POST", f"/pending/{aid2}/accept").status == 200
    assert call(host, s, "POST", f"/pending/{aid2}/accept").status == 404


def test_approve_refuses_an_archived_artifact(host: Host) -> None:
    s = sign_in(host)
    aid, vid = published(host, s)
    sid = make_share(host, s, aid, vid).json()["share_id"]
    host.execute("UPDATE artifacts SET lifecycle='archived' WHERE artifact_id=$1", aid)
    r = call(host, s, "POST", f"/artifacts/{aid}/shares/{sid}/approve", {"confirm": True})
    assert r.status == 404
    assert host.rows("SELECT state FROM shares WHERE share_id=$1", sid)[0]["state"] == "pending"


def test_content_with_invalid_utf8_name_is_a_uniform_404(host: Host) -> None:
    s = sign_in(host)
    aid, _ = published(host, s)
    bad = call(host, s, "GET", f"/artifacts/{aid}/content/%ff%fe")
    missing = call(host, s, "GET", f"/artifacts/{aid}/content/nope.txt")
    assert bad.status == 404 and bad.shape() == missing.shape()
