"""Real HTTP report boundaries: private versions, bounded previews and explicit bytes."""
import asyncio
import copy
import hashlib
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_jobs import ArtifactJobManager
from herdr_engineering.control_plane.artifact_reviews import ArtifactReviewManager
from herdr_engineering.control_plane.artifacts import ArtifactError
from herdr_engineering.control_plane.publication import FakeExposureProvider, PublicationController
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def document():
    sha = hashlib.sha256(b"evidence").hexdigest()
    return {"schema_version": 1, "title": "Generic investigation", "summary": "Open evidence",
            "questions": [{"id": "q1", "text": "What is known?"}],
            "evidence": [{"id": "e1", "title": "Account", "description": "Observed information.",
                          "asset_ids": ["data1"]}],
            "findings": [{"id": "f1", "title": "Uncertainty", "statement": "Collect observations.",
                          "question_ids": ["q1"], "evidence_ids": ["e1"]}],
            "figures": [], "assets": [{"id": "data1", "name": "data.csv", "role": "data",
                "media_type": "text/csv", "sha256": sha, "size_bytes": 8}]}


class Store:
    def __init__(self):
        self.descriptor = document()
        self.feedback = []
        self.calls = []

    def sync_call(self, method, *args, **kwargs):
        return asyncio.run(getattr(self, method)(*args, **kwargs))

    async def report_permission(self, aid, principal, permission):
        return aid == "artifact_report" and hashlib.sha256(b"owner-key").hexdigest()[:16] in principal

    async def get_report(self, aid, vid):
        if aid != "artifact_report" or vid not in {"v1", "v2"}:
            raise ArtifactError("no artifact report version")
        return {"artifact_id": aid, "version_id": vid, "report_descriptor": copy.deepcopy(self.descriptor),
                "assets": copy.deepcopy(self.descriptor["assets"]), "feedback": list(self.feedback),
                "checks": {stage: {"status": "not_checked"} for stage in
                           ("rendered", "evidence_checked", "reviewed")}, "state": "unvalidated"}

    async def get_report_descriptor(self, aid, vid):
        if aid != "artifact_report" or vid not in {"v1", "v2"}:
            raise ArtifactError("no artifact report version")
        return copy.deepcopy(self.descriptor)

    async def get_version(self, vid, aid):
        return {"version_id": vid, "artifact_id": aid} if aid == "artifact_report" and vid in {"v1", "v2"} else None

    async def get_report_job(self, aid, jid):
        return self.persisted_job

    async def cancel_report_job(self, aid, jid):
        self.calls.append("cancel_report_job")
        if self.persisted_job["status"] == "queued":
            self.persisted_job = {**self.persisted_job, "status": "cancelled"}
            return self.persisted_job
        return None

    async def register_report_blob(self, aid, **metadata):
        self.calls.append(metadata)
        return metadata

    async def get_report_diff(self, aid, vid, base):
        await self.get_report(aid, vid)
        await self.get_report(aid, base)
        return {"version_id": vid, "base_version_id": base, "changed": False, "categories": {}}

    async def list_report_feedback(self, aid, vid, **kwargs):
        await self.get_report(aid, vid)
        rows = [row for row in self.feedback if row["version_id"] == vid]
        offset, limit = kwargs.get("offset", 0), kwargs.get("limit", 10)
        return rows[offset:offset + limit]

    async def count_report_feedback(self, aid, vid):
        return sum(row["version_id"] == vid for row in self.feedback)

    async def add_report_feedback(self, aid, vid, **body):
        await self.get_report(aid, vid)
        self.calls.append(body)
        result = {"feedback_id": "note1", "artifact_id": aid, "version_id": vid, **body}
        self.feedback.append(result)
        return result

    async def record_report_check(self, aid, vid, **body):
        await self.get_report(aid, vid)
        self.calls.append(body)
        return {"artifact_id": aid, "version_id": vid, **body}

    async def get_report_asset(self, aid, vid, asset):
        await self.get_report(aid, vid)
        if asset != "data1":
            raise ArtifactError("no artifact report asset")
        value = copy.deepcopy(self.descriptor["assets"][0])
        sha = value["sha256"]
        return {**value, "storage_key": f"sha256/{sha[:2]}/{sha[2:4]}/{sha}"}


class Bytes:
    def __init__(self):
        self.downloads = 0
        self.data = b"evidence"
        self.uploads = 0

    def put_stream(self, stream, *, expected_size, media_type, chunk_size):
        self.uploads += 1
        data = stream.read()
        assert len(data) == expected_size
        sha = hashlib.sha256(data).hexdigest()
        return {"sha256": sha, "size": len(data), "key": f"sha256/{sha[:2]}/{sha[2:4]}/{sha}"}

    def iter_get(self, key, sha, *, chunk_size=1024 * 1024):
        self.downloads += 1
        yield self.data


@pytest.fixture
def reports_server():
    store, blobs = Store(), Bytes()
    store.acl, store.jobs = ArtifactAcl(), ArtifactJobManager()
    store.reviews = ArtifactReviewManager()
    store.publications = PublicationController(provider=FakeExposureProvider())
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"owner-key": "operator", "other-key": "operator",
                                                              "viewer-key": "viewer"},
                                artifact_store=store, rustfs=blobs, artifact_acl=store.acl,
                                artifact_job_manager=store.jobs, artifact_review_manager=store.reviews,
                                publication_controller=store.publications)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    yield f"http://127.0.0.1:{server.port}", store, blobs
    server.shutdown()


def request(base, path, *, key="owner-key", body=None, headers=None):
    headers = {"X-API-Key": key, **(headers or {})}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers


PREFIX = "/api/v1/artifacts/artifact_report/versions/v1"


def test_report_preview_reads_metadata_not_supporting_blob(reports_server):
    base, store, blobs = reports_server
    store.descriptor["assets"][0]["size_bytes"] = 200 * 1024 * 1024
    status, data, _ = request(base, PREFIX + "/report")
    assert status == 200
    assert json.loads(data)["version_id"] == "v1"
    status, preview, headers = request(base, PREFIX + "/preview")
    assert status == 200 and len(preview) <= 256 * 1024
    assert b"Generic investigation" in preview
    assert blobs.downloads == 0
    assert "script-src 'none'" in headers["Content-Security-Policy"]


def test_private_scope_and_foreign_versions_are_nondisclosing(reports_server):
    base, _, _ = reports_server
    for suffix in ["/report", "/preview", "/feedback", "/assets/data1", "/diff?base_version_id=v2"]:
        assert request(base, PREFIX + suffix, key="other-key")[0] == 404
    assert request(base, PREFIX.replace("/versions/v1", "/versions/foreign") + "/report")[0] == 404
    assert request(base, PREFIX + "/report", key="")[0] == 401


def test_feedback_author_is_authenticated_and_version_is_pinned(reports_server):
    base, store, _ = reports_server
    body = {"target_kind": "finding", "target_id": "f1", "body": "Explain the denominator."}
    status, payload, _ = request(base, PREFIX + "/feedback", body=body)
    assert status == 201
    row = json.loads(payload)
    assert row["version_id"] == "v1" and row["actor"].startswith("api-key:")
    assert request(base, PREFIX + "/feedback", body={**body, "actor": "someone-else"})[0] == 400
    assert not json.loads(request(base, PREFIX.replace("/versions/v1", "/versions/v2") + "/feedback")[1])["feedback"]
    assert request(base, PREFIX + "/feedback", key="viewer-key", body=body)[0] == 403
    assert len(store.feedback) == 1


def test_checks_cannot_spoof_the_reviewer(reports_server):
    base, store, _ = reports_server
    body = {"stage": "evidence_checked", "status": "passed", "notes": "Inspected source account.",
            "evidence_refs": ["evidence:e1"]}
    assert request(base, PREFIX + "/checks", body=body)[0] == 201
    assert store.calls[-1]["actor"].startswith("api-key:")
    assert request(base, PREFIX + "/checks", body={**body, "reviewer": "forged"})[0] == 400


def test_asset_download_is_explicit_pinned_and_verified_before_delivery(reports_server):
    base, _, blobs = reports_server
    status, data, headers = request(base, PREFIX + "/assets/data1")
    assert status == 200 and data == b"evidence"
    assert blobs.downloads == 1
    assert headers["Content-Disposition"].startswith("attachment;")
    blobs.data = b"tampered"
    status, data, _ = request(base, PREFIX + "/assets/data1")
    assert status == 502 and b"tampered" not in data


def test_legacy_version_routes_cannot_bypass_report_scope_or_forge_worker_validation(reports_server):
    base, _, _ = reports_server
    assert request(base, "/api/v1/artifacts/artifact_report/versions", key="other-key")[0] == 404
    assert request(base, PREFIX, key="other-key")[0] == 404
    body = {"report_descriptor": document(), "base_version_id": "v1", "source_manifest_blob": None,
            "output_manifest": [], "validation": {"status": "passed", "checks": ["forged"]},
            "provenance": {"toolchain": {"renderer": "forged"}}}
    assert request(base, "/api/v1/artifacts/artifact_report/versions", body=body)[0] == 400


@pytest.mark.parametrize("field, value", [("parent_version_ids", ["v1", "v0"]), ("created_via", "collaborator"),
                                          ("collaborator_agent_id", "agent-x")])
def test_version_route_refuses_collaboration_owned_fields(reports_server, field, value):
    base, store, _ = reports_server
    calls = []

    async def commit_version(artifact_id, **kw):
        calls.append(kw)
        return {"version_id": "never", "artifact_id": artifact_id}
    store.commit_version = commit_version
    body = {"base_version_id": "v1", "source_manifest_blob": None, "output_manifest": [], field: value}
    status, raw, _ = request(base, "/api/v1/artifacts/artifact_report/versions", body=body)
    assert status == 400 and b"set only by collaboration" in raw and field.encode() in raw
    assert calls == []


def test_legacy_job_and_acl_routes_cannot_bypass_report_ownership(reports_server):
    base, store, _ = reports_server
    job = {"profile": "report", "artifact_id": "artifact_report", "base_version_id": "v1",
           "request": {"report_descriptor": document()}}
    assert request(base, "/api/v1/artifact-jobs", key="other-key", body=job)[0] == 404
    acl = {"principal_id": "attacker", "permissions": ["admin"]}
    assert request(base, "/api/v1/artifacts/artifact_report/acl", key="other-key", body=acl)[0] == 404
    assert request(base, "/api/v1/artifacts/artifact_report/acl/revoke", key="other-key", body=acl)[0] == 404
    assert store.jobs._jobs == {} and store.acl._grants == {}


def test_preview_never_loads_feedback_or_check_history(reports_server):
    base, store, _ = reports_server
    async def forbidden(*args):
        raise AssertionError("preview must load only its descriptor")
    store.get_report = forbidden
    assert request(base, PREFIX + "/preview")[0] == 200


def test_feedback_pages_are_bounded_and_report_remaining_rows(reports_server):
    base, store, _ = reports_server
    store.feedback = [{"version_id": "v1", "body": "😀" * 10000, "feedback_id": str(i)} for i in range(21)]
    status, data, _ = request(base, PREFIX + "/feedback")
    page = json.loads(data)
    assert status == 200 and len(page["feedback"]) == 10 and page["has_more"] and page["total"] == 21
    status, maximum_page, _ = request(base, PREFIX + "/feedback?limit=20")
    assert status == 200 and len(json.loads(maximum_page)["feedback"]) == 20
    assert len(maximum_page) < 4 * 1024 * 1024
    page = json.loads(request(base, PREFIX + "/feedback?limit=20&offset=20")[1])
    assert len(page["feedback"]) == 1 and not page["has_more"]
    for query in ("limit=21", "limit=0", "offset=-1", "limit=invalid"):
        assert request(base, PREFIX + "/feedback?" + query)[0] == 400


def test_upload_rejects_parameterized_mime_before_writing(reports_server):
    base, store, blobs = reports_server
    url = base + "/api/v1/artifacts/artifact_report/blobs?filename=data.csv"
    def upload(mime):
        req = urllib.request.Request(url, data=b"evidence", headers={"X-API-Key": "owner-key", "Content-Type": mime})
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())
    assert upload("text/csv; charset=utf-8")[0] == 400
    assert blobs.uploads == 0 and store.calls == []
    status, metadata = upload("text/csv")
    assert status == 201 and blobs.uploads == 1 and metadata["media_type"] == document()["assets"][0]["media_type"]
    assert metadata["sha256"] == document()["assets"][0]["sha256"]


def test_report_cancel_uses_atomic_database_transition_not_stale_manager(reports_server):
    base, store, _ = reports_server
    job = store.jobs.submit(profile="report", artifact_id="artifact_report", base_version_id="v1", request={}, requested_by="owner")
    store.persisted_job = {**job, "status": "running"}
    path = f"/api/v1/artifact-jobs/{job['job_id']}/cancel"
    assert request(base, path, body={})[0] == 409
    assert store.persisted_job["status"] == "running" and store.jobs.get(job["job_id"])["status"] == "queued"
    store.persisted_job["status"] = "queued"
    status, data, _ = request(base, path, body={})
    assert status == 200 and json.loads(data)["status"] == "cancelled"
    assert store.jobs.get(job["job_id"])["status"] == "queued"
    assert store.calls == ["cancel_report_job", "cancel_report_job"]


def test_legacy_review_and_publication_routes_require_scope_and_version_membership(reports_server):
    base, store, _ = reports_server
    review = {"artifact_id": "artifact_report", "version_id": "v1"}
    publication = {"resource_id": "artifact_report", "version_id": "v1", "audience": "unlisted_link", "expires_at": 9999999999}
    for path, body in (("/api/v1/artifact-reviews", review), ("/api/v1/publications/requests", publication)):
        assert request(base, path, body=body, key="other-key")[0] == 404
        assert request(base, path, body={**body, "version_id": "foreign"})[0] == 404
    status, data, _ = request(base, "/api/v1/artifact-reviews", body=review)
    assert status == 201
    rid = json.loads(data)["review_id"]
    status, data, _ = request(base, "/api/v1/publications/requests", body=publication)
    assert status == 201
    pid = json.loads(data)["publication_id"]
    for path in (f"/api/v1/artifact-reviews/{rid}", "/api/v1/artifacts/artifact_report/reviews", f"/api/v1/publications/{pid}"):
        assert request(base, path, key="other-key")[0] == 404
    assert json.loads(request(base, "/api/v1/publications", key="other-key")[1])["publications"] == []
    assert request(base, f"/api/v1/artifact-reviews/{rid}/complete", body={"decision": "approved"}, key="other-key")[0] == 404
    for action in ("approve", "revoke"):
        assert request(base, f"/api/v1/publications/{pid}/{action}", body={}, key="other-key")[0] == 404
    assert store.publications.provider.calls == []
    assert request(base, f"/api/v1/artifact-reviews/{rid}/complete", body={"decision": "approved"})[0] == 200
    assert request(base, f"/api/v1/publications/{pid}/approve", body={})[0] == 200


def test_legacy_artifact_mutations_reject_cross_origin(reports_server):
    base, _, _ = reports_server
    for path in ("/api/v1/artifact-jobs", "/api/v1/artifact-reviews", "/api/v1/publications/requests",
                 "/api/v1/artifacts/artifact_report/acl", "/api/v1/artifacts/artifact_report/versions"):
        for origin in ("https://attacker.invalid", "file://" + base.split("//")[1]):
            assert request(base, path, body={}, headers={"Origin": origin})[0] == 403


def test_legacy_output_download_is_pinned_authorized_and_verified(reports_server):
    base, store, blobs = reports_server
    async def version(vid, aid):
        if vid != "v1" or aid != "artifact_report":
            return None
        asset = store.descriptor["assets"][0]
        return {"output_manifest": [{**asset, "blob_sha256": asset["sha256"]}]}
    store.get_version = version
    path = PREFIX + "/outputs/0"
    assert request(base, path, key="other-key")[0] == 404
    assert request(base, path.replace("/v1/", "/foreign/"))[0] == 404
    for suffix in ("-1", "1", "bad"):
        assert request(base, PREFIX + "/outputs/" + suffix)[0] == 404
    assert blobs.downloads == 0
    status, data, headers = request(base, path)
    assert status == 200 and data == b"evidence"
    assert headers["Content-Disposition"].startswith("attachment;")
    blobs.data = b"tampered"
    status, data, _ = request(base, path)
    assert status == 502 and b"tampered" not in data
    store.descriptor["assets"][0].pop("size_bytes")
    assert request(base, path)[0] == 409


@pytest.mark.parametrize("references", [
    {"blob_sha256": "a" * 64, "sha256": hashlib.sha256(b"evidence").hexdigest()},
    {"storage_key": "sha256/aa/aa/" + "a" * 64, "sha256": hashlib.sha256(b"evidence").hexdigest()},
    {"storage_key": "sha256/aa/aa/" + "a" * 64, "blob_sha256": hashlib.sha256(b"evidence").hexdigest()},
    {"sha256": hashlib.sha256(b"evidence").hexdigest()},
])
def test_output_download_rejects_digest_substitution_before_blob_read(reports_server, references):
    base, store, blobs = reports_server
    async def version(vid, aid):
        return {"output_manifest": [{"name": "stolen.csv", "size_bytes": 8,
                                     "media_type": "text/csv", **references}]}
    store.get_version = version
    status, body, _ = request(base, PREFIX + "/outputs/0")
    assert status == 409
    assert blobs.downloads == 0
    assert b"evidence" not in body
