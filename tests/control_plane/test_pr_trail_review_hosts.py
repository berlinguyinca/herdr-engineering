"""A review that names a repository without a forge host is attached only when the pull request is on one host at most, and is
marked host_unverified, feeds no merged_without_rework value, and a bool can never stand in for a pull request number."""
from tests.control_plane.test_pr_trail import NOW, build, pull, run, session, view


def review(rid="r1", *, repository, number=7, verdict="approved", minutes=40):
    from tests.control_plane.test_pr_trail import stamp
    return {"review_id": rid, "repository": repository, "pull_request_number": number, "verdict": verdict, "status": "done",
            "started_at": stamp(minutes), "reviewer": "codex"}


def trail(reviews, **kw):
    args = dict(pull_requests=[pull()], sessions=[session()], view=view(run(1, 35)), handoffs=[], reviews=reviews, plan_reviews=[], spend=None, prices=False, now=NOW)
    args.update(kw)
    return build("org", "app", 7, **args)


def test_a_hostless_review_attaches_and_is_marked_when_the_pull_request_has_a_known_host():
    out = trail([review(repository="org/app")])
    row = out["reviews"][0]
    assert row["rule"] == "pull_request" and row["host_unverified"] is True and "forge host" in row["host_unverified_reason"]


def test_a_hostless_review_gives_no_merged_without_rework_value():
    out = trail([review(repository="org/app")])
    sig = out["signals"]["merged_without_rework"]
    assert sig["value"] is None and "forge host" in sig["reason"]


def test_even_a_hostless_changes_requested_review_cannot_make_it_false():
    out = trail([review(repository="org/app", verdict="changes_requested")])
    assert out["signals"]["merged_without_rework"]["value"] is None


def test_a_review_with_the_host_is_verified_and_counts():
    out = trail([review(repository="github.com/org/app")])
    assert out["reviews"][0]["host_unverified"] is False
    assert "forge host" not in out["signals"]["merged_without_rework"]["reason"]


def test_a_hostless_review_does_not_attach_when_the_repository_is_on_several_hosts():
    prs = [pull(repository="github.com/org/app"), {**pull(repository="ghe.example.com/org/app"), "url": "https://ghe.example.com/org/app/pull/7"}]
    out = trail([review(repository="org/app")], pull_requests=prs, host="github.com")
    assert out["reviews"] == []


def test_when_nothing_has_a_host_a_hostless_review_is_not_marked_unverified():
    out = trail([review(repository="org/app")], pull_requests=[pull(repository="org/app")], sessions=[session(repository="org/app")], view=None)
    rows = out["reviews"]
    assert rows and rows[0]["host_unverified"] is False


def test_a_bool_is_not_a_pull_request_number():
    row = review(repository="github.com/org/app", number=True)
    out = build("org", "app", 1, pull_requests=[pull(1)], sessions=[session()], view=view(run(1, 35)), handoffs=[], reviews=[row], plan_reviews=[], spend=None, prices=False, now=NOW)
    assert out["reviews"] == []
    legacy = {k: v for k, v in row.items() if k != "pull_request_number"} | {"pr_number": True}
    out = build("org", "app", 1, pull_requests=[pull(1)], sessions=[session()], view=view(run(1, 35)), handoffs=[], reviews=[legacy], plan_reviews=[], spend=None, prices=False, now=NOW)
    assert out["reviews"] == []
