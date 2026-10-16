"""CI entities: test runs/builds, reviews (+ findings), pull requests (issue #32).

Covers the ControlPlaneRepo CI collections added to close herdr-engineering
issue #32 / herdr issue #58 (CI-001, CI-009, UI-004).
"""

from herdr_engineering.control_plane.repo import ControlPlaneRepo


# ------------------------------------------------------------- test runs
def test_test_run_create_then_upsert_updates_only_provided_fields():
    r = ControlPlaneRepo()
    r.upsert_test_run(
        "tr-1", mission_id="MSN-1", status="passed", passed=5, failed=0, repository="repo"
    )
    r.upsert_test_run("tr-1", status="failed", failed=2)
    runs = r.list_test_runs()
    assert len(runs) == 1
    run = runs[0]
    assert run["test_run_id"] == "tr-1"
    assert run["status"] == "failed"  # updated
    assert run["failed"] == 2  # updated
    assert run["passed"] == 5  # untouched
    assert run["mission_id"] == "MSN-1"  # untouched
    assert run["kind"] == "test"


def test_test_run_list_filtered_by_mission():
    r = ControlPlaneRepo()
    r.upsert_test_run("tr-a", mission_id="pi:MSN-1")
    r.upsert_test_run("tr-b", mission_id="MSN-1")
    r.upsert_test_run("tr-c", mission_id="MSN-2")
    only_msn1 = r.list_test_runs(mission_id="MSN-1")
    assert [x["test_run_id"] for x in only_msn1] == ["tr-b"]
    # kind filter
    builds = r.list_test_runs(kind="build")
    assert builds == []


def test_test_run_create_and_update_events_carry_mission():
    r = ControlPlaneRepo()
    r.upsert_test_run("tr-1", mission_id="MSN-1", status="running", passed=0)
    r.upsert_test_run("tr-1", status="passed", passed=3)
    evs = r.stream("test_run", "tr-1")
    assert [e["event_type"] for e in evs] == ["TestRunStarted", "TestRunUpdated"]
    assert all(e["mission_id"] == "MSN-1" for e in evs)


def test_test_run_returned_dict_is_copy():
    r = ControlPlaneRepo()
    r.upsert_test_run(
        "tr-1",
        mission_id="MSN-1",
        status="passed",
        results=[{"name": "a", "status": "ok", "duration_ms": 1, "message": ""}],
    )
    run = r.get_test_run("tr-1")
    run["status"] = "hacked"
    run["results"][0]["status"] = "hacked"
    stored = r.get_test_run("tr-1")
    assert stored["status"] == "passed"
    assert stored["results"][0]["status"] == "ok"


# --------------------------------------------------------------- builds
def test_build_upsert_is_idempotent():
    r = ControlPlaneRepo()
    r.upsert_build("b-1", mission_id="MSN-1", status="success")
    r.upsert_build("b-1", status="failure")
    builds = r.list_test_runs(kind="build")
    assert len(builds) == 1
    assert builds[0]["kind"] == "build"
    assert builds[0]["status"] == "failure"
    assert builds[0]["mission_id"] == "MSN-1"


def test_build_events_named_build():
    r = ControlPlaneRepo()
    r.upsert_build("b-1", mission_id="MSN-1", status="running")
    r.upsert_build("b-1", status="success")
    evs = r.stream("test_run", "b-1")
    assert [e["event_type"] for e in evs] == ["BuildStarted", "BuildUpdated"]


def test_build_list_filtered_by_mission():
    r = ControlPlaneRepo()
    r.upsert_build("b-a", mission_id="MSN-1")
    r.upsert_build("b-b", mission_id="pi:MSN-1")
    r.upsert_test_run("tr-x", mission_id="MSN-1")
    only_msn1 = r.list_test_runs(mission_id="MSN-1", kind="build")
    assert [x["test_run_id"] for x in only_msn1] == ["b-a"]


# ---------------------------------------------------------------- reviews
def test_review_create_then_upsert_updates_only_provided_fields():
    r = ControlPlaneRepo()
    r.upsert_review("rv-1", mission_id="MSN-1", reviewer="alice", status="open", verdict="")
    r.upsert_review("rv-1", verdict="approve")
    reviews = r.list_reviews()
    assert len(reviews) == 1
    rev = reviews[0]
    assert rev["verdict"] == "approve"
    assert rev["reviewer"] == "alice"
    assert rev["mission_id"] == "MSN-1"
    assert rev["findings"] == []


def test_review_list_filtered_by_mission():
    r = ControlPlaneRepo()
    r.upsert_review("rv-a", mission_id="MSN-1")
    r.upsert_review("rv-b", mission_id="pi:MSN-1")
    r.upsert_review("rv-c", mission_id="MSN-2")
    only_msn1 = r.list_reviews(mission_id="MSN-1")
    assert [x["review_id"] for x in only_msn1] == ["rv-a"]


def test_review_events_carry_mission():
    r = ControlPlaneRepo()
    r.upsert_review("rv-1", mission_id="MSN-1", status="open")
    r.upsert_review("rv-1", status="approved")
    evs = r.stream("review", "rv-1")
    assert [e["event_type"] for e in evs] == ["ReviewStarted", "ReviewUpdated"]
    assert all(e["mission_id"] == "MSN-1" for e in evs)


def test_review_returned_dict_is_copy():
    r = ControlPlaneRepo()
    r.upsert_review("rv-1", mission_id="MSN-1", reviewer="alice")
    rev = r.get_review("rv-1")
    rev["reviewer"] = "hacked"
    assert r.get_review("rv-1")["reviewer"] == "alice"


# -------------------------------------------------------- review findings
def test_add_review_finding_unknown_review_raises():
    r = ControlPlaneRepo()
    try:
        r.add_review_finding("rv-nope", severity="high", summary="x")
        raise AssertionError("expected KeyError")
    except KeyError as exc:
        assert exc.args[0] == "rv-nope"


def test_add_review_finding_default_id_and_event():
    r = ControlPlaneRepo()
    r.upsert_review("rv-1", mission_id="MSN-1")
    f1 = r.add_review_finding("rv-1", severity="high", summary="bug a")
    assert f1["finding_id"] == "rv-1:1"
    f2 = r.add_review_finding("rv-1", severity="low", summary="nit")
    assert f2["finding_id"] == "rv-1:2"
    rev = r.get_review("rv-1")
    assert len(rev["findings"]) == 2
    evs = r.stream("review", "rv-1")
    assert [e["event_type"] for e in evs] == [
        "ReviewStarted",
        "ReviewFindingAdded",
        "ReviewFindingAdded",
    ]
    assert evs[-1]["mission_id"] == "MSN-1"
    assert evs[-1]["payload"]["finding_id"] == "rv-1:2"


def test_add_review_finding_duplicate_id_updates():
    r = ControlPlaneRepo()
    r.upsert_review("rv-1", mission_id="MSN-1")
    r.add_review_finding("rv-1", severity="high", summary="bug", finding_id="f-1")
    r.add_review_finding(
        "rv-1", severity="low", summary="fixed", status="resolved", finding_id="f-1"
    )
    rev = r.get_review("rv-1")
    assert len(rev["findings"]) == 1
    assert rev["findings"][0]["summary"] == "fixed"
    assert rev["findings"][0]["severity"] == "low"
    assert rev["findings"][0]["status"] == "resolved"


def test_review_findings_are_copied():
    r = ControlPlaneRepo()
    r.upsert_review("rv-1", mission_id="MSN-1")
    r.add_review_finding("rv-1", severity="high", summary="bug")
    rev = r.get_review("rv-1")
    rev["findings"][0]["summary"] = "hacked"
    assert r.get_review("rv-1")["findings"][0]["summary"] == "bug"


# ----------------------------------------------------------- pull requests
def test_pull_request_create_then_upsert_updates_only_provided_fields():
    r = ControlPlaneRepo()
    r.upsert_pull_request("pr-1", mission_id="MSN-1", repository="repo", number=7, status="open")
    r.upsert_pull_request("pr-1", status="closed")
    prs = r.list_pull_requests()
    assert len(prs) == 1
    pr = prs[0]
    assert pr["status"] == "closed"
    assert pr["number"] == 7
    assert pr["repository"] == "repo"
    assert pr["mission_id"] == "MSN-1"


def test_pull_request_list_filtered_by_mission():
    r = ControlPlaneRepo()
    r.upsert_pull_request("pr-a", mission_id="MSN-1")
    r.upsert_pull_request("pr-b", mission_id="pi:MSN-1")
    r.upsert_pull_request("pr-c", mission_id="MSN-2")
    only_msn1 = r.list_pull_requests(mission_id="MSN-1")
    assert [x["pr_id"] for x in only_msn1] == ["pr-a"]


def test_pull_request_events_carry_mission_and_merge_once():
    r = ControlPlaneRepo()
    r.upsert_pull_request("pr-1", mission_id="MSN-1", repository="repo", number=7, status="open")
    r.upsert_pull_request("pr-1", status="merged")
    r.upsert_pull_request("pr-1", status="merged")
    evs = r.stream("pull_request", "pr-1")
    types = [e["event_type"] for e in evs]
    assert types == [
        "PullRequestOpened",
        "PullRequestUpdated",
        "PullRequestMerged",
        "PullRequestUpdated",
    ]
    assert sum(t == "PullRequestMerged" for t in types) == 1
    assert all(e["mission_id"] == "MSN-1" for e in evs)
    merged = [e for e in evs if e["event_type"] == "PullRequestMerged"][0]
    assert merged["payload"]["status"] == "merged"


def test_pull_request_returned_dict_is_copy():
    r = ControlPlaneRepo()
    r.upsert_pull_request("pr-1", mission_id="MSN-1", status="open")
    pr = r.get_pull_request("pr-1")
    pr["status"] = "hacked"
    assert r.get_pull_request("pr-1")["status"] == "open"


# --------------------------------------------------------------- ordering
def test_lists_sorted_by_created_at_then_id():
    r = ControlPlaneRepo()
    r.upsert_test_run("tr-1", mission_id="MSN-1")
    r.upsert_test_run("tr-2", mission_id="MSN-1")
    r.upsert_test_run("tr-3", mission_id="MSN-1")
    r.upsert_review("rv-1", mission_id="MSN-1")
    r.upsert_review("rv-2", mission_id="MSN-1")
    r.upsert_pull_request("pr-1", mission_id="MSN-1")
    r.upsert_pull_request("pr-2", mission_id="MSN-1")
    assert [x["test_run_id"] for x in r.list_test_runs(mission_id="MSN-1")] == [
        "tr-1",
        "tr-2",
        "tr-3",
    ]
    assert [x["review_id"] for x in r.list_reviews(mission_id="MSN-1")] == ["rv-1", "rv-2"]
    assert [x["pr_id"] for x in r.list_pull_requests(mission_id="MSN-1")] == ["pr-1", "pr-2"]


# ------------------------------------------------- review-fix regressions
def test_lists_tie_break_by_id_on_equal_created_at(monkeypatch):
    import herdr_engineering.control_plane.repo as repo_mod

    monkeypatch.setattr(repo_mod.ControlPlaneRepo, "_stamp", lambda self: "2026-01-01T00:00:00")
    r = ControlPlaneRepo()
    r.upsert_test_run("b", mission_id="MSN-1")
    r.upsert_test_run("a", mission_id="MSN-1")
    assert [x["test_run_id"] for x in r.list_test_runs(mission_id="MSN-1")] == ["a", "b"]


def test_update_with_empty_results_clears_results():
    r = ControlPlaneRepo()
    r.upsert_test_run(
        "tr-1", mission_id="MSN-1", results=[{"name": "a", "status": "ok"}]
    )
    r.upsert_test_run("tr-1", results=[])
    assert r.get_test_run("tr-1")["results"] == []


def test_pull_request_merged_on_create_emits_opened_and_merged_once():
    r = ControlPlaneRepo()
    r.upsert_pull_request("pr-1", mission_id="MSN-1", status="merged")
    r.upsert_pull_request("pr-1", status="merged")
    evs = r.stream("pull_request", "pr-1")
    types = [e["event_type"] for e in evs]
    assert types == [
        "PullRequestOpened",
        "PullRequestMerged",
        "PullRequestUpdated",
    ]
    assert sum(t == "PullRequestMerged" for t in types) == 1


def test_mutating_streamed_event_payload_results_does_not_change_stored_run():
    r = ControlPlaneRepo()
    r.upsert_test_run(
        "tr-1", mission_id="MSN-1", results=[{"name": "a", "status": "ok"}]
    )
    evs = r.stream("test_run", "tr-1")
    evs[0]["payload"]["results"][0]["status"] = "hacked"
    evs[0]["payload"]["results"].append({"name": "b", "status": "ok"})
    assert r.get_test_run("tr-1")["results"] == [{"name": "a", "status": "ok"}]


def test_mutating_returned_record_results_does_not_change_stored_run():
    r = ControlPlaneRepo()
    r.upsert_test_run(
        "tr-1", mission_id="MSN-1", results=[{"name": "a", "status": "ok"}]
    )
    rec = r.get_test_run("tr-1")
    rec["results"][0]["status"] = "hacked"
    rec["results"].append({"name": "b", "status": "ok"})
    assert r.get_test_run("tr-1")["results"] == [{"name": "a", "status": "ok"}]


def test_build_updated_via_upsert_test_run_without_kind_uses_stored_kind():
    r = ControlPlaneRepo()
    r.upsert_build("b-1", mission_id="MSN-1", status="running")
    r.upsert_test_run("b-1", status="success")
    evs = r.stream("test_run", "b-1")
    assert [e["event_type"] for e in evs] == ["BuildStarted", "BuildUpdated"]


def test_namespaced_mission_id_round_trips_through_filters():
    r = ControlPlaneRepo()
    r.upsert_test_run("tr-1", mission_id="pi:MSN-1")
    r.upsert_review("rv-1", mission_id="pi:MSN-1")
    r.upsert_pull_request("pr-1", mission_id="pi:MSN-1")
    runs = r.list_test_runs(mission_id="pi:MSN-1")
    reviews = r.list_reviews(mission_id="pi:MSN-1")
    prs = r.list_pull_requests(mission_id="pi:MSN-1")
    assert [x["test_run_id"] for x in runs] == ["tr-1"]
    assert [x["review_id"] for x in reviews] == ["rv-1"]
    assert [x["pr_id"] for x in prs] == ["pr-1"]
    assert runs[0]["mission_id"] == "pi:MSN-1"
    assert reviews[0]["mission_id"] == "pi:MSN-1"
    assert prs[0]["mission_id"] == "pi:MSN-1"
