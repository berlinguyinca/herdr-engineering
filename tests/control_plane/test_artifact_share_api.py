"""Owner API for artifact share links over real HTTP: create (dry run), approve (frozen bundle), list, revoke, events."""
import asyncio
import copy
import hashlib
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_shares import ShareError
from herdr_engineering.control_plane.artifacts import ArtifactError, ArtifactStore
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_artifact_report_store import Blobs as ScopeBlobs
from tests.control_plane.test_artifact_report_store import Connection as ScopeConnection

AID = "artifact_share"
OWNER = hashlib.sha256(b"owner-key").hexdigest()[:16]
MIB = 1024 * 1024


def key_for(sha):
    return f"sha256/{sha[:2]}/{sha[2:4]}/{sha}"


def output(name, data, *, media_type="text/html", **overrides):
    sha = hashlib.sha256(data).hexdigest()
    value = {"name": name, "storage_key": key_for(sha), "sha256": sha, "size_bytes": len(data),
             "media_type": media_type}
    value.update(overrides)
    return value


def report_document():
    sha = hashlib.sha256(b"evidence").hexdigest()
    return {"schema_version": 1, "title": "Generic investigation", "summary": "Open evidence",
            "questions": [{"id": "q1", "text": "What is known?"}],
            "evidence": [{"id": "e1", "title": "Account", "description": "Observed information.",
                          "asset_ids": ["data1"]}],
            "findings": [{"id": "f1", "title": "Uncertainty", "statement": "Collect observations.",
                          "question_ids": ["q1"], "evidence_ids": ["e1"]}],
            "figures": [], "assets": [{"id": "data1", "name": "data.csv", "role": "data",
                                       "media_type": "text/csv", "sha256": sha, "size_bytes": 8}]}


class Bytes:
    """RustFS stand-in: content-addressed bytes, served in small chunks, counting reads."""

    def __init__(self):
        self.objects = {}
        self.downloads = 0

    def add(self, data):
        sha = hashlib.sha256(data).hexdigest()
        self.objects[key_for(sha)] = data
        return sha

    def iter_get(self, key, sha, *, chunk_size=MIB):
        self.downloads += 1
        data = self.objects[key]
        for start in range(0, len(data), 7):
            yield data[start:start + 7]


class Registry:
    """The artifact registry calls the share API makes (async, like ArtifactStore behind the adapter)."""

    def __init__(self, blobs):
        self.blobs = blobs
        self.versions = {}
        self.rendered = "passed"
        self.foreign = set()          # version ids whose outputs include a blob the artifact does not own
        index = b"<!doctype html><title>x</title><script src='js/app.js'></script>"
        app = b"console.log('hi')"
        table = b"PAR1" + b"\x00" * 40 + b"PAR1"
        for data in (index, app, table, b"%PDF-1.4 fake", b"a,b\n1,2\n", b"evidence"):
            blobs.add(data)
        self.versions["v_html"] = {"version_id": "v_html", "artifact_id": AID, "state": "ready",
                                   "output_manifest": [output("index.html", index),
                                                       output("js/app.js", app, media_type="text/javascript"),
                                                       output("data/deep/t.parquet", table,
                                                              media_type="application/octet-stream")]}
        self.versions["v_pdf"] = {"version_id": "v_pdf", "artifact_id": AID, "state": "ready",
                                  "output_manifest": [output("paper.pdf", b"%PDF-1.4 fake",
                                                             media_type="application/pdf")]}
        self.versions["v_building"] = {**copy.deepcopy(self.versions["v_pdf"]), "version_id": "v_building",
                                       "state": "building"}
        self.versions["v_csv"] = {"version_id": "v_csv", "artifact_id": AID, "state": "ready",
                                  "output_manifest": [output("t.csv", b"a,b\n1,2\n", media_type="text/csv")]}
        self.versions["v_report"] = {"version_id": "v_report", "artifact_id": AID, "state": "ready",
                                     "report_descriptor": report_document(),
                                     "output_manifest": [output("data.csv", b"evidence", media_type="text/csv")]}

    def sync_call(self, method, *args, **kwargs):
        return asyncio.run(getattr(self, method)(*args, **kwargs))

    async def report_permission(self, aid, principal, permission):
        return aid == AID and OWNER in principal and permission in {"read", "edit", "review", "share", "admin"}

    async def get_version(self, vid, aid=None):
        value = self.versions.get(vid)
        if value is None or (aid is not None and value["artifact_id"] != aid):
            return None
        return copy.deepcopy(value)

    async def get_report(self, aid, vid):
        version = await self.get_version(vid, aid)
        if version is None or not version.get("report_descriptor"):
            raise ArtifactError("no artifact report version")
        return {"artifact_id": aid, "version_id": vid, "state": "rendered",
                "checks": {"rendered": {"status": self.rendered}, "evidence_checked": {"status": "not_checked"},
                           "reviewed": {"status": "not_checked"}}}

    async def get_report_descriptor(self, aid, vid):
        return copy.deepcopy(self.versions[vid]["report_descriptor"])

    async def verify_share_outputs(self, aid, vid):
        if vid in self.foreign:
            raise ArtifactError("report reference must be uploaded in this artifact scope")
        return len(self.versions[vid]["output_manifest"])


class Shares:
    """In-memory ShareStore with the real store's rules (no more permissive)."""

    def __init__(self):
        self.rows = {}
        self.event_rows = []
        self.live_bytes_extra = 0
        self.fail_mark_live = False
        self.counter = 0
        self.before_revoke = None
        self.mark_live_error = None

    def sync_call(self, method, *args, **kwargs):
        return getattr(self, method)(*args, **kwargs)

    def _public(self, row):
        return {k: v for k, v in row.items() if k != "token_hash"}

    def create(self, *, artifact_id, version_id, audience, allow_emails=None, allow_groups=None,
               expires_at=None, entry=None, title=None, created_by=None):
        if audience not in ("lab", "public"):
            raise ShareError("audience must be 'lab' or 'public'")
        now = datetime.now(UTC)
        if audience == "lab" and not (allow_emails or allow_groups):
            raise ShareError("a lab share needs at least one allowed email or group")
        if audience == "public":
            if allow_emails or allow_groups:
                raise ShareError("a public share cannot carry an allow-list")
            if expires_at is None:
                raise ShareError("a public share must expire")
            if not now + timedelta(days=1) - timedelta(minutes=5) <= expires_at <= now + timedelta(days=90):
                raise ShareError("a public share must expire within 1-90 days")
        self.counter += 1
        share_id = f"share_{self.counter:024d}"
        row = {"share_id": share_id, "artifact_id": artifact_id, "version_id": version_id, "audience": audience,
               "allow_emails": list(allow_emails or []), "allow_groups": list(allow_groups or []),
               "state": "pending", "entry": entry, "file_count": None, "total_bytes": None, "title": title,
               "expires_at": expires_at, "created_by": created_by, "created_at": now, "approved_by": None,
               "approved_at": None, "revoked_at": None, "token_hash": None}
        self.rows[share_id] = row
        self.event_rows.append({"share_id": share_id, "at": now, "kind": "created", "actor": created_by,
                                "detail": {"audience": audience}})
        return self._public(row)

    def get(self, share_id):
        row = self.rows.get(share_id)
        return copy.deepcopy(row) if row else None

    def list_for_artifact(self, artifact_id):
        return [self._public(r) for r in self.rows.values() if r["artifact_id"] == artifact_id]

    def mark_live(self, share_id, *, token_hash, index, approved_by):
        row = self.rows[share_id]
        if self.fail_mark_live:
            raise RuntimeError("database went away")
        if self.mark_live_error is not None:
            raise self.mark_live_error
        if row["state"] != "pending":
            raise ShareError(f"share is {row['state']}, not pending")
        if row["expires_at"] is not None and row["expires_at"] <= datetime.now(UTC):
            raise ShareError("share has already expired", code="expired")
        row.update(state="live", token_hash=token_hash, entry=index.entry, file_count=len(index.files),
                   total_bytes=index.total_bytes, approved_by=approved_by, approved_at=datetime.now(UTC))
        self.event_rows.append({"share_id": share_id, "at": datetime.now(UTC), "kind": "approved",
                                "actor": approved_by, "detail": {"file_count": len(index.files)}})
        return self._public(row)

    def mark_revoked(self, share_id, *, actor):
        if self.before_revoke is not None:
            self.before_revoke(share_id)
        row = self.rows[share_id]
        if row["state"] != "revoked":
            was = row["state"]
            row.update(state="revoked", revoked_at=datetime.now(UTC))
            self.event_rows.append({"share_id": share_id, "at": datetime.now(UTC), "kind": "revoked",
                                    "actor": actor, "detail": {"was": was}})
        return self._public(row)

    def live_total_bytes(self):
        return self.live_bytes_extra + sum(r["total_bytes"] or 0 for r in self.rows.values() if r["state"] == "live")

    def events(self, share_id, limit=200):
        return [e for e in self.event_rows if e["share_id"] == share_id][:limit]


@contextmanager
def share_server(tmp_path, *, enabled=True, collab=None, **settings):
    blobs = Bytes()
    registry, shares = Registry(blobs), Shares()
    config = {"public_url": "https://share.example/", "max_share_bytes": 512 * MIB, "max_files": 20000,
              "max_total_bytes": 2048 * MIB, "default_public_days": 30, **settings}
    root = tmp_path / "shares"
    root.mkdir(parents=True)
    extra = {"share_store": shares, "shares_root": root, "share_settings": config} if enabled else {}
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"owner-key": "operator", "other-key": "operator"},
                                artifact_store=registry, rustfs=blobs, artifact_acl=ArtifactAcl(), **extra)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    if collab is not None:
        server.collab = collab                      # a room-membership stand-in (set after start: serve() drives a real one)
    try:
        yield f"http://127.0.0.1:{server.port}", registry, shares, blobs, root
    finally:
        server.shutdown()


@pytest.fixture
def env(tmp_path):
    with share_server(tmp_path) as value:
        yield value


def request(base, path, *, key="owner-key", body=None, headers=None, method=None):
    headers = {"X-API-Key": key, **(headers or {})}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


SHARES = f"/api/v1/artifacts/{AID}/shares"


def create(base, version="v_html", **body):
    payload = {"version_id": version, "audience": "public", **body}
    status, data = request(base, SHARES, body=payload)
    return status, json.loads(data)


def approve(base, share_id, body=None, **kwargs):
    status, data = request(base, f"{SHARES}/{share_id}/approve",
                           body={"confirm": True} if body is None else body, **kwargs)
    return status, json.loads(data)


def token_of(url):
    assert url.startswith("https://share.example/s/") and url.endswith("/")
    return url[len("https://share.example/s/"):-1]


def tree(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file())


# ----------------------------------------------------------------------------------------------- create


def test_create_requires_share_permission_and_is_pending_without_token(env):
    base, _, shares, blobs, root = env
    status, _ = request(base, SHARES, key="other-key", body={"version_id": "v_html", "audience": "public"})
    assert status == 404
    assert request(base, SHARES, key="other-key")[0] == 404
    assert not shares.rows
    status, body = create(base)
    assert status == 201, body
    share = body["share"]
    assert share["state"] == "pending" and share["audience"] == "public"
    text = json.dumps(body)
    assert "token" not in text and "/s/" not in text and "url" not in body
    expires = datetime.fromisoformat(share["expires_at"])
    assert timedelta(days=29, hours=23) < expires - datetime.now(UTC) <= timedelta(days=30)
    summary = body["summary"]
    assert summary["entry"] == "index.html" and summary["file_count"] == 3
    assert sorted(f["name"] for f in summary["files"]) == ["data/deep/t.parquet", "index.html", "js/app.js"]
    assert summary["audience"] == "public" and isinstance(summary["warnings"], list)
    # dry run: no bytes copied, nothing on the volume
    assert blobs.downloads == 0 and tree(root) == []
    # lab shares: allow-list rules come from the store; no expiry by default
    status, body = create(base, audience="lab", allow_emails=["a@x.org"], allow_groups=["artifacts-users"])
    assert status == 201 and body["share"]["expires_at"] is None
    assert create(base, audience="lab")[0] == 400
    assert create(base, allow_emails=["a@x.org"])[0] == 400
    assert create(base, expires_in_days=91)[0] == 400
    assert create(base, expires_in_days=True)[0] == 400
    assert create(base, token="chosen")[0] == 400          # identity/secrets are never caller-supplied
    assert create(base, version="nope")[0] == 404


def test_create_refuses_unready_version_and_missing_html_or_pdf(env):
    base, registry, shares, _, _ = env
    status, body = create(base, version="v_building")
    assert status == 400 and "ready" in body["error"]
    status, body = create(base, version="v_csv")
    assert status == 400 and "HTML or PDF" in body["error"]
    assert create(base, version="v_pdf")[1]["summary"]["entry"] == "paper.pdf"
    assert create(base, entry="js/app.js")[0] == 201                   # explicit entry inside the bundle
    assert create(base, entry="missing.html")[0] == 400
    assert create(base, entry="../index.html")[0] == 400
    registry.rendered = "not_checked"
    status, body = create(base, version="v_report")
    assert status == 400 and "rendered" in body["error"]
    assert all(row["version_id"] in {"v_pdf", "v_html"} for row in shares.rows.values())


# ----------------------------------------------------------------------------------------------- approve


def test_approve_requires_confirm_true_and_returns_url_once(env):
    base, _, shares, _, root = env
    share_id = create(base)[1]["share"]["share_id"]
    for body in ({}, {"confirm": False}, {"confirm": "true"}, {"confirm": 1}, {"confirm": True, "x": 1}):
        assert approve(base, share_id, body)[0] == 400, body
    assert request(base, f"{SHARES}/{share_id}/approve", body={"confirm": True}, key="other-key")[0] == 404
    assert tree(root) == [] and shares.rows[share_id]["state"] == "pending"
    status, body = approve(base, share_id)
    assert status == 200, body
    token = token_of(body["url"])
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    assert shares.rows[share_id]["token_hash"] == token_hash and shares.rows[share_id]["state"] == "live"
    summary = body["summary"]
    assert summary["entry"] == "index.html" and summary["file_count"] == 3 and summary["audience"] == "public"
    assert summary["expires_at"] and summary["total_bytes"] > 0
    state = json.loads((root / "by-token" / token_hash / "state.json").read_text())
    assert state["share_id"] == share_id and state["state"] == "live" and state["entry"] == "index.html"
    assert token not in json.dumps(state)
    # the URL is shown once: a second approve is a conflict and never re-issues a token
    status, again = approve(base, share_id)
    assert status == 409 and token not in json.dumps(again) and "url" not in again
    assert shares.rows[share_id]["token_hash"] == token_hash
    status, listed = request(base, SHARES)
    assert status == 200
    text = listed.decode()
    assert share_id in text and token not in text and token_hash not in text and "token_hash" not in text
    status, events = request(base, f"{SHARES}/{share_id}/events")
    assert status == 200 and token not in events.decode() and token_hash not in events.decode()
    assert [e["kind"] for e in json.loads(events)["events"]] == ["created", "approved"]


@pytest.mark.parametrize("public_url", ["https://share.example", "https://share.example/"])
def test_approve_url_joins_public_url_without_double_slash(tmp_path, public_url):
    with share_server(tmp_path, public_url=public_url) as (base, *_):
        share_id = create(base)[1]["share"]["share_id"]
        status, body = approve(base, share_id)
        assert status == 200 and body["url"].startswith("https://share.example/s/") and "//s/" not in body["url"]


def test_approve_copies_nested_outputs_and_pins_content(env):
    base, registry, _, blobs, root = env
    share_id = create(base)[1]["share"]["share_id"]
    assert approve(base, share_id)[0] == 200
    files = root / share_id / "files"
    assert tree(files) == ["data/deep/t.parquet", "index.html", "js/app.js"]
    before = {name: (files / name).read_bytes() for name in tree(files)}
    assert before["data/deep/t.parquet"].startswith(b"PAR1")
    # a later registry change (new bytes, edited manifest) does not alter what the share serves
    registry.versions["v_html"]["output_manifest"][0] = output("index.html", b"<p>changed</p>")
    blobs.add(b"<p>changed</p>")
    for key in list(blobs.objects):
        blobs.objects[key] = b"tampered"
    assert {name: (files / name).read_bytes() for name in tree(files)} == before


def test_approve_refuses_entries_without_hash_and_measures_missing_size(env):
    base, registry, shares, blobs, root = env
    # no hash at all: refused at create (dry run) and at approve
    unhashed = {"name": "index.html", "size_bytes": 5, "media_type": "text/html"}
    registry.versions["v_nohash"] = {"version_id": "v_nohash", "artifact_id": AID, "state": "ready",
                                     "output_manifest": [unhashed]}
    status, body = create(base, version="v_nohash")
    assert status == 400 and "verifiable" in body["error"]
    # a disagreeing second digest is refused as well
    index = registry.versions["v_html"]["output_manifest"][0]
    registry.versions["v_conflict"] = {"version_id": "v_conflict", "artifact_id": AID, "state": "ready",
                                       "output_manifest": [{**index, "blob_sha256": "a" * 64}]}
    assert create(base, version="v_conflict")[0] == 400
    # missing size_bytes / media_type: measured after the hash-verified download
    measured = []
    for item in copy.deepcopy(registry.versions["v_html"]["output_manifest"]):
        item.pop("size_bytes")
        item.pop("media_type")
        measured.append(item)
    registry.versions["v_nosize"] = {"version_id": "v_nosize", "artifact_id": AID, "state": "ready",
                                     "output_manifest": measured}
    status, body = create(base, version="v_nosize")
    assert status == 201 and any("measured" in w for w in body["summary"]["warnings"])
    share_id = body["share"]["share_id"]
    status, body = approve(base, share_id)
    assert status == 200, body
    expected = sum(len(blobs.objects[o["storage_key"]]) for o in measured)
    assert body["summary"]["total_bytes"] == expected == shares.rows[share_id]["total_bytes"]
    # the hash still binds an unsized entry: tampered bytes fail and leave the share pending, nothing on disk
    tampered = copy.deepcopy(measured)
    registry.versions["v_nosize2"] = {**registry.versions["v_nosize"], "version_id": "v_nosize2",
                                      "output_manifest": tampered}
    share_id = create(base, version="v_nosize2")[1]["share"]["share_id"]
    blobs.objects[tampered[1]["storage_key"]] = b"console.log('evil')"
    status, body = approve(base, share_id)
    assert status == 400 and "digest" in body["error"]
    assert shares.rows[share_id]["state"] == "pending" and not (root / share_id).exists()


def test_report_version_gets_generated_preview_html_entry(env):
    base, _, shares, blobs, root = env
    status, body = create(base, version="v_report")
    assert status == 201 and body["summary"]["entry"] == "preview.html"
    assert [f["name"] for f in body["summary"]["files"]] == ["preview.html"]
    share_id = body["share"]["share_id"]
    status, body = approve(base, share_id)
    assert status == 200, body
    files = root / share_id / "files"
    assert tree(files) == ["preview.html"]                 # assets are not included
    preview = (files / "preview.html").read_bytes()
    assert b"Generic investigation" in preview and b"data.csv" in preview
    assert b"/api/v1" not in preview and b'href="report"' not in preview and b'href="#"' not in preview
    assert blobs.downloads == 0
    assert shares.rows[share_id]["entry"] == "preview.html"
    state_files = list((root / "by-token").glob("*/state.json"))
    assert len(state_files) == 1 and json.loads(state_files[0].read_text())["entry"] == "preview.html"


def test_caps_enforced_including_total_live_bytes(tmp_path):
    with share_server(tmp_path, max_share_bytes=40) as (base, _, shares, _, root):
        status, body = create(base)
        assert status == 400 and "40" in body["error"]
    with share_server(tmp_path / "files", max_files=2) as (base, _, _, _, _):
        status, body = create(base)
        assert status == 400 and "2 files" in body["error"]
    with share_server(tmp_path / "total", max_total_bytes=200) as (base, registry, shares, _, root):
        first = create(base, version="v_pdf")[1]["share"]["share_id"]
        assert approve(base, first)[0] == 200
        shares.live_bytes_extra = 190
        second = create(base)[1]["share"]["share_id"]
        status, body = approve(base, second)
        assert status == 400 and "200" in body["error"]
        assert shares.rows[second]["state"] == "pending" and not (root / second).exists()
        # revoking frees the budget
        shares.live_bytes_extra = 0
        assert request(base, f"{SHARES}/{first}/revoke", body={})[0] == 200
        assert approve(base, second)[0] == 200


def test_blob_not_owned_by_artifact_is_refused(env):
    base, registry, shares, blobs, root = env
    share_id = create(base)[1]["share"]["share_id"]
    registry.foreign.add("v_html")
    status, body = approve(base, share_id)
    assert status == 400 and "scope" in body["error"]
    assert blobs.downloads == 0 and shares.rows[share_id]["state"] == "pending"
    assert tree(root) == []


def test_store_verify_share_outputs_reuses_the_commit_scope_rules():
    run = asyncio.run
    conn, blobs = ScopeConnection(), ScopeBlobs()
    store = ArtifactStore(conn, blobs)
    owner = run(store.register_logical(original_filename="private.html", created_by="owner"))["artifact_id"]
    target = run(store.register_logical(original_filename="page.html", created_by="outsider"))["artifact_id"]
    private = b"<p>private</p>"
    key, sha = blobs.put(private)
    run(store.register_report_blob(owner, storage_key=key, sha256=sha, size_bytes=len(private),
                                   media_type="text/html"))
    legacy = b"<p>legacy</p>"
    legacy_key, legacy_sha = blobs.put(legacy)
    conn.versions["v_stolen"] = {"version_id": "v_stolen", "artifact_id": target, "report_descriptor": None,
                                 "output_manifest": [{"name": "index.html", "storage_key": key, "sha256": sha,
                                                      "size_bytes": len(private), "media_type": "text/html"}]}
    conn.versions["v_legacy"] = {"version_id": "v_legacy", "artifact_id": target, "report_descriptor": None,
                                 "output_manifest": [{"name": "index.html", "storage_key": legacy_key,
                                                      "sha256": legacy_sha, "size_bytes": len(legacy),
                                                      "media_type": "text/html"}]}
    conn.versions["v_own"] = {**conn.versions["v_stolen"], "version_id": "v_own", "artifact_id": owner}
    with pytest.raises(ArtifactError, match="scope"):
        run(store.verify_share_outputs(target, "v_stolen"))
    assert run(store.verify_share_outputs(target, "v_legacy")) == 1      # legacy unscoped blob, as at commit
    assert run(store.verify_share_outputs(owner, "v_own")) == 1
    with pytest.raises(ArtifactError, match="no artifact"):
        run(store.verify_share_outputs(owner, "v_stolen"))               # version of another artifact
    # report versions use the strict scoped rule for every output, like commit_version
    conn.versions["v_report"] = {**conn.versions["v_legacy"], "version_id": "v_report",
                                 "report_descriptor": report_document()}
    with pytest.raises(ArtifactError, match="scope"):
        run(store.verify_share_outputs(target, "v_report"))


def test_failed_publish_leaves_share_pending_and_volume_clean(env):
    base, _, shares, _, root = env
    share_id = create(base)[1]["share"]["share_id"]
    shares.fail_mark_live = True
    status, _ = approve(base, share_id)
    assert status >= 500
    assert shares.rows[share_id]["state"] == "pending" and shares.rows[share_id]["token_hash"] is None
    assert tree(root) == [] and not (root / share_id).exists()
    shares.fail_mark_live = False
    assert approve(base, share_id)[0] == 200


def test_existing_bundle_directory_is_a_conflict_and_left_alone(env):
    base, _, shares, _, root = env
    share_id = create(base)[1]["share"]["share_id"]
    (root / share_id).mkdir()
    (root / share_id / "marker").write_text("someone else's")
    status, body = approve(base, share_id)
    assert status == 409
    assert (root / share_id / "marker").read_text() == "someone else's"
    assert shares.rows[share_id]["state"] == "pending"


def test_pending_public_share_that_expired_cannot_be_approved(env):
    base, _, shares, _, root = env
    share_id = create(base)[1]["share"]["share_id"]
    shares.rows[share_id]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    status, body = approve(base, share_id)
    assert status == 410 and "expired" in body["error"]
    assert tree(root) == []


# ----------------------------------------------------------------------------------------------- revoke / misc


def test_revoke_removes_state_and_bundle_and_is_idempotent(env):
    base, _, shares, _, root = env
    share_id = create(base)[1]["share"]["share_id"]
    token = token_of(approve(base, share_id)[1]["url"])
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    assert (root / "by-token" / token_hash / "state.json").exists() and (root / share_id).exists()
    assert request(base, f"{SHARES}/{share_id}/revoke", body={}, key="other-key")[0] == 404
    status, body = request(base, f"{SHARES}/{share_id}/revoke", body={})
    assert status == 200
    assert json.loads(body)["share"]["state"] == "revoked" and "token_hash" not in body.decode()
    assert not (root / "by-token" / token_hash).exists() and not (root / share_id).exists()
    assert request(base, f"{SHARES}/{share_id}/revoke", body={})[0] == 200
    assert approve(base, share_id)[0] == 409
    assert [e["kind"] for e in shares.events(share_id)] == ["created", "approved", "revoked"]
    # a pending share (no token yet) revokes too
    pending = create(base)[1]["share"]["share_id"]
    assert request(base, f"{SHARES}/{pending}/revoke", body={})[0] == 200
    assert shares.rows[pending]["state"] == "revoked"


def test_share_of_another_artifact_is_not_found(env):
    base, registry, shares, _, _ = env
    share_id = create(base)[1]["share"]["share_id"]
    shares.rows[share_id]["artifact_id"] = "artifact_other"
    assert request(base, f"{SHARES}/{share_id}/approve", body={"confirm": True})[0] == 404
    assert request(base, f"{SHARES}/{share_id}/revoke", body={})[0] == 404
    assert request(base, f"{SHARES}/{share_id}/events")[0] == 404
    assert request(base, f"{SHARES}/share_unknown/events")[0] == 404


def test_cross_origin_post_refused(env):
    base, _, shares, _, root = env
    share_id = create(base)[1]["share"]["share_id"]
    for path, body in ((SHARES, {"version_id": "v_html", "audience": "public"}),
                       (f"{SHARES}/{share_id}/approve", {"confirm": True}),
                       (f"{SHARES}/{share_id}/revoke", {})):
        for origin in ("https://attacker.invalid", "file://" + base.split("//")[1]):
            assert request(base, path, body=body, headers={"Origin": origin})[0] == 403
    assert len(shares.rows) == 1 and shares.rows[share_id]["state"] == "pending" and tree(root) == []


def test_sharing_disabled_answers_503(tmp_path):
    with share_server(tmp_path, enabled=False) as (base, *_):
        status, body = request(base, SHARES)
        assert status == 503 and b"sharing is not enabled" in body
        assert request(base, SHARES, body={"version_id": "v_html", "audience": "public"})[0] == 503


def test_share_settings_defaults_and_validation():
    from herdr_engineering.control_plane.artifact_share_api import share_settings_config
    settings = share_settings_config({}, public_url="https://x.example/")
    assert settings == {"public_url": "https://x.example/", "max_share_bytes": 512 * MIB, "max_files": 20000,
                        "max_total_bytes": 2048 * MIB, "default_public_days": 30}
    assert share_settings_config({"max_files": 5}, public_url="https://x.example/")["max_files"] == 5
    for bad in ({"max_files": 0}, {"max_files": True}, {"max_share_bytes": "1"}, {"default_public_days": 91}):
        with pytest.raises(ValueError):
            share_settings_config(bad, public_url="https://x.example/")
    with pytest.raises(ValueError):
        share_settings_config({}, public_url="http://x.example/")
    with pytest.raises(ValueError):
        share_settings_config({}, public_url=None)


def test_cli_share_wiring_is_off_unless_enabled(monkeypatch, tmp_path):
    from herdr_engineering import cli
    assert cli._share_wiring({}, "postgresql:///x") == (None, None, None)
    assert cli._share_wiring({"share": {"enabled": False}}, "postgresql:///x") == (None, None, None)
    cp = {"share": {"enabled": True, "max_files": 7, "shares_dir": str(tmp_path)},
          "collab": {"public_url": "https://share.example/"}}
    assert cli._share_wiring(cp, None) == (None, None, None)              # no registry database: no sharing
    made = []

    class FakeAdapter:
        def __init__(self, factory):
            made.append(factory)

    monkeypatch.setattr("herdr_engineering.control_plane.artifact_adapter.SyncArtifactAdapter", FakeAdapter)
    adapter, root, settings = cli._share_wiring(cp, "postgresql:///x")
    assert isinstance(adapter, FakeAdapter) and len(made) == 1
    assert root == tmp_path and settings["max_files"] == 7 and settings["public_url"] == "https://share.example/"
    with pytest.raises(SystemExit, match="public_url"):
        cli._share_wiring({"share": {"enabled": True}}, "postgresql:///x")


def test_revoke_racing_an_approve_removes_the_state_it_did_not_see(env):
    base, _, shares, _, root = env
    share_id = create(base)[1]["share"]["share_id"]
    raced = {}

    def approve_lands_first(sid):            # revoke read the share while pending; approve then went live
        token_hash = hashlib.sha256(b"raced-token").hexdigest()
        (root / "by-token" / token_hash).mkdir(parents=True)
        (root / "by-token" / token_hash / "state.json").write_text("{}")
        shares.rows[sid].update(state="live", token_hash=token_hash)
        raced["path"] = root / "by-token" / token_hash

    shares.before_revoke = approve_lands_first
    assert request(base, f"{SHARES}/{share_id}/revoke", body={})[0] == 200
    assert shares.rows[share_id]["state"] == "revoked" and not raced["path"].exists()


def test_early_answers_to_posts_close_the_connection(tmp_path):
    def headers_of(base, path, key="owner-key"):
        req = urllib.request.Request(base + path, data=b'{"confirm": true}', method="POST",
                                     headers={"X-API-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.headers

    with share_server(tmp_path) as (base, *_):
        # the body is validated (read) before the share lookup, so an unknown share is an ordinary 404
        assert headers_of(base, f"{SHARES}/share_unknown/approve")[0] == 404
        status, headers = headers_of(base, f"{SHARES}/x/bogus")       # answered before the body is read
        assert status == 404 and headers.get("Connection", "").lower() == "close"
        status, headers = headers_of(base, f"{SHARES}/share_unknown/approve", key="other-key")
        assert status == 404 and headers.get("Connection", "").lower() == "close"
    with share_server(tmp_path / "off", enabled=False) as (base, *_):
        status, headers = headers_of(base, SHARES)
        assert status == 503 and headers.get("Connection", "").lower() == "close"


# ----------------------------------------------------------------------------------------------- fix round 1


def unsized_version(registry, vid="v_unsized"):
    outputs = copy.deepcopy(registry.versions["v_html"]["output_manifest"])
    for item in outputs:
        item.pop("size_bytes")
    registry.versions[vid] = {"version_id": vid, "artifact_id": AID, "state": "ready", "output_manifest": outputs}
    return vid


def test_measured_sizes_over_total_cap_are_refused_before_any_write(tmp_path, monkeypatch):
    from herdr_engineering.control_plane import artifact_share_api
    built = []
    real = artifact_share_api.build_bundle
    monkeypatch.setattr(artifact_share_api, "build_bundle", lambda *a, **k: built.append(a) or real(*a, **k))
    with share_server(tmp_path, max_total_bytes=200) as (base, registry, shares, _, root):
        share_id = create(base, version=unsized_version(registry))[1]["share"]["share_id"]
        shares.live_bytes_extra = 150          # declared sizes are 0, so only the measured total exceeds the cap
        status, body = approve(base, share_id)
        assert status == 400 and "200 byte total cap" in body["error"]
        assert built == [] and list(root.iterdir()) == []
        assert shares.rows[share_id]["state"] == "pending"


def test_get_share_returns_row_and_pending_summary_without_token(env):
    base, registry, shares, _, _ = env
    share_id = create(base)[1]["share"]["share_id"]
    path = f"{SHARES}/{share_id}"
    assert request(base, path, key="other-key")[0] == 404
    status, data = request(base, path)
    assert status == 200
    body = json.loads(data)
    assert body["share"]["share_id"] == share_id and body["share"]["state"] == "pending"
    summary = body["summary"]
    assert summary["entry"] == "index.html" and summary["file_count"] == 3 and summary["audience"] == "public"
    assert summary["total_bytes"] > 0 and summary["expires_at"] and isinstance(summary["warnings"], list)
    token = token_of(approve(base, share_id)[1]["url"])
    status, data = request(base, path)
    text = data.decode()
    assert status == 200 and json.loads(data)["share"]["state"] == "live" and "summary" not in json.loads(data)
    assert token not in text and "token_hash" not in text and shares.rows[share_id]["token_hash"] not in text
    shares.rows[share_id]["artifact_id"] = "artifact_other"
    assert request(base, path)[0] == 404
    assert request(base, f"{SHARES}/share_unknown")[0] == 404
    assert request(base, path, body={})[0] == 404            # GET only


def test_expires_in_days_is_bounded_for_every_audience(env):
    base, *_ = env
    for days in (3651, 10 ** 12):
        assert create(base, audience="lab", allow_emails=["a@x.org"], expires_in_days=days)[0] == 400
    assert create(base, audience="lab", allow_emails=["a@x.org"], expires_in_days=3650)[0] == 201


def test_create_refuses_duplicate_and_clashing_names(env):
    base, registry, _, _, _ = env
    index = registry.versions["v_html"]["output_manifest"][0]
    registry.versions["v_dup"] = {"version_id": "v_dup", "artifact_id": AID, "state": "ready",
                                  "output_manifest": [index, {**index, "name": "INDEX.html"}]}
    status, body = create(base, version="v_dup")
    assert status == 400 and "duplicate" in body["error"]
    registry.versions["v_clash"] = {"version_id": "v_clash", "artifact_id": AID, "state": "ready",
                                    "output_manifest": [index, {**index, "name": "index.html/inner.html"}]}
    status, body = create(base, version="v_clash")
    assert status == 400 and "both a file and a directory" in body["error"]


def test_mark_live_expiry_maps_to_410_by_code_not_message(env):
    base, _, shares, _, root = env
    share_id = create(base)[1]["share"]["share_id"]
    shares.mark_live_error = ShareError("too late", code="expired")
    assert approve(base, share_id)[0] == 410
    shares.mark_live_error = ShareError("token hash already in use")
    assert approve(base, share_id)[0] == 409
    assert list((root / "by-token").iterdir()) == [] and not (root / share_id).exists()


# ----------------------------------------------------------------------------------------------- share with a room


class Rooms:
    def __init__(self, rooms):
        self.rooms = rooms

    def room_member_emails(self, room_id):
        return list(self.rooms.get(room_id, []))


def test_a_lab_share_to_a_room_allows_the_rooms_people_and_only_those(tmp_path):
    with share_server(tmp_path, collab=Rooms({"r1": ["bob@x.org", "carol@x.org"]})) as (base, _, shares, _b, _r):
        status, body = create(base, audience="lab", room_id="r1", allow_emails=["dave@x.org", "bob@x.org"])
        assert status == 201, body
        assert sorted(body["share"]["allow_emails"]) == ["bob@x.org", "carol@x.org", "dave@x.org"]
        status, body = create(base, audience="lab", room_id="r1")
        assert status == 201 and sorted(body["share"]["allow_emails"]) == ["bob@x.org", "carol@x.org"]
        assert "room_id" not in str(list(shares.rows.values())[-1])               # the room is not stored; the emails are


def test_a_room_share_is_refused_for_public_unknown_rooms_and_bad_ids(tmp_path):
    with share_server(tmp_path, collab=Rooms({"r1": ["bob@x.org"], "empty": []})) as (base, *_):
        assert create(base, audience="public", room_id="r1")[0] == 400
        assert create(base, audience="lab", room_id="nope")[0] == 400
        assert create(base, audience="lab", room_id="empty")[0] == 400
        assert create(base, audience="lab", room_id="")[0] == 400
        assert create(base, audience="lab", room_id=5)[0] == 400
        assert create(base, audience="lab", room_id="r1", allow_emails="x@x.org")[0] == 400


def test_a_room_share_without_collaboration_is_refused(env):
    base, *_ = env
    assert create(base, audience="lab", room_id="r1")[0] == 400
