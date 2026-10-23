"""Event-sourced per-PR review pipeline state machine."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace

from herdr_engineering.control_plane.redact import redact_text
from herdr_engineering.control_plane.repo import ControlPlaneRepo

from .fingerprint import finding_fingerprint
from .models import Finding, PrKey, State, Verdict

ENTITY = "pr_gate"


@dataclass(frozen=True)
class PrSnapshot:
    key: PrKey
    head_sha: str | None
    state: State
    review_rounds: int
    ci_fix_rounds: int
    last_findings: tuple[Finding, ...]
    approved_sha: str | None
    approved_patch_id: str | None
    reason: str | None


def _redacted(f: Finding) -> Finding:
    return replace(
        f,
        path=redact_text(f.path),
        message=redact_text(f.message),
        required_change=redact_text(f.required_change),
    )


def _payload(event: dict) -> dict:
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload


_REASON_MAX = 200


def _clip_reason(reason: str) -> str:
    return redact_text(reason)[:_REASON_MAX]


_REVIEWABLE = (State.CI_GREEN, State.REVIEW_QUEUED, State.REVIEWING)


def _fold(snap: PrSnapshot, event_type: str, p: dict) -> PrSnapshot:
    """Apply one event, but only if its writer's guard would have allowed it.

    Defence in depth: each branch re-checks the transition the matching
    ``on_*`` method enforces (current head sha, legal source state), so an
    out-of-order or forged event in the log cannot move the PR (for example to
    ``APPROVED``). Every log the store itself writes folds unchanged.
    """
    sha = p.get("sha")
    on_head = isinstance(sha, str) and sha == snap.head_sha
    if event_type == "push":
        if not isinstance(sha, str) or snap.state == State.MERGED or sha == snap.head_sha:
            return snap
        if snap.state == State.ESCALATED:  # only an operator resume clears escalation
            return replace(snap, head_sha=sha)
        return replace(snap, head_sha=sha, state=State.CI_PENDING, reason=None)
    if event_type == "ci_green":
        if not on_head or snap.state != State.CI_PENDING:
            return snap
        return replace(snap, state=State.CI_GREEN)
    if event_type == "ci_red":
        if not on_head or snap.state not in (State.CI_PENDING, State.CI_GREEN):
            return snap
        return replace(
            snap,
            ci_fix_rounds=snap.ci_fix_rounds + 1,
            state=State.ESCALATED if p.get("escalated") else State.CI_RED,
            reason=p.get("reason"),
        )
    if event_type == "review_started":
        if not on_head or snap.state not in _REVIEWABLE:
            return snap
        return replace(snap, state=State.REVIEWING)
    if event_type == "verdict":
        if not on_head or snap.state != State.REVIEWING:
            return snap
        if p.get("verdict") == "PASS":
            return replace(
                snap,
                state=State.APPROVED,
                approved_sha=sha,
                approved_patch_id=p.get("patch_id"),
                reason=None,
            )
        if p.get("verdict") != "FAIL":
            return snap
        return replace(
            snap,
            review_rounds=snap.review_rounds + 1,
            last_findings=tuple(Finding(**f) for f in p.get("findings", ())),
            approved_sha=None,
            approved_patch_id=None,
            state=State.ESCALATED if p.get("escalated") else State.CHANGES_REQUESTED,
            reason=p.get("reason"),
        )
    if event_type == "review_failed":
        reason = p.get("reason")
        if not on_head or snap.state != State.REVIEWING or not isinstance(reason, str):
            return snap
        return replace(snap, state=State.ESCALATED, reason=_clip_reason(reason))
    if event_type == "merged":
        if not on_head or snap.state != State.APPROVED:
            return snap
        return replace(snap, state=State.MERGED)
    if event_type == "resume":
        if snap.state != State.ESCALATED:
            return snap
        return replace(
            snap,
            state=State.CI_PENDING,
            review_rounds=0,
            ci_fix_rounds=0,
            last_findings=(),
            reason=None,
        )
    if event_type == "carry_over_approval":
        if (
            not on_head
            or snap.approved_patch_id is None
            or p.get("patch_id") != snap.approved_patch_id
            or snap.state not in (State.CI_PENDING, *_REVIEWABLE)
        ):
            return snap
        return replace(snap, state=State.APPROVED, approved_sha=sha, reason=None)
    return snap


def _safe_fold(snap: PrSnapshot, event: dict) -> PrSnapshot:
    """A malformed event is skipped: one bad row must never break every read."""
    try:
        payload = _payload(event)
        if not isinstance(payload, dict):
            return snap
        return _fold(snap, event["event_type"], payload)
    except (KeyError, TypeError, ValueError):
        return snap


class PipelineStore:
    def __init__(
        self,
        repo: ControlPlaneRepo,
        *,
        review_cap: int = 4,
        ci_fix_cap: int = 3,
        review_caps: Mapping[str, int] | None = None,
        ci_fix_caps: Mapping[str, int] | None = None,
    ):
        """Caps default to ``review_cap``/``ci_fix_cap``; ``*_caps`` override per canonical repo."""
        self._repo = repo
        self._review_cap = review_cap
        self._ci_fix_cap = ci_fix_cap
        self._review_caps = dict(review_caps or {})
        self._ci_fix_caps = dict(ci_fix_caps or {})
        # Per store instance: serialises writers within one process only.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ read
    def snapshot(self, key: PrKey) -> PrSnapshot:
        with self._lock:
            snap = PrSnapshot(key, None, State.CI_PENDING, 0, 0, (), None, None, None)
            for event in self._repo.stream(ENTITY, str(key)):
                snap = _safe_fold(snap, event)
            return snap

    def known_keys(self) -> list[PrKey]:
        """Every PR the gate has recorded events for, sorted by repo and number."""
        keys = []
        for entity_id in self._repo.entity_ids(ENTITY):
            try:
                keys.append(PrKey.parse(entity_id))
            except ValueError:
                continue
        return sorted(keys, key=lambda k: (k.repo, k.number))

    def snapshots(self) -> list[PrSnapshot]:
        return [self.snapshot(k) for k in self.known_keys()]

    def _record(self, key: PrKey, event_type: str, payload: dict) -> bool:
        # Durable-required: a failed write raises before memory is touched.
        self._repo.record_event(ENTITY, str(key), event_type, payload, require_durable=True)
        return True

    # ----------------------------------------------------------------- write
    def on_push(self, key: PrKey, sha: str) -> bool:
        with self._lock:
            snap = self.snapshot(key)
            if snap.state == State.MERGED or sha == snap.head_sha:
                return False
            return self._record(key, "push", {"sha": sha, "superseded_sha": snap.head_sha})

    def on_ci_green(self, key: PrKey, sha: str) -> bool:
        # Only legal from CI_PENDING: a same-SHA CI re-run after ci_red is
        # ignored until a new push.
        with self._lock:
            snap = self.snapshot(key)
            if sha != snap.head_sha or snap.state != State.CI_PENDING:
                return False
            return self._record(key, "ci_green", {"sha": sha})

    def on_ci_red(self, key: PrKey, sha: str, failures: Sequence[str]) -> bool:
        with self._lock:
            snap = self.snapshot(key)
            if sha != snap.head_sha or snap.state not in (State.CI_PENDING, State.CI_GREEN):
                return False
            payload: dict = {"sha": sha, "failures": [redact_text(f) for f in failures]}
            if snap.ci_fix_rounds + 1 >= self._ci_fix_caps.get(key.repo, self._ci_fix_cap):
                payload.update(escalated=True, reason="ci fix cap reached")
            return self._record(key, "ci_red", payload)

    def on_review_started(self, key: PrKey, sha: str, reviewer_model: str) -> bool:
        with self._lock:
            snap = self.snapshot(key)
            # REVIEWING -> REVIEWING is allowed for the current head: a worker
            # died, its lease expired and the queue re-offered the job.
            if sha != snap.head_sha or snap.state not in (
                State.CI_GREEN,
                State.REVIEW_QUEUED,
                State.REVIEWING,
            ):
                return False
            return self._record(
                key, "review_started", {"sha": sha, "reviewer_model": reviewer_model}
            )

    def on_verdict(self, key: PrKey, verdict: Verdict, *, patch_id: str | None = None) -> bool:
        with self._lock:
            snap = self.snapshot(key)
            if verdict.sha != snap.head_sha or snap.state != State.REVIEWING:
                return False
            findings = tuple(_redacted(f) for f in verdict.findings)
            payload: dict = {
                "sha": verdict.sha,
                "verdict": verdict.verdict,
                "reviewer_model": verdict.reviewer_model,
                "patch_id": patch_id,
                "findings": [asdict(f) for f in findings],
            }
            if verdict.verdict == "FAIL":
                previous = {finding_fingerprint(f) for f in snap.last_findings}
                repeated = any(finding_fingerprint(f) in previous for f in findings)
                if snap.review_rounds + 1 >= self._review_caps.get(key.repo, self._review_cap):
                    payload.update(escalated=True, reason="review cap reached")
                elif repeated:
                    payload.update(escalated=True, reason="repeated finding")
            return self._record(key, "verdict", payload)

    def on_review_failed(self, key: PrKey, sha: str, reason: str) -> bool:
        """The review could not complete: escalate without consuming a review round."""
        with self._lock:
            snap = self.snapshot(key)
            if sha != snap.head_sha or snap.state != State.REVIEWING:
                return False
            return self._record(key, "review_failed", {"sha": sha, "reason": _clip_reason(reason)})

    def on_merged(self, key: PrKey, sha: str) -> bool:
        with self._lock:
            snap = self.snapshot(key)
            if sha != snap.head_sha or snap.state != State.APPROVED:
                return False
            return self._record(key, "merged", {"sha": sha})

    def resume(self, key: PrKey) -> bool:
        with self._lock:
            if self.snapshot(key).state != State.ESCALATED:
                return False
            return self._record(key, "resume", {})

    def carry_over_approval(self, key: PrKey, new_sha: str, patch_id: str) -> bool:
        with self._lock:
            snap = self.snapshot(key)
            if (
                new_sha != snap.head_sha
                or snap.approved_patch_id is None
                or patch_id != snap.approved_patch_id
                or snap.state
                not in (State.CI_PENDING, State.CI_GREEN, State.REVIEW_QUEUED, State.REVIEWING)
            ):
                return False
            return self._record(key, "carry_over_approval", {"sha": new_sha, "patch_id": patch_id})
