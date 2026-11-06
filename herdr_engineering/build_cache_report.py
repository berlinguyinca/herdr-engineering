"""Report this host's recorded builds to the control plane (``POST /api/v1/host/build-cache/report``).

Only the non-secret summary fields of ``builds.jsonl`` leave the host (the same allowlist the control plane applies):
never argv, environment, cwd or paths. The caller supplies the control-plane URL and an ingest-role API key (read from
YAML or a secret file by whoever schedules this); nothing here reads environment variables. Not wired into a timer or
the CLI yet; call it from a service loop when the control plane has ``build_cache.ingest.enabled``.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

from .control_plane.build_cache_routes import INGEST_MAX_RECORDS, _slim

_TAIL_BYTES = 2 * 1024 * 1024
_EVENTS = ("build_started", "build_finished")


def collect(state_dir: Path | str, limit: int = INGEST_MAX_RECORDS) -> list[dict]:
    """The last ``limit`` build_started/build_finished records, slimmed to the allowlisted fields."""
    try:
        with open(Path(state_dir) / "builds.jsonl", "rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - _TAIL_BYTES))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("event") in _EVENTS and isinstance(rec.get("build_id"), str):
            out.append(_slim(rec))
    return out[-min(limit, INGEST_MAX_RECORDS):]


def report_builds(state_dir: Path | str, base_url: str, api_key: str, *, host_id: str | None = None,
                  limit: int = INGEST_MAX_RECORDS, timeout: float = 10.0) -> dict:
    """POST the recent window; returns the server's JSON answer. Raises ``OSError`` on transport or HTTP failure."""
    body = json.dumps({"host_id": host_id or socket.gethostname(), "builds": collect(state_dir, limit)}).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/api/v1/host/build-cache/report", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise OSError(f"control plane refused the report: HTTP {exc.code}") from None
