"""Deployment actions (deploy-4 Task 5): audited force redeploy, the host permit gate, and
the operator acknowledgement of a stale instance.

Hosts pull, and herdr never holds a production credential: a force redeploy only RECORDS
a request. The host updater reads it with its fleet host token, asks the permit endpoint,
clears the request (that is the "once") and redeploys by itself.

Operator side (``POST /api/deployments/force`` and ``/ack``, also under ``/api/v1``):
KEYED MODE ONLY (an open tailnet server has no credential a DNS-rebound page lacks, so it
refuses them), the operator role (X-API-Key), a Host an attacker cannot rebind
(``_dashboard_host_ok``), no cross-site ``Sec-Fetch-Site``, a same-origin ``Origin``,
application/json, a small body cap and the hot ``control_plane.deployment_actions: true``
flag (fail closed). Auditing:

* accepted actions: a durable row (``AUDIT_ENTITY``) BEFORE the request is stored or the
  ack applied (no audit, no action: 503);
* refusals of an AUTHENTICATED operator (browser guards, bad body, unknown target, flag
  off): a best-effort row kept out of the shared feed, at most ``REFUSED_ROWS_PER_MIN``
  a minute (the rest are counted);
* refusals before authentication (open mode, no or a bad key, a non-operator role, a host
  token on an operator route): NO row. They are an in-memory counter per reason
  (``RefusalCounter``, shown in the Deployments view under ``actions.refused``) and a log
  line at most once a minute per reason, so an unauthenticated flood writes nothing.

Rows go to one stream per action per UTC day (``force:2026-10-29``), which bounds the
per-stream sequence scan ``record_event`` does. Audit values come from validated fields only.

Host side (fleet host token, ``Authorization: Bearer`` or ``X-Host-Token``; it grants
these routes and nothing else):

* ``GET /api/deployments/requests?host=`` lists the pending force entries for the
  components DEFINED for that host. A stage force is expanded at POST time into one entry
  per component, so each host clears only its own.
* ``DELETE /api/deployments/requests/<id>`` clears that host's entries of the request.
* ``GET /api/deployments/permit?component=&commit=[&generation=]`` answers ``{permitted, reason,
  generation, target_commit, forced, request_id[, repair]}`` from the newest coordinator tick,
  with no side effects. ``repair`` (Task 8, additive) is ``{id, action: retry|restart|rollback,
  attempt, reason, restart?, rollback_to?}``: herdr's one decided repair for a failed
  component on the target. Hosts read it independently of ``permitted``, act on each ``id``
  once, and refuse a ``rollback_to`` they do not hold locally. It is NOT behind the flag (a host gate that 404s would freeze every
  gated host); with the flag off, pending forces are ignored and the list is empty (the
  kill switch).

Permit rule (``permit_decision``): any doubt is ``permitted: false`` with a reason. No
tick, a stale tick (flag, error, or older than ``PERMIT_STALE_S``), no plan or target for
the component's track, a commit other than the plan's target, or a ``generation`` other
than the plan's (when the host sends one): false. A component in
the tick's ``permitted`` is permitted. Otherwise a pending force for that component whose
captured target is the same commit (a replan onto another commit voids it) permits it,
but only where the plan is held by STAGING - a Start whose turn has not come, a hold
timer (``Hold`` with ``remaining_s``) or a halt of the component's OWN stage (a halt in an
earlier stage still blocks a later stage's force) - and only
when the same tick saw the release confirmed AND the target on main. A force never
overrides "release not confirmed", "target no longer on main", an earlier-release or
unknown-ancestry hold, or a coordinator error.
"""
from __future__ import annotations

import copy
import re
import secrets
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime

from .deployment_coordinator import same_commit

#: Durable request store: a private stream, like the other deployment entities.
ENTITY_TYPE = "deployment_request"
#: The audit trail: ordinary durable history (``repo.record_event``).
AUDIT_ENTITY = "deployment_action"
REQUEST_EVENT = "DeploymentForceRecorded"
CLEARED_EVENT = "DeploymentForceCleared"

MAX_BODY_BYTES = 4096
#: Pending requests kept; a new one beyond this is refused (429), never evicting another.
MAX_PENDING = 256
#: A request nobody picked up expires after this long.
REQUEST_TTL_S = 24 * 3600
#: A permit answer needs a tick at most this old (the tick runs every 30 s).
PERMIT_STALE_S = 90
#: Durable rows for an authenticated operator's refusals per minute; the rest are counted.
REFUSED_ROWS_PER_MIN = 60

COMPONENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
INSTANCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
COMMIT = re.compile(r"[A-Za-z0-9._-]{7,128}")
REQUEST_ID = re.compile(r"force-[0-9a-f]{16}")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_STAGE = 99


class ActionRefused(Exception):
    """A refusal with its HTTP status; ``target`` is the validated target (None if none)."""

    def __init__(self, status, message, target=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.target = target


def stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ validation
def parse_force(body):
    """``{"component": name}`` or ``{"stage": n}``, exactly one -> ("component"|"stage", value)."""
    if not isinstance(body, dict):
        raise ActionRefused(400, "the body must be a JSON object")
    if set(body) - {"component", "stage"} or len(body) != 1:
        raise ActionRefused(400, "give exactly one of component or stage")
    if "component" in body:
        name = body["component"]
        if not isinstance(name, str) or not COMPONENT.fullmatch(name):
            raise ActionRefused(400, "component must be a component name")
        return "component", name
    stage = body["stage"]
    if not isinstance(stage, int) or isinstance(stage, bool) or not 1 <= stage <= MAX_STAGE:
        raise ActionRefused(400, "stage must be a stage number")
    return "stage", stage


def parse_ack(body):
    """``{component, instance[, release]}``, or ``{component, release}`` to lift a
    failure-evicted mark -> (component, instance|None, release|None)."""
    if not isinstance(body, dict):
        raise ActionRefused(400, "the body must be a JSON object")
    if set(body) - {"component", "instance", "release"} or "component" not in body:
        raise ActionRefused(400, "give component, instance and optionally release")
    name, instance, release = body["component"], body.get("instance"), body.get("release")
    if not isinstance(name, str) or not COMPONENT.fullmatch(name):
        raise ActionRefused(400, "component must be a component name")
    if instance is not None and (not isinstance(instance, str) or not INSTANCE.fullmatch(instance)):
        raise ActionRefused(400, "instance must be an instance id")
    if release is not None and (not isinstance(release, str) or not COMMIT.fullmatch(release)):
        raise ActionRefused(400, "release must be a commit")
    if instance is None and release is None:
        raise ActionRefused(400, "give an instance (or a release to lift a failure-evicted mark)")
    return name, instance, release


# ------------------------------------------------------------------ the store
def _valid_entry(e):
    return (isinstance(e, dict) and isinstance(e.get("target_commit"), str)
            and COMMIT.fullmatch(e["target_commit"])
            and isinstance(e.get("generation"), int) and not isinstance(e.get("generation"), bool))


class ForceRequests:
    """Pending force redeploy requests: bounded by count and age, idempotent for an
    identical pending request, persisted through ``persist`` (the repo's private sink) and
    replayed by ``hydrate``. A request is {id, type, target, requested_at, actor,
    components: {component: {target_commit, generation, cleared_at, cleared_by}}}."""

    def __init__(self, *, persist=None, persist_required=None, now=None):
        self._persist = persist
        self._persist_required = persist_required
        self._now = now or time.time
        self._lock = threading.Lock()
        self._requests: OrderedDict[str, dict] = OrderedDict()
        self._seq = 0

    # ------------------------------------------------------------- reads
    def _purge(self, now):
        for rid in [rid for rid, r in self._requests.items()
                    if now - r["requested_at"] > REQUEST_TTL_S
                    or all(e["cleared_at"] is not None for e in r["components"].values())]:
            del self._requests[rid]

    def pending(self) -> list:
        with self._lock:
            self._purge(self._now())
            return [copy.deepcopy(r) for r in self._requests.values()]

    def entries(self, components) -> list:
        """Uncleared entries for ``components`` (the asking host's), oldest first."""
        allowed = set(components)
        out = []
        with self._lock:
            self._purge(self._now())
            for r in self._requests.values():
                for name, e in sorted(r["components"].items()):
                    if name in allowed and e["cleared_at"] is None:
                        out.append({"id": r["id"], "type": r["type"], "target": r["target"],
                                    "component": name, "target_commit": e["target_commit"],
                                    "generation": e["generation"],
                                    "requested_at": stamp(r["requested_at"])})
        return out

    def active(self, component, commit):
        """The oldest pending entry forcing ``component`` to ``commit`` -> request id, or None."""
        with self._lock:
            self._purge(self._now())
            for r in self._requests.values():
                e = r["components"].get(component)
                if e is not None and e["cleared_at"] is None and same_commit(e["target_commit"], commit):
                    return r["id"]
        return None

    # ------------------------------------------------------------ writes
    def find(self, kind, target, components):
        """An identical pending request (same type, target and per-component commit and
        generation, nothing cleared yet), or None."""
        want = {c: (commit, gen) for c, (commit, gen) in components.items()}
        with self._lock:
            self._purge(self._now())
            for r in self._requests.values():
                have = {c: (e["target_commit"], e["generation"]) for c, e in r["components"].items()}
                if (r["type"] == kind and r["target"] == target and have == want
                        and all(e["cleared_at"] is None for e in r["components"].values())):
                    return copy.deepcopy(r)
        return None

    def check_capacity(self):
        with self._lock:
            self._purge(self._now())
            if len(self._requests) >= MAX_PENDING:
                raise ActionRefused(429, f"too many pending force requests (at most {MAX_PENDING})")

    @staticmethod
    def new_id() -> str:
        return "force-" + secrets.token_hex(8)

    def add(self, kind, target, components, actor, rid=None) -> dict:
        """Record a new request. ``components``: {component: (target_commit, generation)}."""
        with self._lock:
            now = self._now()
            self._purge(now)
            if len(self._requests) >= MAX_PENDING:
                raise ActionRefused(429, f"too many pending force requests (at most {MAX_PENDING})")
            rid = rid if isinstance(rid, str) and REQUEST_ID.fullmatch(rid) else self.new_id()
            req = {"id": rid, "type": kind, "target": target, "requested_at": float(now),
                   "actor": actor,
                   "components": {c: {"target_commit": commit, "generation": gen,
                                      "cleared_at": None, "cleared_by": None}
                                  for c, (commit, gen) in sorted(components.items())}}
            self._requests[rid] = req
            record = self._record(REQUEST_EVENT, {"kind": "request", **{
                k: v for k, v in req.items() if k != "components"},
                "components": {c: {"target_commit": e["target_commit"],
                                   "generation": e["generation"]}
                               for c, e in req["components"].items()}})
            out = copy.deepcopy(req)
        self._emit(record)
        return out

    def clear(self, rid, components, host) -> list:
        """Spend ``rid``'s uncleared entries among ``components`` (the asking host's), and
        with them EVERY other uncleared entry forcing the same component to the same commit
        (a component force and a stage force overlap: one pickup spends both, so a force is
        granted once). The clear is one record, persisted DURABLY before it is applied:
        ``persist_required`` raising leaves every entry pending and propagates (the route
        answers 503, which the host reads as "do not redeploy"). Returns the components
        cleared ([] when ``rid`` has none of the host's)."""
        allowed = set(components)
        with self._lock:
            now = self._now()
            self._purge(now)
            req = self._requests.get(rid)
            if req is None:
                return []
            spent = {name: e["target_commit"] for name, e in req["components"].items()
                     if name in allowed and e["cleared_at"] is None}
            if not spent:
                return []
            pairs = [[r["id"], name] for r in self._requests.values()
                     for name, e in sorted(r["components"].items())
                     if name in spent and e["cleared_at"] is None
                     and same_commit(e["target_commit"], spent[name])]
            record = self._record(CLEARED_EVENT, {"kind": "cleared", "id": rid, "entries": pairs,
                                                  "host": host, "at": float(now)})
            if self._persist_required is not None:
                self._persist_required(copy.deepcopy(record))      # raises: nothing applied
            for other, name in pairs:
                self._requests[other]["components"][name].update(cleared_at=float(now), cleared_by=host)
            self._purge(now)
        return sorted(spent)

    # ------------------------------------------------------- persistence
    def _record(self, event_type, payload):
        self._seq += 1
        return {"entity_type": ENTITY_TYPE, "entity_id": payload["id"], "event_type": event_type,
                "sequence": self._seq, "payload": {**payload, "seq": self._seq}}

    def _emit(self, record):
        if self._persist is None:
            return
        try:
            self._persist(copy.deepcopy(record))
        except Exception:  # noqa: BLE001 - the audit row is the durable record; this is best effort
            pass

    def hydrate(self, rows) -> int:
        """Replay persisted records (no re-persist); malformed and expired ones are skipped."""
        def seq_of(r):
            p = r.get("payload") if isinstance(r, dict) else None
            s = p.get("seq") if isinstance(p, dict) else None
            return s if isinstance(s, int) and not isinstance(s, bool) else 0

        applied = 0
        with self._lock:
            for row in sorted((r for r in rows if isinstance(r, dict)), key=seq_of):
                p = row.get("payload")
                if not isinstance(p, dict):
                    continue
                self._seq = max(self._seq, seq_of(row))
                applied += self._replay(p)
            self._purge(self._now())
        return applied

    def _replay(self, p) -> int:
        rid = p.get("id")
        if not isinstance(rid, str) or not REQUEST_ID.fullmatch(rid):
            return 0
        if p.get("kind") == "request":
            kind, target, at, actor, comps = (p.get(k) for k in (
                "type", "target", "requested_at", "actor", "components"))
            if (kind not in ("component", "stage") or isinstance(at, bool)
                    or not isinstance(at, (int, float)) or not isinstance(actor, str)
                    or len(actor) > 200 or not isinstance(comps, dict) or not comps
                    or len(comps) > 64 or rid in self._requests):
                return 0
            if kind == "component" and (not isinstance(target, str) or not COMPONENT.fullmatch(target)):
                return 0
            if kind == "stage" and (not isinstance(target, int) or isinstance(target, bool)):
                return 0
            if not all(isinstance(c, str) and COMPONENT.fullmatch(c) and _valid_entry(e)
                       for c, e in comps.items()):
                return 0
            if self._now() - at > REQUEST_TTL_S:
                return 0
            self._requests[rid] = {
                "id": rid, "type": kind, "target": target, "requested_at": float(at), "actor": actor,
                "components": {c: {"target_commit": e["target_commit"], "generation": e["generation"],
                                   "cleared_at": None, "cleared_by": None}
                               for c, e in sorted(comps.items())}}
            return 1
        if p.get("kind") == "cleared":
            entries, host, at = p.get("entries"), p.get("host"), p.get("at")
            if (not isinstance(entries, list) or len(entries) > MAX_PENDING * 64
                    or not isinstance(host, str) or isinstance(at, bool)
                    or not isinstance(at, (int, float))):
                return 0
            applied = 0
            for pair in entries:
                if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair)):
                    continue
                req = self._requests.get(pair[0])
                if req is not None and pair[1] in req["components"]:
                    req["components"][pair[1]].update(cleared_at=float(at), cleared_by=host[:64])
                    applied = 1
            return applied
        return 0


# ------------------------------------------------------------------ the gate
def _deny(reason, view=None, **extra):
    return {"permitted": False, "reason": reason,
            "generation": (view or {}).get("generation"),
            "target_commit": (view or {}).get("target_commit"),
            "forced": False, "request_id": None, **extra}


def _overridable(current, component_stage) -> bool:
    """True when the plan is held by staging (a force may override it), not by a gate. A
    Halt is overridable only for a component of the halted stage itself: a halt in an
    earlier stage keeps blocking a later stage's force."""
    kind, stage = current.get("type"), current.get("stage")
    if kind == "Start":
        return True
    if kind == "Hold":
        return stage is not None and current.get("remaining_s") is not None
    if kind == "Halt":
        return stage is not None and stage == component_stage
    return False


def permit_decision(meta, view, component, commit, force_id, *, now, component_stage=None,
                    generation=None, repair=None) -> dict:
    """The host gate's answer (see the module docstring). ``meta`` is the last tick
    ({ticked_at, stale, error}); ``view`` that tick's track view for the component (None
    when the track has no plan); ``force_id`` a pending force entry for this component and
    commit (None when there is none or actions are off); ``component_stage`` its definition's
    stage; ``generation`` the plan generation the host last saw (None: not checked);
    ``repair`` the escalation's decided repair for this component on the target (Task 8),
    added as ``repair`` only once the tick is fresh and the commit and generation match."""
    ticked = meta.get("ticked_at")
    if ticked is None:
        return _deny("no coordinator tick yet")
    if meta.get("stale") or meta.get("error") or now - ticked > PERMIT_STALE_S:
        return _deny("the coordinator tick is stale")
    if view is None:
        return _deny("no plan for this component's track")
    target, current = view.get("target_commit"), view.get("current")
    if not target or not isinstance(current, dict):
        return _deny("no target release", view)
    if not same_commit(commit, target):
        return _deny("commit is not the plan target", view)
    if generation is not None and generation != view.get("generation"):
        return _deny("generation changed", view)
    extra = {"repair": dict(repair)} if isinstance(repair, dict) else {}
    if component in (view.get("permitted") or ()):
        return {**_deny("permitted by the plan", view, **extra), "permitted": True}
    why = " ".join(str(x) for x in (current.get("type"), current.get("reason")) if x)
    if force_id is None:
        return _deny(f"not permitted: {why}", view, **extra)
    if not (view.get("release_confirmed") is True and view.get("target_on_main") is True
            and _overridable(current, component_stage)):
        return _deny(f"a forced redeploy cannot override: {why}", view, **extra)
    return {**_deny("forced redeploy", view, **extra), "permitted": True, "forced": True,
            "request_id": force_id}


class AuditThrottle:
    """At most ``limit`` rows a minute; ``take()`` -> True when this one may be written."""

    def __init__(self, limit=None, now=None):
        self._limit = REFUSED_ROWS_PER_MIN if limit is None else limit
        self._now = now or time.time
        self._lock = threading.Lock()
        self._window = 0.0
        self._count = 0

    def take(self) -> bool:
        with self._lock:
            now = self._now()
            if now - self._window >= 60:
                self._window, self._count = now, 0
            if self._count >= self._limit:
                return False
            self._count += 1
            return True


class RefusalCounter:
    """Unaudited refusals counted per (action, reason); reasons are this module's constants,
    capped at ``MAX_REASONS`` keys. ``count`` returns True when a log line is due (at most
    once a minute per key)."""

    MAX_REASONS = 32

    def __init__(self, now=None):
        self._now = now or time.time
        self._lock = threading.Lock()
        self._counts: dict = {}
        self._logged: dict = {}

    def count(self, action, reason) -> bool:
        key = f"{action}: {reason}"
        with self._lock:
            if key not in self._counts and len(self._counts) >= self.MAX_REASONS:
                key = f"{action}: other"
            self._counts[key] = self._counts.get(key, 0) + 1
            now = self._now()
            if now - self._logged.get(key, float("-inf")) >= 60:
                self._logged[key] = now
                return True
            return False

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._counts)
