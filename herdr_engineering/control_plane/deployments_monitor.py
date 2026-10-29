"""Deployment event store fed by the InferWeave host updaters (deploy-4 Task 1).

Hosts report every step of a pull deploy as one event (``deploy/pipelines/
deploy_event.py`` in InferWeave/inferweave)::

    {id, ts, host, component, step, outcome, from, to, evidence}

and POST it to ``/api/deployments/events`` with their per-host fleet token. This
module validates, de-duplicates and projects those events into one row per
component. Snapshots are last-known observations reported by hosts, never
execution authority.

Bounds: a fixed number of components, a fixed timeline per component, a bounded
dedupe memory, and capped evidence. Evidence is untrusted text: it was redacted on
the host, and is redacted again here before it is stored or served.

Persistence follows ``host_metrics_store``: accepted events go through
``persist`` (the repo's durable sink) as a private ``deployment`` stream that
``hydrate(events)`` replays after a restart; they never enter the shared feed.
"""
from __future__ import annotations

import copy
import re
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime

from .redact import redact_text

#: The emitter's outcome vocabulary (deploy_event.OUTCOMES).
OUTCOMES = ("started", "ok", "refused", "failed", "rolled-back", "skipped")
FIELDS = ("id", "ts", "host", "component", "step", "outcome", "from", "to", "evidence")
REQUIRED = ("ts", "host", "component", "step", "outcome")

#: Largest accepted request body. The emitter caps evidence at 8192 characters;
#: JSON escaping can grow that several-fold, so leave generous headroom.
MAX_REQUEST_BYTES = 64 * 1024
#: Stored evidence cap (the emitter's own cap), tail kept.
MAX_EVIDENCE = 8192
TRUNC_MARK = "[earlier output truncated]\n"
TIMELINE_LIMIT = 50
MAX_COMPONENTS = 64
#: At least the emitter's spool (5000 lines), so a full spool replay stays idempotent.
SEEN_LIMIT = 10_000
HOLD_S = 600

ENTITY_TYPE = "deployment"
EVENT_TYPE = "DeploymentEvent"

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_ID = re.compile(r"[0-9a-f]{1,64}")
_COMPONENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_STEP_SUFFIX = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}")
_COMMIT = re.compile(r"[A-Za-z0-9._-]{0,128}")

_R = "[REDACTED]"
# Supplements to the shared ``redact_text``: it covers db/s3 connection strings but
# not https userinfo, and it ignores short values after a ``key=``-style name.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,20}://)[^/\s@]{1,1024}@")
_SECRET_PAIR = re.compile(
    r"(?i)(\b[a-z0-9_.-]{0,64}(?:key|token|secret|passw(?:or)?d|pass|pwd|credential|auth))"
    r"([\"']?[ \t]{0,16}[=:][ \t]{0,16})(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;&\"']+)")


class DeploymentEventError(ValueError):
    """A malformed deployment event; the message names the offending field."""


def redact_evidence(text: str) -> str:
    """Second-layer redaction for untrusted evidence text (host already redacted once)."""
    if not text:
        return text
    out = redact_text(text)
    out = _URL_USERINFO.sub(lambda m: m.group(1) + _R + "@", out)
    out = _SECRET_PAIR.sub(lambda m: m.group(1) + m.group(2) + _R, out)
    return out


def _cap(text: str) -> str:
    """Keep the tail (the end of a log is the failure), marked at the front."""
    if len(text) <= MAX_EVIDENCE:
        return text
    tail = text[-(MAX_EVIDENCE - len(TRUNC_MARK)):]
    nl = tail.find("\n", 0, 256)
    if nl >= 0:
        tail = tail[nl + 1:]
    return TRUNC_MARK + tail


def _fail(field, why):
    raise DeploymentEventError(f"deployment event: {field} {why}")


def validate_event(event) -> dict:
    """Return a clean copy of ``event`` (only the emitter's fields), or raise."""
    if not isinstance(event, dict):
        raise DeploymentEventError("deployment event must be a JSON object")
    unknown = sorted(str(k) for k in event if k not in FIELDS)
    if unknown:
        _fail(", ".join(unknown)[:200], "is not a deployment event field")
    for field in REQUIRED:
        if field not in event:
            _fail(field, "is required")
    for field in FIELDS:
        if field in event and not isinstance(event[field], str):
            _fail(field, "must be a string")
    ev = {k: event.get(k, "") for k in FIELDS}
    if "id" in event and not _ID.fullmatch(ev["id"]):
        _fail("id", "must be 1-64 lowercase hex characters")
    try:
        datetime.strptime(ev["ts"], _TS_FORMAT)
    except ValueError:
        _fail("ts", f"must be UTC in the form {_TS_FORMAT}")
    if not _HOST.fullmatch(ev["host"]):
        _fail("host", "must be a hostname")
    if not _COMPONENT.fullmatch(ev["component"]):
        _fail("component", "must match [a-z0-9][a-z0-9_-]{0,63}")
    prefix = f"deployment.{ev['component']}."
    if not ev["step"].startswith(prefix) or not _STEP_SUFFIX.fullmatch(ev["step"][len(prefix):]):
        _fail("step", f"must be {prefix}<step>")
    if ev["outcome"] not in OUTCOMES:
        _fail("outcome", "must be one of " + "|".join(OUTCOMES))
    for field in ("from", "to"):
        if not _COMMIT.fullmatch(ev[field]):
            _fail(field, "must be a commit id of at most 128 characters")
    ev["evidence"] = _cap(redact_evidence(ev["evidence"]))
    if "id" not in event:
        del ev["id"]
    return ev


def _epoch(ts: str) -> float:
    return datetime.strptime(ts, _TS_FORMAT).replace(tzinfo=UTC).timestamp()


class DeploymentsMonitor:
    def __init__(self, *, now=None, persist=None, hold_s=HOLD_S, timeline_limit=TIMELINE_LIMIT,
                 max_components=MAX_COMPONENTS, seen_limit=SEEN_LIMIT):
        self._now = now or time.time
        self._persist = persist
        self._hold_s = hold_s
        self._timeline_limit = max(1, int(timeline_limit))
        self._max_components = max(1, int(max_components))
        self._seen_limit = max(1, int(seen_limit))
        self._lock = threading.Lock()
        self._seen: OrderedDict = OrderedDict()
        self._components: dict[str, dict] = {}
        self._seq = 0

    # ---------------------------------------------------------------- write
    def ingest(self, event: dict, *, reported_by: str | None = None) -> None:
        """Validate and store one event. Duplicates are ignored; malformed raise ValueError."""
        ev = validate_event(event)
        if reported_by is not None:
            ev["reported_by"] = str(reported_by)[:253]
        with self._lock:
            if not self._apply(ev):
                return
            record = copy.deepcopy(ev)
        if self._persist is not None:
            try:
                self._persist({"entity_type": ENTITY_TYPE, "entity_id": ev["component"],
                               "event_type": EVENT_TYPE, "sequence": record["seq"],
                               "payload": record})
            except Exception:  # noqa: BLE001 - durable history is best-effort, as for host metrics
                pass

    @staticmethod
    def _keys(ev):
        keys = [("tuple", ev["host"], ev["ts"], ev["component"], ev["step"], ev["outcome"])]
        if ev.get("id"):
            keys.append(("id", ev["id"]))
        return keys

    def _apply(self, ev) -> bool:
        """Add a validated event under the lock; False when it is a duplicate."""
        keys = self._keys(ev)
        if any(k in self._seen for k in keys):
            return False
        row = self._components.get(ev["component"])
        if row is None:
            if len(self._components) >= self._max_components:
                raise DeploymentEventError(
                    f"deployment event: too many components (limit {self._max_components})")
            row = self._components[ev["component"]] = {
                "timeline": [], "latest": None, "health_event": None,
                "running_event": None, "target_event": None, "terminal": None}
        for k in keys:
            self._seen[k] = None
        while len(self._seen) > self._seen_limit:
            self._seen.popitem(last=False)
        self._seq = max(self._seq + 1, int(ev.get("seq") or 0))
        ev["seq"] = self._seq
        order = (ev["ts"], ev["seq"])
        timeline = row["timeline"]
        timeline.append(ev)
        timeline.sort(key=lambda e: (e["ts"], e["seq"]))
        del timeline[:-self._timeline_limit]

        def newer(slot):
            cur = row[slot]
            if cur is None or order >= (cur["ts"], cur["seq"]):
                row[slot] = ev

        newer("latest")
        outcome = ev["outcome"]
        if outcome != "skipped":
            newer("health_event")
        if outcome != "started":
            newer("terminal")
        if outcome in ("ok", "rolled-back"):
            newer("running_event")
        if outcome == "started":
            newer("target_event")
        return True

    # ----------------------------------------------------------------- read
    def snapshot(self) -> dict:
        now = self._now()
        with self._lock:
            out = {}
            for name, row in sorted(self._components.items()):
                out[name] = self._project(row, now)
            return {"components": copy.deepcopy(out)}

    def _project(self, row, now):
        latest, running, target = row["latest"], row["running_event"], row["target_event"]
        health_ev, terminal = row["health_event"], row["terminal"]
        health = "unknown"
        if health_ev is not None:
            health = {"started": "updating", "ok": "healthy"}.get(health_ev["outcome"], "unhealthy")
        hold = None
        if health == "healthy":
            hold = max(0, int(self._hold_s - (now - _epoch(health_ev["ts"]))))
        if target is None or (running is not None and (running["ts"], running["seq"])
                              > (target["ts"], target["seq"])):
            target = running
        return {
            "running": (running["to"] or None) if running else (latest["from"] or None),
            "target": (target["to"] or None) if target else (latest["to"] or None),
            "step": latest["step"],
            "health": health,
            "last_result": None if terminal is None else {
                k: terminal[k] for k in ("outcome", "step", "ts", "from", "to")},
            "hold_remaining_s": hold,
            "timeline": [{k: v for k, v in e.items() if k != "seq"} for e in row["timeline"]],
        }

    # -------------------------------------------------------------- restore
    def hydrate(self, events: list[dict]) -> int:
        """Replay persisted events (no re-persist); invalid rows are skipped. Returns the count."""
        applied = 0
        ordered = sorted(events, key=lambda e: int(e.get("sequence") or 0))
        for row in ordered:
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            raw = {k: v for k, v in payload.items() if k in FIELDS}
            try:
                ev = validate_event(raw)
            except ValueError:
                continue
            if isinstance(payload.get("reported_by"), str):
                ev["reported_by"] = payload["reported_by"][:253]
            if isinstance(payload.get("seq"), int):
                ev["seq"] = payload["seq"]
            with self._lock:
                try:
                    applied += bool(self._apply(ev))
                except ValueError:
                    continue
        return applied
