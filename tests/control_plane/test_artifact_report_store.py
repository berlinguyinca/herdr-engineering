"""Immutable report commits must bind descriptors, checks and scoped assets."""
import asyncio
import hashlib
import json

import asyncpg
import pytest

from herdr_engineering.control_plane.artifacts import ArtifactError, ArtifactStore
from tests.control_plane.test_artifact_report_postgres import report_postgres  # noqa: F401


def descriptor():
    return {"schema_version": 1, "title": "Procedure investigation", "summary": "Open questions",
            "questions": [{"id": "q1", "text": "Where is coverage missing?"}],
            "evidence": [{"id": "e1", "title": "Observed account", "description": "One account."}],
            "findings": [{"id": "f1", "title": "Coverage", "statement": "More evidence is needed.",
                          "question_ids": ["q1"], "evidence_ids": ["e1"]}],
            "figures": [], "assets": [], "limitations": ["Qualitative observations only."]}


class Connection:
    def __init__(self):
        self.artifacts = {}
        self.versions = {}
        self.requests = {}
        self.blobs = {}

    async def execute(self, sql, *args):
        if "INSERT INTO artifacts " in sql:
            self.artifacts[args[0]] = {"artifact_id": args[0], "artifact_type": args[3],
                                       "created_by": args[4], "current_version_id": None}
        elif "INSERT INTO artifact_versions " in sql:
            self.versions[args[0]] = {"version_id": args[0], "artifact_id": args[1],
                "version_number": args[2], "source_manifest_blob": args[5],
                "output_manifest": json.loads(args[6]), "provenance": json.loads(args[7]),
                "report_descriptor": json.loads(args[14]) if args[14] else None,
                "validation": json.loads(args[15])}
        elif "UPDATE artifacts SET current_version_id" in sql:
            self.artifacts[args[1]]["current_version_id"] = args[0]
        elif "INSERT INTO artifact_idempotency" in sql:
            self.requests[(args[0], args[1])] = {"request_sha256": args[2], "version_id": args[3]}
        elif "INSERT INTO artifact_blobs" in sql:
            # Parent store specifies explicit scope, digest, key, size and MIME.
            self.blobs[(args[0], args[1])] = {"scope": args[0], "blob_sha256": args[1],
                "storage_key": args[2], "size_bytes": args[3], "media_type": args[4]}

    async def fetchrow(self, sql, *args):
        if "FROM artifact_idempotency" in sql:
            return self.requests.get((args[0], args[1]))
        if "MAX(version_number)" in sql:
            return {"next_number": len([v for v in self.versions.values() if v["artifact_id"] == args[0]]) + 1}
        if "FROM artifact_versions" in sql:
            value = self.versions.get(args[0])
            return value if value and (len(args) == 1 or value["artifact_id"] == args[1]) else None
        if "FROM artifact_blobs" in sql:
            if "scope <> 'fabric'" in sql:
                return next((blob for (scope, digest), blob in self.blobs.items()
                             if scope != "fabric" and
                             (blob["storage_key"] == args[0] or digest == args[1])), None)
            return self.blobs.get((args[0], args[1]))
        if "FROM artifact_acl" in sql:
            return None
        if "FROM artifacts" in sql:
            return self.artifacts.get(args[0])
        return None


class Blobs:
    def __init__(self):
        self.objects = {}

    def put(self, value):
        sha = hashlib.sha256(value).hexdigest()
        key = f"sha256/{sha[:2]}/{sha[2:4]}/{sha}"
        self.objects[key] = value
        return key, sha

    def head(self, key):
        return key in self.objects


def run(value):
    return asyncio.run(value)


def setup():
    connection, blobs = Connection(), Blobs()
    store = ArtifactStore(connection, blobs)
    artifact = run(store.register_logical(original_filename="report.html", artifact_type="report",
                                          created_by="owner", mime_type="text/html"))
    return store, connection, blobs, artifact["artifact_id"]


def test_report_descriptor_and_renderer_validation_are_immutable_version_content():
    store, _, _, aid = setup()
    result = run(store.commit_version(aid, base_version_id=None, source_manifest_blob=None,
        output_manifest=[], report_descriptor=descriptor(),
        validation={"status": "passed", "checks": ["descriptor", "render"]},
        created_by="owner", provenance={"toolchain": {"renderer": "generic-report"}}))
    retrieved = run(store.get_version(result["version_id"], aid))
    assert retrieved["report_descriptor"] == result["report_descriptor"]
    assert retrieved["validation"]["checks"] == ["descriptor", "render"]


def test_idempotency_cannot_replace_descriptor_or_validation():
    store, _, _, aid = setup()
    arguments = dict(base_version_id=None, source_manifest_blob=None, output_manifest=[],
                     report_descriptor=descriptor(), created_by="owner", idempotency_key="request")
    first = run(store.commit_version(aid, **arguments))
    assert run(store.commit_version(aid, **arguments))["version_id"] == first["version_id"]
    changed = descriptor()
    changed["summary"] = "Changed interpretation"
    with pytest.raises(ArtifactError, match="idempotency"):
        run(store.commit_version(aid, **{**arguments, "report_descriptor": changed}))
    with pytest.raises(ArtifactError, match="idempotency"):
        run(store.commit_version(aid, **arguments, validation={"status": "passed"}))


def test_report_assets_need_artifact_scoped_ownership_not_just_a_known_blob_hash():
    store, conn, blobs, aid = setup()
    key, sha = blobs.put(b"supporting evidence")
    d = descriptor()
    d["assets"] = [{"id": "data1", "name": "data.txt", "role": "data", "media_type": "text/plain",
                    "sha256": sha, "size_bytes": 19}]
    outputs = [{"name": "data.txt", "storage_key": key, "sha256": sha,
                "media_type": "text/plain", "size_bytes": 19}]
    with pytest.raises(ArtifactError, match="scope|scoped|uploaded|digest/reference"):
        run(store.commit_version(aid, base_version_id=None, source_manifest_blob=None,
                                output_manifest=outputs, report_descriptor=d))
    run(store.register_report_blob(aid, storage_key=key, sha256=sha, size_bytes=19,
                                    media_type="text/plain"))
    version = run(store.commit_version(aid, base_version_id=None, source_manifest_blob=None,
                                      output_manifest=outputs, report_descriptor=d))
    assert version["report_descriptor"]["assets"][0]["id"] == "data1"
    assert conn.artifacts[aid]["current_version_id"] == version["version_id"]


def test_report_permissions_are_owner_or_explicit_scoped_grant():
    store, _, _, aid = setup()
    assert run(store.report_permission(aid, "owner", "edit"))
    assert not run(store.report_permission(aid, "other", "read"))
    assert not run(store.report_permission("missing", "owner", "read"))


def test_report_artifact_cannot_be_downgraded_by_omitting_its_descriptor():
    store, _, _, aid = setup()
    with pytest.raises(ArtifactError, match="descriptor"):
        run(store.commit_version(aid, base_version_id=None, source_manifest_blob=None,
                                output_manifest=[]))


def test_existing_nonreport_idempotency_keys_keep_their_original_hash_contract():
    connection, blobs = Connection(), Blobs()
    store = ArtifactStore(connection, blobs)
    artifact = run(store.register_logical(original_filename="note.txt", created_by="owner"))
    aid = artifact["artifact_id"]
    arguments = dict(base_version_id=None, source_manifest_blob=None, output_manifest=[],
                     created_by="owner", idempotency_key="legacy-key")
    first = run(store.commit_version(aid, **arguments))
    legacy_hash = hashlib.sha256(json.dumps({"artifact_id": aid, "base_version_id": None,
        "source_manifest_blob": None, "output_manifest": [], "provenance": {}},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    connection.requests[("owner", "legacy-key")]["request_sha256"] = legacy_hash
    assert run(store.commit_version(aid, **arguments))["version_id"] == first["version_id"]


def test_known_private_blob_cannot_be_smuggled_as_an_extra_output_or_source():
    store, _, blobs, aid = setup()
    key, sha = blobs.put(b"private to a different artifact")
    for source, outputs in [
        (None, [{"name": "index.html", "storage_key": key, "sha256": sha,
                 "size_bytes": 31, "media_type": "text/html"}]),
        (key, []),
    ]:
        with pytest.raises(ArtifactError, match="scope|scoped|uploaded|digest/reference"):
            run(store.commit_version(aid, base_version_id=None, source_manifest_blob=source,
                                    output_manifest=outputs, report_descriptor=descriptor()))


@pytest.mark.parametrize("reference_kind", ["source-key", "source-digest", "output-key", "output-digest", "output-false-sha"])
def test_nonreport_target_cannot_attach_another_artifacts_private_blob(reference_kind):
    store, conn, blobs, owner_aid = setup()
    private = b"private evidence"
    key, sha = blobs.put(private)
    run(store.register_report_blob(owner_aid, storage_key=key, sha256=sha,
                                   size_bytes=len(private), media_type="text/plain"))
    # A historical fabric registration must not override a private registration.
    conn.blobs[("fabric", sha)] = dict(conn.blobs[(owner_aid, sha)], scope="fabric")
    target = run(store.register_logical(original_filename="copied.txt", created_by="outsider"))
    aid = target["artifact_id"]
    source = None
    outputs = []
    if reference_kind.startswith("source"):
        source = sha if reference_kind.endswith("digest") else key
    else:
        outputs = [{"name": "copied.txt", "size_bytes": len(private), "media_type": "text/plain"}]
        if reference_kind == "output-digest":
            outputs[0]["blob_sha256"] = sha
        else:
            outputs[0].update(storage_key=key, sha256="0" * 64 if reference_kind.endswith("false-sha") else sha)
    with pytest.raises(ArtifactError, match="scope|scoped|uploaded|digest/reference"):
        run(store.commit_version(aid, base_version_id=None, source_manifest_blob=source,
                                 output_manifest=outputs, created_by="outsider"))
    assert conn.artifacts[aid]["current_version_id"] is None
    assert not conn.versions


@pytest.mark.parametrize("registration", ["unregistered", "fabric", "same-artifact"])
def test_nonreport_target_preserves_legacy_unscoped_and_own_uploaded_blobs(registration):
    store, conn, blobs, _ = setup()
    aid = run(store.register_logical(original_filename="note.txt", created_by="owner"))["artifact_id"]
    content = b"legacy note"
    key, sha = blobs.put(content)
    if registration != "unregistered":
        run(store.register_report_blob(aid if registration == "same-artifact" else "fabric",
            storage_key=key, sha256=sha, size_bytes=len(content), media_type="text/plain"))
    result = run(store.commit_version(aid, base_version_id=None, source_manifest_blob=sha,
        output_manifest=[{"name": "note.txt", "storage_key": key, "sha256": sha,
                          "size_bytes": len(content), "media_type": "text/plain"}]))
    assert conn.artifacts[aid]["current_version_id"] == result["version_id"]


def test_cross_scope_attachment_policy_on_native_postgres(report_postgres):  # noqa: F811 - imported fixture
    async def exercise():
        connection = await asyncpg.connect(report_postgres["dsn"])
        try:
            blobs = Blobs()
            store = ArtifactStore(connection, blobs)
            owner = await store.register_logical(original_filename="private.html", artifact_type="report",
                                                 created_by="private-owner")
            target = await store.register_logical(original_filename="public.txt", created_by="outsider")
            aid = target["artifact_id"]
            private = b"private native evidence"
            key, sha = blobs.put(private)
            metadata = dict(storage_key=key, sha256=sha, size_bytes=len(private), media_type="text/plain")
            await store.register_report_blob(owner["artifact_id"], **metadata)
            await store.register_report_blob("fabric", **metadata)
            _, benign_sha = blobs.put(b"public native content")
            for source, outputs in [(key, []), (sha, []), (None, [{"name": "data.txt", **metadata}]),
                                    (None, [{"name": "data.txt", "blob_sha256": sha}]),
                                    (None, [{"name": "data.txt", **metadata, "sha256": "0" * 64}]),
                                    (None, [{"name": "data.txt", "blob_sha256": benign_sha, "sha256": sha}])]:
                with pytest.raises(ArtifactError, match="scope|scoped|uploaded|digest/reference"):
                    await store.commit_version(aid, base_version_id=None, source_manifest_blob=source,
                                               output_manifest=outputs, created_by="outsider")
            assert await connection.fetchval("SELECT count(*) FROM artifact_versions WHERE artifact_id=$1", aid) == 0
            # An independently uploaded/verified copy in the target scope authorizes reuse.
            await store.register_report_blob(aid, **metadata)
            result = await store.commit_version(aid, base_version_id=None, source_manifest_blob=sha,
                                                output_manifest=[{"name": "data.txt", **metadata}])
            assert result["artifact_id"] == aid
        finally:
            await connection.close()

    run(exercise())


@pytest.mark.parametrize("reference_kind", ["digest-only", "key-sha", "key-blob-sha"])
def test_unscoped_output_cannot_override_its_reference_with_a_private_digest(reference_kind):
    store, conn, blobs, owner_aid = setup()
    private_key, private_sha = blobs.put(b"private evidence")
    run(store.register_report_blob(owner_aid, storage_key=private_key, sha256=private_sha,
                                   size_bytes=16, media_type="text/plain"))
    benign_key, benign_sha = blobs.put(b"public")
    aid = run(store.register_logical(original_filename="copied.txt", created_by="outsider"))["artifact_id"]
    output = {"name": "copied.txt", "size_bytes": 16, "media_type": "text/plain"}
    if reference_kind == "digest-only":
        output.update(blob_sha256=benign_sha, sha256=private_sha)
    elif reference_kind == "key-sha":
        output.update(storage_key=benign_key, sha256=private_sha)
    else:
        output.update(storage_key=benign_key, sha256=benign_sha, blob_sha256=private_sha)
    with pytest.raises(ArtifactError, match="digest|reference"):
        run(store.commit_version(aid, base_version_id=None, source_manifest_blob=None,
                                 output_manifest=[output], created_by="outsider"))
    assert not conn.versions
    assert conn.artifacts[aid]["current_version_id"] is None
