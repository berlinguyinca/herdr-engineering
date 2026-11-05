"""packages/member-upload-vector.json is the server's own upload-id derivation for one fixed input; the browser unit tests
use it, so this test fails when the server derivation (or the file hash it covers) changes and the fixture is stale."""
import hashlib
import json
from pathlib import Path

from herdr_engineering.control_plane.artifact_members import member_owner_key
from herdr_engineering.control_plane.collab_bridge_files import upload_id_for

VECTOR = Path(__file__).resolve().parents[2] / "packages" / "member-upload-vector.json"


def test_the_browser_fixture_matches_the_server_derivation():
    v = json.loads(VECTOR.read_text())
    data = bytes(((i * 31 + (i >> 8)) & 255) for i in range(v["size_bytes"]))
    assert hashlib.sha256(data).hexdigest() == v["file_sha256"]
    assert upload_id_for(member_owner_key(v["member"]), v["artifact_id"], v["name"], v["file_sha256"], v["size_bytes"]) == v["upload_id"]
