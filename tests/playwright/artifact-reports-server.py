"""Real loopback control-plane API/SPA fixture for report reader interactions.

The registry is deliberately in-memory here; durable PostgreSQL semantics have
separate integration tests. Browser requests use the production HTTP handlers.
"""
import asyncio
import copy
import hashlib
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.artifact_renderers import RendererRegistry  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402


class ReportFixture:
    def __init__(self):
        self.data = b"measurement,value\n" + b"sample,1\n" * 250_000
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.key = f"sha256/{self.digest[:2]}/{self.digest[2:4]}/{self.digest}"
        self._rf = self
        self.feedback = {"version_1": [], "version_2": []}
        if os.environ.get("REPORT_MANY_COMMENTS"):
            self.feedback["version_2"] = [{"feedback_id": f"feedback_{index}",
                "artifact_id": "report_1", "version_id": "version_2", "target_kind": "figure",
                "target_id": "figure_latency", "body": f"Figure observation {index}",
                "actor_id": "reader", "created_at": "2026-10-26T20:00:00Z"} for index in range(1, 26)]
        self.checks = {version: {
            "rendered": {"status": "passed", "actor_id": "worker", "notes": "Rendered preview."},
            "evidence_checked": {"status": "not_checked"},
            "reviewed": {"status": "not_checked"},
        } for version in self.feedback}
        self.descriptors = {}
        for number in (1, 2):
            self.descriptors[f"version_{number}"] = {
                "schema_version": 1,
                "title": "Latency investigation <img src=x onerror=window.reportXss=true>",
                "summary": f"Explanation revision {number}",
                "questions": [{"id": "question_latency", "text": "Does the candidate reduce latency?"}],
                "evidence": [{"id": "timing", "title": "Timing observations", "description": "Measured milliseconds.", "asset_ids": ["observations"]}],
                "findings": [{"id": "finding_latency", "title": "A descriptive difference", "statement": f"Finding from version {number}.",
                    "question_ids": ["question_latency"], "evidence_ids": ["timing"], "figure_ids": ["figure_latency"],
                    "limitations": ["No independent validation yet."], "next_steps": ["Collect a fresh evaluation."]}],
                "figures": [{"id": "figure_latency", "title": "Timing chart", "asset_id": "observations",
                    "caption": "Source observations for the chart.", "reading_guide": "Compare like-for-like groups."}],
                "assets": [{"id": "observations", "name": "evidence/timing.csv", "role": "data", "media_type": "text/csv",
                    "sha256": self.digest, "size_bytes": len(self.data)}],
                "analysis": {"method": "descriptive comparison", "configuration": {"units": "ms"}},
                "limitations": ["Exploratory evidence."], "next_steps": ["Verify the denominator."],
            }
        self.outputs = {version: [] for version in self.descriptors}
        self.blobs = {self.key: self.data}
        legacy_versions = ("version_1", "version_2") if os.environ.get("REPORT_ALL_LEGACY") else (
            ("version_1",) if os.environ.get("REPORT_MIXED_HISTORY") else ())
        for version in legacy_versions:
            rendered = RendererRegistry().render("report", f"# Legacy {version}\nPreserved Markdown result.", {})
            self.descriptors[version] = rendered.report_descriptor
            for output in rendered.outputs:
                data = output["content"].encode()
                digest = hashlib.sha256(data).hexdigest()
                key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
                self.blobs[key] = data
                self.outputs[version].append({"name": output["name"], "media_type": output["media_type"],
                    "storage_key": key, "sha256": digest, "size_bytes": len(data)})

    def sync_call(self, method, *args, **kwargs):
        return asyncio.run(getattr(self, method)(*args, **kwargs))

    async def get_record(self, artifact_id, **_kwargs):
        if artifact_id != "report_1":
            return None
        return {"artifact_id": artifact_id, "artifact_type": "other" if os.environ.get("ARTIFACT_REPORT_LEGACY_TYPE") else "report", "original_filename": "Investigation report",
                "mime_type": "text/html", "current_version_id": "version_2", "status": "available"}

    async def list_records(self, **_kwargs):
        return [await self.get_record("report_1")]

    async def get_version(self, version_id, artifact_id=None):
        if version_id not in self.descriptors or artifact_id not in (None, "report_1"):
            return None
        return {"version_id": version_id, "artifact_id": "report_1", "version_number": int(version_id[-1]),
                "state": "ready", "report_descriptor": copy.deepcopy(self.descriptors[version_id]),
                "output_manifest": copy.deepcopy(self.outputs[version_id]), "validation": {"status": "passed"}}

    async def list_versions(self, artifact_id):
        return [await self.get_version(version, artifact_id) for version in ("version_2", "version_1")]

    async def list_version_summaries(self, artifact_id):
        return [{**{key: value for key, value in row.items() if key in
                 {"version_id", "artifact_id", "version_number", "state"}}, "is_report": bool(row["report_descriptor"])}
                for row in await self.list_versions(artifact_id)]

    async def get_report_descriptor(self, artifact_id, version_id):
        version = await self.get_version(version_id, artifact_id)
        return version["report_descriptor"] if version else None

    async def report_permission(self, artifact_id, principal_id, permission):
        return artifact_id == "report_1"

    async def get_report(self, artifact_id, version_id):
        if not await self.get_version(version_id, artifact_id):
            return None
        if os.environ.get("REPORT_FAIL") == version_id:
            raise RuntimeError("Fixture report read failed")
        if version_id == "version_1" and os.environ.get("REPORT_DELAY"):
            await asyncio.sleep(float(os.environ["REPORT_DELAY"]))
        checks = copy.deepcopy(self.checks[version_id])
        return {"artifact_id": artifact_id, "version_id": version_id,
                "report_descriptor": copy.deepcopy(self.descriptors[version_id]),
                "checks": checks, "state": "reviewed" if checks["reviewed"]["status"] == "passed" else "rendered",
                "feedback": copy.deepcopy(self.feedback[version_id][:10]),
                "feedback_count": len(self.feedback[version_id]),
                "feedback_has_more": len(self.feedback[version_id]) > 10,
                "assets": copy.deepcopy(self.descriptors[version_id]["assets"]),
                "preview_url": f"/api/v1/artifacts/{artifact_id}/versions/{version_id}/preview"}

    async def get_report_diff(self, artifact_id, version_id, base_version_id):
        if os.environ.get("DIFF_FAIL"):
            raise RuntimeError("Fixture difference read failed")
        return {"artifact_id": artifact_id, "version_id": version_id, "base_version_id": base_version_id,
                "changed": version_id != base_version_id, "categories": {
                    key: {"changed": key == "editorial", "added": [], "removed": [],
                          "modified": ["summary", "findings.finding_latency.statement"] if key == "editorial" else []}
                    for key in ("data", "analysis", "editorial", "presentation")}}

    async def list_report_feedback(self, artifact_id, version_id, *, limit=10, offset=0):
        return copy.deepcopy(self.feedback[version_id][offset:offset + limit])

    async def count_report_feedback(self, artifact_id, version_id):
        return len(self.feedback[version_id])

    async def add_report_feedback(self, artifact_id, version_id, *, target_kind, target_id, body, actor):
        row = {"feedback_id": f"feedback_{len(self.feedback[version_id]) + 1}", "artifact_id": artifact_id,
               "version_id": version_id, "target_kind": target_kind, "target_id": target_id,
               "body": body, "actor_id": actor, "created_at": "2026-10-26T20:00:00Z"}
        self.feedback[version_id].append(row)
        return copy.deepcopy(row)

    async def record_report_check(self, artifact_id, version_id, *, stage, status, notes, evidence_refs, actor):
        if stage == "reviewed" and self.checks[version_id]["evidence_checked"]["status"] != "passed":
            raise ValueError("Evidence must be checked before review passes")
        row = {"check_id": "check_1", "stage": stage, "status": status, "notes": notes,
               "evidence_refs": evidence_refs, "actor_id": actor, "created_at": "2026-10-26T20:00:00Z"}
        self.checks[version_id][stage] = row
        return copy.deepcopy(row)

    async def get_report_asset(self, artifact_id, version_id, asset_id):
        if not await self.get_version(version_id, artifact_id) or asset_id != "observations":
            return None
        return {**self.descriptors[version_id]["assets"][0], "storage_key": self.key}

    def head(self, key):
        return key in self.blobs

    def iter_get(self, key, digest, chunk_size=1024 * 1024):
        data = self.blobs[key]
        assert hashlib.sha256(data).hexdigest() == digest
        for offset in range(0, len(data), chunk_size):
            yield data[offset:offset + chunk_size]


if __name__ == "__main__":
    store = ReportFixture()
    server = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0,
                                web_root=ROOT / "packages", artifact_store=store, rustfs=store)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    print(f"PORT={server.port}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
