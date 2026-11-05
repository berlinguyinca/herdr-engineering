"""WC-005b: list-level mission-outcomes summaries carry `prs: [{repository, number}]` (additive, read-only)."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.mission_outcome import outcome_summaries
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def _ended(repo, key):
    source, _, raw = key.rpartition(":")
    kw = {"source": source} if source else {}
    repo.upsert_mission(raw, title=key, stage="implementation", **kw)
    repo.upsert_mission(raw, title=key, stage="complete", **kw)


def _world():
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo, host="127.0.0.1", port=0, api_keys={"v1": "viewer", "ing": "ingest"})
    for key in ("pi:MSN-1", "pi:MSN-2", "pi:MSN-3", "MSN-1"):
        _ended(repo, key)
    repo.upsert_pull_request("b", mission_id="pi:MSN-1", repository="org/app", number=20, url="https://github.com/org/app/pull/20", status="open")
    repo.upsert_pull_request("a", mission_id="pi:MSN-1", repository="org/app", number=10, url="https://github.com/org/app/pull/10", status="merged")
    repo.upsert_pull_request("c", mission_id="pi:MSN-2", repository="org/other", number=10, status="open")
    repo.upsert_pull_request("d", mission_id="MSN-1", repository="org/app", number=99, status="open")   # same raw id, other namespace
    return repo, server


def test_each_summary_lists_only_its_own_missions_prs_in_a_stable_order():
    repo, _ = _world()
    out = outcome_summaries(repo)
    assert out["pi:MSN-1"]["prs"] == [{"repository": "org/app", "number": 10}, {"repository": "org/app", "number": 20}]
    assert out["pi:MSN-2"]["prs"] == [{"repository": "org/other", "number": 10}]
    assert out["MSN-1"]["prs"] == [{"repository": "org/app", "number": 99}]   # namespaces never mix
    assert outcome_summaries(repo) == out


def test_a_mission_without_prs_has_an_empty_list():
    repo, _ = _world()
    assert outcome_summaries(repo)["pi:MSN-3"]["prs"] == []


def test_existing_fields_are_unchanged():
    repo, _ = _world()
    assert set(outcome_summaries(repo)["pi:MSN-1"]) == {
        "result", "final_stage", "inferred", "ended_at", "duration_s", "agent_count", "repository", "last_event", "prs"}


def _get(server, key):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/v1/mission-outcomes", headers={"X-API-Key": key} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


@pytest.fixture
def served():
    repo, server = _world()
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    yield server
    server.shutdown()


def test_http_returns_prs_and_keeps_auth_behaviour(served):
    status, body = _get(served, "v1")
    assert status == 200
    assert body["outcomes"]["pi:MSN-1"]["prs"] == [{"repository": "org/app", "number": 10}, {"repository": "org/app", "number": 20}]
    assert _get(served, None)[0] == 401
    assert _get(served, "wrong")[0] == 401
    assert _get(served, "ing")[0] == 200


def test_g2_summaries_carry_no_issue_relationship_and_text_is_never_parsed():
    """G2: no structured mission->issue source exists. Free text naming an issue must not create one."""
    repo = ControlPlaneRepo()
    ControlPlaneServer(repo, host="127.0.0.1", port=0, api_keys={"v1": "viewer"})
    for stage in ("implementation", "complete"):
        repo.upsert_mission("MSN-9", title="Closes #5", purpose="Fixes org/app#5", stage=stage, source="pi")
    repo.upsert_pull_request("p", mission_id="pi:MSN-9", repository="org/app", number=6, url="https://github.com/org/app/pull/6", status="open")
    summary = outcome_summaries(repo)["pi:MSN-9"]
    assert "issues" not in summary and "issue" not in summary
    assert summary["prs"] == [{"repository": "org/app", "number": 6}]


# ---- G3: top-level `pr_refs` covers running missions too; `outcomes` membership/meaning is unchanged ----
from herdr_engineering.control_plane.mission_outcome import pr_refs_by_mission


def _running(repo, key):
    source, _, raw = key.rpartition(":")
    kw = {"source": source} if source else {}
    repo.upsert_mission(raw, title=key, stage="implementation", **kw)


def test_g3_a_running_mission_pr_is_in_pr_refs_but_not_in_outcomes():
    repo, _ = _world()
    _running(repo, "pi:RUN-1")
    repo.upsert_pull_request("r", mission_id="pi:RUN-1", repository="org/app", number=31, status="open")
    assert "pi:RUN-1" not in outcome_summaries(repo)   # presence in outcomes still means "ended"
    assert pr_refs_by_mission(repo)["pi:RUN-1"] == [{"repository": "org/app", "number": 31}]


def test_g3_ended_missions_keep_their_refs_and_namespaces_stay_separate():
    repo, _ = _world()
    refs = pr_refs_by_mission(repo)
    assert refs["pi:MSN-1"] == [{"repository": "org/app", "number": 10}, {"repository": "org/app", "number": 20}]
    assert refs["MSN-1"] == [{"repository": "org/app", "number": 99}]
    assert "pi:MSN-3" not in refs   # no PR => no key
    assert pr_refs_by_mission(repo) == refs


def test_g3_a_pr_of_another_or_unknown_mission_never_leaks_in():
    repo, _ = _world()
    _running(repo, "pi:RUN-2")
    repo.upsert_pull_request("x", mission_id="pi:GONE", repository="org/app", number=77, status="open")   # mission not listed
    repo.upsert_pull_request("y", mission_id=None, repository="org/app", number=78, status="open")
    refs = pr_refs_by_mission(repo)
    assert "pi:RUN-2" not in refs and "pi:GONE" not in refs
    assert all(r["number"] not in (77, 78) for v in refs.values() for r in v)


def test_g3_failing_read_yields_empty_and_the_list_still_answers():
    repo, _ = _world()
    def boom():
        raise RuntimeError("db down")
    repo.list_pull_requests = boom
    assert pr_refs_by_mission(repo) == {}
    assert "pi:MSN-1" in outcome_summaries(repo)


def test_g3_http_envelope_adds_pr_refs_and_keeps_outcomes(served):
    status, body = _get(served, "v1")
    assert status == 200 and set(body) == {"outcomes", "pr_refs"}
    assert body["pr_refs"]["pi:MSN-1"] == [{"repository": "org/app", "number": 10}, {"repository": "org/app", "number": 20}]
    assert _get(served, None)[0] == 401
