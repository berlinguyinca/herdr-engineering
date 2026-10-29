"""Deployment coordinator: the stage/hold/halt rules (deploy-4 Task 3).

The coordinator is a pure ``advance(snapshot, definitions, now)``; component state is
built from the real ``DeploymentsMonitor`` here so the input contract is the one the
server produces. Time is a fake clock throughout.
"""
import os
from datetime import UTC, datetime

import pytest
import yaml

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


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _defn(component, stage, order, host, hold_s=600):
    return Definition(
        component=component, stage=stage, order=order, host=host, updater="u",
        follows={"repo": TRACK[0], "tag": "node-ci-latest", "asset": "inferweave",
                 "branch": TRACK[1], "ci_context": TRACK[2]},
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


class World:
    """A monitor on a fake clock, fed like the ingest route feeds it."""

    def __init__(self):
        self.clock = Clock()
        self.mon = DeploymentsMonitor(now=self.clock)
        self._n = 0

    def emit(self, component, outcome, *, at, to=T1, frm=OLD, reported_by="default", host=None):
        self.clock.t = T0 + at
        self._n += 1
        if reported_by == "default":
            reported_by = DEFS[component].host if component in DEFS else "hive"
        ev = {"id": f"{self._n:032x}", "ts": datetime.fromtimestamp(T0 + at, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"), "host": host or f"{component}.box", "component": component,
            "step": f"deployment.{component}.cycle", "outcome": outcome, "from": frm, "to": to,
            "evidence": ""}
        self.mon.ingest(ev, reported_by=reported_by)

    def deploy(self, component, *, at, to=T1, **kw):
        self.emit(component, "started", at=at, to=to, **kw)
        self.emit(component, "ok", at=at, to=to, **kw)

    def snap(self, *, target=T1, published="same", on_main=True, plan_commit="same",
             generation=1, past=()):
        return Snapshot(
            components=self.mon.snapshot()["components"], target_commit=target,
            published_commit=target if published == "same" else published,
            target_on_main=on_main,
            plan_commit=target if plan_commit == "same" else plan_commit,
            generation=generation, past_targets=tuple(past))


def _at(s):
    return T0 + s


def _only(actions, kind):
    found = [a for a in actions if isinstance(a, kind)]
    assert len(found) == 1, actions
    return found[0]


def _stage1_done(w, at=0, to=T1):
    for c in STAGE1:
        w.deploy(c, at=at, to=to)


# ------------------------------------------------------------------ hold
@pytest.mark.parametrize("elapsed, expected", [
    (0, Hold(stage=1, remaining_s=600, reason="holding stage 1", target_commit=T1, generation=1)),
    (599, Hold(stage=1, remaining_s=1, reason="holding stage 1", target_commit=T1, generation=1)),
    (600, Start(stage=2, order=0, components=STAGE1 + ("whiteale-gateway",),
                target_commit=T1, generation=1)),
    (5000, Start(stage=2, order=0, components=STAGE1 + ("whiteale-gateway",),
                 target_commit=T1, generation=1)),
])
def test_stage_two_waits_for_the_full_hold(elapsed, expected):
    w = World()
    _stage1_done(w, at=0)
    assert advance(w.snap(), DEFS, _at(elapsed)) == [expected]


def test_hold_runs_from_the_later_ok_and_each_definitions_hold_s():
    w = World()
    w.deploy("hive-gateway", at=0)
    w.deploy("hive-nodes", at=100)          # the later ok
    assert advance(w.snap(), DEFS, _at(650)) == [
        Hold(stage=1, remaining_s=50, reason="holding stage 1", target_commit=T1, generation=1)]
    short = dict(DEFS, **{c: _defn(c, 1, DEFS[c].order, "hive", hold_s=60) for c in STAGE1})
    assert isinstance(advance(w.snap(), short, _at(160))[0], Start)


def test_hold_uses_herdr_arrival_not_the_host_clock():
    # The host claims its ok happened an hour ago; herdr received it just now.
    w = World()
    _stage1_done(w, at=0)
    for row in w.mon.snapshot()["components"].values():
        for e in row["timeline"]:
            assert e["received_at"] == datetime.fromtimestamp(T0, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap = w.snap()
    for row in snap.components.values():
        for e in row["timeline"]:
            e["host_ts"] = "2026-10-28T11:00:00Z"
    assert isinstance(advance(snap, DEFS, _at(10))[0], Hold)


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


def test_a_failed_gateway_halts_before_nodes_start():
    w = World()
    w.emit("hive-gateway", "started", at=0)
    w.emit("hive-gateway", "failed", at=5)
    assert [type(a) for a in advance(w.snap(), DEFS, _at(10))] == [Halt]


def test_a_halt_in_stage_two_is_reported_with_stage_two():
    w = World()
    _stage1_done(w, at=0)
    w.emit("whiteale-gateway", "failed", at=700)
    halt = _only(advance(w.snap(), DEFS, _at(800)), Halt)
    assert halt.stage == 2 and halt.components == ("whiteale-gateway",)


def test_unhealthy_resets_the_hold():
    w = World()
    _stage1_done(w, at=0)
    w.emit("hive-nodes", "failed", at=300)
    assert [type(a) for a in advance(w.snap(), DEFS, _at(300))] == [Halt]
    w.deploy("hive-nodes", at=400)          # repaired: the hold starts again from here
    cases = [(900, Hold(stage=1, remaining_s=100, reason="holding stage 1", target_commit=T1,
                        generation=1)),
             (999, Hold(stage=1, remaining_s=1, reason="holding stage 1", target_commit=T1,
                        generation=1))]
    for t, want in cases:
        assert advance(w.snap(), DEFS, _at(t)) == [want]
    assert _only(advance(w.snap(), DEFS, _at(1000)), Start).stage == 2


def test_one_failed_instance_halts_even_when_another_instance_is_ok_later():
    # hive-nodes is one component with many GPU nodes reporting it (distinct event hosts).
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
    assert advance(w.snap(), DEFS, _at(600)) == [
        Hold(stage=1, remaining_s=50, reason="holding stage 1", target_commit=T1, generation=1)]


def test_an_instance_still_updating_keeps_the_stage_open():
    w = World()
    w.deploy("hive-gateway", at=0)
    w.deploy("hive-nodes", at=0, host="node-a")
    w.emit("hive-nodes", "started", at=10, host="node-b")
    start = _only(advance(w.snap(), DEFS, _at(10_000)), Start)
    assert (start.stage, start.order) == (1, 1)


# --------------------------------------------------------------- ordering
def test_ordering_within_a_stage_gateways_then_nodes_without_a_hold_between():
    w = World()
    assert advance(w.snap(), DEFS, _at(0)) == [
        Start(stage=1, order=0, components=("hive-gateway",), target_commit=T1, generation=1)]
    w.emit("hive-gateway", "started", at=0)
    assert _only(advance(w.snap(), DEFS, _at(1)), Start).components == ("hive-gateway",)
    w.emit("hive-gateway", "ok", at=5)
    # nodes may start as soon as the gateways are healthy: the hold is between stages only
    assert advance(w.snap(), DEFS, _at(6)) == [
        Start(stage=1, order=1, components=STAGE1, target_commit=T1, generation=1)]


def test_components_in_one_order_group_are_sorted_by_name():
    defs = dict(DEFS, **{"farm-gateway": _defn("farm-gateway", 1, 0, "farm")})
    w = World()
    start = _only(advance(w.snap(), defs, _at(0)), Start)
    assert start.components == ("farm-gateway", "hive-gateway")


def test_a_rolled_out_release_is_idle():
    w = World()
    _stage1_done(w, at=0)
    for c in ("whiteale-gateway", "whiteale-laya"):
        w.deploy(c, at=700)
    assert advance(w.snap(), DEFS, _at(1000)) == [
        Hold(stage=2, remaining_s=300, reason="holding stage 2", target_commit=T1, generation=1)]
    assert advance(w.snap(), DEFS, _at(1300)) == [Idle(reason="rolled out", target_commit=T1)]


def test_an_ok_for_an_older_commit_is_not_healthy_for_the_target():
    w = World()
    _stage1_done(w, at=0, to=OLD)
    assert _only(advance(w.snap(), DEFS, _at(10_000)), Start).stage == 1


# ------------------------------------------------------------------ replan
def test_a_new_green_release_mid_roll_restarts_from_the_newest_commit():
    w = World()
    _stage1_done(w, at=0, to=T1)
    w.emit("whiteale-gateway", "started", at=700, to=T1)
    assert _only(advance(w.snap(target=T1), DEFS, _at(700)), Start).stage == 2
    actions = advance(w.snap(target=T2, plan_commit=T1, generation=1, past=(OLD,)), DEFS, _at(800))
    assert actions == [
        Replan(new_target=T2, generation=2, previous_commit=T1),
        Start(stage=1, order=0, components=("hive-gateway",), target_commit=T2, generation=2)]


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


# ---------------------------------------------------------------- safety
def test_events_from_a_host_that_is_not_the_definitions_host_are_ignored():
    w = World()
    # accepted before definitions were configured: another host and an unauthenticated one
    w.deploy("hive-gateway", at=0, reported_by="whiteale")
    w.deploy("hive-nodes", at=0, reported_by=None)
    assert _only(advance(w.snap(), DEFS, _at(10_000)), Start) == Start(
        stage=1, order=0, components=("hive-gateway",), target_commit=T1, generation=1)
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
def _write_defs(path):
    os.makedirs(path, mode=0o755, exist_ok=True)
    os.chmod(path, 0o755)
    for d in DEFS.values():
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
    def __init__(self):
        self.calls = []

    def __call__(self, track, target, plan_commit):
        self.calls.append((track, target, plan_commit))
        return ReleaseEvidence(published_commit=target, on_main=True)


def _coordinator(tmp_path, repo_ref, ci, evidence, clock):
    defs_dir = str(tmp_path / "pipelines")
    _write_defs(defs_dir)
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
        Start(stage=1, order=0, components=("hive-gateway",), target_commit=T2, generation=2)]
    gw = {"id": "ab" * 16, "ts": "2026-10-28T12:00:00Z", "host": "login2", "component": "hive-gateway",
          "step": "deployment.hive-gateway.cycle", "outcome": "started", "from": T1, "to": T2,
          "evidence": ""}
    repo[0].deployments.ingest(gw, reported_by="hive")
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

    fresh = ControlPlaneRepo(now=clock)
    load_into(fresh, Source())
    assert not any(k[0] == "deployment_plan" for k in fresh._events)
    repo[0] = fresh
    # a fresh server: Task 2's in-memory plan starts over, the coordinator's generation does not
    state = fresh.deployment_plans
    assert state.plan(TRACK).commit == T2 and state.plan(TRACK).generation == 2
    assert state.plan(TRACK).past == (T1,)
    assert state.attempts("hive-gateway", T2) == 1
    coord2 = _coordinator(tmp_path / "again", repo, ci, ev, clock)
    actions = coord2.tick()[TRACK]
    assert not [a for a in actions if isinstance(a, Replan)]
    assert actions[0].generation == 2


def test_a_restarted_server_seeing_only_a_rerun_of_a_past_target_does_not_go_back(tmp_path):
    clock = Clock()
    repo = [ControlPlaneRepo(now=clock)]
    state = repo[0].deployment_plans
    state.adopt(TRACK, Replan(new_target=T1, generation=1, previous_commit=None))
    state.adopt(TRACK, Replan(new_target=T2, generation=2, previous_commit=T1))
    ci, ev = CI(), Evidence()
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
    state.observe(w.mon.snapshot()["components"], DEFS)
    state.observe(w.mon.snapshot()["components"], DEFS)        # idempotent
    assert state.attempts("hive-nodes", T1) == 2
    assert state.attempts("hive-nodes", T1, instance="node-b") == 1
    assert state.attempts("hive-nodes", T1, instance="node-x") == 0
    assert state.attempts("hive-nodes", T2) == 0
    assert state.attempts("hive-nodes", T1[:10]) == 2
