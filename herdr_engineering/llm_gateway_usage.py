"""Token accounting for traffic that went through the InferWeave gateway.

The gateway keeps cumulative counts per caller principal and model (``GET /v1/admin/usage``:
requests, completed, reported input/output tokens; no prompt text). A caller's principal is
``herdr:<project>:<key_id>`` (see ``llm_gateway_bridge``), so each row maps straight back to the
project key that made the calls. This module turns the growth of those counters into the same
per-request ``token_usage`` events the python proxy reports, tagged ``source="llm-gateway"``.

Counters restart with the gateway; ``startedAt`` tells the two cases apart, and a counter that
shrinks is treated as a restart too. The baseline is saved only after the control plane accepted
the events, so a failed report is retried next pass rather than lost (a crash between the two
steps can repeat one batch, never drop one).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import urllib.request
from pathlib import Path

from .llm_proxy import ControlPlaneClient

log = logging.getLogger("herdr.llm_gateway_usage")

SOURCE = "llm-gateway"
_PREFIX = "herdr:"
_FIELDS = ("requests", "completed", "inputTokens", "outputTokens")
_BATCH = 500


def fetch_usage(gateway_url: str, admin_secret: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(gateway_url.rstrip("/") + "/v1/admin/usage",
                                 headers={"x-inferweave-admin": admin_secret})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read())


def _split(principal: str) -> tuple[str, str] | None:
    """``herdr:<project>:<key_id>`` -> (project, key_id); anything else is not ours."""
    if not principal.startswith(_PREFIX):
        return None
    project, _, key_id = principal[len(_PREFIX):].rpartition(":")
    return (project, key_id) if project and key_id else None


def _cell(row: dict) -> dict[str, int]:
    return {f: int(row.get(f) or 0) for f in _FIELDS}


def deltas(report: dict, state: dict) -> tuple[dict, dict]:
    """(growth per ``principal|model``, the baseline to save once it is reported)."""
    restarted = state.get("startedAt") != report.get("startedAt")
    before = {} if restarted else state.get("rows", {})
    growth, after = {}, {}
    for row in report.get("usage", []):
        cell = _cell(row)
        name = f"{row.get('principal')}|{row.get('model')}"
        after[name] = cell
        prev = before.get(name, dict.fromkeys(_FIELDS, 0))
        if any(cell[f] < prev[f] for f in _FIELDS):
            prev = dict.fromkeys(_FIELDS, 0)
        grew = {f: cell[f] - prev[f] for f in _FIELDS}
        if any(grew.values()):
            growth[name] = grew
    return growth, {"startedAt": report.get("startedAt"), "rows": after}


def events_for(growth: dict) -> list[dict]:
    """One event per request: tokens ride on the completed ones, the rest are failures."""
    out: list[dict] = []
    for name, g in sorted(growth.items()):
        principal, _, model = name.partition("|")
        who = _split(principal)
        if who is None or g["requests"] <= 0:
            continue
        project, key_id = who
        ok = min(g["completed"], g["requests"])
        bearers = ok or 1
        for i in range(g["requests"]):
            success = i < ok
            carrier = i < bearers
            # An even split, remainder to the first, so the totals are exact.
            def share(total):
                return total // bearers + (total % bearers if i == 0 else 0) if carrier else 0
            out.append({"project": project, "key_id": key_id, "model": model,
                        "success": success, "source": SOURCE,
                        "input_tokens": share(g["inputTokens"]),
                        "output_tokens": share(g["outputTokens"])})
    return out


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".usage-state-")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(state, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def sync_once(control: ControlPlaneClient, fetch, state_path: Path) -> int | None:
    """Report what grew since the last pass; the number of events, None on any failure."""
    try:
        report = fetch()
    except Exception as exc:  # noqa: BLE001
        log.warning("gateway usage unavailable: %s", exc)
        return None
    growth, baseline = deltas(report, _load(state_path))
    events = events_for(growth)
    try:
        for start in range(0, len(events), _BATCH):
            control.report_batch(events[start:start + _BATCH])
    except Exception as exc:  # noqa: BLE001
        log.warning("control plane refused usage report: %s", exc)
        return None
    _save(state_path, baseline)
    return len(events)


def run(control: ControlPlaneClient, fetch, state_path: Path, *, interval: float = 30.0,
        sleep=time.sleep, stop=lambda: False) -> None:
    while not stop():
        sync_once(control, fetch, state_path)
        sleep(interval)
