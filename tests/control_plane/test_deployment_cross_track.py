"""Cross-track gate: a definition's optional ``requires`` holds its whole track until a
stage of another ``follows`` track has been healthy for its hold (deploy-4 follow-up B).

zapp-console follows metabolomics-us/inferweave-gateway; it must not start until stage 1
of the InferWeave/inferweave track is done. The required track's state is the
coordinator's own view of that track in the same tick.
"""
import os

import pytest
import yaml

from herdr_engineering.control_plane.deployment_actions import permit_decision
from herdr_engineering.control_plane.deployment_coordinator import (
    DeploymentCoordinator,
    Hold,
    Idle,
    ReleaseEvidence,
    Replan,
    Snapshot,
    Start,
    advance,
    permitted,
    requirement_status,
    track_views,
)
from herdr_engineering.control_plane.deployment_definitions import (
    Definition,
    DeploymentDefinitions,
    load_definitions,
    validate_definition,
    validate_dir,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_deployment_coordinator import OLD, T0, T1, T2, Clock, World

IW = ("InferWeave/inferweave", "main", "ci/woodpecker/push/rust")
GW = ("metabolomics-us/inferweave-gateway", "main", "ci/woodpecker/push/console")
G1 = "a" * 40
REQ_IW1 = [{"repo": IW[0], "ci_context": IW[2], "stage": 1}]


def _doc(component, stage, order, host, track=IW, requires=None, hold_s=600, tag="node-ci-latest"):
    doc = {"component": component, "stage": stage, "order": order, "host": host,
           "updater": "u",
           "follows": {"repo": track[0], "tag": tag, "asset": "inferweave", "branch": "main",
                       "ci_context": track[2]},
           "restart": "gateway-cycle" if order == 0 else "process-restart-in-allocation",
           "gpu": False, "health": {"probe": "served-request", "timeout_s": 600, "hold_s": hold_s},
           "rollback": {"strategy": "previous-version"}}
    if requires is not None:
        doc["requires"] = requires
    return doc


def _defn(doc):
    return Definition(
        component=doc["component"], stage=doc["stage"], order=doc["order"], host=doc["host"],
        updater=doc["updater"], follows=dict(doc["follows"]), restart=doc["restart"],
        gpu=doc["gpu"], health=dict(doc["health"]), rollback=dict(doc["rollback"]),
        requires=tuple(((r["repo"], "main", r["ci_context"]), r["stage"])
                       for r in doc.get("requires") or ()))


DOCS = [
    _doc("hive-gateway", 1, 0, "hive"),
    _doc("hive-nodes", 1, 1, "hive"),
    _doc("bender-node", 2, 0, "bender"),
    _doc("zapp-console", 2, 0, "zapp", track=GW, requires=REQ_IW1, tag="ci-latest"),
]
DEFS = {d["component"]: _defn(d) for d in DOCS}


def _write(path, docs):
    os.makedirs(path, exist_ok=True)
    for doc in docs:
        fn = os.path.join(path, f"{doc['component']}.yaml")
        with open(fn, "w") as fh:
            yaml.safe_dump(doc, fh)
        os.chmod(fn, 0o644)
    os.chmod(path, 0o755)


# ------------------------------------------------------------- validator
def test_requires_is_optional_and_loads_as_track_stage_pairs(tmp_path):
    _write(str(tmp_path), DOCS)
    defs, errors = load_definitions(str(tmp_path))
    assert errors == []
    assert defs["zapp-console"].requires == ((IW, 1),)
    assert defs["hive-gateway"].requires == ()


@pytest.mark.parametrize("value,message", [
    ("InferWeave/inferweave", "requires must be a list"),
    (["x"], "requires[0] must be a mapping"),
    ([{"repo": IW[0], "ci_context": IW[2]}], "missing key requires[0].stage"),
    ([{"repo": IW[0], "ci_context": IW[2], "stage": 1, "branch": "main"}],
     "unknown key requires[0].branch"),
    ([{"repo": IW[0], "ci_context": IW[2], "stage": 3}], "requires[0].stage must be 1 or 2"),
    ([{"repo": IW[0], "ci_context": IW[2], "stage": True}], "requires[0].stage must be 1 or 2"),
    ([{"repo": "", "ci_context": IW[2], "stage": 1}], "requires[0].repo must be a non-empty string"),
    (REQ_IW1 * 2, "requires[1] duplicates an earlier requirement"),
    ([{"repo": IW[0], "ci_context": f"c{i}", "stage": 1} for i in range(9)],
     "requires must have at most 8 entries"),
])
def test_requires_shape_is_validated(value, message):
    doc = _doc("zapp-console", 2, 0, "zapp", track=GW, requires=value)
    assert message in validate_definition(doc, "zapp-console")


def test_requires_its_own_track_is_refused(tmp_path):
    docs = DOCS[:3] + [_doc("zapp-console", 2, 0, "zapp", track=GW,
                            requires=[{"repo": GW[0], "ci_context": GW[2], "stage": 1}])]
    _write(str(tmp_path), docs)
    errors = validate_dir(str(tmp_path))
    assert any("zapp-console.yaml: requires[0] names its own track" in e for e in errors), errors


def test_requires_a_track_no_definition_follows_is_refused(tmp_path):
    docs = DOCS[:3] + [_doc("zapp-console", 2, 0, "zapp", track=GW,
                            requires=[{"repo": "Other/repo", "ci_context": IW[2], "stage": 1}])]
    _write(str(tmp_path), docs)
    errors = validate_dir(str(tmp_path))
    assert any("requires[0] names a track no definition follows" in e for e in errors), errors
    assert load_definitions(str(tmp_path))[0] == {}


def test_requires_a_stage_the_track_lacks_is_refused(tmp_path):
    docs = [_doc("hive-gateway", 1, 0, "hive"),
            _doc("zapp-console", 2, 0, "zapp", track=GW,
                 requires=[{"repo": IW[0], "ci_context": IW[2], "stage": 2}])]
    _write(str(tmp_path), docs)
    errors = validate_dir(str(tmp_path))
    assert any("requires[0] names stage 2" in e for e in errors), errors


def test_a_requires_cycle_between_tracks_is_refused(tmp_path):
    docs = [_doc("hive-gateway", 1, 0, "hive",
                 requires=[{"repo": GW[0], "ci_context": GW[2], "stage": 2}]),
            _doc("zapp-console", 2, 0, "zapp", track=GW, requires=REQ_IW1)]
    _write(str(tmp_path), docs)
    errors = validate_dir(str(tmp_path))
    assert any("requires forms a cycle" in e for e in errors), errors
    assert load_definitions(str(tmp_path))[0] == {}


def test_a_three_track_cycle_is_refused(tmp_path):
    t3 = ("Third/repo", "main", "ci/woodpecker/push/x")
    docs = [_doc("a-comp", 1, 0, "a", requires=[{"repo": t3[0], "ci_context": t3[2], "stage": 1}]),
            _doc("b-comp", 1, 0, "b", track=GW, requires=REQ_IW1),
            _doc("c-comp", 1, 0, "c", track=t3,
                 requires=[{"repo": GW[0], "ci_context": GW[2], "stage": 1}])]
    _write(str(tmp_path), docs)
    assert any("requires forms a cycle" in e for e in validate_dir(str(tmp_path)))


# ------------------------------------------------------- advance (pure)
def _gw_snap(requirements=()):
    return Snapshot(components={}, target_commit=G1, published_commit=G1, target_on_main=True,
                    plan_commit=G1, generation=1, slots={}, requirements=requirements)


GW_DEFS = {"zapp-console": DEFS["zapp-console"]}


def test_an_unmet_requirement_holds_the_track_and_permits_nothing():
    out = advance(_gw_snap((((IW, 1), "stage 1 holding"),)), GW_DEFS, T0)
    assert out == [Hold(stage=None, remaining_s=None,
                        reason="waiting on InferWeave/inferweave ci/woodpecker/push/rust stage 1: "
                               "stage 1 holding",
                        target_commit=G1, generation=1)]
    assert permitted(out) == ()


def test_a_requirement_absent_from_the_snapshot_fails_closed():
    out = advance(_gw_snap(), GW_DEFS, T0)
    assert isinstance(out[-1], Hold) and out[-1].stage is None and "unknown" in out[-1].reason
    assert permitted(out) == ()


def test_a_met_requirement_lets_the_track_roll():
    out = advance(_gw_snap((((IW, 1), None),)), GW_DEFS, T0)
    assert out[-1] == Start(stage=2, order=0, components=("zapp-console",), target_commit=G1,
                            generation=1, permitted=("zapp-console",))


def test_requirement_status_needs_the_stage_rolled_out_and_confirmed():
    w = World()
    iw = {c: d for c, d in DEFS.items() if d.track == IW}

    def snap(**kw):
        base = dict(components=w.mon.snapshot()["components"], target_commit=T1,
                    published_commit=T1, target_on_main=True, plan_commit=T1, generation=1)
        return Snapshot(**{**base, **kw})

    assert requirement_status(snap(), iw, 1, T0) is not None           # nothing deployed
    for c in ("hive-gateway", "hive-nodes"):
        w.deploy(c, at=0)
    assert "holding stage 1" in requirement_status(snap(), iw, 1, T0 + 10)
    assert requirement_status(snap(), iw, 1, T0 + 600) is None          # stage 2 not needed
    assert requirement_status(snap(published_commit=None), iw, 1, T0 + 600) is not None
    assert requirement_status(snap(target_on_main=None), iw, 1, T0 + 600) is not None
    assert requirement_status(snap(target_commit=None), iw, 1, T0 + 600) is not None
    assert requirement_status(snap(), {}, 1, T0 + 600) is not None


# ------------------------------------------------------------ the tick
class CI:
    """CI view with green main pushes for several repositories."""

    def __init__(self):
        self.rows = []

    def green(self, repo, commit, run):
        self.rows.append({"repository": repo, "run_number": run, "revision": commit,
                          "branch": "main", "event": "push", "state": "success"})

    def __call__(self):
        return {"pipelines": {"all": list(self.rows)}}


class Evidence:
    def __init__(self):
        self.unconfirmed, self.fail = set(), set()

    def __call__(self, track, target, plan_commit):
        if track in self.fail:
            raise RuntimeError("down")
        if track in self.unconfirmed:
            return ReleaseEvidence(None, True)
        return ReleaseEvidence(target, True)


class Rig:
    def __init__(self, tmp_path, docs=DOCS, defs=DEFS):
        self.clock = Clock()
        self.world = World(monitor_clock=Clock())
        self.repo = ControlPlaneRepo(now=self.clock)
        self.ci, self.ev = CI(), Evidence()
        path = str(tmp_path / "pipelines")
        _write(path, docs)
        self.defs = defs
        self.coord = DeploymentCoordinator(
            DeploymentDefinitions(lambda: path, ci_snapshot=self.ci),
            monitor=lambda: self.world.mon, state=lambda: self.repo.deployment_plans,
            evidence=self.ev, now=self.clock)

    def deploy(self, component, *, at, to=T1):
        host = self.defs[component].host
        self.world.emit(component, "started", at=at, to=to, reported_by=host)
        self.world.emit(component, "ok", at=at, to=to, reported_by=host)

    def fail(self, component, *, at, to=T1):
        host = self.defs[component].host
        self.world.emit(component, "started", at=at, to=to, reported_by=host)
        self.world.emit(component, "failed", at=at, to=to, reported_by=host)

    def tick(self, at):
        self.clock.t = T0 + at
        return self.coord.tick()


def _gw_last(out, track=GW):
    return out[track][-1]


def _never_starts(out, component="zapp-console", track=GW):
    last = _gw_last(out, track)
    assert not isinstance(last, Start), last
    assert component not in permitted(out[track])
    assert permitted(out[track]) == ()


def _stage_before(rig):
    rig.ci.green(IW[0], T1, 10)
    rig.ci.green(GW[0], G1, 20)


@pytest.mark.parametrize("case", ["start", "hold", "halt", "unconfirmed", "evidence-raises",
                                  "no-target"])
def test_a_separate_tracks_stage_2_never_starts_while_the_required_stage_1_is_not_done(tmp_path, case):
    rig = Rig(tmp_path)
    rig.ci.green(GW[0], G1, 20)
    if case != "no-target":
        rig.ci.green(IW[0], T1, 10)
    if case == "hold":
        rig.deploy("hive-gateway", at=0)
        rig.deploy("hive-nodes", at=0)
    elif case == "halt":
        rig.deploy("hive-gateway", at=0)
        rig.fail("hive-nodes", at=0)
    elif case == "unconfirmed":
        rig.deploy("hive-gateway", at=0)
        rig.deploy("hive-nodes", at=0)
        rig.ev.unconfirmed.add(IW)
    elif case == "evidence-raises":
        rig.deploy("hive-gateway", at=0)
        rig.deploy("hive-nodes", at=0)
        rig.ev.fail.add(IW)
    at = 60 if case == "hold" else 3600
    for t in (at, at + 30):                      # two ticks: the plans are adopted
        out = rig.tick(t)
        _never_starts(out)
        last = _gw_last(out)
        assert last.reason.startswith(
            "waiting on InferWeave/inferweave ci/woodpecker/push/rust stage 1"), last
    # the required track's own stage 2 never started either, except after a rolled-out stage 1
    if case in ("start", "hold", "halt"):
        assert "bender-node" not in permitted(out[IW])


def test_the_separate_track_starts_once_stage_1_has_held_and_is_withdrawn_on_a_replan(tmp_path):
    rig = Rig(tmp_path)
    _stage_before(rig)
    rig.deploy("hive-gateway", at=0)
    rig.deploy("hive-nodes", at=0)
    _never_starts(rig.tick(300))
    out = rig.tick(600)
    assert _gw_last(out) == Start(stage=2, order=0, components=("zapp-console",), target_commit=G1,
                                  generation=1, permitted=("zapp-console",))
    assert isinstance(out[IW][-1], Start) and out[IW][-1].stage == 2
    # a newer InferWeave release restarts that plan at stage 1: the console's permission goes
    rig.ci.green(IW[0], T2, 11)
    out = rig.tick(630)
    assert isinstance(out[IW][0], Replan)
    _never_starts(out)
    assert "stage 1" in _gw_last(out).reason


def test_a_stage_1_component_turning_unhealthy_withdraws_the_separate_track(tmp_path):
    rig = Rig(tmp_path)
    _stage_before(rig)
    rig.deploy("hive-gateway", at=0)
    rig.deploy("hive-nodes", at=0)
    assert isinstance(_gw_last(rig.tick(600)), Start)
    rig.fail("hive-nodes", at=610)
    _never_starts(rig.tick(620))


def test_the_gate_does_not_depend_on_track_sort_order(tmp_path):
    # the dependent track sorts BEFORE the one it requires
    early = ("Aaa/console", "main", "ci/woodpecker/push/console")
    docs = DOCS[:3] + [_doc("zapp-console", 2, 0, "zapp", track=early, requires=REQ_IW1)]
    defs = {d["component"]: _defn(d) for d in docs}
    rig = Rig(tmp_path, docs, defs)
    rig.ci.green(IW[0], T1, 10)
    rig.ci.green(early[0], G1, 20)
    rig.deploy("hive-gateway", at=0)
    rig.deploy("hive-nodes", at=0)
    out = rig.tick(600)
    assert list(out)[0] == early
    assert isinstance(_gw_last(out, early), Start)


def test_a_track_without_requires_is_unchanged(tmp_path):
    rig = Rig(tmp_path)
    _stage_before(rig)
    out = rig.tick(10)
    assert isinstance(out[IW][0], Replan)
    assert out[IW][-1] == Start(stage=1, order=0, components=("hive-gateway",), target_commit=T1,
                                generation=1, permitted=("hive-gateway",))


def test_a_force_cannot_override_an_unmet_requirement(tmp_path):
    rig = Rig(tmp_path)
    _stage_before(rig)
    rig.deploy("hive-gateway", at=0)
    rig.deploy("hive-nodes", at=0)
    now = T0 + 60
    out = rig.tick(60)
    views = track_views(out, rig.defs, rig.repo.deployment_plans, now,
                        {IW: ReleaseEvidence(T1, True), GW: ReleaseEvidence(G1, True)})
    view = next(v for v in views if v["track"]["repo"] == GW[0])
    meta = {"ticked_at": now, "stale": False, "error": None}
    answer = permit_decision(meta, view, "zapp-console", G1, "force-1", now=now, component_stage=2)
    assert answer["permitted"] is False and answer["forced"] is False
    assert "waiting on InferWeave/inferweave" in answer["reason"]


def test_an_idle_required_track_without_stage_2_counts_as_rolled_out(tmp_path):
    docs = DOCS[:2] + DOCS[3:]
    defs = {d["component"]: _defn(d) for d in docs}
    rig = Rig(tmp_path, docs, defs)
    _stage_before(rig)
    rig.deploy("hive-gateway", at=0)
    rig.deploy("hive-nodes", at=0)
    out = rig.tick(600)
    assert out[IW][-1] == Idle(reason="rolled out", target_commit=T1,
                               permitted=("hive-gateway", "hive-nodes"))
    assert isinstance(_gw_last(out), Start)


def test_old_target_of_required_track_is_not_enough(tmp_path):
    # stage 1 healthy on an OLD commit does not satisfy a requirement on the current target
    rig = Rig(tmp_path)
    _stage_before(rig)
    rig.deploy("hive-gateway", at=0, to=OLD)
    rig.deploy("hive-nodes", at=0, to=OLD)
    _never_starts(rig.tick(3600))
