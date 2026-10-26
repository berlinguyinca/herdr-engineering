import asyncio
import json

import pytest

from herdr_engineering.control_plane.artifacts import ArtifactConflict, ArtifactError, ArtifactStore


class _VersionConn:
    def __init__(self):
        self.artifacts = {}
        self.versions = {}
        self.idempotency = {}
        self.audit = []

    async def execute(self, sql, *args):
        if "INSERT INTO artifacts" in sql:
            aid = args[0]
            self.artifacts[aid] = {"artifact_id": aid, "current_version_id": None}
        elif "INSERT INTO artifact_versions" in sql:
            version_id, artifact_id, number = args[:3]
            self.versions[version_id] = {
                "version_id": version_id, "artifact_id": artifact_id,
                "version_number": number, "state": "ready",
                "source_manifest_blob": args[5],
                "output_manifest": json.loads(args[6]),
            }
        elif "UPDATE artifacts SET current_version_id" in sql:
            self.artifacts[args[1]]["current_version_id"] = args[0]
        elif "INSERT INTO artifact_idempotency" in sql:
            self.idempotency[(args[0], args[1])] = (args[2], args[3])
        elif "INSERT INTO artifact_audit" in sql:
            self.audit.append(args)

    async def fetchrow(self, sql, *args):
        if "FROM artifacts" in sql:
            return self.artifacts.get(args[0])
        if "FROM artifact_idempotency" in sql:
            prior = self.idempotency.get((args[0], args[1]))
            return ({"version_id": prior[1], "request_sha256": prior[0]}
                    if prior else None)
        if "MAX(version_number)" in sql:
            numbers = [v["version_number"] for v in self.versions.values()
                       if v["artifact_id"] == args[0]]
            return {"next_number": (max(numbers) + 1) if numbers else 1}
        if "FROM artifact_versions" in sql:
            return self.versions.get(args[0])
        return None

    async def fetch(self, sql, *args):
        rows = [v for v in self.versions.values() if v["artifact_id"] == args[0]]
        return sorted(rows, key=lambda v: v["version_number"], reverse=True)


class _NoopRustFS:
    def head(self, reference):
        return True

    def get(self, reference, expected_sha256):
        return b"materialized"


def _run(coro):
    return asyncio.run(coro)


def _store():
    conn = _VersionConn()
    return ArtifactStore(conn, _NoopRustFS()), conn


def test_commit_version_advances_logical_head_and_preserves_manifest():
    store, conn = _store()
    artifact = _run(store.register_logical(original_filename="index.html",
                                            artifact_type="web", created_by="agent-a"))
    version = _run(store.commit_version(
        artifact["artifact_id"], base_version_id=None,
        source_manifest_blob="sha-source",
        output_manifest=[{"name": "index.html", "blob_sha256": "sha-output"}],
        provenance={"host_id": "host-a"}, created_by="agent-a",
        idempotency_key="req-1"))
    assert version["version_number"] == 1
    assert version["version_id"].startswith("version_")
    assert version["output_manifest"] == [{"name": "index.html", "blob_sha256": "sha-output"}]
    assert conn.artifacts[artifact["artifact_id"]]["current_version_id"] == version["version_id"]


def test_commit_version_rejects_stale_base_without_losing_head():
    store, _ = _store()
    artifact = _run(store.register_logical(original_filename="a.md"))
    first = _run(store.commit_version(artifact["artifact_id"], base_version_id=None,
                                      source_manifest_blob="s1", output_manifest=[]))
    with pytest.raises(ArtifactConflict) as exc:
        _run(store.commit_version(artifact["artifact_id"], base_version_id=None,
                                  source_manifest_blob="s2", output_manifest=[]))
    assert exc.value.current_version_id == first["version_id"]


def test_commit_version_idempotency_returns_same_version():
    store, _ = _store()
    artifact = _run(store.register_logical(original_filename="a.md"))
    first = _run(store.commit_version(artifact["artifact_id"], base_version_id=None,
                                      source_manifest_blob="s1", output_manifest=[],
                                      idempotency_key="same"))
    retry = _run(store.commit_version(artifact["artifact_id"], base_version_id=None,
                                      source_manifest_blob="s1", output_manifest=[],
                                      idempotency_key="same"))
    assert retry["version_id"] == first["version_id"]
    assert retry["version_number"] == first["version_number"]


def test_commit_version_rejects_unreferenced_output_manifest_entries():
    store, _ = _store()
    artifact = _run(store.register_logical(original_filename="a.md"))
    with pytest.raises(ArtifactError, match="blob reference"):
        _run(store.commit_version(artifact["artifact_id"], base_version_id=None,
                                  source_manifest_blob="s1",
                                  output_manifest=[{"name": "index.html"}]))


def test_materialize_version_writes_verified_outputs_atomically(tmp_path):
    store, _ = _store()
    artifact = _run(store.register_logical(original_filename="index.html"))
    version = _run(store.commit_version(
        artifact["artifact_id"], base_version_id=None, source_manifest_blob="sha-source",
        output_manifest=[{"name": "index.html", "storage_key": "sha-output",
                          "sha256": "sha-output"}]))
    target = tmp_path / "checkout"
    result = _run(store.materialize_version(version["version_id"], host_id="host-b",
                                            session_id="session-b", target_dir=target))
    assert result["files"] == ["index.html"]
    assert (target / "index.html").read_bytes() == b"materialized"


def test_materialize_version_rejects_path_escape(tmp_path):
    store, _ = _store()
    artifact = _run(store.register_logical(original_filename="bad"))
    version = _run(store.commit_version(
        artifact["artifact_id"], base_version_id=None, source_manifest_blob="sha-source",
        output_manifest=[{"name": "../escape", "storage_key": "sha-output",
                          "sha256": "sha-output"}]))
    with pytest.raises(ArtifactError, match="unsafe output path"):
        _run(store.materialize_version(version["version_id"], host_id="host-b",
                                       session_id="session-b", target_dir=tmp_path / "checkout"))

