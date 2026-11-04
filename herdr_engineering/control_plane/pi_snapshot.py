"""Translate decoded .pi-eng/orchestration-snapshot.json documents for ingest.

Conversion is pure and does not invent liveness from snapshot generation time.
Call ``post_envelopes(base_url, None, envelopes)`` to read HERDR_CP_INGEST_KEY,
which must identify an ingest-role principal. CLI polling is intentionally separate.
Telemetry fields are mapped into envelopes; exposing them in mission projections
is deferred to the projection workstream.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException

SUPPORTED = frozenset({1, 2, 3})


class UnsupportedSnapshotVersion(ValueError):
    """The snapshot contract version is not supported by this reader."""


def snapshot_to_envelopes(doc: dict) -> list[dict]:
    """Map versions 1–3 to one mission envelope each, preserving absent/null data."""
    version = doc.get("contractVersion")
    if type(version) is not int or version not in SUPPORTED:
        raise UnsupportedSnapshotVersion("Supported snapshot contractVersion values are 1, 2, 3")
    envelopes = []
    for mission in doc["missions"]:
        payload = {key: mission[key] for key in ("title", "status") if key in mission}
        if "goal" in mission:
            payload["purpose"] = mission["goal"]
        if "status" in mission:
            # Existing mission ingest projects stage into the displayed status.
            payload["stage"] = mission["status"]
        observability = mission.get("observability") or {}
        for source, target in (
            ("lastHeartbeatAt", "last_heartbeat_at"),
            ("lastMeaningfulProgressAt", "last_progress_at"),
            ("nextAction", "next_action"),
            ("action", "recovery_action"),
            ("health", "health"),
        ):
            if source in observability:
                payload[target] = observability[source]
        progress = observability.get("progress") or {}
        if "approximatePercent" in progress:
            payload["progress"] = progress["approximatePercent"]
        envelope = {"source": "pi-engineering", "entity_type": "mission",
                    "entity_id": mission["id"], "event_type": "mission.updated",
                    "payload": payload}
        if "generatedAt" in doc:
            envelope["occurred_at"] = doc["generatedAt"]
        envelopes.append(envelope)
    return envelopes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never send an ingest credential to a server-selected destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_envelopes(base_url: str, api_key: str | None, envelopes: list[dict], *, allow_open: bool = False) -> int:
    """POST each envelope and return the accepted count; stop on the first error.

    The caller may retry the snapshot after a partial failure: mission upserts
    are idempotent. HTTP bodies, URLs, credentials and raw transport exceptions
    are never included in diagnostics. Redirects fail without being followed.
    """
    key = os.environ.get("HERDR_CP_INGEST_KEY") if api_key is None else api_key
    if (not key or not key.strip()) and not allow_open:
        raise ValueError("Set HERDR_CP_INGEST_KEY to an ingest-role API key")
    try:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError
        url = base_url.rstrip("/") + "/api/v1/ingest"
    except ValueError:
        raise ValueError("Invalid control-plane base URL for HERDR_CP_INGEST_KEY") from None
    opener = urllib.request.build_opener(_NoRedirect())
    count = 0
    for envelope in envelopes:
        try:
            request = urllib.request.Request(
                url, data=json.dumps(envelope).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json", **({"X-API-Key": key} if key else {})},
            )
            with opener.open(request, timeout=10):
                count += 1
        except (OSError, ValueError, HTTPException) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            raise RuntimeError("Snapshot ingest failed; check endpoint and HERDR_CP_INGEST_KEY") from None
    return count
