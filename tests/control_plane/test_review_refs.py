"""A review can name its pull request; absent means unknown, and the PR trail attaches by that explicit reference only."""
import pytest

from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.pr_trail import build
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.review_refs import pull_request_number, repository
from herdr_engineering.control_plane.reviews import ReviewService
from herdr_engineering.plannotator import main as plannotator_main
from herdr_engineering.plannotator import run_review
from tests.control_plane.test_pr_trail import NOW, pull, run, session, view

REPO = "github.com/org/app"


@pytest.mark.parametrize("bad", [0, -1, True, False, "7", 7.0, 2**31, [], {}])
def test_pull_request_number_rejects_everything_but_a_bounded_positive_int(bad):
    with pytest.raises(ValueError):
        pull_request_number(bad)


def test_pull_request_number_absent_stays_absent_and_valid_values_pass():
    assert pull_request_number(None) is None
    assert pull_request_number(1) == 1 and pull_request_number(2**31 - 1) == 2**31 - 1


@pytest.mark.parametrize("bad", ["", "app", "a b/c", "org/app\n", "../x", 7, "x" * 201, "org//app"])
def test_repository_rejects_unbounded_or_odd_strings(bad):
    with pytest.raises(ValueError):
        repository(bad)


def test_code_review_keeps_repository_and_pull_request_number():
    r = ControlPlaneRepo()
    rec = r.upsert_review("rv-1", reviewer="codex", repository=REPO, pull_request_number=7)
    assert (rec["repository"], rec["pull_request_number"]) == (REPO, 7)
    assert r.list_reviews()[0]["pull_request_number"] == 7
    started = r.stream("review", "rv-1")[0]["payload"]
    assert started["pull_request_number"] == 7 and started["repository"] == REPO


def test_old_style_review_has_no_reference_not_zero():
    r = ControlPlaneRepo()
    rec = r.upsert_review("rv-old", reviewer="codex", status="open")
    assert rec["pull_request_number"] is None and rec["repository"] is None


def test_code_review_rejects_bad_values_and_a_number_without_a_repository():
    r = ControlPlaneRepo()
    with pytest.raises(ValueError):
        r.upsert_review("rv-1", pull_request_number="7", repository=REPO)
    with pytest.raises(ValueError):
        r.upsert_review("rv-2", pull_request_number=7)
    with pytest.raises(ValueError):
        r.upsert_review("rv-3", pull_request_number=7, repository="not a repo")
    assert r.list_reviews() == []


def test_update_can_add_the_number_to_a_review_that_already_has_a_repository():
    r = ControlPlaneRepo()
    r.upsert_review("rv-1", repository=REPO, status="open")
    assert r.upsert_review("rv-1", pull_request_number=9)["pull_request_number"] == 9
    assert r.upsert_review("rv-1", status="done")["pull_request_number"] == 9, "an update that omits it keeps it"


def test_ingest_carries_the_reference_and_rejects_bad_types():
    repo = ControlPlaneRepo()
    svc = IngestService(repo)
    svc.ingest({"entity_type": "review", "entity_id": "rv-1", "event_type": "review.started",
                "payload": {"reviewer": "codex", "repository": REPO, "pull_request_number": 7}})
    assert repo.list_reviews()[0]["pull_request_number"] == 7
    with pytest.raises(ValueError):
        svc.ingest({"entity_type": "review", "entity_id": "rv-2", "event_type": "review.started",
                    "payload": {"reviewer": "codex", "repository": REPO, "pull_request_number": "7"}})


def test_reference_survives_event_replay():
    r = ControlPlaneRepo()
    r.upsert_review("rv-1", repository=REPO, pull_request_number=7)
    r.upsert_review("rv-1", status="approved")
    r._reviews = {}
    r._rebuild_delivery_from_events()
    rec = r.list_reviews()[0]
    assert (rec["repository"], rec["pull_request_number"]) == (REPO, 7)


def test_plan_review_names_its_pull_request_and_keeps_it_across_restart(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    service = ReviewService(repo)
    row = service.register(dict(review_id="plan-1", instance_id="i1", kind="plan", plan="# plan", repository=REPO, pull_request_number=7))
    assert row["pull_request_number"] == 7
    restored = ControlPlaneRepo(sink=acknowledged_sink)
    restored.import_state(repo.export_state())
    assert ReviewService(restored).get("plan-1")["pull_request_number"] == 7


def test_plan_review_without_one_has_none_and_bad_ones_are_rejected(acknowledged_sink):
    service = ReviewService(ControlPlaneRepo(sink=acknowledged_sink))
    assert service.register(dict(review_id="p1", instance_id="i", kind="plan", plan="x", repository=REPO))["pull_request_number"] is None
    for bad in ({"pull_request_number": "7", "repository": REPO}, {"pull_request_number": 0, "repository": REPO}, {"pull_request_number": 7}):
        with pytest.raises(ValueError):
            service.register(dict(review_id="p2", instance_id="i", kind="plan", plan="x", **bad))


def test_relaunching_a_plan_review_for_another_pull_request_is_refused(acknowledged_sink):
    service = ReviewService(ControlPlaneRepo(sink=acknowledged_sink))
    base = dict(review_id="p1", instance_id="i", kind="plan", plan="x", repository=REPO)
    service.register(dict(base, pull_request_number=7))
    with pytest.raises(ValueError):
        service.register(dict(base, pull_request_number=8))


def _trail(number, **kw):
    return build("org", "app", number, pull_requests=[pull(number)], sessions=[session("agent")], view=view(run(1, 35)), handoffs=[], spend=None, prices=False, now=NOW, **kw)


def test_pr_trail_attaches_a_review_by_explicit_reference_end_to_end(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    repo.upsert_review("code-7", reviewer="codex", status="done", verdict="approved", repository=REPO, pull_request_number=7)
    repo.upsert_review("code-8", reviewer="codex", status="done", verdict="changes_requested", repository=REPO, pull_request_number=8)
    repo.upsert_review("code-none", reviewer="codex", status="done", verdict="changes_requested")
    service = ReviewService(repo)
    service.register(dict(review_id="plan-7", instance_id="i", kind="plan", plan="# p", repository=REPO, pull_request_number=7))
    out = _trail(7, reviews=repo.list_reviews(), plan_reviews=service.list_reviews())
    assert {r["review_id"] for r in out["reviews"]} == {"code-7", "plan-7"}, "only reviews that name this pull request: not PR 8's, not the unnamed one"
    assert "changes_requested" not in str(out["signals"]["review_verdicts"]["value"])


def test_a_review_naming_another_repository_never_attaches():
    repo = ControlPlaneRepo()
    repo.upsert_review("other", repository="github.com/other/app", pull_request_number=7, verdict="changes_requested")
    assert _trail(7, reviews=repo.list_reviews(), plan_reviews=[])["reviews"] == []


def test_cli_sends_the_number_only_when_given_and_never_guesses_one(tmp_path):
    binary = tmp_path / "plannotator"
    binary.write_text("#!/usr/bin/python3\nimport json,os\nfrom pathlib import Path\n"
                      "Path(os.environ['PLANNOTATOR_READY_FILE']).write_text(json.dumps({'url':'http://h:1','port':1}))\n"
                      "print(json.dumps({'decision':'approved'}))\n")
    binary.chmod(0o700)
    plan = tmp_path / "p.md"
    plan.write_text("# p")
    bodies = []
    kw = dict(kind="plan", cwd=tmp_path, control_plane="http://herdr", plan_file=plan, binary=str(binary), repository="org/app", revision="abc",
              post=lambda base, path, body: bodies.append(body))
    run_review(**kw)
    run_review(pull_request_number=7, **kw)
    registrations = [b for b in bodies if b.get("kind") == "plan"]
    assert "pull_request_number" not in registrations[0]
    assert registrations[1]["pull_request_number"] == 7


def test_cli_pr_flag_must_be_an_integer(tmp_path):
    with pytest.raises(SystemExit):
        plannotator_main(["plan", "--cwd", str(tmp_path), "--pr", "seven"])
