# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""Rakazo files over the artifact registry, using an in-memory registry with the real store's method shapes."""
import base64
import hashlib

import pytest

from tests.control_plane.test_rakazo_bridge import alice, make_server, new_bot, srv  # noqa: F401
from tests.control_plane.test_rakazo_contract import validate


class FakeRegistry:
    """register_logical / commit_version / list_records / get_record / list_versions with the real return shapes."""
    available = True

    def __init__(self):
        self.records, self.versions, self.blobs, self.idem, self.grants = {}, {}, {}, {}, []

    def put(self, data):
        sha = hashlib.sha256(data).hexdigest()
        self.blobs[f"k/{sha}"] = data
        return f"k/{sha}", sha

    def get(self, key, sha256):
        assert hashlib.sha256(self.blobs[key]).hexdigest() == sha256
        return self.blobs[key]

    def grant(self, artifact_id, principal):
        self.grants.append((artifact_id, principal))

    def call(self, method, *args, **kw):
        return getattr(self, "_" + method)(*args, **kw)

    def _register_logical(self, *, original_filename, artifact_type, created_by, mime_type, workspace_id=None):
        aid = f"art_{len(self.records) + 1}"
        self.records[aid] = {"artifact_id": aid, "original_filename": original_filename, "mime_type": mime_type,
                             "artifact_type": artifact_type, "created_by": created_by,
                             "created_at": "2026-10-30T10:00:00+00:00", "current_version_id": None}
        return self.records[aid]

    def _commit_version(self, artifact_id, *, base_version_id, source_manifest_blob, output_manifest, provenance=None,
                        created_by=None, idempotency_key=None, **_):
        if idempotency_key in self.idem:
            return self.idem[idempotency_key]
        rows = self.versions.setdefault(artifact_id, [])
        row = {"version_id": f"ver_{artifact_id}_{len(rows) + 1}", "artifact_id": artifact_id,
               "version_number": len(rows) + 1, "output_manifest": output_manifest,
               "created_at": "2026-10-30T10:00:01+00:00"}
        rows.append(row)
        self.records[artifact_id]["current_version_id"] = row["version_id"]
        self.idem[idempotency_key] = row
        return row

    def _list_versions(self, artifact_id):
        return list(reversed(self.versions.get(artifact_id, [])))

    def _list_records(self, *, principal_id, query=None, artifact_type=None, status=None, limit=200, offset=0):
        return [r for r in self.records.values() if r["created_by"] == principal_id][offset:offset + limit]

    def _get_record(self, artifact_id, *, principal_id):
        r = self.records.get(artifact_id)
        return r if r and r["created_by"] == principal_id else None


@pytest.fixture()
def reg(srv):
    registry = FakeRegistry()
    srv[0].rakazo.artifacts._backend_fn = lambda: registry
    return registry


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def rpc(api, proc, inp, token):
    return api.rpc(proc, inp, token)[0]


def test_create_get_list_versions_and_hide(srv, alice, reg):
    _, api = srv
    bot = new_bot(api, alice)
    made = api.ok("artifacts/create", {"botId": bot["id"], "name": "notes.txt", "description": "d", "mimeType": "text/plain",
                                       "contentBase64": b64(b"hello"), "clientNonce": "n1"}, alice)
    validate("artifacts/create", made)
    assert (made["name"], made["size"], made["version"], made["botId"]) == ("notes.txt", 5, 1, bot["id"])
    again = api.ok("artifacts/create", {"botId": bot["id"], "name": "notes.txt", "mimeType": "text/plain",
                                        "contentBase64": b64(b"hello"), "clientNonce": "n1"}, alice)
    assert again["id"] == made["id"] and len(reg.records) == 1                    # a retried upload is not a second file
    assert reg.grants == [(made["id"], srv[0].rakazo.accounts[0].principal_id)]
    got = api.ok("artifacts/get", {"artifactId": made["id"]}, alice)
    validate("artifacts/get", got)
    assert base64.b64decode(got["contentBase64"]) == b"hello"
    validate("artifacts/getById", api.ok("artifacts/getById", {"artifactId": made["id"]}, alice))
    listing = api.ok("artifacts/list", {"botId": bot["id"]}, alice)
    validate("artifacts/list", listing)
    assert [a["id"] for a in listing] == [made["id"]]
    space = api.ok("artifacts/listSpace", {"limit": 10}, alice)
    validate("artifacts/listSpace", space)
    assert space["items"][0]["versionCount"] == 1 and space["nextCursor"] is None
    versions = api.ok("artifacts/listVersions", {"familyId": made["id"]}, alice)
    validate("artifacts/listVersions", versions)
    old = api.ok("artifacts/get", {"artifactId": versions[0]["id"]}, alice)
    assert old["id"] == made["id"]
    validate("artifacts/remove", api.ok("artifacts/remove", {"artifactId": made["id"]}, alice))
    assert api.ok("artifacts/list", {"botId": bot["id"]}, alice) == []
    assert made["id"] in reg.records                                                 # hidden from the app, never deleted
    assert rpc(api, "artifacts/get", {"artifactId": made["id"]}, alice) == 404


def test_pages_and_spaces_are_separate(srv, alice, reg):
    _, api = srv
    for i in range(3):
        api.ok("artifacts/create", {"name": f"f{i}", "mimeType": "text/plain", "contentBase64": b64(b"x")}, alice)
    first = api.ok("artifacts/listSpace", {"limit": 2}, alice)
    assert len(first["items"]) == 2 and first["nextCursor"] == "2"
    second = api.ok("artifacts/listSpace", {"limit": 2, "cursor": first["nextCursor"]}, alice)
    assert len(second["items"]) == 1 and second["nextCursor"] is None
    other = api.ok("spaces/create", {"name": "Other"}, alice)
    status, body, _ = api.rpc("artifacts/listSpace", {}, alice, **{"x-rakazo-space-id": other["id"]})
    assert status == 200 and body["json"]["items"] == []
    status, _, _ = api.rpc("artifacts/get", {"artifactId": first["items"][0]["id"]}, alice, **{"x-rakazo-space-id": other["id"]})
    assert status == 404


def test_bad_uploads_are_refused(srv, alice, reg):
    _, api = srv
    base = {"name": "a.txt", "mimeType": "text/plain", "contentBase64": b64(b"x")}
    for change in ({"contentBase64": "not base64!!"}, {"name": "a/b"}, {"name": ""}, {"botId": "nope"},
                   {"description": "x" * 281}, {"contentBase64": b64(b"x" * (8 * 1024 * 1024 + 1))}):
        assert rpc(api, "artifacts/create", {**base, **change}, alice) in (400, 404), change
    assert not reg.records


def test_without_a_registry_lists_are_empty_and_writes_say_why(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    srv[0].rakazo.artifacts._backend_fn = lambda: None
    assert api.ok("artifacts/list", {"botId": bot["id"]}, alice) == []
    assert api.ok("artifacts/listSpace", {}, alice) == {"items": [], "nextCursor": None}
    status, body, _ = api.rpc("artifacts/create", {"name": "a", "mimeType": "text/plain", "contentBase64": b64(b"x")}, alice)
    assert status == 400 and "not enabled" in body["json"]["message"]
