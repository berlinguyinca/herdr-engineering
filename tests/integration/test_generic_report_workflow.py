"""Authenticated report workflow through the real registry, worker and HTTP API.

PostgreSQL is an isolated temporary server. Only the S3 transport is in-memory;
RustFSClient performs the actual content hashing, spooling and verified reads.
"""
import copy
import hashlib
import io
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import asyncpg
import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter
from herdr_engineering.control_plane.artifact_jobs import ArtifactJobManager
from herdr_engineering.control_plane.artifact_render_runner import ArtifactRenderRunner
from herdr_engineering.control_plane.artifact_renderers import RendererRegistry
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.rustfs import RustFSClient
from herdr_engineering.control_plane.worker import run_one_pass
from tests.control_plane.test_artifact_report_postgres import report_postgres  # noqa: F401

OWNER = "test-report-owner"
REVIEWER = "test-report-reviewer"
OUTSIDER = "test-report-outsider"
WORKER = "host-generic-report-integration"
ROOT = Path(__file__).resolve().parents[2]


def principal(key):
    return "api-key:" + hashlib.sha256(key.encode()).hexdigest()[:16] + ":operator"


class TrackedBody(io.BytesIO):
    def __init__(self, data, transport):
        super().__init__(data)
        self.transport = transport

    def read(self, size=-1):
        self.transport.download_read_sizes.append(size)
        return super().read(size)


class MemoryS3Transport:
    def __init__(self):
        self.objects = {}
        self.get_keys = []
        self.upload_read_sizes = []
        self.download_read_sizes = []

    def put_object(self, *, Bucket, Key, Body):
        self.objects[Key] = bytes(Body)

    def upload_fileobj(self, stream, bucket, key, **kwargs):
        chunks = []
        while True:
            size = 1024 * 1024
            self.upload_read_sizes.append(size)
            chunk = stream.read(size)
            if not chunk:
                break
            chunks.append(chunk)
        self.objects[key] = b"".join(chunks)

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, *, Bucket, Key):
        self.get_keys.append(Key)
        return {"Body": TrackedBody(self.objects[Key], self)}


class WorkflowStore(ArtifactStore):
    """Test lifecycle hooks; all artifact behavior is inherited unchanged."""

    async def run_test_pass(self):
        return await run_one_pass(
            self._conn, worker_id=WORKER,
            artifact_job_runner=ArtifactRenderRunner(self, RendererRegistry()),
            max_artifact_jobs=1,
        )

    async def seed_test_host(self):
        await self._conn.execute(
            "INSERT INTO hosts (host_id,hostname,status) VALUES ($1,'isolated-test-worker','healthy') "
            "ON CONFLICT (host_id) DO NOTHING", WORKER,
        )

    async def grant_test_reviewer(self, artifact_id):
        await self.record_acl_event("grant", {"artifact_id": artifact_id,
            "principal_id": principal(REVIEWER), "permissions": ["read", "review"],
            "granted_by": principal(OWNER)})

    async def close_connection(self):
        await self._conn.close()


class Workflow:
    def __init__(self, dsn):
        self.dsn = dsn
        self.transport = MemoryS3Transport()
        self.rustfs = RustFSClient(endpoint="unused-test-endpoint", bucket="test-reports", s3=self.transport)
        self.adapter = None
        self.server = None
        self.thread = None
        self.start()

    def start(self):
        async def connect():
            return WorkflowStore(await asyncpg.connect(self.dsn), self.rustfs)

        self.adapter = SyncArtifactAdapter(connect)
        self.adapter("seed_test_host")
        manager = ArtifactJobManager(on_change=lambda action, job:
                                     self.adapter("record_job_event", action, job))
        self.server = ControlPlaneServer(
            ControlPlaneRepo(), artifact_store=self.adapter, rustfs=self.rustfs,
            artifact_job_manager=manager,
            api_keys={OWNER: "operator", REVIEWER: "operator", OUTSIDER: "operator"},
        )
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()
        self.server.wait_until_ready()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.thread.join(timeout=5)
            self.server = None
        if self.adapter:
            self.adapter("close_connection")
            self.adapter.close()
            self.adapter = None

    def request(self, path, *, body=None, raw=None, mime="application/json", key=OWNER, status=200):
        payload = json.dumps(body).encode() if body is not None else raw
        headers = {"X-API-Key": key, "Content-Type": mime}
        request = urllib.request.Request(self.base + path, data=payload, headers=headers,
                                         method="POST" if payload is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                actual, data, received_headers = response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            actual, data, received_headers = error.code, error.read(), dict(error.headers)
        assert actual == status, (path, actual, data[:1200])
        if received_headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(data)
        return data, received_headers

    def upload(self, artifact_id, name, data, mime):
        return self.request(f"/api/v1/artifacts/{artifact_id}/blobs?filename={quote(name)}",
                            raw=data, mime=mime, status=201)

    def build(self, artifact_id, descriptor, assets, *, base_version_id=None):
        submitted = self.request(f"/api/v1/artifact-reports/{artifact_id}/jobs", status=201,
            body={"base_version_id": base_version_id,
                  "request": {"report_descriptor": descriptor, "assets": assets}})
        assert submitted["status"] == "queued"
        summary = self.adapter("run_test_pass")
        assert summary["artifact_jobs_claimed"] == 1
        assert summary["artifact_jobs_completed"] == 1, summary
        status = self.request(f"/api/v1/artifact-reports/{artifact_id}/jobs/{submitted['job_id']}")
        assert status["status"] == "completed", status
        assert "request" not in status
        version = self.request(f"/api/v1/artifacts/{artifact_id}/versions/{status['result_version_id']}")
        assert version["host_id"] == WORKER
        assert version["job_id"] == submitted["job_id"]
        assert version["created_by"] == principal(OWNER)
        return version


@pytest.fixture
def workflow(report_postgres):  # noqa: F811 - imported fixture
    current = Workflow(report_postgres["dsn"])
    try:
        yield current
    finally:
        current.stop()


def uploaded_descriptor(workflow, artifact_id):
    descriptor = json.loads((ROOT / "tests/control_plane/fixtures/reports/benchmark.json").read_text())
    # A multi-megabyte evidence file proves it is separate from the preview;
    # storage transport reads remain bounded regardless of complete object size.
    evidence = b"record,elapsed_ms\n" + b"sample,124\n" * 350_000
    figure = b'<svg xmlns="http://www.w3.org/2000/svg"><text x="1" y="20">124 / 118 ms</text></svg>'
    references = [workflow.upload(artifact_id, "evidence/trials.csv", evidence, "text/csv"),
                  workflow.upload(artifact_id, "figures/means.svg", figure, "image/svg+xml")]
    for asset, uploaded in zip(descriptor["assets"], references, strict=True):
        asset.update({key: uploaded[key] for key in ("sha256", "size_bytes", "media_type")})
    descriptor["limitations"] = ["Different devices and unequal sample counts; no causal improvement claim."]
    return descriptor, references, evidence, figure


def test_authenticated_report_build_diff_review_feedback_and_bounded_preview(workflow):
    artifact = workflow.request("/api/v1/artifact-reports", body={"title": "Generic benchmark investigation"}, status=201)
    aid = artifact["artifact_id"]
    descriptor, assets, evidence, figure = uploaded_descriptor(workflow, aid)
    version = workflow.build(aid, descriptor, assets)
    vid = version["version_id"]
    endpoint = f"/api/v1/artifacts/{aid}/versions/{vid}"
    assert version["report_descriptor"] == descriptor
    assert version["validation"]["status"] == "passed"
    assert {entry["name"] for entry in version["output_manifest"]} == {
        "preview.html", "evidence/trials.csv", "figures/means.svg"}

    reads = list(workflow.transport.get_keys)
    report = workflow.request(endpoint + "/report")
    assert report["state"] == "rendered"
    assert report["checks"]["rendered"]["status"] == "passed"
    assert report["checks"]["evidence_checked"]["status"] == "not_checked"
    assert report["checks"]["reviewed"]["status"] == "not_checked"
    preview, headers = workflow.request(endpoint + "/preview")
    assert len(preview) < 256 * 1024
    assert b"Observed difference needs a controlled evaluation" in preview
    assert b"data-1" in preview and b"plot-1" in preview
    assert "script-src 'none'" in headers["Content-Security-Policy"]
    assert workflow.transport.get_keys == reads, "Metadata/preview fetched supporting asset bytes"
    downloaded, headers = workflow.request(endpoint + "/assets/data-1")
    assert downloaded == evidence
    assert "attachment" in headers["Content-Disposition"]
    assert headers["ETag"] == '"' + hashlib.sha256(evidence).hexdigest() + '"'
    assert workflow.request(endpoint + "/assets/plot-1")[0] == figure
    assert workflow.transport.upload_read_sizes and max(workflow.transport.upload_read_sizes) <= 1024 * 1024
    assert workflow.transport.download_read_sizes and max(workflow.transport.download_read_sizes) <= 1024 * 1024
    assert min(workflow.transport.download_read_sizes) >= 0

    workflow.request(endpoint + "/report", key=OUTSIDER, status=404)
    workflow.request(endpoint + "/preview", key=OUTSIDER, status=404)
    workflow.request(endpoint + "/assets/data-1", key=OUTSIDER, status=404)
    workflow.request(endpoint + "/feedback", key=OUTSIDER, status=404,
                     body={"target_kind": "finding", "target_id": "finding-1", "body": "Uninvited"})
    workflow.request(endpoint + "/checks", status=400, body={"stage": "reviewed", "status": "passed",
        "notes": "Premature review", "evidence_refs": ["finding:finding-1"]})

    workflow.adapter("grant_test_reviewer", aid)
    finding_feedback = workflow.request(endpoint + "/feedback", key=REVIEWER, status=201,
        body={"target_kind": "finding", "target_id": "finding-1", "body": "Clarify device confounding."})
    workflow.request(endpoint + "/feedback", key=REVIEWER, status=201,
        body={"target_kind": "figure", "target_id": "figure-1", "body": "Label missing uncertainty."})
    assert finding_feedback["actor"] == principal(REVIEWER)
    assert finding_feedback["version_id"] == vid
    workflow.request(endpoint + "/feedback", key=REVIEWER, status=400,
        body={"target_kind": "figure", "target_id": "missing", "body": "Unknown anchor"})
    workflow.request(endpoint + "/feedback", key=REVIEWER, status=400,
        body={"target_kind": "figure", "target_id": "figure-1", "body": "Spoof author", "actor": "someone-else"})
    for stage in ("evidence_checked", "reviewed"):
        recorded = workflow.request(endpoint + "/checks", key=REVIEWER, status=201,
            body={"stage": stage, "status": "passed", "notes": "Checked cited observations and documented limits.",
                  "evidence_refs": ["evidence:evidence-1", "finding:finding-1"]})
        assert recorded["actor"] == principal(REVIEWER)
    assert workflow.request(endpoint + "/report")["state"] == "reviewed"

    editorial = copy.deepcopy(descriptor)
    editorial["summary"] = "Clarified narrative; measurements and analysis are unchanged."
    v2 = workflow.build(aid, editorial, assets, base_version_id=vid)
    endpoint2 = f"/api/v1/artifacts/{aid}/versions/{v2['version_id']}"
    diff = workflow.request(endpoint2 + f"/diff?base_version_id={vid}")
    assert diff["categories"]["editorial"]["changed"]
    assert not diff["categories"]["data"]["changed"]
    assert not diff["categories"]["analysis"]["changed"]
    assert workflow.request(endpoint2 + "/report")["state"] == "rendered"
    assert workflow.request(endpoint2 + "/feedback")["feedback"] == []

    altered = copy.deepcopy(editorial)
    changed_asset = workflow.upload(aid, "evidence/trials.csv", evidence + b"new,117\n", "text/csv")
    altered["assets"][0].update({key: changed_asset[key] for key in ("sha256", "size_bytes", "media_type")})
    v3 = workflow.build(aid, altered, [changed_asset, assets[1]], base_version_id=v2["version_id"])
    endpoint3 = f"/api/v1/artifacts/{aid}/versions/{v3['version_id']}"
    diff = workflow.request(endpoint3 + f"/diff?base_version_id={v2['version_id']}")
    assert diff["categories"]["data"]["changed"]
    assert not diff["categories"]["analysis"]["changed"]
    analysis_changed = copy.deepcopy(altered)
    analysis_changed["analysis"]["configuration"]["warmup"] = 5
    v4 = workflow.build(aid, analysis_changed, [changed_asset, assets[1]], base_version_id=v3["version_id"])
    endpoint4 = f"/api/v1/artifacts/{aid}/versions/{v4['version_id']}"
    diff = workflow.request(endpoint4 + f"/diff?base_version_id={v3['version_id']}")
    assert diff["categories"]["analysis"]["changed"]
    assert not diff["categories"]["data"]["changed"]

    # A fresh web/store adapter must observe persisted checks, comments and jobs.
    workflow.stop()
    workflow.start()
    assert workflow.request(endpoint + "/report")["state"] == "reviewed"
    feedback = workflow.request(endpoint + "/feedback")
    assert feedback["total"] == 2
    assert {row["target_kind"] for row in feedback["feedback"]} == {"finding", "figure"}
    assert workflow.request(endpoint4 + "/feedback")["feedback"] == []
    assert workflow.request(endpoint4 + "/report")["state"] == "rendered"
    assert workflow.request(endpoint + "/assets/data-1")[0] == evidence
    assert workflow.request(endpoint3 + "/assets/data-1")[0] == evidence + b"new,117\n"

    other = workflow.request("/api/v1/artifact-reports", body={"title": "Unrelated report"}, status=201)
    workflow.request(f"/api/v1/artifacts/{other['artifact_id']}/versions/{vid}/report", status=404)
    workflow.request(f"/api/v1/artifacts/{other['artifact_id']}/versions/{vid}/feedback", status=404,
        body={"target_kind": "finding", "target_id": "finding-1", "body": "Wrong version family"})


def test_report_asset_download_rejects_corrupt_content_before_delivery(workflow):
    artifact = workflow.request("/api/v1/artifact-reports", body={"title": "Integrity investigation"}, status=201)
    aid = artifact["artifact_id"]
    descriptor, assets, _, _ = uploaded_descriptor(workflow, aid)
    version = workflow.build(aid, descriptor, assets)
    key = assets[0]["storage_key"]
    workflow.transport.objects[key] = b"corrupted supporting evidence"
    path = f"/api/v1/artifacts/{aid}/versions/{version['version_id']}"
    reads = len(workflow.transport.get_keys)
    assert workflow.request(path + "/report")["state"] == "rendered"
    assert len(workflow.transport.get_keys) == reads
    result = workflow.request(path + "/assets/data-1", status=502)
    assert "integrity" in result["error"]
