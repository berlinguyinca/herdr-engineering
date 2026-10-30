"""Session -> commit/PR links: only unambiguous matches, never a guess."""
from datetime import UTC, datetime, timedelta

from herdr_engineering.control_plane.session_commits import GRACE, session_commits

T0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
REPO = "github.com/org/app"
SHA = "a" * 40


def stamp(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def session(sid="s1", *, branch="feat/x", start=0, end=60, status="finished", repository=REPO, commit=None):
    return {"session_id": sid, "repository_id": repository, "branch": branch, "created_at": stamp(start),
            "updated_at": stamp(end), "status": status, "commit_sha": commit}


def run(number, minutes, *, branch="feat/x", revision=None, ref=None, repo_id=7):
    return {"run_number": number, "provider_repo_id": repo_id, "branch": branch, "revision": revision or f"{number:040x}",
            "created_at": stamp(minutes), "state": "success", "event": "push", "ref": ref, "url": f"https://ci/x/{number}"}


def view(*rows, forge="https://github.com/org/app"):
    return {"repos": [{"id": 7, "forge_url": forge}], "pipelines": {"org/app": list(rows)}}


NOW = T0 + timedelta(hours=5)


def link(s, others, v):
    return session_commits(s, [s, *others], v, now=NOW)


def test_runs_on_the_agents_own_branch_inside_its_window_are_linked_newest_first():
    out = link(session(), [], view(run(1, 10), run(2, 40), run(3, 61), run(4, 61 + GRACE.seconds // 60 + 5), run(5, -5)))
    assert out["state"] == "linked" and out["reason"] == ""
    assert [c["run_number"] for c in out["commits"]] == [3, 2, 1]
    assert {c["match"] for c in out["commits"]} == {"branch_window"}


def test_pull_request_number_comes_from_the_run_ref():
    out = link(session(), [], view(run(1, 10, ref="refs/pull/42/head"), run(2, 20)))
    assert out["pull_requests"] == [{"number": 42}]
    assert [c.get("pull_request") for c in out["commits"]] == [None, 42]


def pr(number, minutes, *, branch="feat/x", repository="https://github.com/org/app.git"):
    return {"number": number, "url": f"https://github.com/org/app/pull/{number}", "status": "open", "branch": branch,
            "repository": repository, "created_at": stamp(minutes)}


def test_a_pull_request_record_in_the_window_is_linked_with_its_url_and_status():
    out = session_commits(session(), [session()], view(run(1, 10, ref="refs/pull/42/head")), now=NOW,
                          pull_requests=[pr(42, 12), pr(43, 500), pr(44, 12, branch="other"), pr(45, 12, repository="https://github.com/org/else")])
    assert out["state"] == "linked"
    assert out["pull_requests"] == [{"number": 42, "url": "https://github.com/org/app/pull/42", "status": "open", "at": stamp(12)}]


def test_a_pull_request_alone_links_and_a_shared_or_contested_branch_does_not():
    only = session_commits(session(), [session()], view(), now=NOW, pull_requests=[pr(7, 5)])
    assert only["state"] == "linked" and only["commits"] == [] and only["pull_requests"][0]["number"] == 7
    shared = session_commits(session(branch="main"), [session(branch="main")], view(), now=NOW, pull_requests=[pr(7, 5, branch="main")])
    assert shared["state"] == "ambiguous" and shared["pull_requests"] == []
    a, b = session("a", start=0, end=60), session("b", start=30, end=90)
    assert session_commits(a, [a, b], view(), now=NOW, pull_requests=[pr(7, 40)])["pull_requests"] == []


def test_other_branches_and_other_repositories_never_match():
    v = view(run(1, 10, branch="feat/y"), run(2, 10, repo_id=9))
    v["repos"].append({"id": 9, "forge_url": "https://github.com/org/other"})
    out = link(session(), [], v)
    assert out["state"] == "none" and "No CI run or pull request on feat/x" in out["reason"]


def test_a_shared_branch_is_ambiguous_with_no_commits():
    out = link(session(branch="main"), [], view(run(1, 10, branch="main")))
    assert out["state"] == "ambiguous" and out["commits"] == [] and "shared branch" in out["reason"]


def test_two_agents_on_one_branch_at_once_are_both_ambiguous():
    a, b = session("a", start=0, end=60), session("b", start=30, end=90)
    v = view(run(1, 40))
    for s, other in ((a, b), (b, a)):
        out = link(s, [other], v)
        assert out["state"] == "ambiguous" and out["commits"] == [] and "2 agents" in out["reason"]


def test_sequential_agents_on_one_branch_each_keep_their_own_window():
    a, b = session("a", start=0, end=30), session("b", start=300, end=360)
    v = view(run(1, 10), run(2, 320))
    assert [c["run_number"] for c in link(a, [b], v)["commits"]] == [1]
    assert [c["run_number"] for c in link(b, [a], v)["commits"]] == [2]


def test_an_exact_reported_commit_links_even_on_a_shared_branch():
    out = link(session(branch="main", commit=SHA), [], view(run(1, 10, branch="main", revision=SHA), run(2, 11, branch="main")))
    assert out["state"] == "linked"
    assert [(c["run_number"], c["match"]) for c in out["commits"]] == [(1, "commit")]


def test_an_active_session_window_runs_to_now():
    out = link(session(status="active", end=10), [], view(run(1, 200)))
    assert [c["run_number"] for c in out["commits"]] == [1]


def test_missing_repository_or_branch_says_why():
    assert "no repository" in link(session(repository=None), [], view(run(1, 10)))["reason"]
    out = link(session(branch=None), [], view(run(1, 10)))
    assert out["state"] == "none" and "no branch" in out["reason"]


def test_unparseable_times_and_empty_views_do_not_raise():
    s = session()
    s["created_at"] = "not a time"
    assert link(s, [], view(run(1, 10)))["state"] == "none"
    assert link(session(), [], {})["state"] == "none"
    bad = view({"run_number": 1, "provider_repo_id": 7, "branch": "feat/x", "created_at": "junk"})
    assert link(session(), [], bad)["commits"] == []

