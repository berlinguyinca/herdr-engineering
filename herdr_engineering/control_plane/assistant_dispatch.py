"""Consumer for queued assistant delegations (kind ``mission`` / ``branch``).

An approved ``request_mission`` / ``propose_branch`` ends as a ``queued`` DelegationRequest (see
``AssistantService.execute_approved_proposal``). This module turns such a record into a *handoff* in the existing
``HandoffRegistry`` (the control plane's durable Pi/Claude/Codex work queue: ``/api/v1/handoffs`` with its
claim/accept/start/complete transitions). It adds no scheduler and no store.

Honest scope: the handoff is the request. Nothing here writes code, opens a branch or starts a mission: a Pi-side
executor claims the handoff (``POST /api/v1/handoffs/<id>/claim``) and does the work. Until it does, the delegation
is ``running`` only in the sense of "handed to the queue"; ``reconcile`` mirrors the handoff's state back.

Guarantees: fails closed (the delegation must trace to an approved, executed, unmodified proposal in the same
tenant), claim-once (a state flag under the store lock), idempotent (the handoff id derives from the delegation id
and the body is deterministic, so a replay is a registry no-op), audited before and after, and lineage-linked.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import Any

from .assistant import (
    AssistantError,
    AssistantForbidden,
    AssistantNotFound,
    DelegationRequest,
    canonical_json,
    redact_secrets,
)

_log = logging.getLogger(__name__)
KINDS = ("mission", "branch")
HARNESSES = ("pi", "claude", "codex", "opencode")
SYSTEM_ACTOR = "system:delegation-consumer"
STALE_CLAIM = timedelta(minutes=5)
_MIRROR = {"claimed": "running", "accepted": "running", "in_progress": "running", "completed": "succeeded",
           "failed": "failed", "rejected": "failed", "cancelled": "cancelled", "expired": "failed"}


def handoff_id_for(delegation_id: str) -> str:
    return f"assistant.{delegation_id}"


class DelegationConsumer:
    def __init__(self, service, handoffs, *, source_harness: str = "pi"):
        self.svc, self.handoffs = service, handoffs
        self.source_harness = source_harness if source_harness in HARNESSES else "pi"

    # -- helpers --------------------------------------------------------------------------------------------
    @property
    def _store(self):
        return self.svc.store

    def _audit(self, d: DelegationRequest, actor: str, action: str, detail: str = "") -> None:
        self._store.audit_record(tenant_id=d.tenant_id, user_id=d.user_id, actor=actor, action=action,
                                 subject_id=d.delegation_id, detail=detail)

    def _provenance_problem(self, d: DelegationRequest) -> str:
        """"" when the delegation traces to an approved, executed, untouched proposal of the same tenant and user."""
        pid = (d.correlation or {}).get("proposal_id")
        x = self._store.proposals.get(pid) if pid else None
        if x is None or x.tenant_id != d.tenant_id or x.user_id != d.user_id:
            return "no approved proposal behind this delegation"
        if x.kind not in ("propose_branch", "request_mission") or x.status != "approved" or not x.decided_by:
            return "the proposal behind this delegation is not approved"
        if x.execution != "executed" or x.result_ref != d.delegation_id:
            return "the proposal was not executed into this delegation"
        if hashlib.sha256(canonical_json([x.kind, x.payload]).encode()).hexdigest() != x.payload_sha256:
            return "the proposal payload changed after approval"
        if (x.kind == "propose_branch") != (d.kind == "branch"):
            return "the delegation kind does not match its proposal"
        return ""

    def _body(self, d: DelegationRequest) -> dict[str, Any]:
        """Deterministic (no clock, no randomness): the same delegation always yields the same fingerprint."""
        target = d.target_system if d.target_system in HARNESSES else ""
        if not target:
            raise ValueError(f"target_system {d.target_system!r} is not a supported harness")
        c = d.correlation or {}
        prov = {"source": "personal-assistant", "tenant_id": d.tenant_id, "user_id": d.user_id,
                "assistant_id": d.assistant_id, "delegation_id": d.delegation_id, "delegation_kind": d.kind,
                "proposal_id": str(c.get("proposal_id") or ""), "note": d.note,
                "requires": "a Pi-side executor must claim this handoff; the control plane does not run it"}
        for key in ("thread_id", "run_id", "approval_id"):
            if c.get(key):
                prov[key] = str(c[key])
        return {"handoff_id": handoff_id_for(d.delegation_id), "source_harness": self.source_harness,
                "target_harness": target, "source_agent_id": f"assistant:{d.assistant_id}",
                "goal": d.task, "remaining": [d.task[:500]], "provenance": prov,
                "idempotency_key": f"assistant-delegation:{d.delegation_id}"}

    # -- dispatch -------------------------------------------------------------------------------------------
    def dispatch(self, delegation_id: str, *, tenant_id: str, user_id: str, actor: str) -> DelegationRequest:
        """Owner (or configured approver) hands one queued delegation to the handoff queue. Replay-safe."""
        self.svc._require_enabled()
        d = self._store.delegations.get(delegation_id)
        if d is None or d.tenant_id != tenant_id or not (d.user_id == user_id or self.svc._is_approver(user_id)):
            raise AssistantNotFound("delegation not found")
        return self._dispatch(d, actor)

    def _dispatch(self, d: DelegationRequest, actor: str) -> DelegationRequest:
        if d.kind not in KINDS:
            raise AssistantError(f"{d.kind} delegations are not dispatched here")
        state = (d.correlation or {}).get("dispatch") or {}
        if state.get("state") == "handed_off":
            return d                                            # replay: the recorded result
        if d.status != "queued":
            raise AssistantError(f"the delegation is {d.status}")
        problem = self._provenance_problem(d)
        if problem:
            self._fail(d, actor, problem)
            raise AssistantForbidden(problem)
        now = self.svc.now()
        with self._store._lock:                                 # claim: exactly one caller proceeds
            cur = (d.correlation or {}).get("dispatch") or {}
            if d.status != "queued":
                raise AssistantError(f"the delegation is {d.status}")
            if cur.get("state") == "claiming" and cur.get("claimed_at", "") > (now - STALE_CLAIM).isoformat():
                raise AssistantError("the delegation is being dispatched")
            d.correlation = {**(d.correlation or {}),
                             "dispatch": {"state": "claiming", "claimed_at": now.isoformat(), "by": actor}}
        self._store.put_delegation(d)
        try:
            self._audit(d, actor, "assistant.delegation.dispatching", d.kind)
        except Exception:
            self._release(d)                                    # nothing happened: release the claim
            raise
        hid = handoff_id_for(d.delegation_id)
        try:
            row, replayed = self.handoffs.create(self._body(d), {"kind": "assistant", "id": actor,
                                                                 "authenticated_by": "assistant-consumer"})
        except ValueError as exc:                               # a malformed/refused request will never succeed
            self._fail(d, actor, redact_secrets(str(exc))[:300])
            return d
        except Exception as exc:  # noqa: BLE001 - transient (e.g. durable write): retry on the next sweep
            self._release(d)
            self._safe_audit(d, actor, "assistant.delegation.dispatch_deferred", type(exc).__name__)
            raise AssistantError("the handoff queue is unavailable; the delegation stays queued") from None
        with self._store._lock:
            d.status = "running"
            d.correlation = {**d.correlation, "dispatch": {"state": "handed_off", "handoff_id": row["handoff_id"],
                                                          "replayed": replayed, "by": actor,
                                                          "at": self.svc.now().isoformat()}}
            d.artifact_refs = [*d.artifact_refs, f"handoff:{hid}"] if f"handoff:{hid}" not in d.artifact_refs \
                else d.artifact_refs
        self._store.put_delegation(d)
        self._safe_audit(d, actor, "assistant.delegation.dispatched", f"{d.kind} -> handoff {hid}")
        c = d.correlation
        corr = {k: c[k] for k in ("assistant_id", "thread_id", "run_id") if c.get(k)}
        corr.update({"action_id": f"dispatch.{d.delegation_id}", "tool_id": "dispatch_delegation",
                     "delegation_id": d.delegation_id, "proposal_id": c.get("proposal_id", ""),
                     "handoff_id": hid, "assistant_id": d.assistant_id})
        if c.get("proposal_id"):
            corr["target_id"] = f"decision:assistant.exec.{c['proposal_id']}"
        d.correlation["dispatch"]["lineage"] = self.svc._emit_lineage(d.user_id, corr)
        self._store.put_delegation(d)
        return d

    def _release(self, d: DelegationRequest) -> None:
        with self._store._lock:
            rest = {k: v for k, v in (d.correlation or {}).items() if k != "dispatch"}
            d.correlation = rest
        self._store.put_delegation(d)

    def _fail(self, d: DelegationRequest, actor: str, reason: str) -> None:
        with self._store._lock:
            d.status = "failed"
            d.note = f"{d.note}; not dispatched: {reason}"[:600] if d.note else f"not dispatched: {reason}"[:600]
            d.correlation = {**(d.correlation or {}), "dispatch": {"state": "failed", "reason": reason}}
        self._store.put_delegation(d)
        self._safe_audit(d, actor, "assistant.delegation.dispatch_failed", reason)

    def _safe_audit(self, d, actor, action, detail=""):
        try:
            self._audit(d, actor, action, detail)
        except Exception as exc:  # noqa: BLE001 - the state change stands; do not undo it for a log failure
            _log.error("assistant audit entry %s for %s failed: %s", action, d.delegation_id, exc)

    # -- background ----------------------------------------------------------------------------------------
    def sweep(self, *, limit: int = 50) -> dict[str, int]:
        """Dispatch queued mission/branch delegations for EVERY tenant (each stays in its own tenant: the handoff
        carries the tenant in provenance and the audit entry lands in that tenant's chain), then mirror handoff
        state back onto delegations already handed off."""
        out = {"dispatched": 0, "failed": 0, "deferred": 0, "reconciled": 0}
        if not self.svc.config.get("enabled") or not self.svc.config["delegation_consumer"]["enabled"]:
            return out
        pending = [d for d in list(self._store.delegations.values())
                   if d.kind in KINDS and d.status == "queued"][:limit]
        for d in pending:
            try:
                self._dispatch(d, SYSTEM_ACTOR)
                out["dispatched" if d.status == "running" else "failed"] += 1
            except AssistantForbidden:
                out["failed"] += 1
            except AssistantError:
                out["deferred"] += 1
        out["reconciled"] = self.reconcile()
        return out

    def reconcile(self) -> int:
        n = 0
        for d in list(self._store.delegations.values()):
            disp = (d.correlation or {}).get("dispatch") or {}
            if d.status != "running" or disp.get("state") != "handed_off":
                continue
            row = self.handoffs.get(disp.get("handoff_id", ""))
            new = _MIRROR.get((row or {}).get("state", ""))
            if new and new != d.status:
                d.status = new
                self._store.put_delegation(d)
                self._safe_audit(d, SYSTEM_ACTOR, "assistant.delegation.synced", f"handoff {row['state']} -> {new}")
                n += 1
        return n
