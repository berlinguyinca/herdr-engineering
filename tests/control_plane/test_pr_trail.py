"""PR trail: agents, handoffs, reviews and CI for one pull request; links say why and gaps are null, never guessed."""
from datetime import UTC, datetime, timedelta

from herdr_engineering.control_plane.pr_trail import build, same_repository

T0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=8)


def stamp(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def session(sid="s1", *, branch="feat/x", start=0, end=60, mission=None, repository="github.com/org/app"):
    return {"session_id": sid, "repository_id": repository, "branch": branch, "created_at": stamp(start), "updated_at": stamp(end),
            "status": "finished", "mission_id": mission, "agent_role": "coder", "runtime": "pi"}


def pull(number=7, *, status="merged", branch="feat/x", minutes=30, mission=None, repository="github.com/org/app", updated=100):
    return {"number": number, "repository": repository, "branch": branch, "status": status, "created_at": stamp(minutes),
            "updated_at": stamp(updated), "url": f"https://github.com/org/app/pull/{number}", "mission_id": mission}


def run(number, minutes, *, sha=None, state="success", branch="feat/x", event="push", ref=None):
    return {"run_number": number, "provider_repo_id": 7, "branch": branch, "revision": sha or f"{number:040x}", "created_at": stamp(minutes),
            "state": state, "event": event, "ref": ref, "url": f"https://ci/{number}"}


def view(*rows):
    return {"repos": [{"id": 7, "forge_url": "https://github.com/org/app"}], "pipelines": {"org/app": list(rows)}}


def handoff(hid, source, target, *, history, state, mission=None):
    return {"handoff_id": hid, "state": state, "created_at": history[0][1], "mission_id": mission, "goal": "implement the parser",
            "source": {"harness": "pi", "session_id": source}, "target": {"harness": "claude", "session_id": target},
            "history": [{"state": s, "at": a} for s, a in history]}


def trail(**kw):
    args = dict(pull_requests=[pull()], sessions=[session()], view=view(run(1, 10)), handoffs=[], reviews=[], plan_reviews=[], spend=None, prices=False, now=NOW)
    args.update(kw)
    return build("org", "app", 7, **args)


def test_unknown_pull_request_is_none():
    assert trail(pull_requests=[], view=view(run(1, 10))) is None


def test_linked_session_and_signals_for_a_clean_merge():
    out = trail()
    assert out["state"] == "linked" and [s["session_id"] for s in out["sessions"]] == ["s1"]
    assert out["sessions"][0]["rule"] == "pull_request_record"
    assert out["signals"]["merged_without_rework"]["value"] is True
    assert out["signals"]["ci_green_first_try"]["value"] is True
    assert out["signals"]["duration_s"]["value"] == 100 * 60
    assert out["signals"]["cost_usd_estimate"]["value"] is None and "price table" in out["signals"]["cost_usd_estimate"]["reason"]
    kinds = [e["kind"] for e in out["timeline"]]
    assert kinds[0] == "session" and kinds[-1] == "pr_closed" and "ci" in kinds


def test_shared_branch_agents_are_ambiguous_not_linked():
    out = trail(pull_requests=[pull(branch="feat/x")], sessions=[session("a", start=0, end=60), session("b", start=10, end=70)])
    assert out["state"] == "ambiguous" and out["sessions"] == []
    assert {a["session_id"] for a in out["ambiguous"]} == {"a", "b"}


def test_mission_rule_links_a_session_the_branch_rule_cannot():
    out = trail(pull_requests=[pull(mission="m1")], sessions=[session("s1"), session("late", branch="other", start=200, end=210, mission="m1")])
    rules = {s["session_id"]: s["rule"] for s in out["sessions"]}
    assert rules == {"s1": "pull_request_record", "late": "mission"}
    assert [s["session_id"] for s in out["sessions"]] == ["s1", "late"]


def test_rejected_handoff_is_a_problem_and_time_to_accept_is_measured():
    rows = [handoff("h1", "s1", "s2", history=[("queued", stamp(1)), ("claimed", stamp(2)), ("accepted", stamp(4)), ("in_progress", stamp(5))], state="in_progress"),
            handoff("h2", "s1", "s3", history=[("queued", stamp(6)), ("claimed", stamp(7)), ("rejected", stamp(8))], state="rejected"),
            handoff("h9", "zz", "yy", history=[("queued", stamp(6))], state="queued")]
    out = trail(handoffs=rows)
    assert [h["handoff_id"] for h in out["handoffs"]] == ["h1", "h2"]
    assert out["handoffs"][0]["time_to_accept_s"] == 180 and not out["handoffs"][0]["problem"]
    assert out["handoffs"][1]["problem"] and out["handoffs"][1]["time_to_accept_s"] is None
    assert out["signals"]["handoff_problems"]["value"] == 1


def test_changes_requested_review_is_rework_and_not_merged_without_rework():
    review = {"review_id": "r1", "session_id": "s1", "verdict": "changes_requested", "started_at": stamp(40), "reviewer": "codex"}
    out = trail(reviews=[review])
    assert out["signals"]["merged_without_rework"]["value"] is False
    assert out["signals"]["review_verdicts"]["value"] == [{"review_id": "r1", "verdict": "changes_requested"}]


def test_a_revision_built_after_the_first_review_is_rework():
    review = {"review_id": "r1", "session_id": "s1", "verdict": "approved", "started_at": stamp(20)}
    out = trail(reviews=[review], view=view(run(1, 10), run(2, 50)))
    assert out["signals"]["merged_without_rework"]["value"] is False
    assert "after the first review" in out["signals"]["merged_without_rework"]["reason"]


def test_first_try_ci_failure_and_reruns():
    out = trail(view=view(run(1, 10, sha="a" * 40, state="failure"), run(2, 15, sha="a" * 40), run(3, 20, sha="b" * 40)))
    assert out["signals"]["ci_green_first_try"]["value"] is False
    assert out["signals"]["ci_reruns"]["value"] == 1


def test_missing_sources_are_null_with_a_reason_never_false():
    out = trail(view={"available": False}, reviews=None, plan_reviews=None, handoffs=None)
    assert out["sources"]["ci"] == out["sources"]["reviews"] == out["sources"]["handoffs"] == "unavailable"
    for key in ("ci_green_first_try", "merged_without_rework", "review_verdicts", "handoff_problems"):
        assert out["signals"][key]["value"] is None and out["signals"][key]["reason"]


def test_open_pull_request_has_no_merge_signal_or_duration():
    out = trail(pull_requests=[pull(status="open")])
    assert out["signals"]["merged_without_rework"]["value"] is None and out["signals"]["duration_s"]["value"] is None


def test_cost_is_summed_when_priced_and_withheld_when_a_model_is_unpriced():
    ok = trail(prices=True, spend=lambda sid: {"configured": True, "total": 1.25, "unpriced": []})
    assert ok["signals"]["cost_usd_estimate"]["value"] == 1.25
    partial = trail(prices=True, spend=lambda sid: {"configured": True, "total": 1.25, "unpriced": [{"model": "mystery"}]})
    assert partial["signals"]["cost_usd_estimate"]["value"] is None and "mystery" in partial["signals"]["cost_usd_estimate"]["reason"]
    unreadable = trail(prices=True, spend=lambda sid: None)
    assert unreadable["signals"]["cost_usd_estimate"]["value"] is None


def test_output_is_bounded_and_counts_what_it_omits():
    many = [session(f"s{i}", branch=f"b{i}", start=i, end=i + 1, mission="m") for i in range(30)]
    out = trail(pull_requests=[pull(mission="m", branch="none")], sessions=many)
    assert len(out["sessions"]) == 20 and out["omitted"]["sessions"] == 10


def test_pull_request_event_runs_attach_by_number_even_without_a_record():
    out = trail(pull_requests=[], sessions=[], view=view(run(1, 10, event="pull_request", ref="refs/pull/7/head", branch="main")))
    assert out is not None and out["pull_request"]["status"] == "unknown" and out["state"] in ("none", "unknown")
    assert [r["match"] for r in out["ci_runs"]] == ["pull_request"]


def test_same_repository_ignores_a_missing_host_but_not_a_different_name():
    assert same_repository("org/app", "github.com/org/app") and same_repository("https://github.com/Org/App.git", "org/app")
    assert not same_repository("org/app", "org/other") and not same_repository("github.com/org/app", "gitlab.com/org/app")
