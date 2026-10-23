import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from helpers import REPO_ID, git, write  # noqa: E402
from test_api import call, start  # noqa: E402

from herdr_engineering.architecture import analyze  # noqa: E402


_STATE = {}


@pytest.fixture
def world(repo):
    """Two real snapshots (base, head) + sessions/mission/CI/PR records around the head commit."""
    svr, crepo, base = start(architecture_enabled=True)
    a = analyze.analyze_commit(str(repo), "HEAD", "h1", REPO_ID)
    write(repo, {"app/util.py": "import json\n\ndef helper():\n    return 2\n\ndef added(): pass\n"})
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "change")
    b = analyze.analyze_commit(str(repo), "HEAD", "h1", REPO_ID)
    b["snapshot"]["created_at"] = "2099-01-01T00:00:00+00:00"
    for s in (a, b):
        assert call(base, "/api/v1/architecture/snapshots", s)[0] == 201
    head = b["snapshot"]["commit_sha"]
    from herdr_engineering.control_plane.ingest import IngestService
    IngestService(crepo).ingest({"entity_type": "mission", "entity_id": "MSN-1", "event_type": "mission.started", "source": "pi",
                                 "payload": {"title": "Fix helper (#42)", "purpose": "see other/proj#7 and #42"}})
    mission = "MSN-1"
    crepo.upsert_session("pi:s1", host_id="h1", agent_role="implementer", source="pi", repository_id=REPO_ID,
                         commit_sha=head, mission_id=mission, parent_session_id="pi:root")
    crepo.upsert_session("pi:s2", host_id="h1", agent_role="reviewer", source="pi", repository_id=REPO_ID, commit_sha=head)
    crepo.upsert_session("other", host_id="h1", repository_id=REPO_ID, commit_sha="c" * 40)            # different commit
    crepo.upsert_session("branchy", host_id="h1", repository_id=REPO_ID, branch="main", cwd="/x/repo")  # no commit: no link
    crepo.observe_ci_pipeline({"provider_repo_id": "9", "repository": "fixture", "run_number": 5, "state": "success",
                               "branch": "main", "repository_id": REPO_ID, "commit_sha": head, "url": "https://ci/5"})
    crepo.observe_ci_pipeline({"provider_repo_id": "9", "repository": "fixture", "run_number": 4, "state": "failure",
                               "repository_id": REPO_ID, "commit_sha": "d" * 40})
    gate = SimpleNamespace(snapshots=lambda: [
        SimpleNamespace(key=SimpleNamespace(repo=REPO_ID, number=17), head_sha=head, state=SimpleNamespace(value="ci_pending")),
        SimpleNamespace(key=SimpleNamespace(repo=REPO_ID, number=18), head_sha="e" * 40, state=SimpleNamespace(value="merged"))])
    svr.architecture.gate_store_fn = lambda: gate
    _STATE["repo"] = crepo
    yield base, a["snapshot"]["snapshot_id"], b["snapshot"]["snapshot_id"], head
    svr.shutdown()


def test_overlays_link_only_by_exact_repository_and_full_sha(world):
    base, _a, b, head = world
    o = call(base, f"/api/v1/architecture/snapshots/{b}/overlays")[1]
    assert [r["run_number"] for r in o["ci"]] == [5] and o["ci"][0]["url"] == "https://ci/5"
    assert [p["pr_ref"] for p in o["pull_requests"]] == [f"{REPO_ID}#17"]
    assert o["pull_requests"][0]["url"] == "https://github.com/example/fixture/pull/17"
    assert sorted(s["session_id"] for s in o["sessions"]) == ["pi:s1", "pi:s2"]
    assert o["missions"] and "branch names and cwd are not evidence" in o["match_rule"]
    base_o = call(base, f"/api/v1/architecture/snapshots/{_a}/overlays")[1]
    assert base_o["ci"] == [] and base_o["sessions"] == [] and base_o["pull_requests"] == []


def test_issue_mentions_are_explicit_text_and_marked_inferred(world):
    base, _a, b, _h = world
    issues = {i["issue_ref"]: i for i in call(base, f"/api/v1/architecture/snapshots/{b}/overlays")[1]["issues"]}
    assert set(issues) == {f"{REPO_ID}#42", "github.com/other/proj#7"}
    assert all(i["inferred"] for i in issues.values())


def test_correlation_records_carry_pi_identities_and_name_unknowns(world):
    base, _a, b, head = world
    c = call(base, f"/api/v1/architecture/snapshots/{b}/correlations")[1]
    by = {r["session_id"]: r for r in c["correlations"]}
    r = by["pi:s1"]
    assert r["parent_worker_id"] == "pi:root" and r["flow"] == "pi" and r["commit_sha"] == head and r["host_id"] == "h1"
    assert r["mission_id"] and r["pr_ref"] == f"{REPO_ID}#17"
    assert {"issue_ref", "room_ref", "autospec_ref", "worktree_id", "run_id", "task_id", "plan_ref"} <= set(r["unknown"])
    assert by["pi:s2"]["mission_id"] is None and "mission_id" in by["pi:s2"]["unknown"]     # non-mission flow still works
    assert any("commit-level" in n for n in c["notes"])


def test_node_correlation_only_for_symbols_the_commit_changed(world):
    base, _a, b, _h = world
    changed = call(base, f"/api/v1/architecture/snapshots/{b}/correlations?node=function:app.util.added")[1]
    assert changed["change"] == "added" and len(changed["correlations"]) == 2
    mod = call(base, f"/api/v1/architecture/snapshots/{b}/correlations?node=function:app.util.helper")[1]
    assert mod["change"] == "modified" and len(mod["correlations"]) == 2
    untouched = call(base, f"/api/v1/architecture/snapshots/{b}/correlations?node=class:app.core.Engine")[1]
    assert untouched["change"] == "unchanged" and untouched["correlations"] == []
    first = call(base, f"/api/v1/architecture/snapshots/{_a}/correlations?node=class:app.core.Engine")[1]
    assert first["change"] == "unknown"                                  # no base snapshot: cannot attribute
    assert call(base, f"/api/v1/architecture/snapshots/{b}/correlations?node=nope")[0] == 404


def test_session_changes_lists_changed_symbols_with_source_lines(world):
    base, _a, b, _h = world
    out = call(base, "/api/v1/architecture/sessions/pi:s1/changes")[1]
    assert out["snapshot_id"] == b and out["base_snapshot_id"] == _a
    names = {(c["qualified_name"], c["change"]) for c in out["changes"]}
    assert ("app.util.added", "added") in names and ("app.util.helper", "modified") in names
    assert all(c["relative_path"] and c["line_start"] for c in out["changes"])
    none = call(base, "/api/v1/architecture/sessions/other/changes")[1]
    assert none["snapshot"] is None and none["changes"] == []
    assert call(base, "/api/v1/architecture/sessions/ghost/changes")[0] == 404
    assert call(base, "/api/v1/architecture/sessions/branchy/changes")[1]["snapshot"] is None   # branch/cwd is not evidence


def test_overlays_work_without_the_review_gate(repo):
    svr, crepo, base = start(architecture_enabled=True)
    try:
        s = analyze.analyze_commit(str(repo), "HEAD", "h1", REPO_ID)
        call(base, "/api/v1/architecture/snapshots", s)
        o = call(base, f"/api/v1/architecture/snapshots/{s['snapshot']['snapshot_id']}/overlays")[1]
        assert o["pull_requests"] == [] and o["gate_available"] is False
    finally:
        svr.shutdown()


def test_autospec_and_non_autospec_flows_both_correlate(world):
    base, _a, b, head = world
    from herdr_engineering.control_plane.ingest import IngestService
    crepo = _STATE["repo"]
    IngestService(crepo).ingest({"entity_type": "mission", "entity_id": "SPEC-9", "event_type": "mission.started",
                                 "source": "autospec", "payload": {"title": "autospec run"}})
    crepo.upsert_session("as:w1", host_id="h1", agent_role="worker", source="autospec", repository_id=REPO_ID,
                         commit_sha=head, mission_id="SPEC-9")
    rows = call(base, f"/api/v1/architecture/snapshots/{b}/correlations")[1]["correlations"]
    flows = {r["session_id"]: r["flow"] for r in rows}
    assert flows["pi:s1"] == "pi" and flows["as:w1"] == "autospec" and flows["pi:s2"] == "unknown"
    assert all(r["autospec_ref"] is None and "autospec_ref" in r["unknown"] for r in rows)   # no record carries a spec/issue id


def test_timeline_has_independent_tracks_linked_only_by_exact_commit(world):
    base, a, b, head = world
    t = call(base, f"/api/v1/architecture/timeline?repository_id={REPO_ID}")[1]
    assert [x["snapshot_id"] for x in t["architecture"]] == [a, b]                       # chronological
    assert {x["session_id"] for x in t["agents"]} >= {"pi:s1", "pi:s2", "other", "branchy"}   # all repo sessions, linked or not
    s1 = next(x for x in t["agents"] if x["session_id"] == "pi:s1")
    assert s1["snapshot_ids"] == [b] and s1["parent_session_id"] == "pi:root"
    assert next(x for x in t["agents"] if x["session_id"] == "other")["snapshot_ids"] == []
    assert next(x for x in t["agents"] if x["session_id"] == "branchy")["commit_sha"] is None
    assert sorted(next(x for x in t["architecture"] if x["snapshot_id"] == b)["session_ids"]) == ["pi:s1", "pi:s2"]
    assert call(base, "/api/v1/architecture/timeline?repository_id=bogus")[0] == 400
    assert call(base, "/api/v1/architecture/timeline")[0] == 400
