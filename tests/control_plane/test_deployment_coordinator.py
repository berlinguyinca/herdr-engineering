"""Deployment coordinator: the stage/hold/halt rules (deploy-4 Task 3).

The coordinator is a pure ``advance(snapshot, definitions, now)``; component state is
built from the real ``DeploymentsMonitor`` here so the input contract is the one the
server produces. Time is a fake clock throughout.
"""
import os
from datetime import UTC, datetime

import pytest
import yaml

from herdr_engineering.control_plane import deployment_coordinator as dc
from herdr_engineering.control_plane.bridge import load_into
from herdr_engineering.control_plane.deployment_coordinator import (
    CoordinatorState,
    DeploymentCoordinator,
    Halt,
    Hold,
    Idle,
    ReleaseEvidence,
    Replan,
    Snapshot,
    Start,
    advance,
    permitted,
    same_commit,
)
from herdr_engineering.control_plane.deployment_definitions import Definition, DeploymentDefinitions
from herdr_engineering.control_plane.deployments_monitor import DeploymentsMonitor
from herdr_engineering.control_plane.repo import ControlPlaneRepo

T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC).timestamp()
OLD = "0" * 40
T1 = "1" * 40
T2 = "2" * 40
TRACK = ("InferWeave/inferweave", "main", "ci/woodpecker/push/rust")
TRACK2 = ("InferWeave/inferweave", "main", "ci/woodpecker/push/console")


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _defn(component, stage, order, host, hold_s=600, track=TRACK):
    return Definition(
        component=component, stage=stage, order=order, host=host, updater="u",
        follows={"repo": track[0], "tag": "node-ci-latest", "asset": "inferweave",
                 "branch": track[1], "ci_context": track[2]},
        restart="gateway-cycle" if order == 0 else "process-restart-in-allocation",
        gpu=False, health={"probe": "served-request", "timeout_s": 600, "hold_s": hold_s},
        rollback={"strategy": "previous-version"})


DEFS = {d.component: d for d in (
    _defn("hive-gateway", 1, 0, "hive"),
    _defn("hive-nodes", 1, 1, "hive"),
    _defn("whiteale-gateway", 2, 0, "whiteale"),
    _defn("whiteale-laya", 2, 1, "whiteale"),
)}
STAGE1 = ("hive-gateway", "hive-nodes")
ALL = STAGE1 + ("whiteale-gateway", "whiteale-laya")


class World:
    """A monitor on a fake clock, fed like the ingest route feeds it."""

    def __init__(self, monitor_clock=None):
        self.clock = monitor_clock or Clock()
        self.mon = DeploymentsMonitor(now=self.clock)
        self._n = 0

    def emit(self, component, outcome, *, at, to=T1, frm=OLD, reported_by="default", host=None,
             instance=None, host_at=None, rx=None):
        self.clock.t = T0 + (at if rx is None else rx)
        self._n += 1
        if reported_by == "default":
            reported_by = DEFS[component].host if component in DEFS else "hive"
        ev = {"id": f"{self._n:032x}", "ts": datetime.fromtimestamp(
            T0 + (at if host_at is None else host_at), UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": host or f"{component}.box", "component": component,
            "step": f"deployment.{component}.cycle", "outcome": outcome, "from": frm, "to": to,
            "evidence": ""}
        if instance is not None:
            ev["instance"] = instance
        self.mon.ingest(ev, reported_by=reported_by)

    def deploy(self, component, *, at, to=T1, **kw):
        self.emit(component, "started", at=at, to=to, **kw)
        self.emit(component, "ok", at=at, to=to, **kw)

    def snap(self, *, target=T1, published="same", on_main=True, plan_commit="same",
             generation=1, past=(), slots=None):
        return Snapshot(
            components=self.mon.snapshot()["components"], target_commit=target,
            published_commit=target if published == "same" else published,
            target_on_main=on_main,
            plan_commit=target if plan_commit == "same" else plan_commit,
            generation=generation, past_targets=tuple(past), slots=slots)


def _at(s):
    return T0 + s


def _only(actions, kind):
    found = [a for a in actions if isinstance(a, kind)]
    assert len(found) == 1, actions
    return found[0]


def _stage1_done(w, at=0, to=T1):
    for c in STAGE1:
        w.deploy(c, at=at, to=to)


def _hold1(remaining, **kw):
    return Hold(stage=1, remaining_s=remaining, reason="holding stage 1", target_commit=T1,
                generation=1, permitted=STAGE1, **kw)


START_GW = Start(stage=1, order=0, components=("hive-gateway",), target_commit=T1, generation=1,
                 permitted=("hive-gateway",))
START_NODES = Start(stage=1, order=1, components=("hive-nodes",), target_commit=T1, generation=1,
                    permitted=STAGE1)
START_S2 = Start(stage=2, order=0, components=("whiteale-gateway",), target_commit=T1,
                 generation=1, permitted=STAGE1 + ("whiteale-gateway",))


# ------------------------------------------------------------------ hold
@pytest.mark.parametrize("elapsed, expected", [
    (0, _hold1(600)), (599, _hold1(1)), (600, START_S2), (5000, START_S2)])
def test_stage_two_waits_for_the_full_hold(elapsed, expected):
    w = World()
    _stage1_done(w, at=0)
    assert advance(w.snap(), DEFS, _at(elapsed)) == [expected]


def test_hold_runs_from_the_later_ok_and_each_definitions_hold_s():
    w = World()
    w.deploy("hive-gateway", at=0)
    w.deploy("hive-nodes", at=100)          # the later ok
    assert advance(w.snap(), DEFS, _at(650)) == [_hold1(50)]
    short = dict(DEFS, **{c: _defn(c, 1, DEFS[c].order, "hive", hold_s=60) for c in STAGE1})
    assert isinstance(advance(w.snap(), short, _at(160))[0], Start)


def test_hold_uses_herdr_arrival_not_the_host_clock():
    # The host claims its ok happened an hour ago; herdr received it just now.
    w = World()
    for c in STAGE1:
        w.deploy(c, at=0, host_at=-3600)
    assert advance(w.snap(), DEFS, _at(10)) == [_hold1(590)]


def test_a_fractional_arrival_never_ends_the_hold_early():
    w = World()
    _stage1_done(w, at=0.9)                  # received_at floors to T0; the hold must not
    assert advance(w.snap(), DEFS, _at(0.9 + 599.2)) == [_hold1(1)]
    assert advance(w.snap(), DEFS, _at(0.9 + 600)) == [START_S2]


# ------------------------------------------------------------ halt / reset
@pytest.mark.parametrize("outcome, frm, to", [
    ("failed", OLD, T1),
    ("refused", OLD, T1),
    ("rolled-back", T1, OLD),       # rolled back from the target to the previous version
])
def test_a_failed_component_halts_its_stage_and_blocks_the_next(outcome, frm, to):
    w = World()
    w.deploy("hive-gateway", at=0)
    w.emit("hive-nodes", "started", at=0)
    w.emit("hive-nodes", outcome, at=10, frm=frm, to=to)
    actions = advance(w.snap(), DEFS, _at(10_000))
    halt = _only(actions, Halt)
    assert halt.stage == 1 and halt.components == ("hive-nodes",)
    assert outcome in halt.reason
    assert not [a for a in actions if isinstance(a, Start)]
    assert permitted(actions) == ()


def test_a_failed_gateway_halts_before_nodes_start():
    w = World()
    w.emit("hive-gateway", "started", at=0)
    w.emit("hive-gateway", "failed", at=5)
    assert [type(a) for a in advance(w.snap(), DEFS, _at(10))] == [Halt]


def test_a_halt_in_stage_two_permits_stage_one_only():
    w = World()
    _stage1_done(w, at=0)
    w.emit("whiteale-gateway", "failed", at=700)
    actions = advance(w.snap(), DEFS, _at(800))
    halt = _only(actions, Halt)
    assert halt.stage == 2 and halt.components == ("whiteale-gateway",)
    assert permitted(actions) == STAGE1


def test_unhealthy_resets_the_hold():
    w = World()
    _stage1_done(w, at=0)
    w.emit("hive-nodes", "failed", at=300)
    assert [type(a) for a in advance(w.snap(), DEFS, _at(300))] == [Halt]
    w.deploy("hive-nodes", at=400)          # repaired: the hold starts again from here
    assert advance(w.snap(), DEFS, _at(900)) == [_hold1(100)]
    assert advance(w.snap(), DEFS, _at(999)) == [_hold1(1)]
    assert advance(w.snap(), DEFS, _at(1000)) == [START_S2]


def test_one_failed_instance_halts_even_when_another_instance_is_ok_later():
    w = World()
    w.deploy("hive-gateway", at=0)
    w.emit("hive-nodes", "started", at=0, host="node-a")
    w.emit("hive-nodes", "started", at=0, host="node-b")
    w.emit("hive-nodes", "failed", at=10, host="node-a")
    w.emit("hive-nodes", "ok", at=20, host="node-b")
    halt = _only(advance(w.snap(), DEFS, _at(10_000)), Halt)
    assert halt.components == ("hive-nodes",)
    # node-a recovers: healthy, held from the latest ok across instances
    w.emit("hive-nodes", "ok", at=50, host="node-a")
    assert advance(w.snap(), DEFS, _at(600)) == [_hold1(50)]


def test_a_late_arriving_older_ok_does_not_lift_a_halt():
    w = World()
    w.deploy("hive-gateway", at=0)
    w.emit("hive-nodes", "failed", at=100, host="node-a")
    # node-a's spool replays an ok its own clock stamped BEFORE the failure
    w.emit("hive-nodes", "ok", at=200, host="node-a", host_at=50)
    assert [type(a) for a in advance(w.snap(), DEFS, _at(10_000))] == [Halt]


def test_an_instance_still_updating_keeps_the_stage_open():
    w = World()
    w.deploy("hive-gateway", at=0)
    w.deploy("hive-nodes", at=0, host="node-a")
    w.emit("hive-nodes", "started", at=10, host="node-b")
    assert advance(w.snap(), DEFS, _at(10_000)) == [START_NODES]


# ------------------------------------------------------- instances (probes)
def test_an_instance_absent_for_the_target_is_listed_but_does_not_block():
    # The gateway job moved from cpu-a (T1) to cpu-b (T2): cpu-a never reports T2.
    w = World()
    w.deploy("hive-gateway", at=0, host="cpu-a")
    w.deploy("hive-nodes", at=0, host="gpu-1")
    w.deploy("hive-gateway", at=1000, to=T2, frm=T1, host="cpu-b")
    w.deploy("hive-nodes", at=1000, to=T2, frm=T1, host="gpu-1")
    hold = advance(w.snap(target=T2), DEFS, _at(1100))[0]
    assert isinstance(hold, Hold) and hold.stage == 1 and hold.remaining_s == 500
    assert hold.missing == ("hive-gateway:cpu-a",)
    assert isinstance(advance(w.snap(target=T2), DEFS, _at(1600))[0], Start)


def test_a_requeued_node_with_a_stable_instance_id_clears_its_own_failure():
    w = World()
    w.deploy("hive-gateway", at=0)
    w.emit("hive-nodes", "started", at=0, host="gpu-1", instance="job-42")
    w.emit("hive-nodes", "failed", at=5, host="gpu-1", instance="job-42")     # preempted
    w.deploy("hive-nodes", at=60, host="gpu-2", instance="job-42")            # requeued
    assert advance(w.snap(), DEFS, _at(700)) == [START_S2]


def test_without_an_instance_id_a_requeued_node_keeps_the_halt():
    # Containment: a failure toward the target is only cleared by the same instance
    # (or Task 5's audited forget). The emitters are to send a stable ``instance``.
    w = World()
    w.deploy("hive-gateway", at=0)
    w.emit("hive-nodes", "failed", at=5, host="gpu-1")
    w.deploy("hive-nodes", at=60, host="gpu-2")
    assert [type(a) for a in advance(w.snap(), DEFS, _at(10**7))] == [Halt]


def test_a_failure_pushed_out_of_the_timeline_still_halts():
    state = CoordinatorState()
    w = World()
    w.deploy("hive-gateway", at=0)
    w.emit("hive-nodes", "failed", at=1, host="node-0")
    state.observe(w.mon.snapshot()["components"], DEFS)
    for i in range(1, 61):
        w.emit("hive-nodes", "ok", at=1 + i, host=f"node-{i}")
        state.observe(w.mon.snapshot()["components"], DEFS)
    timeline = w.mon.snapshot()["components"]["hive-nodes"]["timeline"]
    assert not [e for e in timeline if e["outcome"] == "failed"]
    actions = advance(w.snap(slots=state.slots_for(T1, DEFS)), DEFS, _at(10_000))
    assert _only(actions, Halt).components == ("hive-nodes",)


def test_slot_eviction_keeps_failures_and_marks_an_evicted_one(monkeypatch):
    monkeypatch.setattr(dc, "INSTANCES_PER_RELEASE", 3)
    state = CoordinatorState()
    w = World()
    w.deploy("hive-gateway", at=0)
    w.emit("hive-nodes", "failed", at=1, host="bad")
    for i in range(5):
        w.emit("hive-nodes", "ok", at=2 + i, host=f"ok-{i}")
    state.observe(w.mon.snapshot()["components"], DEFS)
    slots = state.slots_for(T1, DEFS)
    assert "bad" in slots["hive-nodes"]["instances"] and len(slots["hive-nodes"]["instances"]) == 3
    assert not slots["hive-nodes"]["failure_evicted"]
    for i in range(3):
        w.emit("hive-nodes", "failed", at=20 + i, host=f"f-{i}")
    state.observe(w.mon.snapshot()["components"], DEFS)
    slots = state.slots_for(T1, DEFS)
    assert slots["hive-nodes"]["failure_evicted"]
    w2 = World()
    halt = _only(advance(w2.snap(slots=slots), DEFS, _at(10_000)), Halt)
    assert "failed" in halt.reason or "failure-evicted" in halt.reason


# --------------------------------------------------------------- ordering
def test_ordering_within_a_stage_gateways_then_nodes_without_a_hold_between():
    w = World()
    assert advance(w.snap(), DEFS, _at(0)) == [START_GW]
    w.emit("hive-gateway", "started", at=0)
    assert advance(w.snap(), DEFS, _at(1)) == [START_GW]
    w.emit("hive-gateway", "ok", at=5)
    # nodes may start as soon as the gateways are healthy: the hold is between stages only
    assert advance(w.snap(), DEFS, _at(6)) == [START_NODES]


def test_components_in_one_order_group_are_sorted_by_name():
    defs = dict(DEFS, **{"farm-gateway": _defn("farm-gateway", 1, 0, "farm")})
    w = World()
    start = _only(advance(w.snap(), defs, _at(0)), Start)
    assert start.components == ("farm-gateway", "hive-gateway") == start.permitted


def test_a_rolled_out_release_is_idle_and_permits_everything():
    w = World()
    _stage1_done(w, at=0)
    for c in ("whiteale-gateway", "whiteale-laya"):
        w.deploy(c, at=700)
    assert advance(w.snap(), DEFS, _at(1000)) == [
        Hold(stage=2, remaining_s=300, reason="holding stage 2", target_commit=T1, generation=1,
             permitted=ALL)]
    assert advance(w.snap(), DEFS, _at(1300)) == [
        Idle(reason="rolled out", target_commit=T1, permitted=ALL)]


def test_an_ok_for_an_older_commit_is_not_healthy_for_the_target():
    w = World()
    _stage1_done(w, at=0, to=OLD)
    start = _only(advance(w.snap(), DEFS, _at(10_000)), Start)
    assert start.stage == 1 and start.missing == ("hive-gateway:hive-gateway.box",
                                                  "hive-nodes:hive-nodes.box")


# ------------------------------------------------------------- permission
def test_permission_follows_a_stage_one_node_from_started_to_ok_between_ticks():
    w = World()
    w.deploy("hive-gateway", at=0)
    w.emit("hive-nodes", "started", at=10, host="node-a")
    assert permitted(advance(w.snap(), DEFS, _at(11))) == STAGE1
    w.emit("hive-nodes", "ok", at=20, host="node-a")
    actions = advance(w.snap(), DEFS, _at(21))
    assert actions == [_hold1(599)] and permitted(actions) == STAGE1
    assert permitted(advance(w.snap(), DEFS, _at(620))) == STAGE1 + ("whiteale-gateway",)


def test_a_halt_on_hive_nodes_denies_every_other_node():
    w = World()
    w.deploy("hive-gateway", at=0)
    w.emit("hive-nodes", "started", at=10, host="node-a")
    w.emit("hive-nodes", "failed", at=11, host="node-a")
    actions = advance(w.snap(), DEFS, _at(12))
    assert _only(actions, Halt).components == ("hive-nodes",)
    assert "hive-nodes" not in permitted(actions)       # node-b..z may not proceed
    assert "hive-gateway" not in permitted(actions)     # the halted stage stops entirely


@pytest.mark.parametrize("kw", [
    {"published": None}, {"on_main": None}, {"on_main": False},
    {"target": OLD, "plan_commit": T1, "past": (OLD,)}, {"target": None, "plan_commit": None},
])
def test_gate_actions_permit_nothing(kw):
    w = World()
    _stage1_done(w, at=0)
    assert permitted(advance(w.snap(**kw), DEFS, _at(10_000))) == ()


def test_a_start_held_for_an_unpublished_release_permits_nothing():
    w = World()
    w.deploy("hive-gateway", at=0)
    actions = advance(w.snap(published=OLD), DEFS, _at(1))
    assert actions == [Hold(stage=1, remaining_s=None, reason="release not confirmed",
                            target_commit=T1, generation=1)]
    assert permitted(actions) == () and permitted([]) == ()


# ------------------------------------------------------------------ replan
def test_a_new_green_release_mid_roll_restarts_from_the_newest_commit():
    w = World()
    _stage1_done(w, at=0, to=T1)
    w.emit("whiteale-gateway", "started", at=700, to=T1)
    assert _only(advance(w.snap(target=T1), DEFS, _at(700)), Start).stage == 2
    actions = advance(w.snap(target=T2, plan_commit=T1, generation=1, past=(OLD,)), DEFS, _at(800))
    assert actions == [
        Replan(new_target=T2, generation=2, previous_commit=T1),
        Start(stage=1, order=0, components=("hive-gateway",), target_commit=T2, generation=2,
              permitted=("hive-gateway",),
              missing=("hive-gateway:hive-gateway.box", "hive-nodes:hive-nodes.box",
                       "whiteale-gateway:whiteale-gateway.box"))]
    assert permitted(actions) == ("hive-gateway",)


def test_a_first_target_is_a_replan_to_generation_one():
    w = World()
    actions = advance(w.snap(plan_commit=None, generation=0), DEFS, _at(0))
    assert actions[0] == Replan(new_target=T1, generation=1, previous_commit=None)
    assert actions[1].generation == 1


def test_a_target_that_was_a_past_target_is_never_replanned_onto():
    w = World()
    actions = advance(w.snap(target=OLD, plan_commit=T1, generation=3, past=(OLD,)), DEFS, _at(0))
    assert [type(a) for a in actions] == [Hold]
    assert "earlier release" in actions[0].reason


def test_a_past_target_reported_off_main_holds_and_does_not_halt():
    # A rerun of a past target is behind plan_commit, so its evidence says on_main=False.
    w = World()
    actions = advance(w.snap(target=OLD, plan_commit=T1, generation=3, past=(OLD,),
                             on_main=False), DEFS, _at(0))
    assert [type(a) for a in actions] == [Hold] and "earlier release" in actions[0].reason


# ---------------------------------------------------------------- safety
def test_events_from_a_host_that_is_not_the_definitions_host_are_ignored():
    w = World()
    # accepted before definitions were configured: another host and an unauthenticated one
    w.deploy("hive-gateway", at=0, reported_by="whiteale")
    w.deploy("hive-nodes", at=0, reported_by=None)
    assert advance(w.snap(), DEFS, _at(10_000)) == [START_GW]
    w.emit("hive-gateway", "failed", at=10, reported_by="whiteale")
    assert not [a for a in advance(w.snap(), DEFS, _at(10_000)) if isinstance(a, Halt)]


@pytest.mark.parametrize("published", [None, OLD, "", T1[:6]])
def test_an_unpublished_release_holds(published):
    w = World()
    actions = advance(w.snap(published=published), DEFS, _at(0))
    assert actions == [Hold(stage=1, remaining_s=None, reason="release not confirmed",
                            target_commit=T1, generation=1)]


def test_a_published_short_commit_is_accepted():
    w = World()
    assert isinstance(advance(w.snap(published=T1[:12]), DEFS, _at(0))[0], Start)


def test_a_target_no_longer_on_main_halts_without_advancing():
    w = World()
    _stage1_done(w, at=0)
    actions = advance(w.snap(on_main=False, plan_commit=OLD, generation=4), DEFS, _at(10_000))
    assert actions == [Halt(stage=None, reason="target no longer on main", target_commit=T1,
                            generation=4, components=())]


def test_default_evidence_holds_as_release_not_confirmed():
    w = World()
    snap = Snapshot(components=w.mon.snapshot()["components"], target_commit=T1)
    assert advance(snap, DEFS, _at(0)) == [Hold(stage=None, remaining_s=None,
                                                reason="release not confirmed",
                                                target_commit=T1, generation=0)]


def test_unknown_ancestry_holds():
    w = World()
    actions = advance(w.snap(on_main=None, plan_commit=OLD, generation=4), DEFS, _at(0))
    assert actions == [Hold(stage=None, remaining_s=None, reason="target ancestry not confirmed",
                            target_commit=T1, generation=4)]


def test_no_target_or_no_definitions_is_idle():
    w = World()
    assert advance(w.snap(target=None, plan_commit=None), DEFS, _at(0)) == [
        Idle(reason="no green release", target_commit=None)]
    assert advance(w.snap(), {}, _at(0)) == [Idle(reason="no definitions", target_commit=T1)]


def test_same_commit():
    assert same_commit(T1, T1) and same_commit(T1, T1[:7]) and same_commit(T1[:9], T1)
    assert not same_commit(T1, T1[:6]) and not same_commit(T1, "") and not same_commit(None, T1)
    assert not same_commit(T1, T2)


# ------------------------------------------------------------- idempotent
@pytest.mark.parametrize("build", [
    lambda w: w.snap(),
    lambda w: w.snap(target=T2, plan_commit=T1),
    lambda w: w.snap(published=None),
    lambda w: w.snap(on_main=False),
])
def test_advance_is_idempotent(build):
    w = World()
    _stage1_done(w, at=0)
    w.emit("whiteale-gateway", "failed", at=700)
    snap = build(w)
    first = advance(snap, DEFS, _at(900))
    assert advance(snap, DEFS, _at(900)) == first
    assert advance(build(w), DEFS, _at(900)) == first


# ---------------------------------------------------------- stateful wrapper
def _write_defs(path, defs=DEFS):
    os.makedirs(path, mode=0o755, exist_ok=True)
    os.chmod(path, 0o755)
    for d in defs.values():
        fn = os.path.join(path, f"{d.component}.yaml")
        with open(fn, "w") as fh:
            yaml.safe_dump({"component": d.component, "stage": d.stage, "order": d.order,
                            "host": d.host, "updater": d.updater, "follows": d.follows,
                            "restart": d.restart, "gpu": d.gpu, "health": d.health,
                            "rollback": d.rollback}, fh)
        os.chmod(fn, 0o644)


class CI:
    def __init__(self):
        self.calls = 0
        self.rows = []

    def green(self, commit, run):
        self.rows.append({"repository": TRACK[0], "run_number": run, "revision": commit,
                          "branch": "main", "event": "push", "state": "success"})

    def __call__(self):
        self.calls += 1
        return {"pipelines": {"inferweave": list(self.rows)}}


class Evidence:
    """Follows the contract: published = the target; on_main False for a commit behind the
    plan (``behind``), True otherwise; ``fail`` tracks raise."""

    def __init__(self, behind=(), fail=()):
        self.calls = []
        self.behind = set(behind)
        self.fail = set(fail)

    def __call__(self, track, target, plan_commit):
        self.calls.append((track, target, plan_commit))
        if track in self.fail:
            raise RuntimeError("release API down")
        return ReleaseEvidence(published_commit=target, on_main=target not in self.behind)


def _coordinator(tmp_path, repo_ref, ci, evidence, clock, defs=DEFS):
    defs_dir = str(tmp_path / "pipelines")
    _write_defs(defs_dir, defs)
    definitions = DeploymentDefinitions(lambda: defs_dir, ci_snapshot=ci)
    return DeploymentCoordinator(definitions, monitor=lambda: repo_ref[0].deployments,
                                 state=lambda: repo_ref[0].deployment_plans,
                                 evidence=evidence, now=clock)


def test_one_ci_read_and_one_evidence_read_per_tick(tmp_path):
    clock = Clock()
    repo = [ControlPlaneRepo(now=clock)]
    ci, ev = CI(), Evidence()
    ci.green(T1, 10)
    coord = _coordinator(tmp_path, repo, ci, ev, clock)
    out = coord.tick()
    assert ci.calls == 1 and len(ev.calls) == 1
    assert out[TRACK][0] == Replan(new_target=T1, generation=1, previous_commit=None)
    coord.tick()
    assert ci.calls == 2 and len(ev.calls) == 2


def test_without_evidence_the_coordinator_only_holds(tmp_path):
    clock = Clock()
    repo = [ControlPlaneRepo(now=clock)]
    ci = CI()
    ci.green(T1, 10)
    defs_dir = str(tmp_path / "pipelines")
    _write_defs(defs_dir)
    coord = DeploymentCoordinator(DeploymentDefinitions(lambda: defs_dir, ci_snapshot=ci),
                                  monitor=lambda: repo[0].deployments,
                                  state=lambda: repo[0].deployment_plans, now=clock)
    assert coord.tick()[TRACK] == [Hold(stage=None, remaining_s=None, reason="release not confirmed",
                                        target_commit=T1, generation=0)]
    assert repo[0].deployment_plans.plan(TRACK).generation == 0


def test_a_raising_evidence_call_holds_only_its_track(tmp_path):
    clock = Clock()
    repo = [ControlPlaneRepo(now=clock)]
    ci = CI()
    ci.green(T1, 10)
    defs = dict(DEFS, **{"zapp-console": _defn("zapp-console", 2, 0, "zapp", track=TRACK2)})
    coord = _coordinator(tmp_path, repo, ci, Evidence(fail={TRACK2}), clock, defs)
    out = coord.tick()
    assert out[TRACK2] == [Hold(stage=None, remaining_s=None,
                                reason="release evidence unavailable: RuntimeError",
                                target_commit=T1, generation=0)]
    assert isinstance(out[TRACK][0], Replan)


def test_an_unreadable_monitor_holds_every_track(tmp_path):
    clock = Clock()
    repo = [ControlPlaneRepo(now=clock)]
    ci = CI()
    ci.green(T1, 10)
    coord = _coordinator(tmp_path, repo, ci, Evidence(), clock)

    def broken():
        raise RuntimeError("boom")

    coord._monitor = broken
    out = coord.tick()
    assert out[TRACK][0].reason == "deployment events unavailable" and permitted(out[TRACK]) == ()


def test_unreadable_definitions_decide_nothing():
    class Broken:
        def definitions(self):
            raise OSError("gone")

    coord = DeploymentCoordinator(Broken(), monitor=lambda: None, state=lambda: CoordinatorState())
    assert coord.tick() == {}


def test_generation_and_attempts_persist_across_a_restart(tmp_path, acknowledged_sink):
    clock = Clock()
    repo = [ControlPlaneRepo(sink=acknowledged_sink, now=clock)]
    ci, ev = CI(), Evidence()
    ci.green(T1, 10)
    coord = _coordinator(tmp_path, repo, ci, ev, clock)
    assert coord.tick()[TRACK][0].generation == 1
    ci.green(T2, 11)
    actions = coord.tick()[TRACK]
    assert actions[0] == Replan(new_target=T2, generation=2, previous_commit=T1)
    assert coord.tick()[TRACK] == [
        Start(stage=1, order=0, components=("hive-gateway",), target_commit=T2, generation=2,
              permitted=("hive-gateway",))]
    gw = {"id": "ab" * 16, "ts": "2026-10-28T12:00:00Z", "host": "login2", "component": "hive-gateway",
          "step": "deployment.hive-gateway.cycle", "outcome": "started", "from": T1, "to": T2,
          "evidence": ""}
    repo[0].deployments.ingest(gw, reported_by="hive")
    repo[0].deployments.ingest(dict(gw, id="cd" * 16, outcome="failed"), reported_by="hive")
    coord.tick()
    assert repo[0].deployment_plans.attempts("hive-gateway", T2) == 1

    class Source:
        def missions(self):
            return []

        def sessions(self):
            return []

        def services(self):
            return []

        def hosts(self):
            return []

        def events(self):
            return list(acknowledged_sink.events)

    fresh = ControlPlaneRepo(now=clock)       # an empty monitor: the failure lives in the fold
    load_into(fresh, Source())
    assert not any(k[0] == "deployment_plan" for k in fresh._events)
    fresh.deployments = DeploymentsMonitor(now=clock)
    repo[0] = fresh
    state = fresh.deployment_plans
    assert state.plan(TRACK).commit == T2 and state.plan(TRACK).generation == 2
    assert state.plan(TRACK).past == (T1,)
    assert state.attempts("hive-gateway", T2) == 1
    coord2 = _coordinator(tmp_path / "again", repo, ci, ev, clock)
    actions = coord2.tick()[TRACK]
    assert not [a for a in actions if isinstance(a, Replan)]
    assert _only(actions, Halt).components == ("hive-gateway",) and actions[0].generation == 2


def test_a_restarted_server_seeing_only_a_rerun_of_a_past_target_does_not_go_back(tmp_path):
    clock = Clock()
    repo = [ControlPlaneRepo(now=clock)]
    state = repo[0].deployment_plans
    state.adopt(TRACK, Replan(new_target=T1, generation=1, previous_commit=None))
    state.adopt(TRACK, Replan(new_target=T2, generation=2, previous_commit=T1))
    ci, ev = CI(), Evidence(behind={T1})     # T1 is behind the plan: on_main False
    ci.green(T1, 99)                # a rerun of T1: the only row the new server sees
    out = _coordinator(tmp_path, repo, ci, ev, clock).tick()[TRACK]
    assert [type(a) for a in out] == [Hold] and "earlier release" in out[0].reason
    assert state.plan(TRACK).generation == 2


def test_attempts_count_restarts_per_instance_not_fan_out():
    state = CoordinatorState()
    w = World()
    for host in ("node-a", "node-b", "node-c"):
        w.emit("hive-nodes", "started", at=0, host=host)
    w.emit("hive-nodes", "started", at=60, host="node-a")      # node-a retried
    w.emit("hive-nodes", "started", at=60, host="node-x", reported_by="farm")   # foreign
    w.emit("hive-nodes", "started", at=70, host="gpu-9", instance="job-7")
    w.emit("hive-nodes", "started", at=80, host="gpu-3", instance="job-7")       # same job, requeued
    state.observe(w.mon.snapshot()["components"], DEFS)
    assert state.observe(w.mon.snapshot()["components"], DEFS) == 0        # idempotent
    assert state.attempts("hive-nodes", T1) == 2
    assert state.attempts("hive-nodes", T1, instance="node-b") == 1
    assert state.attempts("hive-nodes", T1, instance="job-7") == 2
    assert state.attempts("hive-nodes", T1, instance="node-x") == 0
    assert state.attempts("hive-nodes", T2) == 0
    assert state.attempts("hive-nodes", T1[:10]) == 2


def test_seventeen_releases_in_the_timeline_do_not_churn():
    state = CoordinatorState(persist=lambda r: None)
    w = World()
    for i in range(17):
        w.emit("hive-gateway", "started", at=i, to=f"{i:02d}".ljust(40, "a"))
    comps = w.mon.snapshot()["components"]
    assert state.observe(comps, DEFS) > 0
    assert [state.observe(comps, DEFS) for _ in range(3)] == [0, 0, 0]


def test_a_full_timeline_of_distinct_releases_does_not_churn():
    state = CoordinatorState()
    w = World()
    for i in range(60):
        w.emit("hive-gateway", "started", at=i, to=f"{i:02d}".ljust(40, "c"))
    comps = w.mon.snapshot()["components"]
    state.observe(comps, DEFS)
    assert state.observe(comps, DEFS) == 0


def test_hydrate_skips_malformed_rows_and_reapplies_caps(monkeypatch):
    monkeypatch.setattr(dc, "INSTANCES_PER_RELEASE", 2)
    good = {"kind": "slot", "component": "hive-nodes", "release": T1, "outcome": "ok",
            "host_ts": "2026-10-28T12:00:00Z", "rx": T0}
    rows = [
        None, "x", {"payload": "nope"}, {"payload": {"kind": "unknown"}},
        {"payload": {"kind": "plan", "track": "abc", "generation": 1, "past": []}},
        {"payload": {"kind": "plan", "track": list(TRACK), "generation": True, "past": [],
                     "commit": T1}},
        {"payload": {"kind": "plan", "track": list(TRACK), "generation": 1, "past": [5],
                     "commit": T1}},
        {"payload": {"kind": "attempt", "component": "c", "release": T1, "instance": "i" * 500,
                     "key": "k"}},
        {"payload": {"kind": "attempt", "component": "c", "release": "bad release", "instance": "i",
                     "key": "k"}},
        {"payload": {**good, "instance": "a", "outcome": "exploded"}},
        {"payload": {**good, "instance": "a", "rx": float("nan")}},
        {"payload": {**good, "instance": "a", "rx": True}},
        {"payload": {**good, "instance": "a", "host_ts": "yesterday"}},
        {"sequence": "1", "payload": {**good, "instance": "x" * 500}},
    ]
    state = CoordinatorState()
    assert state.hydrate(rows) == 0
    assert state.plan(TRACK).generation == 0
    ok_rows = [{"sequence": i, "payload": {**good, "instance": f"n{i}", "seq": i}} for i in range(5)]
    assert state.hydrate(ok_rows) == 5
    assert len(state.slots_for(T1, {"hive-nodes": None})["hive-nodes"]["instances"]) == 2


def test_a_failure_evicted_release_halts_even_with_only_healthy_slots_left():
    state = CoordinatorState()
    state.hydrate([
        {"sequence": 1, "payload": {"kind": "failure-evicted", "component": "hive-nodes",
                                    "release": T1, "seq": 1}},
        {"sequence": 2, "payload": {"kind": "slot", "component": "hive-nodes", "release": T1,
                                    "instance": "n1", "outcome": "ok",
                                    "host_ts": "2026-10-28T12:00:00Z", "rx": T0, "seq": 2}},
        {"sequence": 3, "payload": {"kind": "slot", "component": "hive-gateway", "release": T1,
                                    "instance": "g", "outcome": "ok",
                                    "host_ts": "2026-10-28T12:00:00Z", "rx": T0, "seq": 3}},
    ])
    halt = _only(advance(World().snap(slots=state.slots_for(T1, DEFS)), DEFS, _at(10_000)), Halt)
    assert halt.reason == "stage 1 halted: hive-nodes: failure-evicted"


def test_releases_are_kept_least_recently_seen_first(monkeypatch):
    monkeypatch.setattr(dc, "RELEASES_PER_COMPONENT", 2)
    a, b, c = ("a" * 40, "b" * 40, "c" * 40)
    state = CoordinatorState()
    w = World()
    w.emit("hive-gateway", "started", at=0, to=a)
    w.emit("hive-gateway", "started", at=1, to=b)
    state.observe(w.mon.snapshot()["components"], DEFS)
    w.emit("hive-gateway", "ok", at=2, to=a)           # a seen again: b is now the oldest
    w.emit("hive-gateway", "started", at=3, to=c)
    state.observe(w.mon.snapshot()["components"], DEFS)
    names = {"hive-gateway": None}
    assert state.slots_for(a, names)["hive-gateway"]["instances"]
    assert not state.slots_for(b, names)["hive-gateway"]["instances"]
