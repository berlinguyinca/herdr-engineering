"""Deployment event store fed by the InferWeave host updaters (deploy-4 Task 1).

Hosts report every step of a pull deploy as one event (``deploy/pipelines/
deploy_event.py`` in InferWeave/inferweave)::

    {id, ts, host, component, step, outcome, from, to, evidence}

and POST it to ``/api/deployments/events`` with their per-host fleet token. This
module validates, de-duplicates and projects those events into one row per
component. Snapshots are last-known observations reported by hosts, never
execution authority.

Time: the host's ``ts`` is kept as ``host_ts`` but never trusted for ordering or
the stage hold. Each event is stamped ``received_at`` on arrival; "latest" is
arrival order and the hold runs from the arrival of the ``ok``, so a host clock
running behind cannot shorten the hold and one running ahead cannot freeze a row.
A late or replayed event can only make the hold longer. The difference between
the two clocks is shown as ``clock_skew_s``.

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

from .deployment_redact import _drop_partial_start
from .deployment_redact import redact as _emitter_redact
from .redact import redact_text

#: The emitter's outcome vocabulary (deploy_event.OUTCOMES).
OUTCOMES = ("started", "ok", "refused", "failed", "rolled-back", "skipped")
FIELDS = ("id", "ts", "host", "component", "step", "outcome", "from", "to", "evidence")
REQUIRED = ("ts", "host", "component", "step", "outcome")

#: Largest accepted request body. The emitter caps evidence at 8192 characters and
#: sends ``json.dumps`` with ensure_ascii, so one astral character costs 12 bytes
#: (a surrogate pair): ~98 KB of evidence alone. A refused POST is lost (the emitter
#: only warns), so leave headroom above that worst case.
MAX_REQUEST_BYTES = 128 * 1024
#: Stored evidence cap (the emitter's own cap), tail kept.
MAX_EVIDENCE = 8192
TRUNC_MARK = "[earlier output truncated]\n"
#: Evidence longer than this is cut to its tail BEFORE redaction, so redaction work is
#: bounded whatever a host sends. The emitter sends at most MAX_EVIDENCE characters;
#: the margin keeps a key that sits just before the final cap inside the window.
PRE_REDACT_CAP = MAX_EVIDENCE + 1024
TIMELINE_LIMIT = 50
MAX_COMPONENTS = 64
#: Dedupe keys kept (one per event: its id, else its tuple): 20,000 events fleet-wide,
#: four full emitter spools (5000 lines each).
SEEN_LIMIT = 20_000
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
# Supplements: the shared ``redact_text`` covers db/s3 connection strings but not
# https userinfo, and it ignores short values after a ``key=``-style name.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,20}://)[^/\s@]{1,1024}@")
_SECRET_PAIR = re.compile(
    r"(?i)(\b[a-z0-9_.-]{0,64}(?:key|token|secret|passw(?:or)?d|pass|pwd|credential|auth))"
    r"([\"']?[ \t]{0,16}[=:][ \t]{0,16})(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;&\"']+)")


class DeploymentEventError(ValueError):
    """A malformed deployment event; the message names the offending field."""


def redact_evidence(text: str) -> str:
    """Second-layer redaction for untrusted evidence text (host already redacted once).

    The emitter's own rule set (vendored in ``deployment_redact``, which also bounds
    the input to its last 64 KiB) runs first, then the shared ``redact_text``, then
    two local supplements.
    """
    if not text:
        return text
    if len(text) > PRE_REDACT_CAP:
        # Drop the (possibly partial) first line too: a value whose key was cut off
        # would no longer look like a secret.
        text = TRUNC_MARK + _drop_partial_start(text[-PRE_REDACT_CAP:])
    out = redact_text(_emitter_redact(text))
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


def _stamp(ev: dict, received: float) -> dict:
    """Keep the host's ts as ``host_ts``; stamp herdr's arrival time (``_rx`` is internal)."""
    ev["host_ts"] = ev.pop("ts")
    ev["_rx"] = received
    ev["received_at"] = datetime.fromtimestamp(received, UTC).strftime(_TS_FORMAT)
    return ev


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
        ev = _stamp(validate_event(event), self._now())
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
    def _key(ev):
        """The event id when present (distinct ids are distinct events, even in one second);
        otherwise the tuple, which includes outcome because started/ok share a step and a ts."""
        if ev.get("id"):
            return ("id", ev["id"])
        return ("tuple", ev["host"], ev["host_ts"], ev["component"], ev["step"], ev["outcome"])

    def _apply(self, ev) -> bool:
        """Add a stamped event under the lock; False when it is a duplicate."""
        key = self._key(ev)
        if key in self._seen:
            return False
        row = self._components.get(ev["component"])
        if row is None:
            if len(self._components) >= self._max_components:
                raise DeploymentEventError(
                    f"deployment event: too many components (limit {self._max_components})")
            row = self._components[ev["component"]] = {
                "timeline": [], "latest": None, "health_event": None,
                "running_event": None, "target_event": None, "terminal": None,
                "from_event": None}
        self._seen[key] = None
        while len(self._seen) > self._seen_limit:
            self._seen.popitem(last=False)
        self._seq = max(self._seq + 1, int(ev.get("seq") or 0))
        ev["seq"] = self._seq
        order = (ev["_rx"], ev["seq"])
        timeline = row["timeline"]
        timeline.append(ev)
        timeline.sort(key=lambda e: (e["_rx"], e["seq"]))
        del timeline[:-self._timeline_limit]

        def newer(slot):
            cur = row[slot]
            if cur is None or order >= (cur["_rx"], cur["seq"]):
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
        if ev["from"]:
            newer("from_event")
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
        health_ev, terminal, from_ev = row["health_event"], row["terminal"], row["from_event"]
        health = "unknown"
        if health_ev is not None:
            health = {"started": "updating", "ok": "healthy"}.get(health_ev["outcome"], "unhealthy")
        hold = None
        if health == "healthy":
            # From arrival, never from the host's clock (a clock behind would skip the hold).
            hold = min(self._hold_s, max(0, int(self._hold_s - (now - health_ev["_rx"]))))
        if target is None or (running is not None and (running["_rx"], running["seq"])
                              > (target["_rx"], target["seq"])):
            target = running
        if running is not None:
            current = running["to"] or None
        else:
            current = from_ev["from"] if from_ev is not None else None
        return {
            "running": current,
            "target": (target["to"] or None) if target else (latest["to"] or None),
            "step": latest["step"],
            "health": health,
            "last_result": None if terminal is None else {
                k: terminal[k] for k in ("outcome", "step", "host_ts", "received_at", "from", "to")},
            "hold_remaining_s": hold,
            # Host clock minus herdr's clock at the latest event (positive: host ahead).
            "clock_skew_s": int(_epoch(latest["host_ts"]) - latest["_rx"]),
            "timeline": [{k: v for k, v in e.items() if k not in ("seq", "_rx")}
                         for e in row["timeline"]],
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
            raw["ts"] = payload.get("host_ts", payload.get("ts"))
            rx = payload.get("_rx")
            try:
                ev = validate_event(raw)
                if isinstance(rx, bool) or not isinstance(rx, (int, float)):
                    rx = _epoch(ev["ts"])
                ev = _stamp(ev, float(rx))
            except (TypeError, ValueError, OverflowError):
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
