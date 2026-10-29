"""Deployment coordinator: stage, hold and halt decisions for a release (deploy-4 Task 3).

``advance(snapshot, definitions, now) -> [Action]`` is pure and deterministic: no I/O,
no host clock (``now`` is injected), the same input always gives the same actions.
``DeploymentCoordinator`` is the thin stateful wrapper: per tick it reads ONE CI view
(``DeploymentDefinitions.plans()``), one monitor snapshot and one release-evidence answer
per track, calls ``advance`` and commits what it decided (a new plan generation, the
attempts it saw) to ``CoordinatorState``, which persists through the repo's private
sink stream like the deployment events do and is replayed by ``hydrate``.

Rules (spec section 4):

* A stage's components are the definitions with that stage number. Within a stage
  the order groups run in turn (gateways, order 0, before nodes): a group may start
  once every earlier group of the stage is healthy on the target. There is no hold
  between order groups.
* Stage N+1 starts only when EVERY stage-N component has been healthy on the target
  for its definition's ``health.hold_s``, counted from the herdr arrival
  (``received_at``) of the later ``ok``, against the injected ``now``. The host's own
  ``ts`` is never used. A component that turns unhealthy resets the hold: the hold
  runs from its next ``ok``.
* A failed component (``failed``/``refused`` for the target, ``rolled-back`` from it)
  halts its stage; a halted stage blocks every later stage. There is no auto-skip.
  The halt is derived from the current events, so a repaired component (a later
  ``ok``) lifts it; the attempt cap that keeps it halted is Task 7's, fed by
  ``CoordinatorState.attempts``.
* A new green release mid-roll restarts the plan from the newest commit: ``Replan``
  bumps the generation and every following action carries it. Actions of an earlier
  generation are superseded; the consumer acts only on the newest generation.

Component state is rebuilt here from the monitor's timeline, not read from the row's
``health``/``running``/``hold_remaining_s`` (those cover every reporter and use the
monitor's global hold and clock):

* only events whose ``reported_by`` (the authenticated fleet host) equals the
  definition's ``host`` count. Events accepted before definitions were configured may
  still be in the store; a missing ``reported_by`` counts as foreign;
* one component can have many instances (``hive-nodes``: every GPU node reports it
  under its own event ``host``). Each instance's latest non-``skipped`` event decides
  that instance; any failed instance halts, any instance not yet healthy keeps the
  stage open, and the hold runs from the latest ``ok`` across instances;
* an event counts for the target only when its commit matches (``same_commit``): an
  ``ok`` for the previous commit is not healthy for the new one.

Safety gates, in this order, before any stage decision:

1. no definitions / no green target: ``Idle``.
2. ``target_on_main is False`` (force-push, revert): ``Halt(stage=None)`` "target no
   longer on main"; nothing advances, no replan.
3. the target is one of the plan's past targets: ``Hold`` (a fresh server whose CI
   window holds only a rerun of an old commit must not go backward).
4. CI green proves the commit passed CI, not that the release asset was published for
   it. A ``Start`` is only emitted when ``published_commit`` matches the target:
   unknown evidence (the default) is ``Hold`` "release not confirmed"; with ancestry
   confirmed the stage decision is made and a Start becomes that same Hold.
5. ``target_on_main is None`` (not checked) with a confirmed release: ``Hold``
   "target ancestry not confirmed".
6. a target other than the plan's commit: ``Replan`` (generation + 1).

Release evidence (contract for the caller; wiring to the real release-asset check is a
later task): ``DeploymentCoordinator(evidence=fn)`` with
``fn(track, target_commit, plan_commit) -> ReleaseEvidence`` called once per track per
tick. ``published_commit`` is the commit the release (``follows.tag`` /
``follows.asset``, its ``.commit`` file) currently carries; ``on_main`` answers "the
target is on ``follows.branch`` and is not behind ``plan_commit``" (True / False /
None = unknown). The default evidence is unknown for both, so until it is wired the
coordinator only ever Holds; that is intended (fail closed).

A ``Start`` never means "restart this host now": hosts pull. It is a permission, ``allow
the updaters of these components to proceed to target_commit``; ``components`` is
cumulative (every component whose turn has come in this generation, earlier stages
included). Hold and Halt carry no permission list, so the consumer (Task 6) applies:
permission = the components of the LAST ``Start`` of the CURRENT generation; a ``Halt``
forbids new starts in its stage and later ones (repairs of an already-permitted
component are Task 7's); a Replan withdraws everything of older generations. A stage-1
component turning unhealthy after stage 2 started yields ``Hold``/``Halt`` at stage 1,
so stage 2's turn is withdrawn until stage 1 has held again (the reset rule). Repairs never
release a GPU (the definitions validator already refuses GPU-releasing restarts).
Herdr-down handling stays on the hosts (Task 6).
"""
from __future__ import annotations

import copy
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import groupby

ENTITY_TYPE = "deployment_plan"
PLAN_EVENT = "DeploymentPlanAdopted"
ATTEMPT_EVENT = "DeploymentAttemptSeen"

#: Past targets kept per track (a replan onto one of them is refused).
PAST_LIMIT = 32
#: Attempt bookkeeping bounds: releases per component, instances per release, starts per instance.
RELEASES_PER_COMPONENT = 16
INSTANCES_PER_RELEASE = 512
STARTS_PER_INSTANCE = 64
MIN_PREFIX = 7

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_FAILED_TO = ("failed", "refused")


# ----------------------------------------------------------------- actions
@dataclass(frozen=True)
class Start:
    """Allow the updaters of ``components`` (cumulative) to proceed to ``target_commit``.
    ``stage``/``order`` name the group whose turn has just come."""
    stage: int
    order: int
    components: tuple
    target_commit: str
    generation: int


@dataclass(frozen=True)
class Hold:
    """Wait. ``stage`` is the stage the plan is at (None: before any stage);
    ``remaining_s`` is the hold left, None when waiting on evidence rather than time."""
    stage: int | None
    remaining_s: int | None
    reason: str
    target_commit: str | None
    generation: int


@dataclass(frozen=True)
class Halt:
    """Stop: ``stage`` is halted (None: the whole plan) and every later stage is blocked."""
    stage: int | None
    reason: str
    target_commit: str | None
    generation: int
    components: tuple = ()


@dataclass(frozen=True)
class Replan:
    """The plan restarts from ``new_target``; earlier generations are superseded."""
    new_target: str
    generation: int
    previous_commit: str | None


@dataclass(frozen=True)
class Idle:
    reason: str
    target_commit: str | None


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
    ``plan_commit``/``generation``/``past_targets`` the persisted plan."""
    components: dict
    target_commit: str | None
    published_commit: str | None = None
    target_on_main: bool | None = None
    plan_commit: str | None = None
    generation: int = 0
    past_targets: tuple = field(default_factory=tuple)


def same_commit(a, b) -> bool:
    """Equal, or one is a prefix (at least 7 characters) of the other; empty never matches."""
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return False
    if a == b:
        return True
    short, full = (a, b) if len(a) < len(b) else (b, a)
    return len(short) >= MIN_PREFIX and full.startswith(short)


def _rx(event):
    try:
        return datetime.strptime(event.get("received_at") or "", _TS_FORMAT).replace(
            tzinfo=UTC).timestamp()
    except (TypeError, ValueError):
        return None


def _authorized(row, definition):
    """Timeline events reported by the definition's host, in arrival order."""
    timeline = row.get("timeline") if isinstance(row, dict) else None
    if not isinstance(timeline, list):
        return []
    return [e for e in timeline if isinstance(e, dict)
            and e.get("reported_by") == definition.host and _rx(e) is not None]


def _instance_state(event, target):
    outcome = event.get("outcome")
    if outcome == "ok" and same_commit(event.get("to"), target):
        return "healthy"
    if outcome == "started" and same_commit(event.get("to"), target):
        return "updating"
    if outcome in _FAILED_TO and same_commit(event.get("to"), target):
        return "failed"
    if outcome == "rolled-back" and same_commit(event.get("from"), target):
        return "failed"
    return "pending"                # its latest event is about another commit


def component_status(row, definition, target):
    """``(state, since, outcome)`` for one component on ``target``.

    state: ``healthy`` (``since`` = arrival of the latest ok), ``failed`` (``outcome`` =
    the failing outcome), ``updating`` or ``pending``.
    """
    latest = {}
    for e in _authorized(row, definition):
        if e.get("outcome") == "skipped":
            continue
        latest[e.get("host") or ""] = e
    if not latest:
        return "pending", None, None
    states = {inst: (_instance_state(e, target), e) for inst, e in latest.items()}
    failed = sorted((inst, e) for inst, (st, e) in states.items() if st == "failed")
    if failed:
        return "failed", None, failed[0][1].get("outcome")
    if any(st == "updating" for st, _ in states.values()):
        return "updating", None, None
    if any(st == "pending" for st, _ in states.values()):
        return "pending", None, None
    return "healthy", max(_rx(e) for _, e in states.values()), None


def _stages(definitions):
    defs = sorted(definitions.values(), key=lambda d: (d.stage, d.order, d.component))
    return [(stage, list(group)) for stage, group in groupby(defs, key=lambda d: d.stage)]


def _roll(snapshot, definitions, now, target, generation):
    """The stage decision for ``target`` (no gates): one Start, Hold, Halt or Idle."""
    components = snapshot.components if isinstance(snapshot.components, dict) else {}
    allowed: list[str] = []
    for stage, defs in _stages(definitions):
        status = {d.component: component_status(components.get(d.component), d, target)
                  for d in defs}
        failed = [d.component for d in defs if status[d.component][0] == "failed"]
        if failed:
            reason = "; ".join(f"{c}: {status[c][2]}" for c in failed)
            return Halt(stage=stage, reason=f"stage {stage} halted: {reason}",
                        target_commit=target, generation=generation, components=tuple(failed))
        if all(status[d.component][0] == "healthy" for d in defs):
            allowed.extend(d.component for d in defs)
            remaining = 0
            for d in defs:
                elapsed = now - status[d.component][1]
                remaining = max(remaining, min(d.hold_s, math.ceil(d.hold_s - elapsed)))
            if remaining > 0:
                return Hold(stage=stage, remaining_s=int(remaining), reason=f"holding stage {stage}",
                            target_commit=target, generation=generation)
            continue
        for order, group in groupby(defs, key=lambda d: d.order):
            group = list(group)
            allowed.extend(d.component for d in group)
            if any(status[d.component][0] != "healthy" for d in group):
                return Start(stage=stage, order=order, components=tuple(allowed),
                             target_commit=target, generation=generation)
    return Idle(reason="rolled out", target_commit=target)


def advance(snapshot: Snapshot, definitions: dict, now: float) -> list:
    """The coordinator's decision for one track. Pure: no I/O, ``now`` is injected."""
    target = snapshot.target_commit or None
    generation = int(snapshot.generation or 0)
    if not definitions:
        return [Idle(reason="no definitions", target_commit=target)]
    if target is None:
        return [Idle(reason="no green release", target_commit=None)]
    if snapshot.target_on_main is False:
        return [Halt(stage=None, reason="target no longer on main", target_commit=target,
                     generation=generation)]
    replan = not same_commit(snapshot.plan_commit, target)
    if replan and any(same_commit(p, target) for p in snapshot.past_targets):
        return [Hold(stage=None, remaining_s=None,
                     reason="target is an earlier release; the plan never goes back",
                     target_commit=target, generation=generation)]
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
    action = _roll(snapshot, definitions, now, target, generation)
    if isinstance(action, Start) and not published:
        action = Hold(stage=action.stage, remaining_s=None, reason="release not confirmed",
                      target_commit=target, generation=generation)
    out.append(action)
    return out


# ------------------------------------------------------------------- state
@dataclass(frozen=True)
class PlanState:
    commit: str | None = None
    generation: int = 0
    past: tuple = ()


def _track_key(track):
    return "plan:" + "#".join(str(p) for p in track).replace("|", "_")[:400]


class CoordinatorState:
    """Persisted coordinator state: the plan per track and the attempts per release.

    ``persist(record)`` is the repo's private sink (``ControlPlaneRepo``); ``hydrate`` replays
    the records after a restart. Bounded: past targets, releases, instances and starts.
    """

    def __init__(self, *, persist=None):
        self._persist = persist
        self._lock = threading.Lock()
        self._plans: dict[tuple, PlanState] = {}
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
        self._emit(record)
        return True

    def _adopt(self, track, commit, generation, past=None):
        cur = self._plans.get(track, PlanState())
        if not isinstance(commit, str) or not commit or generation <= cur.generation:
            return False
        if past is None:
            past = ((cur.commit,) if cur.commit else ()) + cur.past
        self._plans[track] = PlanState(commit=commit, generation=generation,
                                       past=tuple(past)[:PAST_LIMIT])
        return True

    # ---------------------------------------------------------- attempts
    def observe(self, components, definitions) -> int:
        """Record every ``started`` seen from the definition's host, keyed by instance and
        event, so ``attempts`` survives the bounded timeline. Returns the new count."""
        records = []
        with self._lock:
            for name, d in sorted(definitions.items()):
                row = components.get(name) if isinstance(components, dict) else None
                for e in _authorized(row, d):
                    release = e.get("to")
                    if e.get("outcome") != "started" or not isinstance(release, str) or not release:
                        continue
                    instance = str(e.get("host") or "")[:253]
                    key = str(e.get("id") or f"{e.get('host_ts')}/{e.get('step')}")[:200]
                    if self._add_attempt(name, release, instance, key):
                        records.append(self._record(f"attempt:{name}", ATTEMPT_EVENT, {
                            "kind": "attempt", "component": name, "release": release,
                            "instance": instance, "key": key}))
        for record in records:
            self._emit(record)
        return len(records)

    def _add_attempt(self, component, release, instance, key):
        releases = self._attempts.setdefault(component, OrderedDict())
        instances = releases.get(release)
        if instances is None:
            instances = releases[release] = {}
            while len(releases) > RELEASES_PER_COMPONENT:
                releases.popitem(last=False)
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

    def _emit(self, record):
        if self._persist is None:
            return
        try:
            self._persist(copy.deepcopy(record))
        except Exception:  # noqa: BLE001 - durable history is best-effort, as for deployment events
            pass

    def hydrate(self, rows) -> int:
        """Replay persisted records (no re-persist); malformed ones are skipped."""
        applied = 0
        ordered = sorted((r for r in rows if isinstance(r, dict)),
                         key=lambda r: int(r.get("sequence") or 0)
                         if isinstance(r.get("sequence"), int) else 0)
        with self._lock:
            for row in ordered:
                p = row.get("payload")
                if not isinstance(p, dict):
                    continue
                seq = p.get("seq")
                if isinstance(seq, int) and not isinstance(seq, bool):
                    self._seq = max(self._seq, seq)
                if p.get("kind") == "plan":
                    track, gen, past = p.get("track"), p.get("generation"), p.get("past")
                    if (not isinstance(track, list) or not all(isinstance(x, str) for x in track)
                            or not isinstance(gen, int) or isinstance(gen, bool)
                            or not isinstance(past, list)
                            or not all(isinstance(x, str) for x in past)):
                        continue
                    applied += self._adopt(tuple(track), p.get("commit"), gen, past)
                elif p.get("kind") == "attempt":
                    vals = [p.get(k) for k in ("component", "release", "instance", "key")]
                    if all(isinstance(v, str) for v in vals) and vals[0] and vals[1]:
                        applied += self._add_attempt(*vals)
        return applied


# ----------------------------------------------------------------- wrapper
def _unknown_evidence(_track, _target, _plan_commit):
    return ReleaseEvidence()


class DeploymentCoordinator:
    """Per tick: one CI view, one monitor snapshot, one evidence call per track.

    ``monitor`` and ``state`` are callables returning the repo's current objects, because
    a re-import replaces them. ``tick()`` returns ``{track: [Action]}`` and commits a Replan's
    generation and the attempts it observed; it decides nothing ``advance`` did not.
    """

    def __init__(self, definitions, *, monitor, state, evidence=None, now=None):
        import time
        self._definitions = definitions
        self._monitor = monitor
        self._state = state
        self._evidence = evidence or _unknown_evidence
        self._now = now or time.time

    def tick(self) -> dict:
        defs = self._definitions.definitions()
        plans = self._definitions.plans()              # the one CI read of this tick
        components = self._monitor().snapshot()["components"]
        state = self._state()
        now = self._now()
        state.observe(components, defs)
        out = {}
        for track, plan in sorted(plans.items()):
            tdefs = {c: d for c, d in defs.items() if d.track == track}
            ps = state.plan(track)
            target = plan.target_commit
            evidence = (self._evidence(track, target, ps.commit) if target
                        else ReleaseEvidence())
            if not isinstance(evidence, ReleaseEvidence):
                evidence = ReleaseEvidence()
            snap = Snapshot(components=components, target_commit=target,
                            published_commit=evidence.published_commit,
                            target_on_main=evidence.on_main, plan_commit=ps.commit,
                            generation=ps.generation, past_targets=ps.past)
            actions = advance(snap, tdefs, now)
            for action in actions:
                if isinstance(action, Replan):
                    state.adopt(track, action)
            out[track] = actions
        return out
