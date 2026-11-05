# ruff: noqa: F811  (pytest fixtures are imported by name from registry_pg)
"""Operator controls for member-owned artifacts over real HTTP, on a real PostgreSQL (all migrations): usage and links,
limits, disabling sharing, the D4 operator access rule and purge. No mocks: real stores, real share bundles on disk."""
import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter
from herdr_engineering.control_plane.artifact_member_intents import (
    MemberRuntime,
    sweep_disabled_members,
)
from herdr_engineering.control_plane.artifact_member_quota import settle
from herdr_engineering.control_plane.artifact_members_api import MemberAdmin
from herdr_engineering.control_plane.artifact_share_api import (
    _call,
    approve_share,
    share_settings_config,
)
from herdr_engineering.control_plane.artifact_shares import ShareStore
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.publication import (
    PrivateOnlyExposureProvider,
    PublicationController,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.registry_pg import FakeBlobs, registry_pg  # noqa: F401 - imported fixture

ALICE, BOB = "member:alice", "member:bob"
HTML = b"<!doctype html><html><head><title>t</title></head><body><p>hello</p></body></html>"
BASE_BODY = "/api/v1/artifact-members"


def entry(blobs, name, data, media_type="text/html"):
    key, digest = blobs.put(data)
    return {"name": name, "media_type": media_type, "sha256": digest, "storage_key": key, "size_bytes": len(data)}


class Env:
    def __init__(self, pg, blobs, root):
        self.pg, self.blobs, self.root = pg, blobs, root
        self.base = ""
        self.server = None
        self.shares = None

    def run(self, body):
        async def go():
            conn = await asyncpg.connect(self.pg["dsn"])
            try:
                return await body(ArtifactStore(conn, self.blobs), conn)
            finally:
                await conn.close()
        return asyncio.run(go())

    def sql(self, query, *args):
        async def body(store, conn):
            return await conn.fetch(query, *args)
        return self.run(body)

    def artifact(self, owner=ALICE, files="default"):
        files = [entry(self.blobs, "index.html", HTML)] if files == "default" else files

        async def body(store, conn):
            record = await store.register_logical(original_filename="index.html", created_by=owner)
            aid = record["artifact_id"]
            for item in files:
                await store.register_report_blob(aid, storage_key=item["storage_key"], sha256=item["sha256"],
                                                 size_bytes=item["size_bytes"], media_type=item["media_type"])
            version = await store.commit_version(aid, base_version_id=None, source_manifest_blob=None,
                                                 output_manifest=files, created_by=owner)
            await settle(conn, owner)
            return aid, version["version_id"]
        return self.run(body)

    def share(self, aid, vid, owner=ALICE, audience="public", approve=True, **extra):
        kwargs = {"allow_emails": ["a@lab.example.org"], "member_emails": frozenset({"a@lab.example.org"}),
                  "member_groups": frozenset()} if audience == "lab" else {}
        if audience == "public":
            extra.setdefault("expires_at", datetime.now(UTC) + timedelta(days=7))
        row = _call(self.shares, "create", artifact_id=aid, version_id=vid, audience=audience, created_by=owner,
                                    **kwargs, **extra)
        if approve:
            approve_share(self.server, row, approved_by=owner)
            row = _call(self.shares, "get", row["share_id"])
        return row

    def bundle(self, share_id):
        return (Path(self.root) / "bundles" / share_id).exists() if (Path(self.root) / "bundles").exists() else \
            any(share_id in str(p) for p in Path(self.root).rglob("*"))

    def req(self, path, *, key="owner-key", body=None, headers=None, method=None):
        headers = {"X-API-Key": key, **(headers or {})}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, headers=headers, data=data, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, json.loads(raw) if raw else None


@contextmanager
def members_server(pg, blobs, root):
    settings = {**share_settings_config(None, public_url="https://share.example.org")}

    async def registry():
        return ArtifactStore(await asyncpg.connect(pg["dsn"]), blobs)

    async def shares():
        return ShareStore(await asyncpg.connect(pg["dsn"]), lock_connect=lambda: asyncpg.connect(pg["dsn"]))

    async def admin():
        return MemberAdmin(await asyncpg.connect(pg["dsn"]))

    reg, sh, adm = SyncArtifactAdapter(registry), SyncArtifactAdapter(shares), SyncArtifactAdapter(admin)
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"owner-key": "operator", "viewer-key": "viewer"},
                                artifact_store=reg, artifact_acl=ArtifactAcl(), rustfs=blobs, share_store=sh,
                                shares_root=root, share_settings=settings, member_admin=adm,
                                publication_controller=PublicationController(provider=PrivateOnlyExposureProvider()))
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    env = Env(pg, blobs, root)
    env.base, env.server, env.shares = f"http://127.0.0.1:{server.port}", server, sh
    try:
        yield env
    finally:
        server.shutdown()
        for adapter in (reg, sh, adm):
            adapter.close()


@pytest.fixture
def env(registry_pg, tmp_path):
    root = tmp_path / "shares"
    root.mkdir()
    with members_server(registry_pg, FakeBlobs(), root) as value:
        value.sql("TRUNCATE artifacts, artifact_acl, artifact_shares, artifact_member_usage, artifact_member_events,"
                  " artifact_blobs CASCADE")
        yield value


def retract(env, aid):
    async def body(store, conn):
        await store.record_retraction(aid, reason="member delete")
        await settle(conn, ALICE)
    env.run(body)


# ------------------------------------------------------------------------------------------------ role gate

ROUTES = [("GET", BASE_BODY, None), ("GET", BASE_BODY + "/alice", None), ("GET", BASE_BODY + "/links", None),
          ("POST", BASE_BODY + "/alice/limits", {"max_artifacts": 3}),
          ("POST", BASE_BODY + "/alice/sharing", {"enabled": False}),
          ("POST", BASE_BODY + "/alice/shares/s1/revoke", {}),
          ("POST", BASE_BODY + "/alice/artifacts/a1/purge", {"confirm": "a1"})]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_only_an_operator_may_use_any_member_route(env, method, path, body):
    aid, _ = env.artifact()
    assert env.req(path, key="viewer-key", body=body, method=method)[0] == 403
    assert env.req(path, key="nobody", body=body, method=method)[0] in {401, 403}
    assert env.sql("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", aid)[0]["lifecycle"] == "active"


def test_a_viewer_changes_nothing(env):
    env.artifact()
    env.req(BASE_BODY + "/alice/limits", key="viewer-key", body={"max_artifacts": 1})
    assert env.sql("SELECT max_artifacts FROM artifact_member_usage WHERE member_principal=$1", ALICE)[0][0] is None


# ------------------------------------------------------------------------------------------------ list, show, links

def test_list_and_show_report_accounting_and_links(env):
    aid, vid = env.artifact()
    env.artifact(owner=BOB)
    share = env.share(aid, vid)
    status, body = env.req(BASE_BODY)
    assert status == 200
    members = {m["member_principal"]: m for m in body["members"]}
    assert set(members) == {ALICE, BOB} and members[ALICE]["bytes_used"] == len(HTML)
    assert members[ALICE]["max_artifacts"] is None
    status, shown = env.req(BASE_BODY + "/alice")
    assert status == 200 and shown["member"] == ALICE and [a["artifact_id"] for a in shown["artifacts"]] == [aid]
    assert [link["share_id"] for link in shown["links"]] == [share["share_id"]]
    assert "token" not in json.dumps(shown)
    assert env.req(BASE_BODY + "/member:alice")[0] == 200
    assert env.req(BASE_BODY + "/zed")[0] == 404
    assert env.req(BASE_BODY + "/bad%20user")[0] == 404


def test_links_lists_member_links_and_filters(env):
    aid, vid = env.artifact()
    pub = env.share(aid, vid)
    lab = env.share(aid, vid, audience="lab", approve=False)
    status, body = env.req(BASE_BODY + "/links")
    assert status == 200 and {link["share_id"] for link in body["links"]} == {pub["share_id"], lab["share_id"]}
    status, body = env.req(BASE_BODY + "/links?audience=lab")
    assert [link["share_id"] for link in body["links"]] == [lab["share_id"]]
    assert env.req(BASE_BODY + "/links?state=live")[1]["links"][0]["share_id"] == pub["share_id"]
    assert env.req(BASE_BODY + "/links?audience=nope")[0] == 400
    assert env.req(BASE_BODY + "/links?state=live&state=pending")[0] == 400


def test_operator_revokes_a_link_and_the_bundle_goes(env):
    aid, vid = env.artifact()
    share = env.share(aid, vid)
    assert any(Path(env.root).rglob("*")), "approval left a bundle on disk"
    status, body = env.req(f"{BASE_BODY}/alice/shares/{share['share_id']}/revoke", body={})
    assert status == 200 and body["share"]["state"] == "revoked" and "token_hash" not in json.dumps(body)
    assert not [p for p in Path(env.root).rglob("*") if p.is_file()]
    events = _call(env.shares, "events", share["share_id"])
    assert [e for e in events if e["kind"] == "revoked"][-1]["actor"].endswith(":operator")
    assert env.req(f"{BASE_BODY}/alice/shares/{share['share_id']}/revoke", body={})[0] == 200    # idempotent
    # another member's id for the same share is not found: no existence leak
    assert env.req(f"{BASE_BODY}/bob/shares/{share['share_id']}/revoke", body={})[0] == 404


def test_cross_origin_posts_are_refused(env):
    aid, vid = env.artifact()
    share = env.share(aid, vid)
    status, _ = env.req(f"{BASE_BODY}/alice/shares/{share['share_id']}/revoke", body={},
                        headers={"Origin": "https://evil.example"})
    assert status == 403
    assert _call(env.shares, "get", share["share_id"])["state"] == "live"


# ------------------------------------------------------------------------------------------------ limits

def test_limits_set_clear_and_validate(env):
    aid, _ = env.artifact()
    path = BASE_BODY + "/alice/limits"
    status, body = env.req(path, body={"max_artifacts": 7, "max_member_bytes": 10 ** 9})
    assert status == 200 and body["usage"]["max_artifacts"] == 7
    assert env.req(path, body={"max_artifacts": None})[1]["usage"]["max_artifacts"] is None
    # a limit below current use only blocks new reservations: nothing is removed
    assert env.req(path, body={"max_member_bytes": 1})[0] == 200
    assert env.sql("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", aid)[0][0] == "active"
    for bad in ({}, {"max_artifacts": 0}, {"max_artifacts": -1}, {"max_artifacts": True}, {"max_artifacts": 1.5},
                {"max_artifacts": "3"}, {"max_artifacts": 2 ** 70}, {"bogus": 1}, {"max_artifacts": 1, "bogus": 1}):
        assert env.req(path, body=bad)[0] == 400, bad
    assert env.req(BASE_BODY + "/zed/limits", body={"max_artifacts": 1})[0] == 404
    assert env.sql("SELECT max_member_bytes FROM artifact_member_usage WHERE member_principal=$1", ALICE)[0][0] == 1


# ------------------------------------------------------------------------------------------------ disable sharing

def test_disabling_sharing_revokes_live_and_pending_links_and_blocks_new_ones(env):
    aid, vid = env.artifact()
    pub = env.share(aid, vid)
    lab = env.share(aid, vid, audience="lab", approve=False)
    other_aid, other_vid = env.artifact(owner=BOB)
    bobs = env.share(other_aid, other_vid, owner=BOB)
    status, body = env.req(BASE_BODY + "/alice/sharing", body={"enabled": False})
    assert status == 200 and sorted(body["revoked_shares"]) == sorted([pub["share_id"], lab["share_id"]])
    assert body["usage"]["sharing_disabled"] is True
    assert {_call(env.shares, "get", s["share_id"])["state"] for s in (pub, lab)} == {"revoked"}
    assert _call(env.shares, "get", bobs["share_id"])["state"] == "live"
    assert [e["actor"] for e in _call(env.shares, "events", pub["share_id"]) if e["kind"] == "revoked"][-1] \
        .endswith(":operator")
    assert env.req(BASE_BODY + "/alice/sharing", body={"enabled": "no"})[0] == 400
    assert env.sql("SELECT sharing_disabled FROM artifact_member_usage WHERE member_principal=$1", ALICE)[0][0]
    status, body = env.req(BASE_BODY + "/alice/sharing", body={"enabled": True})
    assert status == 200 and body["revoked_shares"] == [] and body["usage"]["sharing_disabled"] is False


# ------------------------------------------------------------------------------------------------ sweep

def test_sweep_revokes_for_a_disabled_member_idempotently_and_never_for_unknown(env, tmp_path):
    aid, vid = env.artifact()
    bid, bvid = env.artifact(owner=BOB)
    alice, bob = env.share(aid, vid), env.share(bid, bvid, owner=BOB)
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "member-directory.json").write_text(json.dumps({
        "group": "lab", "updated": time.time(),
        "users": {"alice": {"status": "disabled"}, "bob": {"status": "unknown"}}}))
    rt = MemberRuntime(outbox=outbox, mirror_root=tmp_path / "mirror", group="lab", enabled=True, defaults={},
                       shares_root=env.root, clock=time.time)

    async def sweep(store, conn):
        return await sweep_disabled_members(conn, rt)
    assert env.run(sweep) == 1
    assert _call(env.shares, "get", alice["share_id"])["state"] == "revoked"
    assert _call(env.shares, "get", bob["share_id"])["state"] == "live"
    assert [e for e in _call(env.shares, "events", alice["share_id"]) if e["kind"] == "revoked"][-1]["actor"] \
        == "member-disabled"
    assert env.run(sweep) == 0


# ------------------------------------------------------------------------------------------------ D4 operator access

def test_operator_reads_and_shares_member_artifacts_but_cannot_edit(env):
    aid, vid = env.artifact()
    status, body = env.req(f"/api/v1/artifacts/{aid}")
    assert status == 200 and body["artifact_id"] == aid
    status, body = env.req(f"/api/v1/artifacts/{aid}/shares", body={"version_id": vid, "audience": "public"})
    assert status == 201, body
    # not edit, not commit
    status, _ = env.req(f"/api/v1/artifacts/{aid}/versions", body={"base_version_id": vid, "output_manifest": []})
    assert status == 404
    assert len(env.sql("SELECT 1 FROM artifact_versions WHERE artifact_id=$1", aid)) == 1
    # a viewer-role key does not gain the operator's access
    assert env.req(f"/api/v1/artifacts/{aid}", key="viewer-key")[0] in {403, 404}


# ------------------------------------------------------------------------------------------------ purge

def purge_path(aid, member="alice"):
    return f"{BASE_BODY}/{member}/artifacts/{aid}/purge"


def test_purge_needs_the_matching_confirmation_and_a_retracted_artifact(env):
    aid, _ = env.artifact()
    for bad in ({}, {"confirm": "other"}, {"confirm": True}, {"confirm": aid, "x": 1}):
        assert env.req(purge_path(aid), body=bad)[0] == 400, bad
    status, body = env.req(purge_path(aid), body={"confirm": aid})
    assert status == 409 and "deleted first" in body["error"]
    assert env.sql("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", aid)[0][0] == "active"
    assert env.req(purge_path(aid, "bob"), body={"confirm": aid})[0] == 404          # not that member's artifact
    assert env.req(purge_path("nope"), body={"confirm": "nope"})[0] == 404
    assert env.blobs.deleted == []


def test_purge_removes_the_bytes_keeps_the_tombstone_and_revokes_links(env):
    aid, vid = env.artifact()
    share = env.share(aid, vid)
    retract(env, aid)
    assert env.sql("SELECT bytes_retracted FROM artifact_member_usage WHERE member_principal=$1", ALICE)[0][0] > 0
    # a retracted artifact still has a live link here (the member path revokes them; the operator path must too)
    status, body = env.req(purge_path(aid), body={"confirm": aid})
    assert status == 200, body
    assert body["purged"] is True and body["revoked_shares"] == [share["share_id"]] and body["deleted"] >= 1
    row = env.sql("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", aid)[0]
    assert row["lifecycle"] == "tombstoned"
    assert env.sql("SELECT count(*) FROM artifact_versions WHERE artifact_id=$1 AND purged_at IS NULL", aid)[0][0] == 0
    assert env.sql("SELECT count(*) FROM artifact_blobs WHERE scope=$1", aid)[0][0] == 0
    assert env.blobs.deleted, "the object bytes were deleted"
    usage = env.sql("SELECT bytes_used, bytes_retracted FROM artifact_member_usage WHERE member_principal=$1", ALICE)[0]
    assert (usage["bytes_used"], usage["bytes_retracted"]) == (0, 0)
    assert _call(env.shares, "get", share["share_id"])["state"] == "revoked"
    assert not [p for p in Path(env.root).rglob("*") if p.is_file()]
    life = env.sql("SELECT action, actor FROM artifact_lifecycle_events WHERE artifact_id=$1 ORDER BY event_id", aid)
    assert life[-1]["action"] == "purge" and life[-1]["actor"].endswith(":operator")
    member = env.sql("SELECT actor FROM artifact_member_events WHERE action='artifact_purge'")
    assert len(member) == 1 and ":operator" in json.dumps(member[0]["actor"])
    # the artifact is gone from the readers
    assert env.req(f"/api/v1/artifacts/{aid}")[0] == 404
    assert aid not in [a["artifact_id"] for a in env.req(BASE_BODY + "/alice")[1]["artifacts"]]
    assert env.req(BASE_BODY + "/links")[1]["links"] == []


def test_a_tombstoned_member_artifact_is_excluded_from_the_operator_reader(env):
    aid, _ = env.artifact()
    retract(env, aid)
    assert env.req(f"/api/v1/artifacts/{aid}")[0] == 200       # retracted is still readable by the operator
    env.sql("UPDATE artifacts SET lifecycle='tombstoned' WHERE artifact_id=$1", aid)
    assert env.req(f"/api/v1/artifacts/{aid}")[0] == 404


# ------------------------------------------------------------------------------------------------ fix round 1

def tombstone_held(env, aid):
    """A tombstoned artifact with bytes left and no purge date: held, as artifact-delete's legal hold leaves it."""
    retract(env, aid)
    env.sql("UPDATE artifacts SET lifecycle='tombstoned', tombstoned_at=now(), purge_after=NULL WHERE artifact_id=$1", aid)


def test_purge_refuses_an_artifact_under_legal_hold(env):
    aid, _ = env.artifact()
    tombstone_held(env, aid)
    status, body = env.req(purge_path(aid), body={"confirm": aid})
    assert status == 409 and "legal hold" in body["error"]
    assert env.blobs.deleted == []
    assert env.sql("SELECT count(*) FROM artifact_blobs WHERE scope=$1", aid)[0][0] == 1
    env.sql("UPDATE artifacts SET purge_after=now() WHERE artifact_id=$1", aid)              # hold lifted
    assert env.req(purge_path(aid), body={"confirm": aid})[0] == 200


def test_purge_refuses_the_legal_hold_flag_even_when_a_purge_date_is_set(env):
    aid, _ = env.artifact()
    retract(env, aid)
    env.sql("UPDATE artifacts SET lifecycle='tombstoned', tombstoned_at=now(), purge_after=now(), legal_hold=true "
            "WHERE artifact_id=$1", aid)
    status, body = env.req(purge_path(aid), body={"confirm": aid})
    assert status == 409 and "legal hold" in body["error"]
    assert env.blobs.deleted == []
    assert env.sql("SELECT count(*) FROM artifact_versions WHERE artifact_id=$1 AND purged_at IS NULL", aid)[0][0] == 1
    assert not env.sql("SELECT 1 FROM artifact_lifecycle_events WHERE artifact_id=$1 AND action='purge'", aid)


def test_member_purge_commits_first_and_defers_the_object_delete_to_the_shared_queue(env, monkeypatch):
    aid, _ = env.artifact()
    retract(env, aid)

    def down(key):
        raise OSError("object store down")
    monkeypatch.setattr(env.blobs, "delete", down)
    status, body = env.req(purge_path(aid), body={"confirm": aid})
    assert status == 200, body                       # the purge record is committed even though the delete failed
    assert body["purged"] is True and body["deleted"] == 0
    assert env.sql("SELECT count(*) FROM artifact_versions WHERE artifact_id=$1 AND purged_at IS NULL", aid)[0][0] == 0
    assert env.sql("SELECT count(*) FROM artifact_pending_deletes")[0][0] >= 1
    event = env.sql("SELECT detail FROM artifact_lifecycle_events WHERE artifact_id=$1 AND action='purge'", aid)
    assert event and "queued_delete" in str(event[0]["detail"])
    # anonymisation and usage settle happened in the same committed transaction
    assert env.sql("SELECT original_filename FROM artifacts WHERE artifact_id=$1", aid)[0][0] == "[removed]"
    usage = env.sql("SELECT bytes_used, bytes_retracted FROM artifact_member_usage WHERE member_principal=$1", ALICE)[0]
    assert (usage["bytes_used"], usage["bytes_retracted"]) == (0, 0)


def test_purge_anonymises_annotations_viewer_events_uploads_and_the_filename(env):
    aid, vid = env.artifact()
    share = env.share(aid, vid)
    sid = share["share_id"]
    env.sql("INSERT INTO artifact_annotations (annotation_id, artifact_id, version_id, share_id, file_name, anchor,"
            " body, author_identity, author_email, author_role, created_at) VALUES"
            " ('ann_' || repeat('a', 22), $1, $2, $3, 'index.html', '{}'::jsonb, 'secret comment', 'ident-x',"
            " 'viewer@lab.example.org', 'member', now())", aid, vid, sid)
    for kind in ("opened", "denied"):
        _call(env.shares, "add_event", sid, kind, "viewer@lab.example.org", {"ip": "10.0.0.9"})
    env.sql("INSERT INTO artifact_collab_uploads (owner_key, upload_id, artifact_id, name, sha256, size_bytes,"
            " storage_key, media_type, charged) VALUES ('m_k', 'upl_1', $1, 'private-notes.html', 'x', 1, 'k',"
            " 'text/html', false)", aid)
    env.sql("UPDATE artifacts SET display_name='Dr Smith report', original_filename='smith-report.html'"
            " WHERE artifact_id=$1", aid)
    retract(env, aid)
    assert env.req(purge_path(aid), body={"confirm": aid})[0] == 200
    note = env.sql("SELECT body, author_identity, author_email, file_name FROM artifact_annotations")[0]
    assert "secret" not in json.dumps(dict(note)) and "viewer@" not in json.dumps(dict(note))
    assert "viewer@" not in json.dumps([dict(r) for r in env.sql(
        "SELECT actor, detail FROM artifact_share_events WHERE share_id=$1", sid)], default=str)
    assert "10.0.0.9" not in json.dumps([dict(r) for r in env.sql(
        "SELECT detail FROM artifact_share_events WHERE share_id=$1", sid)], default=str)
    assert env.sql("SELECT count(*) FROM artifact_share_events WHERE share_id=$1 AND kind='created'", sid)[0][0] == 1
    assert "private-notes" not in env.sql("SELECT name FROM artifact_collab_uploads")[0][0]
    row = env.sql("SELECT display_name, original_filename FROM artifacts WHERE artifact_id=$1", aid)[0]
    assert "smith" not in json.dumps(dict(row)).lower()


def test_purge_blanks_names_and_messages_in_manifests_provenance_and_share_entries(env):
    aid, vid = env.artifact()
    share = env.share(aid, vid)
    before = env.sql("SELECT output_manifest FROM artifact_versions WHERE version_id=$1", vid)[0][0]
    before = json.loads(before) if isinstance(before, str) else before
    assert before and all("name" in o for o in before), before
    env.sql("ALTER TABLE artifact_versions DISABLE TRIGGER artifact_versions_freeze")
    try:
        env.sql("UPDATE artifact_versions SET provenance=provenance || '{\"message\":\"jane-doe diagnosis\","
                "\"entry\":\"jane-doe.html\",\"via\":\"member_web\"}'::jsonb WHERE version_id=$1", vid)
    finally:
        env.sql("ALTER TABLE artifact_versions ENABLE TRIGGER artifact_versions_freeze")
    env.sql("UPDATE artifact_shares SET entry='jane-doe.html' WHERE share_id=$1", share["share_id"])
    retract(env, aid)
    assert env.req(purge_path(aid), body={"confirm": aid})[0] == 200
    row = env.sql("SELECT output_manifest, provenance FROM artifact_versions WHERE version_id=$1", vid)[0]
    manifest = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    prov = json.loads(row[1]) if isinstance(row[1], str) else row[1]
    assert [o["name"] for o in manifest] == ["[removed]"] * len(before)
    assert [o["storage_key"] for o in manifest] == [o["storage_key"] for o in before]          # bytes stay addressable
    assert [o["sha256"] for o in manifest] == [o["sha256"] for o in before]
    assert prov["message"] == "[removed]" and prov["entry"] == "[removed]" and prov["via"] == "member_web"
    assert env.sql("SELECT entry FROM artifact_shares WHERE share_id=$1", share["share_id"])[0][0] is None
    assert "jane" not in json.dumps([dict(r) for r in env.sql("SELECT * FROM artifact_versions")], default=str)


def test_the_version_freeze_still_refuses_edits_outside_a_purge(env):
    aid, vid = env.artifact()
    import pytest
    with pytest.raises(Exception, match="immutable"):
        env.sql("UPDATE artifact_versions SET output_manifest=jsonb_set(output_manifest, '{0,name}', '\"x\"') "
                "WHERE version_id=$1", vid)
    with pytest.raises(Exception, match="immutable"):
        env.sql("UPDATE artifact_versions SET output_manifest=jsonb_set(output_manifest, '{0,storage_key}', '\"x\"') "
                "WHERE version_id=$1 AND set_config('herdr.purge_redaction','on',false)='on'", vid)


def test_purge_clears_every_viewer_identity_quoted_anchor_text_and_feedback(env):
    from herdr_engineering.control_plane.artifact_shares import EVENT_KINDS
    aid, vid = env.artifact()
    sid = env.share(aid, vid)["share_id"]
    env.sql("INSERT INTO artifact_annotations (annotation_id, artifact_id, version_id, share_id, file_name, anchor,"
            " body, author_identity, author_email, author_role, created_at) VALUES"
            " ('ann_' || repeat('b', 22), $1, $2, $3, 'index.html',"
            " '{\"file\": \"index.html\", \"exact\": \"quoted sentence\", \"prefix\": \"before text\","
            " \"suffix\": \"after text\"}'::jsonb, 'c', 'ident-x', 'viewer@lab.example.org', 'member', now())",
            aid, vid, sid)
    for kind in [k for k in EVENT_KINDS if k != "created"]:
        _call(env.shares, "add_event", sid, kind, "viewer@lab.example.org", {"file": "private.html"})
    env.sql("INSERT INTO artifact_report_feedback (feedback_id, artifact_id, version_id, target_kind, body, actor)"
            " VALUES ('fb_1', $1, $2, 'report', 'reviewer remark', 'carol@lab.example.org')", aid, vid)
    env.sql("INSERT INTO artifact_annotation_ops (ingest_key, annotation_id, op, at)"
            " VALUES ('k1', 'ann_' || repeat('b', 22), 'edit', now())")
    retract(env, aid)
    assert env.req(purge_path(aid), body={"confirm": aid})[0] == 200
    events = [dict(r) for r in env.sql("SELECT kind, actor, detail FROM artifact_share_events WHERE share_id=$1", sid)]
    blob = json.dumps(events, default=str)
    assert "viewer@" not in blob and "private.html" not in blob
    assert {e["kind"] for e in events} >= set(EVENT_KINDS) - {"created"}, "audit rows must remain"
    assert [e["actor"] for e in events if e["kind"] == "created"] != ["[removed]"]
    anchor = json.dumps(env.sql("SELECT anchor::text FROM artifact_annotations")[0][0])
    assert not any(t in anchor for t in ("quoted sentence", "before text", "after text"))
    fb = json.dumps(dict(env.sql("SELECT body, actor FROM artifact_report_feedback")[0]))
    assert "remark" not in fb and "carol@" not in fb


def test_operator_can_share_only_active_member_artifacts_and_not_via_publications(env):
    aid, vid = env.artifact()
    pub = {"resource_id": aid, "version_id": vid, "audience": "unlisted_link", "expires_at": time.time() + 3600}
    status, _ = env.req("/api/v1/publications/requests", body=pub)
    assert status in {403, 404}
    assert env.server.publication_controller.list() == []
    retract(env, aid)
    assert env.req(f"/api/v1/artifacts/{aid}")[0] == 200                  # read stays for inspection
    status, _ = env.req(f"/api/v1/artifacts/{aid}/shares", body={"version_id": vid, "audience": "public"})
    assert status == 404
    env.sql("UPDATE artifacts SET lifecycle='archived' WHERE artifact_id=$1", aid)
    assert env.req(f"/api/v1/artifacts/{aid}/shares", body={"version_id": vid, "audience": "public"})[0] == 404
    assert env.sql("SELECT count(*) FROM artifact_shares")[0][0] == 0


def test_a_link_cannot_be_created_once_sharing_is_disabled_even_past_the_gate(env):
    from herdr_engineering.control_plane.artifact_shares import ShareError
    aid, vid = env.artifact()
    assert env.req(BASE_BODY + "/alice/sharing", body={"enabled": False})[0] == 200
    with pytest.raises(ShareError) as caught:
        env.share(aid, vid, approve=False)
    assert caught.value.code == "sharing_disabled"
    assert env.sql("SELECT count(*) FROM artifact_shares")[0][0] == 0


def test_sweep_revokes_for_disabled_sharing_without_a_fresh_directory(env, tmp_path):
    aid, vid = env.artifact()
    share = env.share(aid, vid)
    env.sql("UPDATE artifact_member_usage SET sharing_disabled=true WHERE member_principal=$1", ALICE)
    rt = MemberRuntime(outbox=tmp_path / "no-outbox", mirror_root=tmp_path / "mirror", group="lab", enabled=True,
                       defaults={}, shares_root=env.root, clock=time.time)

    async def sweep(store, conn):
        return await sweep_disabled_members(conn, rt)
    assert env.run(sweep) == 1
    assert _call(env.shares, "get", share["share_id"])["state"] == "revoked"


def test_a_repeated_purge_replays_as_success(env):
    aid, _ = env.artifact()
    retract(env, aid)
    assert env.req(purge_path(aid), body={"confirm": aid})[0] == 200
    status, body = env.req(purge_path(aid), body={"confirm": aid})
    assert status == 200 and body["already_purged"] is True
