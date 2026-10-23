"""Bounded, read-only live verification of durable recovery submissions."""

from __future__ import annotations

import os
import re
import subprocess
import time

from .pi_recovery import IDENTITY, RecoveryTransport, compatible, error_code, proof

INTERACTIVE_STATES = {
    "accepted",
    "verification_waiting",
    "verification_unknown",
    "progress_verified",
    "inference_failed",
}


def _pending_operation(entry):
    for key in ("resume", "reload", "prepare"):
        operation = entry.get("operations", {}).get(key)
        if (
            operation
            and operation.get("receipt", {}).get("status") in (None, "running")
            and operation.get("request", {}).get("operationId")
        ):
            return operation
    return None


def _operation_status(entry, target, transport):
    operation = _pending_operation(entry)
    request = operation["request"]
    current = transport.observe(target, entry["paneId"])
    compared = [key for key in IDENTITY if key != "instanceId" or request["action"] != "reload"]
    if any(current.get(key) != entry["identity"][key] for key in compared):
        raise ValueError("session/process ownership changed")
    # Status is the only maintenance action permitted by this verifier.
    receipt = transport.maintenance(
        target,
        current["sourceRoot"],
        current["instanceId"],
        {
            "action": "status",
            "operationId": request["operationId"],
            "expectedInstanceId": current["instanceId"],
        },
    ).get("maintenance", {})
    if (
        receipt.get("operationId") != request["operationId"]
        or receipt.get("action") != request["action"]
        or receipt.get("status") not in ("running", "succeeded", "failed", "skipped")
    ):
        raise ValueError("invalid maintenance status receipt")
    verification = {
        "state": "operation_" + receipt["status"],
        "action": request["action"],
        "operationId": request["operationId"],
    }
    if request["action"] == "reload":
        verification["loadedCommitVerified"] = (
            receipt["status"] == "succeeded"
            and isinstance(request.get("expectedCommit"), str)
            and bool(re.fullmatch(r"[0-9a-f]{40}", request["expectedCommit"]))
            and current.get("runtime", {}).get("commit") == request.get("expectedCommit")
            and current.get("runtime", {}).get("pending") is False
        )
    # Do not change entry.status, identity or operation requests: even a successful
    # reload receipt is not mission progress and does not authorize automatic replay.
    entry["verification"] = verification


def _original_requirements(entry):
    recorded = entry.get("requirements")
    if (
        isinstance(recorded, dict)
        and type(recorded.get("requiredContext")) is int
        and recorded["requiredContext"] > 0
        and all(type(recorded.get(key)) is bool for key in ("requiresVision", "requiresReasoning"))
    ):
        return recorded, "recorded_requirements"
    candidates = entry.get("candidates")
    if (
        not isinstance(candidates, list)
        or not candidates
        or any(
            not isinstance(model, dict)
            or type(model.get("contextWindow")) is not int
            or model["contextWindow"] <= 0
            or not isinstance(model.get("input"), list)
            for model in candidates
        )
    ):
        return None, "unavailable"
    # Every recorded candidate passed the original requirements. Their minimum
    # context and common capabilities conservatively bound that original envelope.
    return {
        "requiredContext": min(model["contextWindow"] for model in candidates),
        "requiresVision": all("image" in model.get("input", []) for model in candidates),
        "requiresReasoning": all(model.get("reasoning") is True for model in candidates),
    }, "recorded_candidate_envelope"


def _interactive_status(entry, target, transport):
    current = transport.observe(target, entry["paneId"], baseline=entry["baseline"])
    if proof(current) != entry["identity"]:
        raise ValueError("session/process ownership changed")
    evidence = current.get("progressEvidence") or {}
    if evidence.get("baselinePresent") is not True or evidence.get("baselineAncestor") is not True:
        entry.update(
            status="verification_unknown",
            verification={
                "state": "verification_unknown",
                "reason": "baseline or descendant lineage unavailable",
            },
        )
        return
    assistant = evidence.get("assistant")
    if not assistant:
        entry.update(
            status="verification_waiting",
            verification={
                "state": "verification_waiting",
                "reason": "no new assistant evidence after submission",
            },
        )
        return
    if not assistant.get("id"):
        raise ValueError("assistant identity unavailable")
    observed = {
        "baselineId": entry["baseline"],
        "assistantId": assistant.get("id"),
        "provider": assistant.get("provider"),
        "model": assistant.get("model"),
        "stopReason": assistant.get("stopReason"),
        "toolResultIds": evidence.get("successfulToolResults", []),
    }
    selected_confirmed = assistant.get("provider") == "metabolomics" and assistant.get(
        "model"
    ) == entry.get("selectedModel")
    verification = {
        "evidence": observed,
        "selectedModelConfirmed": selected_confirmed,
        "modelChanged": not selected_confirmed,
    }
    if assistant.get("stopReason") == "error":
        state = "inference_failed"
    elif assistant.get("stopReason") == "stop" or observed["toolResultIds"]:
        requirements_known = (
            type(current.get("requiredContext")) is int and current["requiredContext"] > 0
        )
        original, basis = _original_requirements(entry)
        requirements = dict(current)
        if original and requirements_known:
            requirements.update(
                requiredContext=max(current["requiredContext"], original["requiredContext"]),
                requiresVision=bool(current.get("requiresVision") or original["requiresVision"]),
                requiresReasoning=bool(
                    current.get("requiresReasoning") or original["requiresReasoning"]
                ),
            )
        verification["compatibilityBasis"] = (
            basis if original else "selected_model" if selected_confirmed else "unavailable"
        )
        model_verified = (
            requirements_known
            and (selected_confirmed or original is not None)
            and assistant.get("provider") == "metabolomics"
            and any(m["id"] == assistant.get("model") for m in compatible(requirements))
        )
        state = "progress_verified" if model_verified else "verification_unknown"
        if not model_verified:
            verification["reason"] = "progress observed; actual model compatibility is unverified"
    else:
        state = "verification_waiting"
        verification["reason"] = "assistant has not completed or produced a successful tool result"
    verification["state"] = state
    entry.update(status=state, verification=verification)


def verify_recovery(
    store,
    run_id,
    *,
    transport=None,
    managed=None,
    now=time.time,
    max_sessions=10,
    budget_seconds=60,
    progress=None,
):
    if not (os.environ.get("HERDR_ENV") == "1" if managed is None else managed):
        raise ValueError("Live verification requires a Herdr-managed caller")
    if (
        type(max_sessions) is not int
        or not 1 <= max_sessions <= 100
        or not 0 < budget_seconds <= 300
    ):
        raise ValueError("invalid verification bound")
    transport = transport or RecoveryTransport()
    progress = progress or (lambda event: None)
    try:
        with store.lock():
            plan = store.read(run_id)
            candidates = [
                entry
                for entry in plan["sessions"]
                if entry.get("status") in INTERACTIVE_STATES
                or entry.get("status") in ("unknown", "maintenance_running")
            ]
            candidates.sort(key=lambda entry: entry.get("verification", {}).get("checkedAt", 0))
            started, checked = now(), 0
            for entry in candidates:
                if checked >= max_sessions or now() - started >= budget_seconds:
                    break
                progress("Verifying " + entry["host"] + " " + entry["paneId"])
                original_status = entry["status"]
                try:
                    target = plan["hosts"][entry["host"]]["target"]
                    if original_status in ("unknown", "maintenance_running") and _pending_operation(
                        entry
                    ):
                        _operation_status(entry, target, transport)
                    elif (
                        original_status in INTERACTIVE_STATES
                        and entry.get("missionFence") == []
                        and entry.get("request", {}).get("action") != "resume"
                    ):
                        _interactive_status(entry, target, transport)
                    else:
                        entry["verification"] = {
                            "state": "manual_pending",
                            "reason": "typed mission or uncertain submission needs separate authoritative reconciliation",
                        }
                except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
                    if (
                        original_status in INTERACTIVE_STATES
                        and entry.get("missionFence") == []
                        and entry.get("request", {}).get("action") != "resume"
                    ):
                        entry["status"] = "verification_unknown"
                    entry["verification"] = {
                        "state": "verification_unknown",
                        "errorCode": error_code(error),
                    }
                entry["verification"]["checkedAt"] = now()
                checked += 1
                store.write(plan)
            plan["verificationSummary"] = {
                "checked": checked,
                "deferred": len(candidates) - checked,
                "progressVerified": sum(
                    e.get("status") == "progress_verified" for e in plan["sessions"]
                ),
                "inferenceFailed": sum(
                    e.get("status") == "inference_failed" for e in plan["sessions"]
                ),
            }
            unresolved = any(
                e.get("status") not in ("excluded", "progress_verified") for e in plan["sessions"]
            )
            plan["status"] = (
                "needs_verification"
                if unresolved
                else "verified"
                if plan["verificationSummary"]["progressVerified"]
                else "settled"
            )
            store.write(plan)
            return plan
    except BlockingIOError:
        plan = store.read(run_id)
        return {**plan, "verificationDeferred": "recovery_in_progress"}
