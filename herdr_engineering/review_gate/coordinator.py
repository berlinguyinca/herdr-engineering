"""Hand review jobs to reviewer workers and turn their output into verdicts.

This is the trust boundary between a reviewer model and an auto-merge, so
every path fails closed: anything unexpected is ``stale`` (nothing written) or
``rejected`` (counted as a failed attempt, never an approval).

Locking: one ``threading.RLock`` per coordinator guards every queue/store
transition and the in-memory per-job record. It is NOT held across the GitHub
``review_material`` call. That is safe because the job is leased to the
calling worker while the lock is dropped, and the lease is re-checked with
``queue.live_claim`` (plus the store head/state) after the lock is re-taken
and before anything is written. A lease lost during the fetch therefore never
leads to ``on_review_started`` or ``on_verdict``.

Write order in ``submit``: ``store.on_verdict`` first, then ``queue.complete``.
A crash between them leaves the job re-offered; the next claim finds the PR no
longer reviewable and completes it as stale, so there is never an approval
without a durable record.

Per-job memory: the material's ``truncated`` flag and the reviewer model are
remembered from ``claim``. After a coordinator restart ``submit`` re-fetches
the material to recompute ``truncated``; a failed or moved fetch returns
``rejected`` without recording anything or counting an attempt. The reviewer
model is then unknown and recorded as a fixed placeholder (it is informational
only: nothing decides on a verdict's ``reviewer_model``).

Nothing derived from model output or PR text is logged, returned or stored
except through ``parse_verdict``'s validated ``Verdict``.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Collection
from dataclasses import dataclass

from herdr_engineering.control_plane.redact import redact_text

from .config import ReviewerConfig
from .models import State
from .queue import ReviewJob
from .review_prompt import TRUNCATED_PASS, VerdictError, build_messages, parse_verdict

MAX_SKIPS = 10
REASON_MAX = 200
INVALID_OUTPUT = "invalid review output"
UNKNOWN_REVIEWER = "unknown"
_REVIEWABLE = (State.CI_GREEN, State.REVIEW_QUEUED, State.REVIEWING)


@dataclass(frozen=True)
class Claim:
    job_id: str
    repo: str
    number: int
    sha: str
    messages: list[dict]
    lease_seconds: int
    attempt: int


@dataclass(frozen=True)
class _Held:
    truncated: bool
    reviewer_model: str


class ReviewCoordinator:
    def __init__(self, *, store, queue, app, config: ReviewerConfig, enrolled: Collection[str]):
        self._store = store
        self._queue = queue
        self._app = app
        self._config = config
        self._enrolled = frozenset(enrolled)
        self._held: dict[str, _Held] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ helpers
    def _fetch(self, job: ReviewJob):
        return self._app.review_material(
            job.key.repo, job.key.number, job.sha, max_bytes=self._config.max_diff_bytes
        )

    def _is_current(self, job: ReviewJob, states=_REVIEWABLE) -> bool:
        snap = self._store.snapshot(job.key)
        return snap.head_sha == job.sha and snap.state in states

    def _done(self, job_id: str, worker_id: str) -> None:
        self._held.pop(job_id, None)
        self._queue.complete(job_id, worker_id)

    def _release_quietly(self, job_id: str, worker_id: str) -> None:
        with self._lock:
            self._held.pop(job_id, None)
            with contextlib.suppress(Exception):
                self._queue.release(job_id, worker_id)

    # ------------------------------------------------------------- public
    def claim(self, worker_id: str, reviewer_model: str) -> Claim | None:
        for _ in range(MAX_SKIPS):
            with self._lock:
                job = self._queue.claim(worker_id, reviewer_model)
                if job is None:
                    return None
                if job.key.repo not in self._enrolled or not self._is_current(job):
                    self._done(job.job_id, worker_id)  # stale
                    continue
            try:
                material = self._fetch(job)  # lock dropped: network call
                with self._lock:
                    if material is None:  # PR moved or closed on GitHub
                        self._done(job.job_id, worker_id)
                        continue
                    if self._queue.live_claim(job.job_id, worker_id) is None:
                        continue  # lease lost during the fetch; nothing to release
                    snap = self._store.snapshot(job.key)
                    messages = build_messages(
                        material, snap.last_findings, max_findings=self._config.max_findings
                    )
                    if not self._store.on_review_started(job.key, job.sha, reviewer_model):
                        self._release_quietly(job.job_id, worker_id)
                        continue
                    self._held[job.job_id] = _Held(bool(material.truncated), reviewer_model)
                    return Claim(
                        job_id=job.job_id,
                        repo=job.key.repo,
                        number=job.key.number,
                        sha=job.sha,
                        messages=messages,
                        lease_seconds=self._config.lease_seconds,
                        attempt=self._queue.attempts(job.job_id),
                    )
            except BaseException:
                self._release_quietly(job.job_id, worker_id)
                raise
        return None

    def heartbeat(self, job_id: str, worker_id: str) -> bool:
        with self._lock:
            return self._queue.renew(job_id, worker_id)

    def submit(self, job_id: str, worker_id: str, output: str) -> str:
        with self._lock:
            job = self._queue.live_claim(job_id, worker_id)
            if job is None or not self._is_current(job, (State.REVIEWING,)):
                return "stale"
            if not isinstance(output, str) or (
                len(output.encode("utf-8", "surrogatepass")) > self._config.max_output_bytes
            ):
                self.fail(job_id, worker_id, INVALID_OUTPUT)
                return "rejected"
            held = self._held.get(job_id)
        if held is None:
            # Restarted since the claim: recompute ``truncated`` (lock dropped).
            try:
                material = self._fetch(job)
            except Exception:
                return "rejected"
            if material is None:
                return "rejected"
            held = _Held(bool(material.truncated), UNKNOWN_REVIEWER)
        with self._lock:
            job = self._queue.live_claim(job_id, worker_id)
            if job is None or not self._is_current(job, (State.REVIEWING,)):
                return "stale"
            held = self._held.setdefault(job_id, held)
            try:
                verdict = parse_verdict(
                    output,
                    sha=job.sha,
                    reviewer_model=held.reviewer_model,
                    max_findings=self._config.max_findings,
                    truncated=held.truncated,
                )
            except VerdictError as exc:
                reason = TRUNCATED_PASS if str(exc) == TRUNCATED_PASS else INVALID_OUTPUT
                self.fail(job_id, worker_id, reason)
                return "rejected"
            recorded = self._store.on_verdict(job.key, verdict)
            self._done(job_id, worker_id)
            return "recorded" if recorded else "stale"

    def fail(self, job_id: str, worker_id: str, reason: str) -> str:
        with self._lock:
            job = self._queue.live_claim(job_id, worker_id)
            if job is None:
                return "stale"
            if self._queue.attempts(job_id) >= self._config.max_attempts:
                text = reason if isinstance(reason, str) and reason else "review failed"
                # Redact before clipping so a clip never splits a secret past redaction.
                escalated = self._store.on_review_failed(
                    job.key, job.sha, redact_text(text)[:REASON_MAX]
                )
                self._done(job_id, worker_id)
                return "escalated" if escalated else "stale"
            self._held.pop(job_id, None)
            return "released" if self._queue.release(job_id, worker_id) else "stale"
