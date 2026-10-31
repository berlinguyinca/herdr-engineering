"""Durable-contract-shaped artifact build job lifecycle.

The manager owns validation and state transitions; actual rendering is injected
through the existing scheduler/worker boundary and never runs in an HTTP route.
"""
from __future__ import annotations

import secrets
import time
from collections.abc import Callable

PROFILES = frozenset({"web", "document", "diagram", "visualization", "presentation", "report"})
TERMINAL = frozenset({"completed", "failed", "cancelled"})


class ArtifactJobError(ValueError):
    pass


class ArtifactJobManager:
    def __init__(self, *, enqueue: Callable[[dict], None] | None = None,
                 now: Callable[[], float] = time.time, on_change=None):
        self.enqueue = enqueue
        self.now = now
        self.on_change = on_change
        self._jobs: dict[str, dict] = {}

    def submit(self, *, profile: str, artifact_id: str, base_version_id: str | None,
               request: dict, requested_by: str) -> dict:
        if profile not in PROFILES:
            raise ArtifactJobError(f"unsupported artifact profile: {profile}")
        if not artifact_id or not requested_by or not isinstance(request, dict):
            raise ArtifactJobError("artifact, requester, and request object are required")
        job_id = "job_" + secrets.token_hex(12)
        job = {"job_id": job_id, "artifact_id": artifact_id,
               "base_version_id": base_version_id, "profile": profile,
               "request": dict(request), "requested_by": requested_by,
               "status": "queued", "assigned_host_id": None,
               "result_version_id": None, "outputs": [], "validation": {},
               "error": None, "created_at": self.now(), "started_at": None,
               "completed_at": None}
        self._persist("created", job)
        if self.enqueue is not None:
            self.enqueue(dict(job))
        return dict(job)

    def claim(self, job_id: str, *, host_id: str) -> dict:
        job = dict(self._job(job_id))
        self._transition(job, {"queued"}, "running")
        job["assigned_host_id"] = host_id
        job["started_at"] = self.now()
        self._persist("claimed", job)
        return dict(job)

    def complete(self, job_id: str, *, result_version_id: str,
                 outputs: list[dict], validation: dict) -> dict:
        job = dict(self._job(job_id))
        self._transition(job, {"running"}, "completed")
        job["result_version_id"] = result_version_id
        job["outputs"] = list(outputs)
        job["validation"] = dict(validation)
        job["completed_at"] = self.now()
        self._persist("completed", job)
        return dict(job)

    def fail(self, job_id: str, *, error: str) -> dict:
        job = dict(self._job(job_id))
        self._transition(job, {"queued", "running"}, "failed")
        job["error"] = str(error)
        job["completed_at"] = self.now()
        self._persist("failed", job)
        return dict(job)

    def cancel(self, job_id: str) -> dict:
        job = dict(self._job(job_id))
        self._transition(job, {"queued", "running"}, "cancelled")
        job["completed_at"] = self.now()
        self._persist("cancelled", job)
        return dict(job)

    def execute(self, job_id: str, *, host_id: str, renderer, commit) -> dict:
        job = self.claim(job_id, host_id=host_id)
        try:
            rendered = renderer.render(job["profile"],
                                      job["request"].get("source", ""),
                                      job["request"])
            version_id = commit(
                artifact_id=job["artifact_id"], base_version_id=job["base_version_id"],
                source=rendered.source, outputs=rendered.outputs,
                validation=rendered.validation, profile=job["profile"],
                requested_by=job["requested_by"], host_id=host_id)
            return self.complete(job_id, result_version_id=version_id,
                                 outputs=rendered.outputs, validation=rendered.validation)
        except Exception as exc:  # noqa: BLE001 - worker failure is durable state
            self.fail(job_id, error=str(exc))
            raise

    def get(self, job_id: str) -> dict:
        return dict(self._job(job_id))

    def load(self, jobs: list[dict]) -> None:
        for job in jobs:
            self._jobs[job["job_id"]] = dict(job)

    def _persist(self, action: str, job: dict) -> None:
        # A rejected durable CAS must leave the previous local snapshot intact.
        if self.on_change:
            self.on_change(action, dict(job))
        self._jobs[job["job_id"]] = job

    def _job(self, job_id: str) -> dict:
        if job_id not in self._jobs:
            raise ArtifactJobError("artifact job not found")
        return self._jobs[job_id]

    @staticmethod
    def _transition(job: dict, allowed: set[str], target: str) -> None:
        if job["status"] not in allowed:
            raise ArtifactJobError(
                f"cannot transition {job['status']} job to {target}")
        job["status"] = target

