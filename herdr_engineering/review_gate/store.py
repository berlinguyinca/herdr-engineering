"""Event-sourced per-PR review pipeline state machine."""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace

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


def _finding_dict(f: Finding) -> dict:
    return {
        "path": redact_text(f.path),
        "category": f.category,
        "message": redact_text(f.message),
        "severity": f.severity,
        "required_change": redact_text(f.required_change),
        "line": f.line,
    }


def _payload(event: dict) -> dict:
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload


def _fold(snap: PrSnapshot, event_type: str, p: dict) -> PrSnapshot:
    if event_type == "push":
        return replace(snap, head_sha=p["sha"], state=State.CI_PENDING, reason=None)
    if event_type == "ci_green":
        return replace(snap, state=State.CI_GREEN)
    if event_type == "ci_red":
        return replace(
            snap,
            ci_fix_rounds=snap.ci_fix_rounds + 1,
            state=State.ESCALATED if p.get("escalated") else State.CI_RED,
            reason=p.get("reason"),
        )
    if event_type == "review_started":
        return replace(snap, state=State.REVIEWING)
    if event_type == "verdict":
        if p["verdict"] == "PASS":
            return replace(
                snap,
                state=State.APPROVED,
                approved_sha=p["sha"],
                approved_patch_id=p.get("patch_id"),
                reason=None,
            )
        return replace(
            snap,
            review_rounds=snap.review_rounds + 1,
            last_findings=tuple(Finding(**f) for f in p.get("findings", ())),
            approved_sha=None,
            approved_patch_id=None,
            state=State.ESCALATED if p.get("escalated") else State.CHANGES_REQUESTED,
            reason=p.get("reason"),
        )
    if event_type == "merged":
        return replace(snap, state=State.MERGED)
    if event_type == "resume":
        return replace(
            snap,
            state=State.CI_PENDING,
            review_rounds=0,
            ci_fix_rounds=0,
            last_findings=(),
            reason=None,
        )
    if event_type == "carry_over_approval":
        return replace(snap, state=State.APPROVED, approved_sha=p["sha"], reason=None)
    return snap


class PipelineStore:
    def __init__(self, repo: ControlPlaneRepo, *, review_cap: int = 4, ci_fix_cap: int = 3):
        self._repo = repo
        self._review_cap = review_cap
        self._ci_fix_cap = ci_fix_cap
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ read
    def snapshot(self, key: PrKey) -> PrSnapshot:
        with self._lock:
            snap = PrSnapshot(key, None, State.CI_PENDING, 0, 0, (), None, None, None)
            for event in self._repo.stream(ENTITY, str(key)):
                snap = _fold(snap, event["event_type"], _payload(event))
            return snap

    def _record(self, key: PrKey, event_type: str, payload: dict) -> bool:
        self._repo.record_event(ENTITY, str(key), event_type, payload)
        return True

    # ----------------------------------------------------------------- write
    def on_push(self, key: PrKey, sha: str) -> bool:
        with self._lock:
            snap = self.snapshot(key)
            if snap.state == State.MERGED or sha == snap.head_sha:
                return False
            return self._record(key, "push", {"sha": sha, "superseded_sha": snap.head_sha})

    def on_ci_green(self, key: PrKey, sha: str) -> bool:
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
            if snap.ci_fix_rounds + 1 >= self._ci_fix_cap:
                payload.update(escalated=True, reason="ci fix cap reached")
            return self._record(key, "ci_red", payload)

    def on_review_started(self, key: PrKey, sha: str, reviewer_model: str) -> bool:
        with self._lock:
            snap = self.snapshot(key)
            if sha != snap.head_sha or snap.state not in (State.CI_GREEN, State.REVIEW_QUEUED):
                return False
            return self._record(
                key, "review_started", {"sha": sha, "reviewer_model": reviewer_model}
            )

    def on_verdict(self, key: PrKey, verdict: Verdict, *, patch_id: str | None = None) -> bool:
        with self._lock:
            snap = self.snapshot(key)
            if verdict.sha != snap.head_sha or snap.state != State.REVIEWING:
                return False
            payload: dict = {
                "sha": verdict.sha,
                "verdict": verdict.verdict,
                "reviewer_model": verdict.reviewer_model,
                "patch_id": patch_id,
                "findings": [_finding_dict(f) for f in verdict.findings],
            }
            if verdict.verdict == "FAIL":
                previous = {finding_fingerprint(f) for f in snap.last_findings}
                repeated = any(finding_fingerprint(f) in previous for f in verdict.findings)
                if snap.review_rounds + 1 >= self._review_cap:
                    payload.update(escalated=True, reason="review cap reached")
                elif repeated:
                    payload.update(escalated=True, reason="repeated finding")
            return self._record(key, "verdict", payload)

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
