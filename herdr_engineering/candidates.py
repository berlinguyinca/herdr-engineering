"""Multi-agent worktree / candidate comparison (spec 0140).

A candidate is a projection over existing ownership identifiers, never a new
executor: mission/issue -> worker/session -> machine -> repo/worktree/revision
-> diff -> dev leases -> tests -> CI -> browser artifacts -> review.
Safe cleanup coordinates with the owner and never deletes another candidate's
resources based on path/name heuristics.
"""
from __future__ import annotations

import builtins
import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Candidate:
    id: str
    mission_id: str | None = None
    issue_id: str | None = None
    worker_session_id: str | None = None
    machine_id: str | None = None
    repository: str | None = None
    worktree_id: str | None = None
    branch: str | None = None
    revision: str | None = None
    status: str = "unknown"
    attention: str | None = None
    changed_files: list[str] = field(default_factory=list)
    diff_summary: str = ""
    dev_ports: list[int] = field(default_factory=list)
    test_summary: dict[str, Any] = field(default_factory=dict)
    ci_state: str | None = None
    review_state: str | None = None
    screenshots: int = 0
    browser_error_count: int = 0
    artifact_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class CandidateGroup:
    """Groups candidates for one mission/issue and guards cleanup."""

    def __init__(self, group_id: str) -> None:
        self.group_id = group_id
        self._candidates: dict[str, Candidate] = {}

    def upsert(self, candidate: Candidate) -> Candidate:
        self._candidates[candidate.id] = candidate
        return candidate

    def list(self) -> builtins.list[Candidate]:
        return list(self._candidates.values())

    def get(self, candidate_id: str) -> Candidate | None:
        return self._candidates.get(candidate_id)

    def cleanup(self, candidate_id: str, *, owned_by: str, owner_token: str,
                dev_fabric_close, evidence_materializer) -> bool:
        """Coordinate safe cleanup.

        Only the owner (matching worker_session_id == owned_by) may trigger
        cleanup. Never remove a worktree by path heuristics; the owning
        workflow must confirm.
        """
        cand = self._candidates.get(candidate_id)
        if cand is None:
            return False
        if cand.worker_session_id != owned_by:
            return False  # not this owner's candidate
        if not owner_token:
            return False
        # close dev leases
        for port in cand.dev_ports:
            dev_fabric_close(port)
        # materialize retained evidence
        evidence_materializer(cand)
        cand.status = "cleaned"
        return True


def candidate_id(repository: str, worktree_id: str, branch: str) -> str:
    raw = f"{repository}:{worktree_id}:{branch}"
    return "cand_" + hashlib.sha256(raw.encode()).hexdigest()[:20]
