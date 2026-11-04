"""Consumer for queued assistant delegations (kind ``mission`` / ``branch``).

An approved ``request_mission`` / ``propose_branch`` ends as a ``queued`` DelegationRequest (see
``AssistantService.execute_approved_proposal``). This module turns such a record into a *handoff* in the existing
``HandoffRegistry`` (the control plane's durable Pi/Claude/Codex work queue: ``/api/v1/handoffs`` with its
claim/accept/start/complete transitions). It adds no scheduler and no store.

Honest scope: the handoff is the request. Nothing here writes code, opens a branch or starts a mission: a Pi-side
executor claims the handoff (``POST /api/v1/handoffs/<id>/claim``) and does the work. Until it does, the delegation
is ``running`` only in the sense of "handed to the queue"; ``reconcile`` mirrors the handoff's state back.

Guarantees: fails closed (the delegation must trace to an approved, executed, unmodified proposal in the same
tenant, and the handoff is built from that VERIFIED payload, not from the delegation's own text), claim-once (a state
flag under the store lock), idempotent (the handoff id derives from the delegation id and the body is deterministic),
audited before and after, and lineage-linked. A proposal that is still ``executing`` is a transient state: the
delegation stays queued and the next sweep retries. A cancel is serialised with the hand-off and also cancels the
handoff, so a Pi executor cannot run cancelled work.
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
    proposal_delegation_fields,
    redact_secrets,
)

_log = logging.getLogger(__name__)
KINDS = ("mission", "branch")
HARNESSES = ("pi", "claude", "codex", "opencode")
SYSTEM_ACTOR = "system:delegation-consumer"
STALE_CLAIM = timedelta(minutes=5)
BACKOFF_START, BACKOFF_MAX = timedelta(seconds=30), timedelta(hours=1)
_MIRROR = {"claimed": "running", "accepted": "running", "in_progress": "running", "completed": "succeeded",
           "failed": "failed", "rejected": "failed", "cancelled": "cancelled", "expired": "failed"}


class Deferred(AssistantError):
    """Transient: the delegation stays queued and a later attempt may succeed."""


def handoff_id_for(delegation_id: str) -> str:
    return f"assistant.{delegation_id}"


def adapter_owned(d: DelegationRequest) -> bool:
    """Records the scoped runtime adapter owns (a role binding or an external task) never enter the engineering
    handoff queue: that path is for approved mission/branch proposals only."""
    return bool(d.role_ref or d.external_id)


class DelegationConsumer:
    def __init__(self, service, handoffs, *, source_harness: str = "pi"):
        self.svc, self.handoffs = service, handoffs
        self.source_harness = source_harness if source_harness in HARNESSES else "pi"
        self._offset = 0
        self._retry: dict[str, tuple[Any, timedelta]] = {}      # delegation_id -> (not before, last delay)
        self._deferred_logged: set[str] = set()
        service.delegation_consumer = self                      # so cancel_delegation can reach the handoff

    @property
    def _store(self):
        return self.svc.store

    def _audit(self, d: DelegationRequest, actor: str, action: str, detail: str = "") -> None:
        self._store.audit_record(tenant_id=d.tenant_id, user_id=d.user_id, actor=actor, action=action,
                                 subject_id=d.delegation_id, detail=detail)

    def _safe_audit(self, d, actor, action, detail=""):
        try:
            self._audit(d, actor, action, detail)
        except Exception as exc:  # noqa: BLE001 - the state change stands; do not undo it for a log failure
            _log.error("assistant audit entry %s for %s failed: %s", action, d.delegation_id, exc)

    # -- verification ---------------------------------------------------------------------------------------
    def _verify(self, d: DelegationRequest):
        """(proposal, "") when the delegation traces to an approved, executed, untouched proposal of the same tenant and
        user. (None, reason) for a terminal problem. Raises Deferred while the proposal is still being executed."""
        pid = (d.correlation or {}).get("proposal_id")
        x = self._store.proposals.get(pid) if pid else None
        if x is None or x.tenant_id != d.tenant_id or x.user_id != d.user_id:
            return None, "no approved proposal behind this delegation"
        if x.kind not in ("propose_branch", "request_mission") or x.status != "approved" or not x.decided_by:
            return None, "the proposal behind this delegation is not approved"
        if hashlib.sha256(canonical_json([x.kind, x.payload]).encode()).hexdigest() != x.payload_sha256:
            return None, "the proposal payload changed after approval"
        if (x.kind == "propose_branch") != (d.kind == "branch"):
            return None, "the delegation kind does not match its proposal"
        if x.execution == "executing":
            raise Deferred("the proposal is still being executed; the delegation stays queued")
        if x.execution != "executed" or x.result_ref != d.delegation_id:
            return None, "the proposal was not executed into this delegation"
        return x, ""

    def _body(self, d: DelegationRequest, x) -> dict[str, Any]:
        """Deterministic (no clock, no randomness) and built from the VERIFIED proposal payload."""
        task, target, _kind = proposal_delegation_fields(x.kind, x.payload)
        task = task[:4000]
        if target not in HARNESSES:
            raise ValueError(f"target_system {target!r} is not a supported harness")
        if not task.strip():
            raise ValueError("the approved proposal has no task")
        c = d.correlation or {}
        prov = {"source": "personal-assistant", "tenant_id": d.tenant_id, "user_id": d.user_id,
                "assistant_id": d.assistant_id, "delegation_id": d.delegation_id, "delegation_kind": d.kind,
                "proposal_id": x.proposal_id, "approved_by": x.decided_by,
                "requires": "a Pi-side executor must claim this handoff; the control plane does not run it"}
        for key in ("thread_id", "run_id", "approval_id"):
            if c.get(key):
                prov[key] = str(c[key])
        return {"handoff_id": handoff_id_for(d.delegation_id), "source_harness": self.source_harness,
                "target_harness": target, "source_agent_id": f"assistant:{d.assistant_id}",
                "goal": task, "remaining": [task[:500]], "provenance": prov,
                "idempotency_key": f"assistant-delegation:{d.delegation_id}"}

    # -- dispatch -------------------------------------------------------------------------------------------
    def dispatch(self, delegation_id: str, *, tenant_id: str, user_id: str, actor: str) -> DelegationRequest:
        """Owner (or configured approver) hands one queued delegation to the handoff queue. Replay-safe."""
        self.svc._require_enabled()
        d = self._store.delegations.get(delegation_id)
        if d is None or d.tenant_id != tenant_id or not (d.user_id == user_id or self.svc._is_approver(user_id)):
            raise AssistantNotFound("delegation not found")
        return self._dispatch(d, actor)

    def _handed_off(self, d) -> bool:
        return ((d.correlation or {}).get("dispatch") or {}).get("state") == "handed_off"

    def _dispatch(self, d: DelegationRequest, actor: str) -> DelegationRequest:
        if d.kind not in KINDS or adapter_owned(d):
            raise AssistantError(f"{d.kind} delegations are not dispatched here")
        if self._handed_off(d):
            return d                                            # replay: the recorded result, whatever it became since
        if d.status != "queued":
            raise AssistantError(f"the delegation is {d.status}")
        x, problem = self._verify(d)                            # raises Deferred while the proposal is executing
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
            row, replayed = self.handoffs.create(self._body(d, x), {"kind": "assistant", "id": actor,
                                                                    "authenticated_by": "assistant-consumer"})
        except ValueError as exc:                               # a malformed/refused request will never succeed
            self._fail(d, actor, redact_secrets(str(exc))[:300])
            return d
        except Exception as exc:  # noqa: BLE001 - transient (e.g. durable write): retry on the next sweep
            self._release(d)
            self._note_deferred(d, actor, type(exc).__name__)
            raise Deferred("the handoff queue is unavailable; the delegation stays queued") from None
        cancelled = False
        with self._store._lock:
            if d.status != "queued":                            # cancelled (or otherwise moved) while we built it
                cancelled = True
            else:
                d.status = "running"
                d.correlation = {**d.correlation, "dispatch": {
                    "state": "handed_off", "handoff_id": row["handoff_id"], "replayed": replayed, "by": actor,
                    "at": self.svc.now().isoformat()}}
                if f"handoff:{hid}" not in d.artifact_refs:
                    d.artifact_refs = [*d.artifact_refs, f"handoff:{hid}"]
        if cancelled:
            self._cancel_handoff(hid, actor)
            self._store.put_delegation(d)
            self._safe_audit(d, actor, "assistant.delegation.dispatch_abandoned", f"{d.status} during hand-off")
            return d
        self._store.put_delegation(d)
        self._retry.pop(d.delegation_id, None)
        self._deferred_logged.discard(d.delegation_id)
        self._safe_audit(d, actor, "assistant.delegation.dispatched", f"{d.kind} -> handoff {hid}")
        c = d.correlation
        corr = {k: c[k] for k in ("assistant_id", "thread_id", "run_id") if c.get(k)}
        corr.update({"action_id": f"dispatch.{d.delegation_id}", "tool_id": "dispatch_delegation",
                     "delegation_id": d.delegation_id, "proposal_id": x.proposal_id,
                     "handoff_id": hid, "assistant_id": d.assistant_id,
                     "target_id": f"decision:assistant.exec.{x.proposal_id}"})
        d.correlation["dispatch"]["lineage"] = self.svc._emit_lineage(d.user_id, corr)
        self._store.put_delegation(d)
        return d

    def _note_deferred(self, d, actor, why):
        """One audit entry per outage, and exponential back-off for the sweep (an explicit dispatch ignores it)."""
        now = self.svc.now()
        _, last = self._retry.get(d.delegation_id, (None, BACKOFF_START / 2))
        delay = min(last * 2, BACKOFF_MAX)
        self._retry[d.delegation_id] = (now + delay, delay)
        if d.delegation_id not in self._deferred_logged:
            self._deferred_logged.add(d.delegation_id)
            self._safe_audit(d, actor, "assistant.delegation.dispatch_deferred", why)

    def _release(self, d: DelegationRequest) -> None:
        with self._store._lock:
            d.correlation = {k: v for k, v in (d.correlation or {}).items() if k != "dispatch"}
        self._store.put_delegation(d)

    def _fail(self, d: DelegationRequest, actor: str, reason: str) -> None:
        with self._store._lock:
            d.status = "failed"
            d.note = f"{d.note}; not dispatched: {reason}"[:600] if d.note else f"not dispatched: {reason}"[:600]
            d.correlation = {**(d.correlation or {}), "dispatch": {"state": "failed", "reason": reason}}
        self._store.put_delegation(d)
        self._safe_audit(d, actor, "assistant.delegation.dispatch_failed", reason)

    # -- cancel ----------------------------------------------------------------------------------------------
    def _cancel_handoff(self, hid: str, actor: str) -> None:
        try:
            self.handoffs.transition(hid, "cancelled", {"kind": "assistant", "id": actor,
                                                        "authenticated_by": "assistant-consumer"})
        except (KeyError, ValueError):
            pass                                                # absent, or already terminal: nothing left to stop

    def on_cancel(self, d: DelegationRequest, actor: str) -> None:
        """Called by ``cancel_delegation`` after the delegation is ``cancelled``. A queued/claimed/accepted handoff is
        cancelled too. If a hand-off is in flight, the dispatcher sees ``cancelled`` and cancels it itself."""
        if d.kind in KINDS and not adapter_owned(d) and (d.correlation or {}).get("dispatch"):
            self._cancel_handoff(handoff_id_for(d.delegation_id), actor)

    # -- background ----------------------------------------------------------------------------------------
    def sweep(self, *, limit: int = 50) -> dict[str, int]:
        """Dispatch queued mission/branch delegations for EVERY tenant (each stays in its own tenant: the handoff
        carries the tenant in provenance and the audit entry lands in that tenant's chain), then mirror handoff
        state back. One bad delegation never stops the others; the start rotates so none is starved."""
        out = {"dispatched": 0, "failed": 0, "deferred": 0, "errors": 0, "reconciled": 0}
        if not self.svc.config.get("enabled") or not self.svc.config["delegation_consumer"]["enabled"]:
            return out
        now = self.svc.now()
        pending = sorted((d for d in list(self._store.delegations.values())
                          if d.kind in KINDS and d.status == "queued" and not adapter_owned(d)),
                         key=lambda d: (d.created_at, d.delegation_id))
        if pending:
            k = self._offset % len(pending)
            pending = pending[k:] + pending[:k]
        batch = [d for d in pending if (self._retry.get(d.delegation_id) or (None,))[0] is None
                 or self._retry[d.delegation_id][0] <= now][:limit]
        self._offset += max(1, len(batch))
        for d in batch:
            try:
                self._dispatch(d, SYSTEM_ACTOR)
                out["dispatched" if d.status == "running" else "failed"] += 1
            except AssistantForbidden:
                out["failed"] += 1
            except AssistantError:
                out["deferred"] += 1
            except Exception as exc:  # noqa: BLE001 - isolate the item; back off, never stop the sweep
                out["errors"] += 1
                _log.error("assistant delegation %s dispatch failed: %s", d.delegation_id, type(exc).__name__)
                self._note_deferred(d, SYSTEM_ACTOR, type(exc).__name__)
        try:
            out["reconciled"] = self.reconcile()
        except Exception as exc:  # noqa: BLE001
            _log.error("assistant delegation reconcile failed: %s", type(exc).__name__)
        return out

    def reconcile(self) -> int:
        n = 0
        for d in list(self._store.delegations.values()):
            disp = (d.correlation or {}).get("dispatch") or {}
            if adapter_owned(d) or d.status != "running" or disp.get("state") != "handed_off":
                continue
            row = self.handoffs.get(disp.get("handoff_id", ""))
            if row is None:                                     # the queue lost it: say so rather than stay "running"
                with self._store._lock:
                    d.status = "failed"
                    d.note = (f"{d.note}; " if d.note else "") + "the handoff disappeared from the queue"
                self._store.put_delegation(d)
                self._safe_audit(d, SYSTEM_ACTOR, "assistant.delegation.synced", "handoff missing -> failed")
                n += 1
                continue
            new = _MIRROR.get(row.get("state", ""))
            if new and new != d.status:
                with self._store._lock:
                    if d.status != "running":                   # cancelled meanwhile: the cancel wins
                        continue
                    d.status = new
                self._store.put_delegation(d)
                self._safe_audit(d, SYSTEM_ACTOR, "assistant.delegation.synced", f"handoff {row['state']} -> {new}")
                n += 1
        return n
