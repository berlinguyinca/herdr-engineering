"""Executes an APPROVED assistant proposal, once, for exactly the payload that was approved.

Consent is the approval of one payload hash by someone other than the requester. This module adds the rest of
the contract: it runs only when the ``execute`` flag is on and the kill switch is off, claims the proposal
atomically (a second call finds it already claimed), re-hashes the payload, validates it against a strict
per-action schema, and records success or failure on the proposal. A handler that is not wired reports
``failed: no executor wired`` instead of pretending. Handlers do nothing beyond the one action.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Callable

from . import project_intelligence as pi
from .intel_assistant import ActionBoard, Proposal, payload_hash

_REF = re.compile(r"[A-Za-z0-9:_./#@\-]{1,200}")
_OWNER_NAME = re.compile(r"[A-Za-z0-9_.\-]{1,100}/[A-Za-z0-9_.\-]{1,100}")


def _text(payload: dict, key: str, pattern=_REF) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise pi.IntelligenceError(f"{key} is missing or malformed")
    return value


class ProposalExecutor:
    def __init__(self, board: ActionBoard, store, *, section_fn: Callable[[], dict],
                 ci_repair: Callable[[str, int, str, str], object] | None = None,
                 ci_rerun: Callable[[str, int], object] | None = None,
                 create_issue: Callable[[str, str, str, str], object] | None = None):
        self._board, self._store, self._section = board, store, section_fn
        self._handlers: dict[str, Callable[[Proposal], str]] = {
            "link_entities": self._link, "pause_ingestion": self._pause,
            "request_ci_repair": self._repair, "ci_rerun": self._rerun, "propose_issue": self._issue}
        self._ci_repair, self._ci_rerun, self._create_issue = ci_repair, ci_rerun, create_issue
        self._lock = threading.Lock()

    def execute(self, proposal_id: str, org_id: str) -> Proposal:
        cfg = pi.config(self._section())
        p = self._board.get(proposal_id, org_id)
        if not cfg["enabled"] or not cfg["execute"] or cfg["kill_switch"]:
            return p                                    # off: an approval stays a record of permission only
        with self._lock:
            if p.state != "approved":
                raise pi.Conflict(f"proposal is {p.state}")
            p.state = "executing"                       # claimed: nothing else can start it
        try:
            if payload_hash(p.payload) != p.payload_sha256:
                raise pi.IntelligenceError("payload changed after approval")
            summary = self._handlers[p.action](p)
        except Exception as exc:  # noqa: BLE001 - recorded on the proposal; it is never retried automatically
            p.state = "failed"
            p.history.append(f"execution failed: {type(exc).__name__}: {str(exc)[:200]}")
        else:
            p.state = "executed"
            p.history.append(f"executed by {p.approver}'s approval: {summary[:200]}")
        self._store.record_audit({"action": f"proposal_{p.state}", "id": p.id, "actor": p.approver, "kind": p.action})
        return p

    # ---- handlers: each validates its own payload and does one thing ------------------------------
    def _link(self, p: Proposal) -> str:
        pl = p.payload
        rel = self._store.add_relationship({
            "org_id": p.org_id, "project_ids": [_text(pl, "project")], "from_ref": _text(pl, "from_ref"),
            "to_ref": _text(pl, "to_ref"), "predicate": _text(pl, "predicate", re.compile(r"[a-z_]{1,40}")),
            "derivation": "human_verified", "security_label": "internal",
            "evidence_refs": [{"source_ref": f"proposal:{p.id}", "anchor_type": "agent_event", "anchor_value": f"approved:{p.approver or 'approver'}"}]},
            cfg=pi.config(self._section()), actor=p.approver or "approver")
        return f"linked {rel.from_ref} -> {rel.to_ref}"

    def _pause(self, p: Proposal) -> str:
        source = _text(p.payload, "source", re.compile(r"[a-z_]{1,40}"))
        self._store.pause_source(p.org_id, source, True)
        return f"paused source {source}"

    def _repair(self, p: Proposal) -> str:
        if self._ci_repair is None:
            raise pi.IntelligenceError("no executor wired for request_ci_repair")
        repository, run = _text(p.payload, "repo"), p.payload.get("run")
        if isinstance(run, bool) or not isinstance(run, int) or run < 1:
            raise pi.IntelligenceError("run must be a positive integer")
        self._ci_repair(repository, run, p.id, p.approver or "approver")     # p.id is the idempotency key
        return f"CI repair requested for {repository}#{run}"

    def _rerun(self, p: Proposal) -> str:
        """Restart one CI pipeline run (Woodpecker). The CI provider decides whether this run may be restarted."""
        if self._ci_rerun is None:
            raise pi.IntelligenceError("no executor wired for ci_rerun")
        repository, run = _text(p.payload, "repo", _OWNER_NAME), p.payload.get("run")
        if repository.split("/")[0] in (".", "..") or repository.split("/")[1] in (".", ".."):
            raise pi.IntelligenceError("repo is missing or malformed")
        if isinstance(run, bool) or not isinstance(run, int) or run < 1:
            raise pi.IntelligenceError("run must be a positive integer")
        self._ci_rerun(repository, run)
        return f"CI re-run requested for {repository}#{run}"

    def _issue(self, p: Proposal) -> str:
        if self._create_issue is None:
            raise pi.IntelligenceError("no executor wired for propose_issue")
        title = _text(p.payload, "title", re.compile(r"[^\x00-\x1f]{1,200}"))
        body = p.payload.get("body", "")
        if not isinstance(body, str) or len(body) > 2000:
            raise pi.IntelligenceError("body must be text of at most 2000 characters")
        self._create_issue(_text(p.payload, "repo"), title, body, p.id)
        return f"issue proposed in {p.payload['repo']}"
