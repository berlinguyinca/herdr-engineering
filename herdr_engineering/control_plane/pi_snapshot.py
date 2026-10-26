"""Pi orchestration-snapshot reader (issue #29).

Reads a ``.pi-eng/orchestration-snapshot.json`` (``contractVersion`` 1-3) and
maps each mission onto an ingest envelope for ``/api/v1/ingest`` under the
``pi-engineering`` source, so a Pi mission appears as ``pi-engineering:<id>``.

This is the read-only ingest half of the Pi delivery integration: the Pi
runtime *publishes* a versioned snapshot, and this module turns it into the
same envelopes the control-plane already understands. No new dashboard, no
server-side rule changes.

Contract reference: ``pi-engineering/src/orchestration/missionSnapshot.ts``
(the stable, additive public contract). Unknown ``contractVersion`` values are
rejected rather than guessed at.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

SUPPORTED_CONTRACT_VERSIONS = frozenset({1, 2, 3})

# Mission status -> control-plane mission stage. The snapshot's lifecycle status
# is authoritative for the *stage* the mission is in.
_COMPLETE_STATUSES = frozenset({"completed", "complete", "done", "success", "succeeded"})
_FAILED_STATUSES = frozenset({"failed", "error", "cancelled", "canceled"})

# Observability fields mapped onto the mission envelope (issue #28 stall-field
# names). Only present, non-null values are copied so missing fields stay
# ``None`` on the server rather than being fabricated.
_OBSERVABILITY_FIELDS = (
    "lastHeartbeatAt",
    "lastOutputAt",
    "lastMeaningfulProgressAt",
    "nextAction",
    "nextActionAt",
    "currentObjective",
    "waitingReason",
    "waitingSince",
    "completionStatus",
)


class UnsupportedSnapshotVersion(ValueError):
    """Raised when a snapshot's ``contractVersion`` is not supported."""


def _stage_from_status(status: str) -> str:
    if not status:
        return "active"
    s = status.lower()
    if s in _COMPLETE_STATUSES:
        return "complete"
    if s in _FAILED_STATUSES:
        return "failed"
    if s in {"review", "in_review", "awaiting_review", "under_review", "pr", "in_pr"}:
        return "review"
    if s in {"testing", "in_testing", "running_tests"}:
        return "testing"
    if s in {"implementing", "implementation", "in_progress", "in_progress", "working"}:
        return "implementation"
    if s in {"planning", "specification", "ready", "planning", "spec"}:
        return "planning"
    return "active"


def snapshot_to_envelopes(doc: dict) -> list[dict]:
    """Map a snapshot file onto ingest envelopes (one per mission).

    Pure and deterministic. Unknown ``contractVersion`` raises
    ``UnsupportedSnapshotVersion``; a mission with no id is skipped.
    """
    version = doc.get("contractVersion")
    if version not in SUPPORTED_CONTRACT_VERSIONS:
        raise UnsupportedSnapshotVersion(
            f"unsupported snapshot contractVersion: {version!r}")

    envelopes: list[dict] = []
    for mission in doc.get("missions") or []:
        mid = mission.get("id")
        if not mid:
            log.warning("snapshot contains a mission with no id; skipping")
            continue
        payload: dict[str, Any] = {
            "title": mission.get("title") or mid,
            "goal": mission.get("goal") or "",
            "status": mission.get("status") or "",
            "stage": _stage_from_status(mission.get("status") or ""),
            "workflowClass": mission.get("workflowClass"),
            "riskProfile": mission.get("riskProfile"),
        }
        # Observability is additive (v2+); copy present non-null stall fields.
        obs = mission.get("observability") or {}
        if isinstance(obs, dict):
            for field in _OBSERVABILITY_FIELDS:
                value = obs.get(field)
                if value is not None:
                    payload[field] = value
            progress = obs.get("progress")
            if isinstance(progress, dict) and progress.get("approximatePercent") is not None:
                payload["progressPercent"] = progress["approximatePercent"]
        # A stop block means the mission is complete/stalled.
        if mission.get("stop"):
            payload["stage"] = "complete"
        envelopes.append({
            "source": "pi-engineering",
            "entity_type": "mission",
            "entity_id": mid,
            "event_type": "mission.updated",
            "payload": payload,
        })
    return envelopes


def post_envelopes(base_url: str, api_key: str, envelopes: list[dict],
                   *, timeout: float = 5.0, expected=(200, 202)) -> int:
    """POST each envelope to ``{base_url}/api/v1/ingest`` with the given key.

    Returns the number of envelopes successfully posted. Raises
    ``ValueError`` on an unexpected HTTP status. The key is never logged or
    echoed in error messages.
    """
    if not api_key:
        raise ValueError("an ingest-role API key is required")
    url = base_url.rstrip("/") + "/api/v1/ingest"
    posted = 0
    for envelope in envelopes:
        body = json.dumps(envelope).encode()
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "X-API-Key": api_key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status not in expected:
                    raise ValueError(f"ingest returned HTTP {resp.status}")
                posted += 1
        except urllib.error.HTTPError as exc:
            raise ValueError(f"ingest returned HTTP {exc.code}") from exc
    return posted


def read_snapshot(path) -> dict:
    """Load and parse a snapshot file, returning its dict."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
