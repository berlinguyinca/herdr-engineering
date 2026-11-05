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
