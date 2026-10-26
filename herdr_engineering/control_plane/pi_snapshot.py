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

# A redirect (301/302/303) from the ingest endpoint to another origin would have
# urllib's default handler copy the ``X-API-Key`` header into the redirected
# request, leaking the ingest credential to the redirect target. This client
# never follows redirects.
_NO_REDIRECT = urllib.request.HTTPRedirectHandler()
_NO_REDIRECT.redirect_request = lambda *args, **kwargs: None
_OPENER = urllib.request.build_opener(_NO_REDIRECT)

log = logging.getLogger(__name__)

SUPPORTED_CONTRACT_VERSIONS = frozenset({1, 2, 3})

# Mission status -> control-plane mission stage. The snapshot's lifecycle status
# is authoritative for the *stage* the mission is in.
_COMPLETE_STATUSES = frozenset({"completed", "complete", "done", "success", "succeeded"})
_FAILED_STATUSES = frozenset({"failed", "error", "cancelled", "canceled"})




class UnsupportedSnapshotVersion(ValueError):
    """Raised when a snapshot's ``contractVersion`` is not supported."""


def _stage_from_status(status: str) -> str:
    """Map a Pi ``MissionStatus`` value onto a control-plane mission stage.

    The snapshot publishes Pi's lifecycle values verbatim (``EXECUTING``,
    ``REVIEWING``, ``VALIDATING``, ... see pi-engineering/src/orchestration/
    types.ts), so map them explicitly rather than substring-matching arbitrary
    free text."""
    if not status:
        return "active"
    s = status.strip().upper()
    if s in {"COMPLETE", "COMPLETED", "DONE", "SUCCESS", "SUCCEEDED"}:
        return "complete"
    if s in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
        return "failed"
    # Pi lifecycle states (types.ts)
    if s in {"REVIEWING", "FINAL_VALIDATION", "WAITING_FOR_USER", "PR"}:
        return "review"
    if s in {"VALIDATING", "INTEGRATING", "TESTING"}:
        return "testing"
    if s in {"EXECUTING", "REPAIRING", "STARTING", "QUEUED", "NEW", "CLASSIFYING",
              "IN_PROGRESS", "WORKING", "IMPLEMENTATION"}:
        return "implementation"
    if s in {"PLANNING", "SPECIFICATION", "READY", "SPEC"}:
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
            # The mission ingest path reads ``purpose`` for the description; map
            # the snapshot's goal there so the dashboard shows the intended
            # outcome rather than an empty description.
            "purpose": mission.get("goal") or "",
            "status": mission.get("status") or "",
            "stage": _stage_from_status(mission.get("status") or ""),
            "cwd": mission.get("cwd"),
            "workflowClass": mission.get("workflowClass"),
            "riskProfile": mission.get("riskProfile"),
        }
        # A durable stop blocks or pauses the mission (resume condition,
        # preserved work); it does NOT mean the mission completed. Keep the
        # status-derived stage so interrupted work is never presented as done.
        # (Observability fields like lastHeartbeatAt live on the separate
        # mission_activity.snapshot path, not the mission ingest envelope, so
        # they are intentionally not emitted here.)
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
            with _OPENER.open(req, timeout=timeout) as resp:
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
