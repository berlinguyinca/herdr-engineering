"""Shared artifact workspace tests (spec 0080)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from herdr_engineering.artifacts import FilesystemArtifactWorkspace


def test_publish_read_roundtrip(tmp_path):
    ws = FilesystemArtifactWorkspace(root=tmp_path)
    res = ws.publish_bytes(title="log", kind="test-log",
                           data=b"hello", project="proj", mission_or_session="msn")
    assert res.ok and res.status == "published"
    assert res.artifact.sha256
    assert ws.read(res.artifact) == b"hello"


def test_content_hash_mismatch_rejected(tmp_path):
    ws = FilesystemArtifactWorkspace(root=tmp_path)
    res = ws.publish_bytes(title="t", kind="k", data=b"data",
                           project="p", mission_or_session="m")
    ref = res.artifact
    ref.sha256 = "0" * 64  # corrupt the claimed hash
    assert ws.read(ref) is None


def test_secret_exclusion(tmp_path):
    ws = FilesystemArtifactWorkspace(root=tmp_path)
    res = ws.publish_bytes(title=".ssh/id_rsa", kind="key", data=b"secret",
                           project="p", mission_or_session="m")
    assert res.ok is False and res.status == "failed"


def test_degrades_when_storage_down(tmp_path):
    ws = FilesystemArtifactWorkspace(root=tmp_path)
    ws.mark_unavailable()
    res = ws.publish_bytes(title="t", kind="k", data=b"x",
                           project="p", mission_or_session="m")
    assert res.ok is False and res.status == "queued"


def test_retention(tmp_path):
    import time
    ws = FilesystemArtifactWorkspace(root=tmp_path, retention_days=1)
    ws.publish_bytes(title="old", kind="k", data=b"old",
                     project="p", mission_or_session="m")
    removed = ws.enforce_retention(now_ts=time.time() + 3 * 86400)  # 3 days later
    assert removed >= 1
