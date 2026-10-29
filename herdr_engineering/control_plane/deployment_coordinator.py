"""Deployment coordinator: stage, hold and halt decisions for a release (deploy-4 Task 3).

``advance(snapshot, definitions, now) -> [Action]`` is pure and deterministic: no I/O,
no host clock (``now`` is injected), the same input always gives the same actions.
``DeploymentCoordinator`` is the thin stateful wrapper: per tick it reads ONE CI view
(``DeploymentDefinitions.plans()``), one monitor snapshot and one release-evidence answer
per track, folds the events into ``CoordinatorState`` (persisted through the repo's
private sink stream, replayed by ``hydrate``), calls ``advance`` and adopts a Replan's
generation. It decides nothing ``advance`` did not.

Rules (spec section 4):

* A stage's components are the definitions with that stage number. Within a stage the
  order groups run in turn (gateways, order 0, before nodes): a group may start once
  every earlier group of the stage is healthy on the target. No hold between groups.
* Stage N+1 starts only when EVERY stage-N component has been healthy on the target for
  its definition's ``health.hold_s``, counted from herdr's exact arrival time
  (``received_epoch``, else ``received_at``) of the later ``ok``, against the injected
  ``now``. A component that turns unhealthy resets the hold (it runs from its next ok).
* A failed component halts its stage; a halted stage blocks every later stage. No
  auto-skip. A later ``ok`` toward the same target from the SAME instance lifts it; the
  attempt cap that keeps it halted is Task 7's, fed by ``CoordinatorState.attempts``.
* A new green release mid-roll restarts the plan from the newest commit: ``Replan``
  bumps the generation and every following action carries it.

Instances and the per-release fold (``CoordinatorState.observe``):

* only events whose ``reported_by`` (the authenticated fleet host) equals the
  definition's ``host`` count; a missing ``reported_by`` counts as foreign (events
  accepted before definitions were configured may still be in the store);
* a component has instances: the event's optional ``instance`` field (a stable id such as
  a Slurm job id) or else its ``host``. Each event is filed under its RELEASE (``to`` for
  started/ok/failed/refused, ``from`` for rolled-back) and instance; the slot keeps the
  instance's decisive event, ordered by (host ``ts``, outcome rank, arrival) - within one
  instance the host's own clock orders, so a late-arriving older ok cannot lift a newer
  failure; on a tie a failure beats an ok beats a started. Slots are persisted, so a
  failure that has left the monitor's 50-event timeline still halts;
* for the target, an instance with no slot for it is ABSENT: listed in ``missing``
  (``component:instance``) and it does not block. A failed slot blocks; any started slot
  keeps the stage open; otherwise the component is healthy from the latest ok;
* bounds: ``INSTANCES_PER_RELEASE`` slots per (component, release). A new instance
  evicts the oldest ok, then the oldest started; a failed slot is evicted only when all
  are failed, and the release is then marked ``failure-evicted`` (a permanent halt
  reason for that release). Releases per component are LRU (touched on every observe);
  the cap exceeds the monitor's timeline, so nothing visible churns.

Safety gates, in this order, before any stage decision:

1. no definitions / no green target: ``Idle``.
2. the target is one of the plan's past targets: ``Hold`` "target is an earlier
   release" (a fresh server whose CI window holds only a rerun of an old commit must
   not go backward; such a target is also behind ``plan_commit``, so its evidence says
   ``on_main=False`` and must not read as a force-push).
3. ``target_on_main is False`` (force-push, revert): ``Halt(stage=None)`` "target no
   longer on main"; nothing advances, no replan.
4. ``published_commit`` does not match the target: ``Hold`` "release not confirmed"
   (CI green proves CI passed, not that the release asset was published). With
   ancestry confirmed the stage decision is made first and only a Start is converted,
   so a halt still shows.
5. ``target_on_main is None`` with a confirmed release: ``Hold`` "target ancestry not
   confirmed".
6. a target other than the plan's commit: ``Replan`` (generation + 1).

Release evidence (contract for the caller; wiring to the real release-asset check is a
later task): ``DeploymentCoordinator(evidence=fn)``, ``fn(track, target_commit,
plan_commit) -> ReleaseEvidence``, called once per track per tick. ``published_commit``
is the commit the release (``follows.tag``/``follows.asset``, its ``.commit``) carries;
``on_main`` is True only when the target is on ``follows.branch`` AND not behind
``plan_commit`` (False / None = unknown). The default is unknown for both, so until it
is wired the coordinator only ever Holds (fail closed).

Permission (the Task 6 gate is stateless): every action carries ``permitted``, the
components whose updaters may proceed to ``target_commit`` NOW. A tick's permission is
its LAST action's (``permitted(actions)``); a component is permitted iff it is in that
tuple for the current generation and commit. Hosts pull, so permission never means
"restart now", and repairs never release a GPU.

* ``Start(s, o)``: every component of the stages before ``s`` plus stage ``s``'s order
  groups up to and including ``o``;
* ``Hold(s)`` on the hold timer: every component of stages up to and including ``s``
  (all on the target; their updaters may keep and repair them);
* ``Halt(s)``: the stages before ``s`` only - a halt stops the halted stage's remaining
  components too (spec 7.1);
* ``Idle("rolled out")``: every component;
* ``Hold(None)``, ``Halt(None)``, a Start converted to "release not confirmed", the
  other ``Idle``s and ``Replan``: nothing.

A stage-1 component turning unhealthy after stage 2 started yields Start/Hold/Halt at
stage 1, which withdraws stage 2's permission until stage 1 has held again. Herdr-down
handling stays on the hosts (Task 6). Expected-reporter sets (a node that never reports
blocking its stage) are deliberately not built here.

Operator acknowledgement (Task 5, ``CoordinatorState.acknowledge``): an audited operator
action for a stale instance (a preempted node whose failure would otherwise halt its stage
for good) or a ``failure-evicted`` release. Acknowledging an instance removes its slot for
that release and leaves a tombstone holding the acknowledged slot's order, so the same
(older) events, re-observed from the 50-event timeline or replayed after a restart, stay
ignored; a NEWER event from that instance counts again. Attempts are never touched.
"""
from __future__ import annotations

import copy
import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import groupby

from .deployments_monitor import TIMELINE_LIMIT

ENTITY_TYPE = "deployment_plan"
PLAN_EVENT = "DeploymentPlanAdopted"
ATTEMPT_EVENT = "DeploymentAttemptSeen"
SLOT_EVENT = "DeploymentInstanceObserved"
EVICTED_EVENT = "DeploymentFailureEvicted"
ACK_EVENT = "DeploymentInstanceAcknowledged"
EVICTION_ACK_EVENT = "DeploymentEvictionAcknowledged"

#: Past targets kept per track (a replan onto one of them is refused).
PAST_LIMIT = 32
#: Releases kept per component (LRU). Above the monitor's timeline so nothing churns.
RELEASES_PER_COMPONENT = 64
assert RELEASES_PER_COMPONENT > TIMELINE_LIMIT
#: Slots per (component, release). Above the timeline too, so an instance the timeline still
#: shows is not evicted and re-added every tick in practice (no tombstones are kept).
INSTANCES_PER_RELEASE = 256
assert INSTANCES_PER_RELEASE > TIMELINE_LIMIT
STARTS_PER_INSTANCE = 64
MISSING_LIMIT = 50
MIN_PREFIX = 7
_ID_CAP = 128
_KEY_CAP = 200

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_RANK = {"started": 0, "ok": 1, "failed": 2, "refused": 2, "rolled-back": 2}
_COMMIT = re.compile(r"[A-Za-z0-9._-]{1,128}")
_TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


# ----------------------------------------------------------------- actions
@dataclass(frozen=True)
class Start:
    """The order group ``components`` of ``stage`` has its turn. ``permitted`` is cumulative."""
    stage: int
    order: int
    components: tuple
    target_commit: str
    generation: int
    permitted: tuple = ()
    missing: tuple = ()


@dataclass(frozen=True)
class Hold:
    """Wait. ``stage``: the stage the plan is at (None: before any stage); ``remaining_s``:
    the hold left, None when waiting on evidence rather than time."""
    stage: int | None
    remaining_s: int | None
    reason: str
    target_commit: str | None
    generation: int
    permitted: tuple = ()
    missing: tuple = ()


@dataclass(frozen=True)
class Halt:
    """Stop: ``stage`` is halted (None: the whole plan) and every later stage is blocked."""
    stage: int | None
    reason: str
    target_commit: str | None
    generation: int
    components: tuple = ()
    permitted: tuple = ()
    missing: tuple = ()


@dataclass(frozen=True)
class Replan:
    """The plan restarts from ``new_target``; earlier generations are superseded."""
    new_target: str
    generation: int
    previous_commit: str | None
    permitted: tuple = ()


@dataclass(frozen=True)
class Idle:
    reason: str
    target_commit: str | None
    permitted: tuple = ()
    missing: tuple = ()


def permitted(actions) -> tuple:
    """The tick's permission: the last action's ``permitted`` (nothing for no actions)."""
    return tuple(actions[-1].permitted) if actions else ()


# ------------------------------------------------------------------- input
@dataclass(frozen=True)
class ReleaseEvidence:
    """What the caller knows about the target beyond "CI is green" (None = unknown)."""
    published_commit: str | None = None
    on_main: bool | None = None


@dataclass(frozen=True)
class Snapshot:
    """One tick's input. ``components`` is ``DeploymentsMonitor.snapshot()['components']``;
    ``target_commit`` the track's newest CI-green main commit (one CI read per tick);
    ``plan_commit``/``generation``/``past_targets`` the persisted plan. ``slots`` is
    ``CoordinatorState.slots_for(target, ...)`` (the persisted fold); when None it is
    folded from ``components`` alone, which forgets what left the timeline."""
    components: dict
    target_commit: str | None
    published_commit: str | None = None
    target_on_main: bool | None = None
    plan_commit: str | None = None
    generation: int = 0
    past_targets: tuple = field(default_factory=tuple)
    slots: dict | None = None


def same_commit(a, b) -> bool:
    """Equal, or one is a prefix (at least 7 characters) of the other; empty never matches."""
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return False
    if a == b:
        return True
    short, full = (a, b) if len(a) < len(b) else (b, a)
    return len(short) >= MIN_PREFIX and full.startswith(short)


def _arrival(event):
    """herdr's arrival time: the exact ``received_epoch`` when present, else ``received_at``."""
    rx = event.get("received_epoch")
    if isinstance(rx, (int, float)) and not isinstance(rx, bool) and math.isfinite(rx):
        return float(rx)
    try:
        return datetime.strptime(event.get("received_at") or "", _TS_FORMAT).replace(
            tzinfo=UTC).timestamp()
    except (TypeError, ValueError):
        return None


def _instance_of(event):
    return str(event.get("instance") or event.get("host") or "")[:_ID_CAP]


def _release_of(event):
    outcome = event.get("outcome")
    if outcome in ("started", "ok", "failed", "refused"):
        release = event.get("to")
    elif outcome == "rolled-back":
        release = event.get("from")
    else:
        return None
    return release if isinstance(release, str) and _COMMIT.fullmatch(release) else None


def _authorized(row, definition):
    """Timeline events reported by the definition's host, in arrival order."""
    timeline = row.get("timeline") if isinstance(row, dict) else None
    if not isinstance(timeline, list):
        return []
    return [e for e in timeline if isinstance(e, dict)
            and e.get("reported_by") == definition.host and _arrival(e) is not None]


def _order(slot):
    return (slot["host_ts"], _RANK[slot["outcome"]], slot["rx"])


def _state_of(outcome):
    return {"started": "updating", "ok": "healthy"}.get(outcome, "failed")


# ------------------------------------------------------------- decisions
def _status(slot_view):
    """``(state, since, outcome)`` for one component from its target slots."""
    if not slot_view:
        return "pending", None, None
    instances = slot_view.get("instances") or {}
    failed = sorted((inst, s) for inst, s in instances.items() if _state_of(s["outcome"]) == "failed")
    if failed:
        return "failed", None, failed[0][1]["outcome"]
    if slot_view.get("failure_evicted"):
        return "failed", None, "failure-evicted"
    if not instances:
        return "pending", None, None
    if any(_state_of(s["outcome"]) == "updating" for s in instances.values()):
        return "updating", None, None
    return "healthy", max(s["rx"] for s in instances.values()), None


def _stages(definitions):
    defs = sorted(definitions.values(), key=lambda d: (d.stage, d.order, d.component))
    return [(stage, list(group)) for stage, group in groupby(defs, key=lambda d: d.stage)]


def _roll(slots, definitions, now, target, generation):
    """The stage decision for ``target`` (no gates): one Start, Hold, Halt or Idle."""
    missing = []
    for name in sorted(definitions):
        missing.extend(f"{name}:{inst}" for inst in (slots.get(name) or {}).get("absent", ()))
    missing = tuple(missing[:MISSING_LIMIT])
    done: list[str] = []
    for stage, defs in _stages(definitions):
        status = {d.component: _status(slots.get(d.component)) for d in defs}
        failed = [d.component for d in defs if status[d.component][0] == "failed"]
        if failed:
            reason = "; ".join(f"{c}: {status[c][2]}" for c in failed)
            return Halt(stage=stage, reason=f"stage {stage} halted: {reason}",
                        target_commit=target, generation=generation, components=tuple(failed),
                        permitted=tuple(done), missing=missing)
        if all(status[d.component][0] == "healthy" for d in defs):
            remaining = 0
            for d in defs:
                elapsed = now - status[d.component][1]
                remaining = max(remaining, min(d.hold_s, math.ceil(d.hold_s - elapsed)))
            done.extend(d.component for d in defs)
            if remaining > 0:
                return Hold(stage=stage, remaining_s=int(remaining), reason=f"holding stage {stage}",
                            target_commit=target, generation=generation, permitted=tuple(done),
                            missing=missing)
            continue
        allowed = list(done)
        for order, group in groupby(defs, key=lambda d: d.order):
            group = [d.component for d in group]
            allowed.extend(group)
            if any(status[c][0] != "healthy" for c in group):
                return Start(stage=stage, order=order, components=tuple(group),
                             target_commit=target, generation=generation,
                             permitted=tuple(allowed), missing=missing)
    return Idle(reason="rolled out", target_commit=target, permitted=tuple(done), missing=missing)


def advance(snapshot: Snapshot, definitions: dict, now: float) -> list:
    """The coordinator's decision for one track. Pure: no I/O, ``now`` is injected."""
    target = snapshot.target_commit or None
    generation = int(snapshot.generation or 0)
    if not definitions:
        return [Idle(reason="no definitions", target_commit=target)]
    if target is None:
        return [Idle(reason="no green release", target_commit=None)]
    replan = not same_commit(snapshot.plan_commit, target)
    if replan and any(same_commit(p, target) for p in snapshot.past_targets):
        return [Hold(stage=None, remaining_s=None,
                     reason="target is an earlier release; the plan never goes back",
                     target_commit=target, generation=generation)]
    if snapshot.target_on_main is False:
        return [Halt(stage=None, reason="target no longer on main", target_commit=target,
                     generation=generation)]
    published = same_commit(snapshot.published_commit, target)
    if not published and snapshot.target_on_main is not True:
        return [Hold(stage=None, remaining_s=None, reason="release not confirmed",
                     target_commit=target, generation=generation)]
    if snapshot.target_on_main is not True:
        return [Hold(stage=None, remaining_s=None, reason="target ancestry not confirmed",
                     target_commit=target, generation=generation)]
    out: list = []
    if replan:
        generation += 1
        out.append(Replan(new_target=target, generation=generation,
                          previous_commit=snapshot.plan_commit))
    slots = snapshot.slots
    if slots is None:
        scratch = CoordinatorState()
        scratch.observe(snapshot.components if isinstance(snapshot.components, dict) else {},
                        definitions)
        slots = scratch.slots_for(target, definitions)
    action = _roll(slots, definitions, now, target, generation)
    if isinstance(action, Start) and not published:
        action = Hold(stage=action.stage, remaining_s=None, reason="release not confirmed",
                      target_commit=target, generation=generation, missing=action.missing)
    out.append(action)
    return out


# ------------------------------------------------------------------- state
@dataclass(frozen=True)
class PlanState:
    commit: str | None = None
    generation: int = 0
    past: tuple = ()


def _slot_entry():
    return {"instances": OrderedDict(), "failure_evicted": False, "acked": OrderedDict()}


def _track_key(track):
    return "plan:" + "#".join(str(p) for p in track).replace("|", "_")[:400]


def _str(v, cap, allow_empty=False):
    return isinstance(v, str) and len(v) <= cap and (allow_empty or bool(v))


class CoordinatorState:
    """Persisted coordinator state: plans per track, the per-release instance fold and the
    attempts. ``persist(record)`` is the repo's private sink; ``hydrate`` replays records."""

    def __init__(self, *, persist=None):
        self._persist = persist
        self._lock = threading.Lock()
        self._plans: dict[tuple, PlanState] = {}
        # component -> OrderedDict(release -> {"instances": {inst: slot}, "failure_evicted": bool})
        self._slots: dict[str, OrderedDict] = {}
        # component -> OrderedDict(release -> {instance: OrderedDict(start key)})
        self._attempts: dict[str, OrderedDict] = {}
        self._seq = 0

    # ------------------------------------------------------------- plans
    def plan(self, track) -> PlanState:
        with self._lock:
            return self._plans.get(tuple(track), PlanState())

    def adopt(self, track, replan: Replan) -> bool:
        """Adopt a Replan from ``advance``; a stale or repeated generation is ignored."""
        track = tuple(track)
        with self._lock:
            if not self._adopt(track, replan.new_target, replan.generation):
                return False
            st = self._plans[track]
            record = self._record(_track_key(track), PLAN_EVENT, {
                "kind": "plan", "track": list(track), "commit": st.commit,
                "generation": st.generation, "past": list(st.past)})
        self._emit([record])
        return True

    def _adopt(self, track, commit, generation, past=None):
        cur = self._plans.get(track, PlanState())
        if not _str(commit, _ID_CAP) or generation <= cur.generation:
            return False
        if past is None:
            past = ((cur.commit,) if cur.commit else ()) + cur.past
        self._plans[track] = PlanState(commit=commit, generation=generation,
                                       past=tuple(past)[:PAST_LIMIT])
        return True

    # ------------------------------------------------------------ observe
    def observe(self, components, definitions) -> int:
        """Fold every authorized event into the instance slots and the attempts. Idempotent:
        re-observing the same events persists nothing. Returns the new record count."""
        records = []
        comps = components if isinstance(components, dict) else {}
        with self._lock:
            for name, d in sorted(definitions.items()):
                for e in _authorized(comps.get(name), d):
                    release = _release_of(e)
                    if release is None:
                        continue
                    instance = _instance_of(e)
                    host_ts = e.get("host_ts")
                    if not isinstance(host_ts, str) or not _TS.fullmatch(host_ts):
                        continue
                    slot = {"outcome": e["outcome"], "host_ts": host_ts, "rx": _arrival(e)}
                    records.extend(self._put_slot(name, release, instance, slot))
                    if e["outcome"] == "started":
                        key = str(e.get("id") or f"{host_ts}/{e.get('step')}")[:_KEY_CAP]
                        if self._add_attempt(name, release, instance, key):
                            records.append(self._record(f"attempt:{name}", ATTEMPT_EVENT, {
                                "kind": "attempt", "component": name, "release": release,
                                "instance": instance, "key": key}))
        self._emit(records)
        return len(records)

    def _release_entry(self, table, component, release, new):
        releases = table.setdefault(component, OrderedDict())
        entry = releases.get(release)
        if entry is None:
            entry = releases[release] = new()
            while len(releases) > RELEASES_PER_COMPONENT:
                releases.popitem(last=False)
        else:
            releases.move_to_end(release)       # LRU: what the timeline still shows stays
        return entry

    def _put_slot(self, component, release, instance, slot):
        """File ``slot`` if it is decisive for (component, release, instance); returns records."""
        entry = self._release_entry(self._slots, component, release, _slot_entry)
        instances = entry["instances"]
        cur = instances.get(instance)
        if cur is not None and _order(slot) <= _order(cur):
            return []
        acked = entry["acked"].get(instance)
        if acked is not None:
            if _order(slot) <= acked:
                return []                       # acknowledged by an operator: stays ignored
            del entry["acked"][instance]        # a newer event from the instance counts again
        out = []
        if cur is None and len(instances) >= INSTANCES_PER_RELEASE:
            victim = self._victim(instances)
            if _state_of(instances[victim]["outcome"]) == "failed" and not entry["failure_evicted"]:
                entry["failure_evicted"] = True
                out.append(self._record(f"slot:{component}", EVICTED_EVENT, {
                    "kind": "failure-evicted", "component": component, "release": release}))
            del instances[victim]
        instances[instance] = dict(slot)
        out.append(self._record(f"slot:{component}", SLOT_EVENT, {
            "kind": "slot", "component": component, "release": release, "instance": instance,
            **slot}))
        return out

    @staticmethod
    def _victim(instances):
        """Oldest ok first, then oldest started; a failed one only when every slot failed."""
        for wanted in ("healthy", "updating", "failed"):
            pool = [(_order(s), inst) for inst, s in instances.items()
                    if _state_of(s["outcome"]) == wanted]
            if pool:
                return min(pool)[1]
        raise AssertionError("no slot to evict")

    def slots_for(self, target, definitions) -> dict:
        """{component: {"instances": {inst: slot}, "failure_evicted": bool, "absent": (inst,)}}
        for the releases matching ``target``; ``absent`` = instances known only for others."""
        out = {}
        with self._lock:
            for name in sorted(definitions):
                releases = self._slots.get(name) or {}
                inst: dict = {}
                evicted = False
                known: set = set()
                for rel, entry in releases.items():
                    known.update(entry["instances"])
                    if not same_commit(rel, target):
                        continue
                    evicted = evicted or entry["failure_evicted"]
                    for i, s in entry["instances"].items():
                        if i not in inst or _order(s) > _order(inst[i]):
                            inst[i] = dict(s)
                out[name] = {"instances": inst, "failure_evicted": evicted,
                             "absent": tuple(sorted(known - set(inst)))}
        return out

    # ------------------------------------------------------ operator ack
    @staticmethod
    def _tombstone(entry, instance, order):
        entry["instances"].pop(instance, None)
        acked = entry["acked"]
        acked.pop(instance, None)
        acked[instance] = order
        while len(acked) > INSTANCES_PER_RELEASE:
            acked.popitem(last=False)

    def acknowledgeable(self, component, release, instance=None) -> list:
        """What ``acknowledge`` would act on (read only): ``[{release, outcome, host_ts}]``
        for an instance (the slot being acknowledged), ``[{release}]`` for an eviction."""
        if not _str(release, _ID_CAP) or not _COMMIT.fullmatch(release):
            return []
        out = []
        with self._lock:
            for rel, entry in (self._slots.get(component) or {}).items():
                if not same_commit(rel, release):
                    continue
                if instance is None:
                    if entry["failure_evicted"]:
                        out.append({"release": rel})
                elif instance in entry["instances"]:
                    slot = entry["instances"][instance]
                    out.append({"release": rel, "outcome": slot["outcome"], "host_ts": slot["host_ts"]})
        return out

    def acknowledge(self, component, release, instance=None) -> list:
        """Operator ack (audited by the caller). With ``instance``: drop that instance's slot
        in every release matching ``release`` (prefix-matched) and tombstone it, so it no
        longer blocks and its older events stay ignored. Without: lift the
        ``failure-evicted`` mark of the matching releases. Attempts are untouched. Returns
        the releases acknowledged; [] means there was nothing to acknowledge."""
        if not _str(release, _ID_CAP) or not _COMMIT.fullmatch(release):
            return []
        done, records = [], []
        with self._lock:
            for rel, entry in list((self._slots.get(component) or {}).items()):
                if not same_commit(rel, release):
                    continue
                if instance is None:
                    if entry["failure_evicted"]:
                        entry["failure_evicted"] = False
                        done.append(rel)
                        records.append(self._record(f"slot:{component}", EVICTION_ACK_EVENT, {
                            "kind": "eviction-ack", "component": component, "release": rel}))
                    continue
                slot = entry["instances"].get(instance)
                if slot is None:
                    continue
                self._tombstone(entry, instance, _order(slot))
                done.append(rel)
                records.append(self._record(f"slot:{component}", ACK_EVENT, {
                    "kind": "ack", "component": component, "release": rel, "instance": instance,
                    **slot}))
        self._emit(records)
        return done

    # ---------------------------------------------------------- attempts
    def _add_attempt(self, component, release, instance, key):
        instances = self._release_entry(self._attempts, component, release, dict)
        starts = instances.get(instance)
        if starts is None:
            if len(instances) >= INSTANCES_PER_RELEASE:
                return False
            starts = instances[instance] = OrderedDict()
        if key in starts or len(starts) >= STARTS_PER_INSTANCE:
            return False
        starts[key] = None
        return True

    def attempts(self, component, release, instance=None) -> int:
        """Starts toward ``release`` (prefix-matched) for ``component``: the most made by any
        one instance, or by ``instance``. A fleet roll of 20 nodes is 1 attempt, not 20."""
        with self._lock:
            merged: dict[str, set] = {}
            for rel, instances in self._attempts.get(component, {}).items():
                if not same_commit(rel, release):
                    continue
                for inst, starts in instances.items():
                    merged.setdefault(inst, set()).update(starts)
            if instance is not None:
                return len(merged.get(instance, ()))
            return max((len(s) for s in merged.values()), default=0)

    # ------------------------------------------------------- persistence
    def _record(self, entity_id, event_type, payload):
        self._seq += 1
        return {"entity_type": ENTITY_TYPE, "entity_id": entity_id, "event_type": event_type,
                "sequence": self._seq, "payload": {**payload, "seq": self._seq}}

    def _emit(self, records):
        if self._persist is None:
            return
        for record in records:
            try:
                self._persist(copy.deepcopy(record))
            except Exception:  # noqa: BLE001 - durable history is best-effort, as for deployment events
                pass

    def hydrate(self, rows) -> int:
        """Replay persisted records (no re-persist), with the same caps as live writes;
        malformed ones are skipped."""
        applied = 0

        def seq_of(r):
            s = r.get("sequence")
            return s if isinstance(s, int) and not isinstance(s, bool) else 0

        ordered = sorted((r for r in rows if isinstance(r, dict)), key=seq_of)
        with self._lock:
            for row in ordered:
                p = row.get("payload")
                if not isinstance(p, dict):
                    continue
                seq = p.get("seq")
                if isinstance(seq, int) and not isinstance(seq, bool):
                    self._seq = max(self._seq, seq)
                try:
                    applied += self._replay(p)
                except (TypeError, ValueError, KeyError):
                    continue
        return applied

    def _replay(self, p) -> bool:
        kind = p.get("kind")
        comp, release = p.get("component"), p.get("release")
        if kind == "plan":
            track, gen, past = p.get("track"), p.get("generation"), p.get("past")
            if (not isinstance(track, list) or len(track) != 3
                    or not all(_str(x, 400) for x in track)
                    or not isinstance(gen, int) or isinstance(gen, bool)
                    or not isinstance(past, list) or not all(_str(x, _ID_CAP) for x in past)):
                return False
            return self._adopt(tuple(track), p.get("commit"), gen, past)
        if not _str(comp, 64) or not _str(release, _ID_CAP) or not _COMMIT.fullmatch(release):
            return False
        if kind == "attempt":
            inst, key = p.get("instance"), p.get("key")
            if not _str(inst, _ID_CAP, allow_empty=True) or not _str(key, _KEY_CAP):
                return False
            return self._add_attempt(comp, release, inst, key)
        if kind == "slot":
            inst, outcome, host_ts, rx = (p.get(k) for k in ("instance", "outcome", "host_ts", "rx"))
            if (not _str(inst, _ID_CAP, allow_empty=True) or outcome not in _RANK
                    or not isinstance(host_ts, str) or not _TS.fullmatch(host_ts)
                    or isinstance(rx, bool) or not isinstance(rx, (int, float))
                    or not math.isfinite(rx)):
                return False
            return bool(self._put_slot(comp, release, inst,
                                       {"outcome": outcome, "host_ts": host_ts, "rx": float(rx)}))
        if kind == "failure-evicted":
            entry = self._release_entry(self._slots, comp, release, _slot_entry)
            entry["failure_evicted"] = True
            return True
        if kind == "ack":
            inst, outcome, host_ts, rx = (p.get(k) for k in ("instance", "outcome", "host_ts", "rx"))
            if (not _str(inst, _ID_CAP, allow_empty=True) or outcome not in _RANK
                    or not isinstance(host_ts, str) or not _TS.fullmatch(host_ts)
                    or isinstance(rx, bool) or not isinstance(rx, (int, float))
                    or not math.isfinite(rx)):
                return False
            entry = self._release_entry(self._slots, comp, release, _slot_entry)
            self._tombstone(entry, inst, (host_ts, _RANK[outcome], float(rx)))
            return True
        if kind == "eviction-ack":
            entry = self._release_entry(self._slots, comp, release, _slot_entry)
            entry["failure_evicted"] = False
            return True
        return False


# ----------------------------------------------------------------- wrapper
def _unknown_evidence(_track, _target, _plan_commit):
    return ReleaseEvidence()


class DeploymentCoordinator:
    """Per tick: one CI view, one monitor snapshot, one evidence call per track.

    ``monitor`` and ``state`` are callables returning the repo's current objects, because
    a re-import replaces them. ``tick()`` returns ``{track: [Action]}``. A failure in one
    track's evidence or decision holds that track (nothing permitted) and the others go
    on; when the definitions or the CI view cannot be read at all, the tick returns {}.
    """

    def __init__(self, definitions, *, monitor, state, evidence=None, now=None):
        self.last_evidence: dict = {}
        self._definitions = definitions
        self._monitor = monitor
        self._state = state
        self._evidence = evidence or _unknown_evidence
        self._now = now or time.time

    def tick(self) -> dict:
        # The release evidence this tick decided on, per track (unknown unless the call
        # succeeded): the force-redeploy gate reads it next to the tick's actions.
        self.last_evidence = seen = {}
        try:
            defs = self._definitions.definitions()
            plans = self._definitions.plans()          # the one CI read of this tick
        except Exception:  # noqa: BLE001 - no definitions view: decide nothing, permit nothing
            return {}
        state = self._state()
        try:
            components = self._monitor().snapshot()["components"]
        except Exception:  # noqa: BLE001 - every track holds below
            components = None
        if components is not None:
            state.observe(components, defs)
        now = self._now()
        out = {}
        for track, plan in sorted(plans.items()):
            ps = state.plan(track)
            target = plan.target_commit

            def hold(reason, ps=ps, target=target):
                return [Hold(stage=None, remaining_s=None, reason=reason, target_commit=target,
                             generation=ps.generation)]

            if components is None:
                out[track] = hold("deployment events unavailable")
                continue
            tdefs = {c: d for c, d in defs.items() if d.track == track}
            try:
                evidence = self._evidence(track, target, ps.commit) if target else ReleaseEvidence()
            except Exception as exc:  # noqa: BLE001 - this track holds, the others go on
                out[track] = hold(f"release evidence unavailable: {type(exc).__name__}")
                continue
            if not isinstance(evidence, ReleaseEvidence):
                evidence = ReleaseEvidence()
            seen[track] = evidence
            try:
                snap = Snapshot(components=components, target_commit=target,
                                published_commit=evidence.published_commit,
                                target_on_main=evidence.on_main, plan_commit=ps.commit,
                                generation=ps.generation, past_targets=ps.past,
                                slots=state.slots_for(target, tdefs) if target else {})
                actions = advance(snap, tdefs, now)
            except Exception as exc:  # noqa: BLE001
                out[track] = hold(f"coordinator error: {type(exc).__name__}")
                continue
            for action in actions:
                if isinstance(action, Replan):
                    state.adopt(track, action)
            out[track] = actions
        return out


# ------------------------------------------------------------------- views
def action_dict(action) -> dict:
    """One action as plain JSON-ready data, with its kind in ``type``."""
    from dataclasses import asdict
    return {"type": type(action).__name__, **{k: list(v) if isinstance(v, tuple) else v
                                              for k, v in asdict(action).items()}}


def component_view(slots, definitions, now) -> dict:
    """{component: {state, outcome, since, hold_remaining_s, instances}} from the target
    slots, the same fold ``advance`` decides on (``slots`` = ``slots_for(target, defs)``).
    ``hold_remaining_s`` is the definition's own hold left while healthy, else None."""
    out = {}
    for name, d in sorted(definitions.items()):
        view = (slots or {}).get(name)
        state, since, outcome = _status(view)
        remaining = None
        if state == "healthy" and since is not None:
            remaining = max(0, min(d.hold_s, math.ceil(d.hold_s - (now - since))))
        out[name] = {"state": state, "outcome": outcome, "since": since,
                     "hold_remaining_s": remaining,
                     "instances": len((view or {}).get("instances") or {})}
    return out


def track_views(results, definitions, state, now, evidence=None) -> list:
    """The page's plan view of one ``tick()`` result: per track its generation, target,
    current action, permission, missing instances and per-component coordinator status.
    ``evidence`` ({track: ReleaseEvidence}, the tick's ``last_evidence``) adds
    ``release_confirmed`` / ``target_on_main``; a track without it is unconfirmed."""
    out = []
    for track, actions in sorted(results.items()):
        last = actions[-1] if actions else None
        target = getattr(last, "target_commit", None) or getattr(last, "new_target", None)
        tdefs = {c: d for c, d in definitions.items() if d.track == track}
        plan = state.plan(track)
        components = component_view(state.slots_for(target, tdefs) if target else {}, tdefs, now)
        for name, row in components.items():
            row["attempts"] = state.attempts(name, target) if target else 0
        ev = (evidence or {}).get(track) or ReleaseEvidence()
        out.append({
            "track": {"repo": track[0], "branch": track[1], "ci_context": track[2]},
            "release_confirmed": bool(target) and same_commit(ev.published_commit, target),
            "target_on_main": ev.on_main if isinstance(ev.on_main, bool) else None,
            "generation": plan.generation, "plan_commit": plan.commit, "target_commit": target,
            "actions": [action_dict(a) for a in actions],
            "current": action_dict(last) if last is not None else None,
            "permitted": list(permitted(actions)),
            "missing": list(getattr(last, "missing", ()) or ()),
            "components": components,
        })
    return out
