"""A stored review reference is never changed or moved; only None -> value is allowed. Replay keeps the first reference."""
import pytest

from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.review_refs import keep_reference, repository
from herdr_engineering.control_plane.reviews import ReviewService

REPO = "github.com/org/app"


def named(repo=None):
    r = repo or ControlPlaneRepo()
    r.upsert_review("a", repository=REPO, pull_request_number=7, status="open")
    return r


def test_another_repository_cannot_replace_the_stored_one():
    r = named()
    with pytest.raises(ValueError):
        r.upsert_review("a", repository="github.com/org/other")
    rec = r.list_reviews()[0]
    assert (rec["repository"], rec["pull_request_number"]) == (REPO, 7)


def test_another_number_cannot_move_the_review_to_a_different_pull_request():
    r = named()
    with pytest.raises(ValueError):
        r.upsert_review("a", pull_request_number=9)
    with pytest.raises(ValueError):
        r.upsert_review("a", repository=REPO, pull_request_number=9)
    assert r.list_reviews()[0]["pull_request_number"] == 7


def test_a_rejected_update_changes_nothing_not_even_the_status():
    r = named()
    events = len(r.stream("review", "a"))
    with pytest.raises(ValueError):
        r.upsert_review("a", status="approved", pull_request_number=9)
    assert r.list_reviews()[0]["status"] == "open" and len(r.stream("review", "a")) == events


def test_the_same_reference_again_is_fine_and_an_equivalent_spelling_keeps_the_stored_one():
    r = named()
    assert r.upsert_review("a", repository=REPO, pull_request_number=7, status="done")["status"] == "done"
    rec = r.upsert_review("a", repository="github.com/org/app.git")
    assert rec["repository"] == REPO and rec["pull_request_number"] == 7


def test_only_none_to_value_is_allowed():
    r = ControlPlaneRepo()
    r.upsert_review("a", status="open")
    assert r.upsert_review("a", repository=REPO)["repository"] == REPO
    assert r.upsert_review("a", pull_request_number=3)["pull_request_number"] == 3
    with pytest.raises(ValueError):
        r.upsert_review("a", pull_request_number=4)


def test_ingest_review_updated_cannot_move_a_review_and_answers_with_an_error():
    repo = named()
    svc = IngestService(repo)
    with pytest.raises(ValueError):
        svc.ingest({"entity_type": "review", "entity_id": "a", "event_type": "review.updated",
                    "payload": {"repository": REPO, "pull_request_number": 9}})
    assert repo.list_reviews()[0]["pull_request_number"] == 7


def test_replay_keeps_the_first_reference_even_if_a_conflicting_event_is_in_the_log():
    r = named()
    # a log written by older code, or edited, can hold a later event that names another pull request
    r.record_event("review", "a", "ReviewUpdated", {"status": "approved", "repository": REPO, "pull_request_number": 9},
                   mission_id=None, session_id=None)
    r._reviews = {}
    r._rebuild_delivery_from_events()
    rec = r.list_reviews()[0]
    assert (rec["repository"], rec["pull_request_number"]) == (REPO, 7)


def test_keep_reference_is_pure():
    assert keep_reference(None, None, REPO, 5) == (REPO, 5)
    assert keep_reference(REPO, 5, None, None) == (REPO, 5)
    with pytest.raises(ValueError):
        keep_reference(REPO, 5, "github.com/x/y", None)


@pytest.mark.parametrize("bad", ["org/app/extra", "a:b/c", "localhost:3000/org/app", "a/b/c/d", "x.y/z"])
def test_repository_rejects_hosts_and_shapes_the_trail_cannot_read(bad):
    with pytest.raises(ValueError):
        repository(bad)


@pytest.mark.parametrize("good", ["org/app", "github.com/org/app", "ghe.example.com/org/app", "gitlab.local:8443/org/app"])
def test_repository_accepts_what_the_trail_reads(good):
    assert repository(good) == good


def test_plan_review_with_a_number_needs_a_repository_the_trail_can_read(acknowledged_sink):
    service = ReviewService(ControlPlaneRepo(sink=acknowledged_sink))
    base = dict(review_id="p1", instance_id="i", kind="plan", plan="x", pull_request_number=7)
    for bad in ("a:b/c", "org/app/extra", "localhost:3000/org/app"):
        with pytest.raises(ValueError):
            service.register(dict(base, repository=bad))
    assert service.register(dict(base, repository=REPO))["repository"] == REPO


def test_old_plan_reviews_with_unusual_repositories_still_register_and_relaunch(acknowledged_sink):
    service = ReviewService(ControlPlaneRepo(sink=acknowledged_sink))
    base = dict(review_id="old", instance_id="i", kind="plan", plan="x", repository="https://github.com/org/app.git")
    first = service.register(base)
    assert first["pull_request_number"] is None
    assert service.register(base)["review_id"] == "old"
