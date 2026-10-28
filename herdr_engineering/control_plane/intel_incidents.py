"""Incident projection and failure classification for autonomous repair (spec 03 "Diagnosis", "Incident states").

Pure and in-memory: it folds recorded ``ci_fix`` / review-gate events into ``repair_policy.Incident`` objects and
classifies a failure from structured facts. It starts nothing; the existing ci_fix service and review gate stay the
only executors. Out-of-order or duplicate events never move an incident backwards and never raise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import repair_policy as rp

_INFRA = re.compile(r"(connection (reset|refused|timed out)|no space left|runner (lost|disconnected)|oom[- ]?kill|"
                    r"killed signal 9|toomanyrequests|50[234] (bad gateway|service|gateway)|tls handshake|"
                    r"temporary failure in name resolution|image pull|context deadline exceeded)", re.I)
_ASSERT = re.compile(r"(assertionerror|\bassert\b.*(failed|!=|==)|expected .* (got|but)|compile error|cannot find symbol|"
                     r"syntaxerror|error\[e\d+\]|undefined reference|importerror|modulenotfounderror|\bFAILED\b .*::)", re.I)


@dataclass(frozen=True)
class Classification:
    kind: str                    # infra | flaky | regression | unknown
    confidence: str              # high | medium | low
    action: str                  # rerun | rerun_then_human | repair | human
    reasons: tuple[str, ...]


def classify_failure(facts: dict) -> Classification:
    """``facts``: log_excerpt:str, attempts:[bool ok per rerun of the same SHA], distinct_commits_failed:int,
    changed_paths_overlap:bool (the diff touches code the failing test exercises). Conservative: anything
    ambiguous is ``unknown`` and goes to a human."""
    log = str(facts.get("log_excerpt") or "")[:20000]
    attempts = [bool(a) for a in (facts.get("attempts") or [])]
    infra, assertion = bool(_INFRA.search(log)), bool(_ASSERT.search(log))
    if attempts and any(attempts) and not all(attempts):
        return Classification("flaky", "high", "rerun_then_human", ("same commit both passed and failed",))
    if infra and not assertion:
        return Classification("infra", "high" if len(attempts) > 1 or not attempts else "medium", "rerun",
                              ("infrastructure signature without an assertion/compile failure",))
    if infra and assertion:
        return Classification("unknown", "low", "human", ("infrastructure and code-failure signatures both present",))
    deterministic = (not attempts) or (len(attempts) >= 2 and not any(attempts))
    if assertion and deterministic and facts.get("changed_paths_overlap") is True:
        return Classification("regression", "high" if len(attempts) >= 2 else "medium", "repair",
                              ("deterministic code failure in code the change touched",))
    if assertion and int(facts.get("distinct_commits_failed") or 0) >= 3 and not facts.get("changed_paths_overlap"):
        return Classification("flaky", "medium", "rerun_then_human", ("fails across unrelated commits",))
    return Classification("unknown", "low", "human", ("not enough evidence to classify",))


# ci_fix state -> the incident state it implies. Anything not listed is ignored.
_CI_FIX = {"queued": "AUTHORIZED", "claimed": "EXECUTING", "preparing": "EXECUTING", "running": "EXECUTING",
           "finishing": "EXECUTING", "succeeded": "REVIEWING", "no_change": "FAILED", "failed": "FAILED",
           "expired": "FAILED", "cancelled": "CANCELLED"}
_REVIEW_GATE = {"merged": "VERIFYING", "post_merge_verified": "COMPLETED", "rollback_requested": "ROLLBACK_REQUESTED"}
_ORDER = {s: i for i, s in enumerate(rp.STATES[:9])}


class IncidentBoard:
    def __init__(self, policy_version: str):
        self.policy_version = policy_version
        self.incidents: dict[str, rp.Incident] = {}

    def _get(self, project: str, source_incident: str) -> rp.Incident:
        key = rp.incident_key(project, source_incident, self.policy_version)
        return self.incidents.setdefault(key, rp.Incident(key=key))

    def apply(self, *, project: str, source_incident: str, event_id: str, at: str, kind: str, state: str,
              agent_run_id: str | None = None) -> rp.Incident:
        """``kind`` is ``ci_fix`` or ``review_gate``. Duplicate events attach; stale ones are ignored."""
        inc = self._get(project, source_incident)
        if not inc.attach(event_id):
            return inc
        target = (_CI_FIX if kind == "ci_fix" else _REVIEW_GATE).get(state)
        if target is None or inc.state in rp.TERMINAL:
            return inc
        try:
            if target in ("FAILED", "CANCELLED", "ROLLBACK_REQUESTED"):
                inc.transition(target, at=at, cause_event=event_id, policy_version=self.policy_version, agent_run_id=agent_run_id)
                return inc
            if inc.state in rp._PAUSES or inc.state == "COMPLETED":
                return inc
            # step forward through intermediate states so the history shows every stage; never backwards
            while _ORDER.get(inc.state, 99) < _ORDER[target]:
                inc.transition(rp._FORWARD[inc.state], at=at, cause_event=event_id,
                               policy_version=self.policy_version, agent_run_id=agent_run_id)
        except rp.IllegalTransition:
            pass
        return inc

    def summary(self) -> list[dict]:
        return [{"key": i.key, "state": i.state, "transitions": len(i.history)} for i in self.incidents.values()]
