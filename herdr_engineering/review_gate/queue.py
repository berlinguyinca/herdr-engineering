"""Durable, lease-based review queue (event-sourced).

Storage: ONE index stream, entity type ``review_job`` / entity id ``index``,
holds every ``enqueued`` / ``claimed`` / ``completed`` / ``cancelled`` event
keyed by ``job_id``, so a single fold yields the whole queue across PRs.

Clock: ``now`` is a zero-argument callable returning epoch seconds (float);
default ``time.time``. Lease expiry is evaluated at fold time, so an expired
claim is simply offered again (keeping its FIFO position). Invariants: at most
one live (queued/claimed) job per PrKey; ``complete`` succeeds only for the
current, unexpired holder.

Notes:
- One ReviewQueue per repo is assumed: the lock is per instance, not per store.
- ``complete`` returns False for an expired but un-reclaimed lease (lease lost).
- A sha is superseded once a later job with a different sha exists for the key
  (any state); enqueueing it again returns None. An explicit ``cancel`` with no
  later job still allows re-enqueueing the same sha (PR closed and reopened).
- ``author_model`` is stored redacted and compared against the redacted
  ``reviewer_model``.
- Every operation refolds the whole stream, so cost grows with event count.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from herdr_engineering.control_plane.redact import redact_text
from herdr_engineering.control_plane.repo import ControlPlaneRepo

from .models import PrKey

ENTITY = "review_job"
INDEX = "index"


@dataclass(frozen=True)
class ReviewJob:
    job_id: str
    key: PrKey
    sha: str
    author_model: str | None
    enqueued_at: str


def _payload(event: dict) -> dict:
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload


class ReviewQueue:
    def __init__(
        self,
        repo: ControlPlaneRepo,
        *,
        lease_seconds: int = 900,
        now: Callable[[], float] | None = None,
    ):
        self._repo = repo
        self._lease = lease_seconds
        self._now = now or time.time
        self._lock = threading.RLock()

    def _fold(self) -> dict[str, dict]:
        """job_id -> record (insertion order == enqueue order)."""
        jobs: dict[str, dict] = {}
        for event in self._repo.stream(ENTITY, INDEX):
            p = _payload(event)
            kind = event["event_type"]
            if kind == "enqueued":
                jobs[p["job_id"]] = {**p, "state": "queued", "worker": None, "lease": None}
                continue
            job = jobs.get(p.get("job_id"))
            if job is None:
                continue
            if kind == "claimed":
                job.update(state="claimed", worker=p["worker_id"], lease=p["lease_expires_at"])
            elif kind == "completed":
                job["state"] = "done"
            elif kind == "cancelled":
                job["state"] = "cancelled"
        return jobs

    def _effective(self, job: dict, now: float) -> str:
        if job["state"] == "claimed" and job["lease"] <= now:
            return "queued"  # lease expired: offered again
        return job["state"]

    def _record(self, kind: str, **payload) -> None:
        self._repo.record_event(ENTITY, INDEX, kind, payload)

    @staticmethod
    def _job(j: dict) -> ReviewJob:
        return ReviewJob(
            j["job_id"], PrKey.parse(j["key"]), j["sha"], j["author_model"], j["enqueued_at"]
        )

    def _cancel_locked(self, jobs: dict[str, dict], key: PrKey, except_sha: str | None) -> int:
        now = self._now()
        n = 0
        for j in jobs.values():
            if (
                j["key"] == str(key)
                and self._effective(j, now) in ("queued", "claimed")
                and j["sha"] != except_sha
            ):
                self._record("cancelled", job_id=j["job_id"])
                j["state"] = "cancelled"
                n += 1
        return n

    def enqueue(self, key: PrKey, sha: str, author_model: str | None) -> ReviewJob | None:
        with self._lock:
            jobs = self._fold()
            now = self._now()
            same_key = [j for j in jobs.values() if j["key"] == str(key)]
            for j in same_key:
                if j["sha"] == sha and j["state"] != "cancelled":
                    return None  # queued, claimed or done
            last = max((i for i, j in enumerate(same_key) if j["sha"] == sha), default=-1)
            if last >= 0 and any(j["sha"] != sha for j in same_key[last + 1 :]):
                return None  # superseded by a later job for a different sha
            self._cancel_locked(jobs, key, except_sha=sha)
            payload = {
                "job_id": uuid.uuid4().hex,
                "key": str(key),
                "sha": sha,
                "author_model": redact_text(author_model) if author_model else None,
                "enqueued_at": datetime.fromtimestamp(now, UTC).isoformat(),
            }
            self._record("enqueued", **payload)
            return self._job(payload)

    def claim(self, worker_id: str, reviewer_model: str) -> ReviewJob | None:
        with self._lock:
            now = self._now()
            reviewer = redact_text(reviewer_model)
            for j in self._fold().values():
                if self._effective(j, now) != "queued":
                    continue
                if j["author_model"] and j["author_model"] == reviewer:
                    continue
                self._record(
                    "claimed",
                    job_id=j["job_id"],
                    worker_id=redact_text(worker_id),
                    lease_expires_at=now + self._lease,
                )
                return self._job(j)
            return None

    def complete(self, job_id: str, worker_id: str) -> bool:
        with self._lock:
            j = self._fold().get(job_id)
            if (
                j is None
                or j["state"] != "claimed"
                or j["lease"] <= self._now()
                or j["worker"] != redact_text(worker_id)
            ):
                return False
            self._record("completed", job_id=job_id, worker_id=j["worker"])
            return True

    def cancel(self, key: PrKey, *, except_sha: str | None = None) -> int:
        with self._lock:
            return self._cancel_locked(self._fold(), key, except_sha)

    def depth(self) -> int:
        with self._lock:
            now = self._now()
            return sum(1 for j in self._fold().values() if self._effective(j, now) == "queued")

    def snapshot(self) -> list[dict]:
        with self._lock:
            now = self._now()
            out = []
            for j in self._fold().values():
                state = self._effective(j, now)
                if state not in ("queued", "claimed"):
                    continue
                out.append(
                    {
                        "job_id": j["job_id"],
                        "key": j["key"],
                        "sha": j["sha"],
                        "author_model": j["author_model"],
                        "enqueued_at": j["enqueued_at"],
                        "state": state,
                        "claimed_by": j["worker"] if state == "claimed" else None,
                        "lease_expires_at": j["lease"] if state == "claimed" else None,
                    }
                )
            return out
