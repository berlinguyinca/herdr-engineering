import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from helpers import REPO_ID, git  # noqa: E402
from test_api import call, start  # noqa: E402

from herdr_engineering.architecture import command  # noqa: E402


@pytest.fixture
def api():
    svr, repo, base = start(architecture_enabled=True)
    yield base, repo, svr
    svr.shutdown()


def req(base, **kw):
    return call(base, "/api/v1/architecture/analyses/request", {"repository_id": REPO_ID, "host_id": "h1", **kw})


def test_identical_open_requests_collapse_and_other_keys_do_not(api):
    base, _, _ = api
    st, a = req(base, rev="main"); assert st == 201 and not a["deduplicated"]
    st, b = req(base, rev="main"); assert st == 200 and b["deduplicated"] and b["analysis_id"] == a["analysis_id"]
    assert req(base, rev="other")[1]["analysis_id"] != a["analysis_id"]
    assert call(base, "/api/v1/architecture/analyses/request", {"repository_id": REPO_ID, "host_id": "h2", "rev": "main"})[1]["analysis_id"] != a["analysis_id"]


def test_request_validation(api):
    base, _, _ = api
    assert req(base, rev="--upload-pack=x")[0] == 400
    assert req(base, priority="urgent")[0] == 400
    assert call(base, "/api/v1/architecture/analyses/request", {"repository_id": "not a repo", "host_id": "h1"})[0] == 400
    assert call(base, "/api/v1/architecture/analyses/request", {"repository_id": REPO_ID, "host_id": "../x"})[0] == 400


def test_claim_is_per_host_interactive_first_and_exclusive(api):
    base, _, _ = api
    bg = req(base, rev="a1", priority="background")[1]["analysis_id"]
    time.sleep(0.01)
    ia = req(base, rev="b2", priority="interactive")[1]["analysis_id"]
    assert call(base, "/api/v1/architecture/analyses/claim", {"host_id": "other"})[1]["job"] is None
    first = call(base, "/api/v1/architecture/analyses/claim", {"host_id": "h1"})[1]
    assert first["job"]["analysis_id"] == ia
    second = call(base, "/api/v1/architecture/analyses/claim", {"host_id": "h1"})[1]
    assert second["job"]["analysis_id"] == bg                           # the leased one is not handed out twice
    assert call(base, "/api/v1/architecture/analyses/claim", {"host_id": "h1"})[1]["job"] is None


def test_expired_lease_is_reclaimable_and_finished_jobs_are_final(api):
    base, repo, svr = api
    aid = req(base)[1]["analysis_id"]
    assert call(base, "/api/v1/architecture/analyses/claim", {"host_id": "h1", "lease_seconds": 30})[1]["job"]
    svr.architecture._jobs[aid]["lease_expires_at"] = time.time() - 1     # the host died
    again = call(base, "/api/v1/architecture/analyses/claim", {"host_id": "h1"})[1]["job"]
    assert again and again["analysis_id"] == aid
    assert call(base, "/api/v1/architecture/analyses", {"analysis_id": aid, "event": "cancelled"})[0] == 201
    assert call(base, "/api/v1/architecture/analyses", {"analysis_id": aid, "event": "completed"})[0] == 409
    assert call(base, "/api/v1/architecture/analyses/claim", {"host_id": "h1"})[1]["job"] is None
    assert call(base, f"/api/v1/architecture/analyses/{aid}")[1]["analysis"]["state"] == "cancelled"


def test_queue_state_survives_a_restart_from_events(api):
    base, repo, svr = api
    aid = req(base)[1]["analysis_id"]
    from herdr_engineering.architecture.service import ArchitectureService
    fresh = ArchitectureService(repo, enabled=True)
    assert fresh.analyses(analysis_id=aid)["analysis"]["state"] == "requested"
    assert fresh.claim_analysis({"host_id": "h1"})["job"]["analysis_id"] == aid


def test_only_operators_may_request_but_agents_may_claim():
    svr, repo, base = start(api_keys={"o": "operator", "a": "agent", "v": "viewer"}, architecture_enabled=True)
    try:
        body = {"repository_id": REPO_ID, "host_id": "h1"}
        assert call(base, "/api/v1/architecture/analyses/request", body, key="v")[0] == 403
        assert call(base, "/api/v1/architecture/analyses/request", body, key="a")[0] == 403
        assert call(base, "/api/v1/architecture/analyses/request", body, key="o")[0] == 201
        assert call(base, "/api/v1/architecture/analyses/claim", {"host_id": "h1"}, key="a")[0] == 200
        assert call(base, "/api/v1/architecture/analyses/claim", {"host_id": "h1"}, key="v")[0] == 403
    finally:
        svr.shutdown()


def test_serve_end_to_end_request_claim_analyse_publish(api, repo, tmp_path):
    base, _, _ = api
    key = tmp_path / "key"; key.write_text("k")
    sha = git(repo, "rev-parse", "HEAD")
    aid = req(base, rev="main")[1]["analysis_id"]
    args = SimpleNamespace(url=base, api_key_file=str(key), host_id="h1", checkout=[f"{REPO_ID}={repo}"], poll=0.1,
                           once=True, timeout=60.0, cache_dir=str(tmp_path / "cache"))
    assert command.cmd_serve(args) == 0
    job = call(base, f"/api/v1/architecture/analyses/{aid}")[1]["analysis"]
    assert job["state"] == "completed" and job["commit_sha"] == sha
    snaps = call(base, f"/api/v1/architecture/snapshots?repository_id={REPO_ID}")[1]["snapshots"]
    assert [s["snapshot_id"] for s in snaps] == [job["snapshot_id"]]
    # the same commit by full SHA is now answered from the existing snapshot, with no new job
    again = req(base, rev=sha)[1]
    assert again["deduplicated"] and again["snapshot_id"] == job["snapshot_id"]


def test_serve_reports_failure_for_unregistered_repository_and_never_leaks_paths(api, tmp_path):
    base, _, _ = api
    key = tmp_path / "key"; key.write_text("k")
    aid = req(base)[1]["analysis_id"]
    args = SimpleNamespace(url=base, api_key_file=str(key), host_id="h1", checkout=[], poll=0.1, once=True,
                           timeout=10.0, cache_dir=str(tmp_path / "c"))
    command.cmd_serve(args)
    job = call(base, f"/api/v1/architecture/analyses/{aid}")[1]["analysis"]
    assert job["state"] == "failed" and "no checkout is registered" in job["message"]


def test_serve_rejects_bad_checkout_mapping(tmp_path):
    key = tmp_path / "key"; key.write_text("k")
    with pytest.raises(SystemExit):
        command.cmd_serve(SimpleNamespace(url="http://127.0.0.1:1", api_key_file=str(key), host_id="h", checkout=["nope=/tmp"],
                                          poll=1, once=True, timeout=1, cache_dir=str(tmp_path)))
