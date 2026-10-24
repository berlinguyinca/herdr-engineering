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

Attempts: an attempt is a claim, exactly as ``ReviewQueue.attempts`` counts
``claimed`` events. Invalid output, GitHub errors, PR-moved fetches, refused
``on_review_started`` and abandoned leases all spend the same ``max_attempts``
budget. Both escalation paths mean "this job has been claimed max_attempts
times and has still not produced a verdict":

* ``submit``/``fail``: on a failure during claim number ``attempts >=
  max_attempts`` the PR escalates at once (the failure is known now).
* ``claim``: a claim numbered ``max_attempts + 1`` or later means the
  previous ``max_attempts`` claims ended with no verdict and no ``fail``
  (GitHub error, PR moved, worker vanished). The PR is moved to REVIEWING if
  needed, escalated with ``"review could not be completed"`` and the job
  completed.

A job whose material cannot be fetched, or whose ``on_review_started`` is
refused, is neither released nor completed: it stays claimed and its lease
expiry is the backoff, so one bad PR never blocks a healthy one in the same
call. GateService cancels the job when a PR leaves the open list (or its
head moves), and re-queues the head if the PR comes back while still
REVIEWING or CI_GREEN. Cost: a GitHub outage longer than
about ``max_attempts * lease_seconds`` (45 minutes with the defaults) escalates
every PR it touched, and an operator must ``resume`` them.

Worker-reported failure text is never stored: ``fail`` records the fixed
reason ``"reviewer failed repeatedly"``.

Per-job memory: the material's ``truncated`` flag and the reviewer model are
remembered from ``claim``. After a coordinator restart ``submit`` re-fetches
the material to recompute ``truncated``; a failed or moved fetch returns
``rejected`` without recording anything or counting an attempt. The reviewer
model is then unknown and recorded as a fixed placeholder (it is informational
only: nothing decides on a verdict's ``reviewer_model``).

Nothing derived from model output or PR text is logged, returned or stored
except through ``parse_verdict``'s validated ``Verdict``.

Claim tokens: ``claim`` returns ``claim_token = HMAC-SHA256(secret,
"<job_id>|<worker_id>|<attempt>")`` where ``secret`` is 32 random bytes drawn
when the coordinator is built (memory only; never stored or logged) and
``attempt`` is the job's claim count. ``heartbeat``/``submit``/``fail`` verify
the token (``hmac.compare_digest``) against the CURRENT claim count before any
other check, so a re-claim invalidates older tokens and a caller who only knows
the job id and worker id (both visible on ``GET /queue``) can do nothing. A
wrong or missing token is answered exactly like a stale claim and writes
nothing. A coordinator restart draws a new secret: in-flight reviews become
stale (wasted, never wrongly recorded).

Base ref: the verdict records the PR base the reviewer saw (``base_ref`` of the
material fetched at claim, or re-fetched after a restart). A PASS approves for
that base only; GateService re-reviews when the PR is retargeted.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections.abc import Collection
from dataclasses import dataclass

from .config import ReviewerConfig
from .github_app import GitHubAppError
from .models import State
from .queue import ReviewJob
from .review_prompt import TRUNCATED_PASS, VerdictError, build_messages, parse_verdict

MAX_SKIPS = 10
INVALID_OUTPUT = "invalid review output"
WORKER_FAILED = "reviewer failed repeatedly"
GAVE_UP = "review could not be completed"
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
    claim_token: str


@dataclass(frozen=True)
class _Held:
    truncated: bool
    reviewer_model: str
    base_ref: str | None


def _base(material) -> str | None:
    base = getattr(material, "base_ref", None)
    return base if isinstance(base, str) and base else None


class ReviewCoordinator:
    def __init__(self, *, store, queue, app, config: ReviewerConfig, enrolled: Collection[str]):
        self._store = store
        self._queue = queue
        self._app = app
        self._config = config
        self._enrolled = frozenset(enrolled)
        self._held: dict[str, _Held] = {}
        self._lock = threading.RLock()
        self._secret = secrets.token_bytes(32)  # per process; never persisted or logged

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

    def _token(self, job_id: str, worker_id: str, attempt: int) -> str:
        msg = f"{job_id}|{worker_id}|{attempt}".encode("utf-8", "surrogatepass")
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def _token_ok(self, job_id, worker_id, claim_token) -> bool:
        """Constant-time check against the job's CURRENT claim count (lock held)."""
        if not isinstance(claim_token, str) or not isinstance(job_id, str):
            return False
        if not isinstance(worker_id, str):
            return False
        expected = self._token(job_id, worker_id, self._queue.attempts(job_id))
        return hmac.compare_digest(
            claim_token.encode("utf-8", "surrogatepass"), expected.encode("ascii")
        )

    def _prune(self) -> None:
        live = {row["job_id"] for row in self._queue.snapshot()}
        for job_id in [j for j in self._held if j not in live]:
            del self._held[job_id]

    # ------------------------------------------------------------- public
    def claim(self, worker_id: str, reviewer_model: str) -> Claim | None:
        """Claim the next reviewable job, skipping at most ``MAX_SKIPS`` per call.

        A job whose material cannot be fetched (GitHub error or PR moved/closed),
        or whose ``on_review_started`` is refused, is skipped and stays claimed:
        its lease expiry is the backoff. Only an unexpected non-GitHub exception
        propagates, and that job is likewise left to expire.
        """
        for _ in range(MAX_SKIPS):
            with self._lock:
                job = self._queue.claim(worker_id, reviewer_model)
                if job is None:
                    return None
                self._prune()
                if job.key.repo not in self._enrolled or not self._is_current(job):
                    self._done(job.job_id, worker_id)  # stale: unenrolled or head moved
                    continue
                attempt = self._queue.attempts(job.job_id)
                if attempt > self._config.max_attempts:
                    # max_attempts earlier claims all ended without a verdict.
                    self._store.on_review_started(job.key, job.sha, reviewer_model)
                    self._store.on_review_failed(job.key, job.sha, GAVE_UP)
                    self._done(job.job_id, worker_id)
                    continue
                if not self._store.on_review_started(job.key, job.sha, reviewer_model):
                    continue  # left claimed; the lease expiry re-offers it
                prior = self._store.snapshot(job.key).last_findings
            try:
                material = self._fetch(job)  # lock dropped: network call
            except GitHubAppError:
                continue  # left claimed; the lease expiry is the backoff
            if material is None:
                continue  # PR moved or closed; GateService cancels it if it stays so
            with self._lock:
                if self._queue.live_claim(job.job_id, worker_id) is None:
                    continue  # lease lost during the fetch
                if not self._is_current(job, (State.REVIEWING,)):
                    continue
                messages = build_messages(material, prior, max_findings=self._config.max_findings)
                self._held[job.job_id] = _Held(
                    bool(material.truncated), reviewer_model, _base(material)
                )
                return Claim(
                    job_id=job.job_id,
                    repo=job.key.repo,
                    number=job.key.number,
                    sha=job.sha,
                    messages=messages,
                    lease_seconds=self._config.lease_seconds,
                    attempt=attempt,
                    claim_token=self._token(job.job_id, worker_id, attempt),
                )
        return None

    def heartbeat(self, job_id: str, worker_id: str, *, claim_token) -> bool:
        with self._lock:
            if not self._token_ok(job_id, worker_id, claim_token):
                return False
            return self._queue.renew(job_id, worker_id)

    def submit(self, job_id: str, worker_id: str, output: str, *, claim_token) -> str:
        with self._lock:
            if not self._token_ok(job_id, worker_id, claim_token):
                return "stale"
            job = self._queue.live_claim(job_id, worker_id)
            if job is None or not self._is_current(job, (State.REVIEWING,)):
                return "stale"
            if not isinstance(output, str) or (
                len(output.encode("utf-8", "surrogatepass")) > self._config.max_output_bytes
            ):
                self._fail(job_id, worker_id, INVALID_OUTPUT)
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
            held = _Held(bool(material.truncated), UNKNOWN_REVIEWER, _base(material))
        with self._lock:
            if not self._token_ok(job_id, worker_id, claim_token):
                return "stale"  # re-claimed while the lock was dropped
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
                self._fail(job_id, worker_id, reason)
                return "rejected"
            recorded = self._store.on_verdict(job.key, verdict, base_ref=held.base_ref)
            self._done(job_id, worker_id)
            return "recorded" if recorded else "stale"

    def fail(self, job_id: str, worker_id: str, reason: str, *, claim_token) -> str:
        """A worker reports it could not review. ``reason`` is never stored."""
        del reason  # worker text: ignored, the record uses a fixed string
        with self._lock:
            if not self._token_ok(job_id, worker_id, claim_token):
                return "stale"
            return self._fail(job_id, worker_id, WORKER_FAILED)

    def _fail(self, job_id: str, worker_id: str, fixed_reason: str) -> str:
        with self._lock:
            job = self._queue.live_claim(job_id, worker_id)
            if job is None:
                return "stale"
            if self._queue.attempts(job_id) >= self._config.max_attempts:
                escalated = self._store.on_review_failed(job.key, job.sha, fixed_reason)
                self._done(job_id, worker_id)
                return "escalated" if escalated else "stale"
            self._held.pop(job_id, None)
            return "released" if self._queue.release(job_id, worker_id) else "stale"
