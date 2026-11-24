# ruff: noqa: F811  (imported pytest fixtures are used as test arguments)
"""Artifact library, Task 7: the member web routes over a real ``ControlPlaneServer`` + ``MemberWeb`` (HTTP).

Reads come from the member's view file; every write enqueues one library intent (checked on disk, as the worker
would read it) and returns the ticket result.  Claims are not a member-web route (share_public records them)."""

from __future__ import annotations

import hashlib
import json

import pytest

from herdr_engineering.control_plane.artifact_members import member_owner_key
from herdr_engineering.control_plane.collab_bridge_files import atomic_write_json
from tests.control_plane.test_artifact_member_service import ART
from tests.control_plane.test_member_web import ORIGIN, req, stack  # noqa: F401

FLD = "fld_" + "a" * 24
BASE = "/api/member"


def intents(stack) -> list[dict]:
    root = stack.w.outbox / "intents"
    paths = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime_ns) if root.exists() else []
    return [json.loads(p.read_text()) for p in paths]


def put_library(stack, library, name="op"):
    key = member_owner_key(f"member:{name}")
    path = stack.w.bridge.mirror / "member-views" / f"{key}.json"
    doc = json.loads(path.read_text())
    atomic_write_json(path.parent, path.name, {**doc, "library": library})


def test_library_requires_session_401(stack):
    assert req(stack, "GET", f"{BASE}/library", member=None).status == 401
    assert req(stack, "POST", f"{BASE}/folders", body={"name": "x"}, member=None).status == 401


def test_library_read_returns_the_view_and_defaults_to_empty(stack):
    assert req(stack, "GET", f"{BASE}/library").json == {"items": [], "folders": []}
    lib = {
        "items": [{"artifact_id": ART, "status": "active"}],
        "folders": [{"folder_id": FLD, "name": "F"}],
    }
    put_library(stack, lib)
    got = req(stack, "GET", f"{BASE}/library")
    assert got.status == 200 and got.json == lib
    assert req(stack, "GET", f"{BASE}/library?x=1").status == 400


def test_folder_routes_round_trip(stack):
    calls = [
        (
            "POST",
            f"{BASE}/folders",
            {"name": "Papers"},
            "library_folder_create",
            {"name": "Papers"},
            None,
        ),
        (
            "PUT",
            f"{BASE}/folders/{FLD}",
            {"name": "Renamed"},
            "library_folder_rename",
            {"folder_id": FLD, "name": "Renamed"},
            None,
        ),
        (
            "POST",
            f"{BASE}/folders/{FLD}/items",
            {"artifact_id": ART},
            "library_file",
            {"folder_id": FLD},
            ART,
        ),
        (
            "DELETE",
            f"{BASE}/folders/{FLD}/items/{ART}",
            None,
            "library_unfile",
            {"folder_id": FLD},
            ART,
        ),
        (
            "DELETE",
            f"{BASE}/folders/{FLD}",
            None,
            "library_folder_delete",
            {"folder_id": FLD},
            None,
        ),
        ("POST", f"{BASE}/pending/{ART}/accept", None, "library_accept", {}, ART),
        ("POST", f"{BASE}/pending/{ART}/discard", None, "library_discard", {}, ART),
    ]
    for i, (method, path, body, kind, payload, art) in enumerate(calls, 1):
        got = req(stack, method, path, body=body)
        assert got.status == 200 and got.json["ticket"], (path, got.raw)
        sent = intents(stack)
        assert len(sent) == i
        last = sent[-1]
        assert (last["kind"], last["payload"], last.get("artifact_id")) == (kind, payload, art)
        assert last["actor"]["kind"] == "user"
        assert req(stack, "GET", f"{BASE}/results/{got.json['ticket']}").status == 200


@pytest.mark.parametrize(
    "method,path,body,status",
    [
        ("PUT", f"{BASE}/folders/fld_zz", {"name": "x"}, 404),
        ("DELETE", f"{BASE}/folders/{FLD}x", None, 404),
        ("POST", f"{BASE}/folders/fld_{'A' * 24}/items", {"artifact_id": ART}, 404),
        ("POST", f"{BASE}/folders/{FLD}/items", {"artifact_id": "../x"}, 400),
        ("POST", f"{BASE}/folders/{FLD}/items", {"artifact_id": ART, "extra": 1}, 400),
        ("POST", f"{BASE}/folders/{FLD}/items", {}, 400),
        ("POST", f"{BASE}/folders", {"name": "x", "y": 1}, 400),
        ("POST", f"{BASE}/folders", {}, 400),
        ("DELETE", f"{BASE}/folders/{FLD}/items/bad%20id", None, 404),
        ("POST", f"{BASE}/pending/-bad/accept", None, 400),
        ("POST", f"{BASE}/pending/{ART}/accept", {"x": 1}, 400),
        ("POST", f"{BASE}/pending/{ART}/other", None, 404),
    ],
)
def test_bad_ids_and_bodies_are_refused_before_any_intent(stack, method, path, body, status):
    assert req(stack, method, path, body=body).status == status
    assert intents(stack) == []


def test_claims_route_not_exposed_404(stack):
    # claims are recorded by share_public (events.jsonl); the member web cannot look a token up without a database
    for method in ("POST", "PUT", "DELETE", "GET"):
        got = req(
            stack, method, f"{BASE}/claims", body={"path": "/s/abc/"} if method != "GET" else None
        )
        assert got.status == 404, method
    assert intents(stack) == []


WRITES = [
    ("POST", f"{BASE}/folders", {"name": "x"}),
    ("PUT", f"{BASE}/folders/{FLD}", {"name": "x"}),
    ("DELETE", f"{BASE}/folders/{FLD}", None),
    ("POST", f"{BASE}/folders/{FLD}/items", {"artifact_id": ART}),
    ("DELETE", f"{BASE}/folders/{FLD}/items/{ART}", None),
    ("POST", f"{BASE}/pending/{ART}/accept", None),
    ("POST", f"{BASE}/pending/{ART}/discard", None),
]


@pytest.mark.parametrize("method,path,body", WRITES)
def test_origin_check_applies_to_all_writes(stack, method, path, body):
    assert req(stack, method, path, body=body, origin=None).status == 403
    assert req(stack, method, path, body=body, origin="https://evil.example").status == 403
    assert req(stack, method, path, body=body, origin=ORIGIN).status == 200
    assert len(intents(stack)) == 1


def test_a_non_member_cannot_write_the_library(stack):
    stack.w.person("mal")
    assert req(stack, "POST", f"{BASE}/folders", body={"name": "x"}, member="mal").status in (
        401,
        403,
    )
    assert intents(stack) == []


# ------------------------------------------------------------------------------ content by id
def pool(stack, data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    root = stack.w.bridge.mirror / "library-content"
    root.mkdir(exist_ok=True)
    (root / sha).write_bytes(data)
    return sha


def item(aid, files, status="active", source="mine", **extra):
    return {"artifact_id": aid, "status": status, "source": source, "files": files, **extra}


def entry(data: bytes, path="index.html", ctype="text/html"):
    return {
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "content_type": ctype,
    }


def content(stack, aid, path, **kw):
    return req(stack, "GET", f"{BASE}/artifacts/{aid}/content/{path}", **kw)


@pytest.mark.parametrize(
    "status,source",
    [("active", "mine"), ("active", "assigned"), ("active", "opened"), ("pending", "pending")],
)
def test_content_by_id_for_owner_assigned_and_claimed(stack, status, source):
    data = b"<p>hi</p>"
    pool(stack, data)
    put_library(stack, {"items": [item("art_x1", [entry(data)], status, source)], "folders": []})
    got = content(stack, "art_x1", "index.html")
    assert got.status == 200 and got.raw == data


@pytest.mark.parametrize("status", ["revoked", "expired"])
def test_content_by_id_refused_after_revoke_and_expiry(stack, status):
    data = b"x"
    pool(stack, data)
    put_library(stack, {"items": [item("art_x1", [entry(data)], status, "opened")], "folders": []})
    gone = content(stack, "art_x1", "index.html")
    put_library(
        stack, {"items": [item("art_x1", [entry(data)], "active", "opened")], "folders": []}
    )
    assert content(stack, "art_x1", "index.html").status == 200
    assert gone.status == 404 and gone.json == content(stack, "art_missing", "index.html").json


def test_content_by_id_for_stranger_404_same_as_missing(stack):
    data = b"secret"
    pool(stack, data)
    put_library(stack, {"items": [item("art_x1", [entry(data)])], "folders": []})
    stranger = content(stack, "art_x1", "index.html", member="lab")
    missing = content(stack, "art_zzz", "index.html")
    other_path = content(stack, "art_x1", "nope.html")
    assert stranger.status == missing.status == other_path.status == 404
    assert stranger.raw == missing.raw == other_path.raw
    assert content(stack, "art_x1", "index.html", member=None).status == 401


@pytest.mark.parametrize(
    "data,ctype,inline",
    [
        (b"<script>alert(1)</script>", "text/html", False),
        (b"plain", "text/plain", True),
        (b"\x89PNG", "image/png", True),
        (b"<svg onload=1/>", "image/svg+xml", False),
        (b"%PDF-", "application/pdf", True),
        (b"zz", "application/x-weird", False),
    ],
)
def test_content_has_sandbox_csp_and_nosniff(stack, data, ctype, inline):
    pool(stack, data)
    put_library(stack, {"items": [item("art_x1", [entry(data, "f.bin", ctype)])], "folders": []})
    got = content(stack, "art_x1", "f.bin")
    assert got.status == 200
    assert got.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-disposition"].startswith("inline" if inline else "attachment")


def test_content_path_traversal_and_globbing_rejected(stack):
    data = b"ok"
    pool(stack, data)
    put_library(
        stack, {"items": [item("art_x1", [entry(data, "a/b.txt", "text/plain")])], "folders": []}
    )
    assert content(stack, "art_x1", "a/b.txt").status == 200
    for bad in (
        "../a/b.txt",
        "a/../a/b.txt",
        "%2e%2e/a/b.txt",
        "a//b.txt",
        "./a/b.txt",
        "a/*",
        "a/b.tx",
        "a%2Fb.txt",
        "a\\b.txt",
        "a/b.txt/",
    ):
        assert content(stack, "art_x1", bad).status == 404, bad
    assert content(stack, "art_x1", "a/b.txt?x=1").status == 400


def test_content_is_refused_when_the_pooled_blob_changed_or_is_gone(stack):
    data = b"original"
    sha = pool(stack, data)
    put_library(stack, {"items": [item("art_x1", [entry(data)])], "folders": []})
    (stack.w.bridge.mirror / "library-content" / sha).write_bytes(b"tampered")
    assert content(stack, "art_x1", "index.html").status == 404
    (stack.w.bridge.mirror / "library-content" / sha).unlink()
    assert content(stack, "art_x1", "index.html").status == 404


def test_too_large_is_told_to_the_owner_only(stack):
    put_library(stack, {"items": [item("art_x1", [], too_large=True)], "folders": []})
    stack.w.view("op", ["art_x1"])
    put_library(stack, {"items": [item("art_x1", [], too_large=True)], "folders": []})
    assert content(stack, "art_x1", "index.html").json["error"] == "too_large"
    put_library(
        stack,
        {"items": [item("art_x1", [], source="assigned", too_large=True)], "folders": []},
        "lab",
    )
    assert content(stack, "art_x1", "index.html", member="lab").json["error"] == "not_found"
