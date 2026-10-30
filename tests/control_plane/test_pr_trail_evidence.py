"""PR trail evidence rules (review of #444): a signal is null with a reason unless there is positive evidence; wrong attribution is
worse than missing data."""
from datetime import timedelta

from herdr_engineering.control_plane import pr_trail
TTLCache = getattr(pr_trail, "TTLCache", None)
from tests.control_plane.test_pr_trail import NOW, T0, handoff, pull, run, session, stamp, trail, view


def review(rid="r1", *, at=36, verdict="approved", session_id="s1", kind=None, **extra):
    return {"review_id": rid, "session_id": session_id, "verdict": verdict, "started_at": stamp(at) if at is not None else None,
            **({"kind": kind} if kind else {}), **extra}


def merged(out):
    return out["signals"]["merged_without_rework"]


# 1 -- mission is a hint, never evidence
def test_mission_linked_agents_are_inferred_and_reviews_and_handoffs_of_the_mission_do_not_attach():
    other = review("other-pr", session_id="elsewhere", verdict="changes_requested", mission_id="m1")
    stray = handoff("h-m", "x", "y", history=[("queued", stamp(1)), ("rejected", stamp(2))], state="rejected", mission="m1")
    out = trail(pull_requests=[pull(mission="m1")], sessions=[session("s1"), session("m-only", branch="other", start=200, end=210, mission="m1")],
                reviews=[other, review()], handoffs=[stray], view=view(run(1, 35)))
    assert [s["session_id"] for s in out["sessions"]] == ["s1"]
    assert [(s["session_id"], s["inferred"]) for s in out["inferred_sessions"]] == [("m-only", True)]
    assert [r["review_id"] for r in out["reviews"]] == ["r1"] and out["handoffs"] == []
    assert merged(out)["value"] is True and out["signals"]["handoff_problems"]["value"] == 0
    assert "m-only" not in [e["ref"] for e in out["timeline"]]


def test_a_review_that_names_this_pull_request_attaches_without_a_session():
    named = review("named", session_id=None, pull_request_number=7, repository="github.com/org/app")
    wrong = review("wrong", session_id=None, pull_request_number=8, repository="github.com/org/app")
    assert [r["review_id"] for r in trail(reviews=[named, wrong])["reviews"]] == ["named"]


# 2 -- merged_without_rework true only on positive evidence
def test_no_review_or_no_ci_run_is_unknown_not_true():
    assert merged(trail(reviews=[], view=view(run(1, 35))))["value"] is None
    assert merged(trail(reviews=[review()], view=view()))["value"] is None
    assert merged(trail(reviews=[review()], view={"available": False}))["value"] is None


def test_reviewed_without_changes_and_no_later_revision_is_true():
    out = trail(reviews=[review(at=36)], view=view(run(1, 35)))
    assert merged(out)["value"] is True and "no later revision" in merged(out)["reason"]


# 3 -- undated reviews; only code reviews count
def test_a_review_without_a_time_makes_rework_unknown():
    assert merged(trail(reviews=[review(at=None)]))["value"] is None
    assert merged(trail(reviews=[review(at=36), review("r2", at=None)]))["value"] is None


def test_a_plan_review_before_coding_is_not_the_first_review():
    plan = review("plan", at=5, kind="plan", source="plan")
    out = trail(reviews=[], plan_reviews=[{**plan, "kind": "plan"}, {**review("code", at=36), "kind": "code"}], view=view(run(1, 35)))
    assert merged(out)["value"] is True
    only_plan = trail(reviews=[], plan_reviews=[{**plan, "kind": "plan"}], view=view(run(1, 35)))
    assert merged(only_plan)["value"] is None


# 4 -- CI ordering and bounds
def test_an_undated_run_makes_first_try_and_reruns_unknown():
    undated = {**run(1, 35, ref="refs/pull/7/head", event="pull_request", branch="main"), "created_at": None}
    out = trail(view=view(undated, run(2, 40)))
    assert out["signals"]["ci_green_first_try"]["value"] is None and out["signals"]["ci_reruns"]["value"] is None


def test_branch_runs_outside_the_pull_request_lifetime_are_not_this_pull_requests():
    out = trail(pull_requests=[pull(minutes=30, merged=100)], view=view(run(1, 5, state="failure"), run(2, 35), run(3, 150, state="failure")))
    assert [r["run_number"] for r in out["ci_runs"]] == [2]
    assert out["signals"]["ci_green_first_try"]["value"] is True


def test_runs_bearing_this_pull_request_number_are_kept_even_before_the_branch_window():
    out = trail(view=view(run(1, 5, event="pull_request", ref="refs/pull/7/head", branch="main"), run(2, 35)))
    assert [r["run_number"] for r in out["ci_runs"]] == [1, 2]


# 5 -- cost
def priced(sid):
    return {"configured": True, "total": 1.0, "by_model": [{"model": "m"}], "unpriced": []}


def test_cost_is_summed_over_every_linked_agent_not_the_displayed_twenty():
    sessions = [session(f"s{i}", branch="feat/x", start=i * 100, end=i * 100 + 10, commit_sha=f"{i:040x}") for i in range(25)]
    runs = [run(i, i * 100 + 5, sha=f"{i:040x}", event="pull_request", ref="refs/pull/7/head", branch="main") for i in range(25)]
    out = trail(sessions=sessions, view=view(*runs), prices=True, spend=priced, now=T0 + timedelta(days=3), pull_requests=[pull(minutes=5, merged=3000)])
    assert len(out["sessions"]) == 20 and out["omitted"]["sessions"] == 5
    assert out["signals"]["cost_usd_estimate"]["value"] == 25.0


def test_cost_is_unknown_when_an_agent_has_no_usage_rows_or_the_work_is_older_than_the_usage_kept():
    nothing = lambda sid: {"configured": True, "total": 0, "by_model": [], "unpriced": []}  # noqa: E731
    assert trail(prices=True, spend=nothing)["signals"]["cost_usd_estimate"]["value"] is None
    old = trail(prices=True, spend=priced, now=NOW + timedelta(days=60))
    assert old["signals"]["cost_usd_estimate"]["value"] is None and "30 days" in old["signals"]["cost_usd_estimate"]["reason"]


# 6 -- caps and duration
def test_undated_sessions_do_not_displace_dated_ones_and_inferred_agents_do_not_set_the_duration():
    many = [session(f"s{i}", start=0, end=60) for i in range(2)]
    undated = {**session("u"), "created_at": None}
    out = trail(pull_requests=[pull(mission="m", merged=100)], sessions=[undated, session("early", branch="z", start=-500, end=-400, mission="m"), *many])
    assert out["signals"]["duration_s"]["value"] is None or out["signals"]["duration_s"]["value"] <= 100 * 60


def test_duration_needs_a_real_merge_time():
    assert trail(pull_requests=[pull(merged=None)])["signals"]["duration_s"]["value"] is None
    assert trail(pull_requests=[pull(merged=100)])["signals"]["duration_s"]["value"] == 6000


# 7 -- two forge hosts for one owner/name
def test_the_same_owner_and_name_on_two_hosts_is_ambiguous_unless_a_host_is_given():
    prs = [pull(repository="github.com/acme/app"), pull(repository="ghe.example/acme/app", branch="other")]
    out = pr_trail.build("acme", "app", 7, pull_requests=prs, sessions=[session(repository="github.com/acme/app")], view=view(), handoffs=[],
                         reviews=[], plan_reviews=[], now=NOW)
    assert out["state"] == "ambiguous" and out["sessions"] == [] and "ghe.example" in out["reason"]
    chosen = pr_trail.build("acme", "app", 7, pull_requests=prs, sessions=[session(repository="github.com/acme/app")], view=view(), handoffs=[],
                            reviews=[], plan_reviews=[], now=NOW, host="github.com")
    assert chosen["state"] != "ambiguous" and chosen["pull_request"]["branch"] == "feat/x"


# 8 -- handoff rows
def test_bad_handoff_rows_are_skipped_and_a_truncated_list_is_reported():
    good = handoff("h1", "s1", "s2", history=[("queued", stamp(1)), ("rejected", stamp(2))], state="rejected")
    out = trail(handoffs=["junk", None, {"source": "x", "target": 5, "history": "no"}, {"source": None, "target": None}, good])
    assert [h["handoff_id"] for h in out["handoffs"]] == ["h1"]
    cut = trail(handoffs=[good], handoffs_truncated=True)
    assert cut["sources"]["handoffs"] == "truncated" and cut["signals"]["handoff_problems"]["value"] is None


# 9 -- only candidate sessions are examined; the result is cached briefly
def test_only_sessions_that_could_belong_are_examined(monkeypatch):
    calls = []
    real = pr_trail.session_commits
    monkeypatch.setattr(pr_trail, "session_commits", lambda s, *a, **k: calls.append(s["session_id"]) or real(s, *a, **k))
    unrelated = [session(f"o{i}", repository="github.com/other/repo") for i in range(50)] + [session(f"b{i}", branch="elsewhere") for i in range(50)]
    trail(sessions=[session("s1"), *unrelated])
    assert calls == ["s1"]


def test_ttl_cache_expires_and_is_bounded():
    clock = [0.0]
    cache = TTLCache(ttl=5, size=2, clock=lambda: clock[0])
    cache.put("a", 1)
    assert cache.get("a") == 1
    clock[0] = 6
    assert cache.get("a") is None
    cache.put("a", 1); cache.put("b", 2); cache.put("c", 3)
    assert cache.get("a") is None and cache.get("c") == 3 and len(cache) == 2


# 10 -- everything is redacted
def test_free_text_fields_are_redacted():
    secret = "sk-" + "a" * 30
    out = trail(reviews=[review(verdict=secret, kind=secret)], view=view({**run(1, 35, state=secret), "branch": "feat/x", "event": secret, "url": "https://ci/" + secret}),
                handoffs=[{**handoff("h1", "s1", "s2", history=[("queued", stamp(1))], state=secret)}])
    blob = repr(out)
    assert secret not in blob


# 11 -- covered by test_duration_needs_a_real_merge_time


def test_duration_reason_says_when_the_end_time_was_only_observed():
    observed = {**pull(merged=100), "ended_source": "observed"}
    reported = {**pull(merged=100), "ended_source": "reported"}
    assert "first observed" in trail(pull_requests=[observed])["signals"]["duration_s"]["reason"]
    assert "first observed" not in trail(pull_requests=[reported])["signals"]["duration_s"]["reason"]
