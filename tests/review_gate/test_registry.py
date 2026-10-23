from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.review_gate.models import Author, PrKey
from herdr_engineering.review_gate.registry import AuthorRegistry

KEY = PrKey.parse("github.com/acme/widgets#7")


def make():
    repo = ControlPlaneRepo()
    return repo, AuthorRegistry(repo)


def test_unknown_when_no_session_matches():
    repo, reg = make()
    repo.upsert_session("s1", host_id="h", branch="other", pane_id="1")
    assert reg.author_of(KEY, "feat/x") == Author(kind="unknown")


def test_matches_session_by_branch_and_records_model():
    repo, reg = make()
    repo.upsert_session(
        "s1",
        host_id="h1",
        model="claude-x",
        branch="feat/x",
        pane_id="p3",
        cwd="/work/widgets",
    )
    repo.upsert_session("s2", host_id="h1", model="m", branch="feat/y", pane_id="p4")
    a = reg.author_of(KEY, "feat/x")
    assert a.kind == "collab"
    assert (a.model, a.host_id, a.pane_id, a.session_id) == ("claude-x", "h1", "p3", "s1")


def test_pi_mission_session_is_pi_kind_and_other_repo_cwd_is_skipped():
    repo, reg = make()
    repo.upsert_session("o", host_id="h", branch="feat/x", cwd="/work/other", pane_id="1")
    repo.upsert_session(
        "p", host_id="h", branch="feat/x", cwd="/work/widgets", mission_id="m1", source="pi"
    )
    a = reg.author_of(KEY, "feat/x")
    assert (a.kind, a.session_id, a.mission_id) == ("pi", "p", "m1")


def test_two_sessions_same_branch_picks_most_recent():
    repo, reg = make()
    repo.upsert_session("old", host_id="h", branch="feat/x", pane_id="1")
    repo.upsert_session("new", host_id="h", branch="feat/x", pane_id="2")
    repo.upsert_session("old", host_id="h", branch="feat/x", pane_id="1")  # heartbeat: now newest
    assert reg.author_of(KEY, "feat/x").session_id == "old"


def test_pinned_author_survives_session_removal():
    repo, reg = make()
    repo.upsert_session("s1", host_id="h", model="m", branch="feat/x", pane_id="1")
    first = reg.author_of(KEY, "feat/x")
    reg.pin(KEY, first)
    repo._sessions.clear()
    assert reg.author_of(KEY, "feat/x") == first
    assert AuthorRegistry(repo).author_of(KEY, "feat/x") == first


def test_pin_is_idempotent():
    repo, reg = make()
    a = Author(kind="collab", model="m", session_id="s1")
    reg.pin(KEY, a)
    reg.pin(KEY, a)
    assert len(repo.stream("pr_gate_author", str(KEY))) == 1
    reg.pin(KEY, Author(kind="pi", session_id="s2"))
    assert reg.author_of(KEY, "b") == Author(kind="pi", session_id="s2")
