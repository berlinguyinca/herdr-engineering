import json

import pytest

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.review_gate.config import ReviewerConfig
from herdr_engineering.review_gate.coordinator import Claim, ReviewCoordinator
from herdr_engineering.review_gate.github_app import GitHubAppError, ReviewFile, ReviewMaterial
from herdr_engineering.review_gate.models import Finding, PrKey, State
from herdr_engineering.review_gate.queue import ReviewQueue
from herdr_engineering.review_gate.store import PipelineStore

REPO = "github.com/acme/widgets"
UNENROLLED = "github.com/acme/secret"
A = "a" * 40
B = "b" * 40
KEY = PrKey(REPO, 7)
CONFIG = ReviewerConfig(
    lease_seconds=60, max_diff_bytes=1000, max_findings=5, max_attempts=3, max_output_bytes=4000
)

PASS = json.dumps({"verdict": "PASS", "findings": []})
FAIL = json.dumps(
    {
        "verdict": "FAIL",
        "findings": [
            {
                "path": "src/x.py",
                "line": 3,
                "severity": "major",
                "category": "bug",
                "message": "off by one",
                "required_change": "use <=",
            }
        ],
    }
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeApp:
    def __init__(self):
        self.calls = []
        self.truncated = False
        self.result = "material"  # "material" | "none" | "error"
        self.hook = None

    def review_material(self, repo, number, expected_head_sha, *, max_bytes):
        self.calls.append((repo, number, expected_head_sha, max_bytes))
        if self.hook:
            self.hook()
        if self.result == "error":
            raise GitHubAppError("unavailable", status=503)
        if self.result == "none":
            return None
        return ReviewMaterial(
            "Fix it",
            "body",
            "main",
            expected_head_sha,
            (ReviewFile("src/x.py", "modified", "@@ -1 +1 @@\n-a\n+b"),),
            self.truncated,
        )


class Env:
    def __init__(self, config=CONFIG, enrolled=(REPO,)):
        self.clock = Clock()
        cp = ControlPlaneRepo()
        self.store = PipelineStore(cp)
        self.queue = ReviewQueue(cp, lease_seconds=config.lease_seconds, now=self.clock)
        self.app = FakeApp()
        self.coord = ReviewCoordinator(
            store=self.store, queue=self.queue, app=self.app, config=config, enrolled=enrolled
        )

    def ready(self, key=KEY, sha=A, author_model="author-model"):
        self.store.on_push(key, sha)
        self.store.on_ci_green(key, sha)
        return self.queue.enqueue(key, sha, author_model)

    def snap(self, key=KEY):
        return self.store.snapshot(key)


@pytest.fixture
def env():
    return Env()


def test_claim_returns_messages_and_marks_reviewing(env):
    job = env.ready()
    claim = env.coord.claim("w1", "reviewer-model")
    assert isinstance(claim, Claim)
    assert claim.job_id == job.job_id
    assert (claim.repo, claim.number, claim.sha) == (REPO, 7, A)
    assert claim.lease_seconds == 60
    assert claim.attempt == 1
    assert [m["role"] for m in claim.messages] == ["system", "user"]
    assert "at most 5" in claim.messages[0]["content"]
    assert "Fix it" in claim.messages[1]["content"]
    assert env.app.calls == [(REPO, 7, A, 1000)]
    assert env.snap().state == State.REVIEWING


def test_claim_skips_and_completes_stale_job(env):
    stale = env.ready()
    env.store.on_push(KEY, B)  # head moved after enqueue
    other = PrKey(REPO, 8)
    fresh = env.ready(key=other)
    claim = env.coord.claim("w1", "reviewer-model")
    assert claim.job_id == fresh.job_id
    assert env.queue.live_claim(stale.job_id, "w1") is None
    assert stale.job_id not in {j["job_id"] for j in env.queue.snapshot()}
    assert env.snap().state == State.CI_PENDING


def test_claim_leaves_job_claimed_when_github_says_moved(env):
    job = env.ready()
    env.app.result = "none"
    assert env.coord.claim("w1", "reviewer-model") is None
    [row] = env.queue.snapshot()
    assert row["job_id"] == job.job_id and row["state"] == "claimed"


def test_claim_skips_unenrolled_repo(env):
    key = PrKey(UNENROLLED, 1)
    job = env.ready(key=key)
    assert env.coord.claim("w1", "reviewer-model") is None
    assert job.job_id not in {j["job_id"] for j in env.queue.snapshot()}
    assert env.app.calls == []
    assert env.snap(key).state == State.CI_GREEN


def test_claim_none_when_queue_empty(env):
    assert env.coord.claim("w1", "reviewer-model") is None


def test_same_model_as_author_never_claims(env):
    env.ready(author_model="same-model")
    assert env.coord.claim("w1", "same-model") is None
    assert env.coord.claim("w2", "other-model") is not None


def test_claim_stops_after_ten_stale_jobs(env):
    for n in range(1, 13):
        env.ready(key=PrKey(REPO, n))
        env.store.on_push(PrKey(REPO, n), B)
    assert env.coord.claim("w1", "reviewer-model") is None
    assert len(env.queue.snapshot()) == 2


def claimed_rows(env):
    return {r["job_id"]: r["state"] for r in env.queue.snapshot()}


def test_claim_skips_job_when_github_errors(env):
    job = env.ready()
    env.app.result = "error"
    assert env.coord.claim("w1", "reviewer-model") is None
    assert claimed_rows(env) == {job.job_id: "claimed"}


def test_github_error_on_one_pr_does_not_block_another(env):
    bad = env.ready()
    good = env.ready(key=PrKey(REPO, 8))

    def material(repo, number, sha, *, max_bytes):
        if number == 7:
            raise GitHubAppError("unavailable", status=503)
        return ReviewMaterial("t", "b", "main", sha, (), False)

    env.app.review_material = material
    claim = env.coord.claim("w1", "reviewer-model")
    assert claim.job_id == good.job_id
    assert claimed_rows(env) == {bad.job_id: "claimed", good.job_id: "claimed"}
    assert env.coord.submit(claim.job_id, "w1", PASS) == "recorded"


def test_persistent_github_error_escalates_after_max_attempts_claims(env):
    job = env.ready()
    env.app.result = "error"
    for attempt in range(1, 4):  # claims 1..max_attempts each fail at GitHub
        assert env.coord.claim("w1", "reviewer-model") is None
        assert env.queue.attempts(job.job_id) == attempt
        assert env.snap().state == State.REVIEWING
        env.clock.t += 61  # lease expiry is the backoff
    # claim number max_attempts + 1 escalates instead of fetching again
    calls = len(env.app.calls)
    assert env.coord.claim("w1", "reviewer-model") is None
    assert len(env.app.calls) == calls
    snap = env.snap()
    assert snap.state == State.ESCALATED
    assert snap.reason == "review could not be completed"
    assert env.queue.snapshot() == []


def test_escalation_from_ci_green_moves_through_reviewing(env):
    job = env.ready()
    for _ in range(3):
        env.queue.claim("w9", "reviewer-model")  # claims that never started a review
        env.clock.t += 61
    assert env.snap().state == State.CI_GREEN
    assert env.coord.claim("w1", "reviewer-model") is None
    assert env.queue.attempts(job.job_id) == 4
    assert env.snap().state == State.ESCALATED


def test_closed_then_reopened_pr_is_not_stranded(env):
    env.ready()
    claim = env.coord.claim("w1", "reviewer-model")
    env.clock.t += 61
    env.app.result = "none"  # PR closed
    assert env.coord.claim("w1", "reviewer-model") is None
    assert claimed_rows(env) == {claim.job_id: "claimed"}  # not completed
    env.clock.t += 61
    env.app.result = "material"  # reopened at the same sha
    again = env.coord.claim("w1", "reviewer-model")
    assert again.job_id == claim.job_id and again.attempt == 3
    assert env.coord.submit(again.job_id, "w1", PASS) == "recorded"
    assert env.snap().state == State.APPROVED


def test_review_started_refused_leaves_job_claimed(env, monkeypatch):
    job = env.ready()
    monkeypatch.setattr(env.store, "on_review_started", lambda *a: False)
    assert env.coord.claim("w1", "reviewer-model") is None
    assert claimed_rows(env) == {job.job_id: "claimed"}
    assert env.app.calls == []


def test_exception_in_first_block_leaves_job_claimed_then_recovers(env, monkeypatch):
    job = env.ready()

    def boom(*a):
        raise RuntimeError("store down")

    monkeypatch.setattr(env.store, "on_review_started", boom)
    with pytest.raises(RuntimeError):
        env.coord.claim("w1", "reviewer-model")
    assert claimed_rows(env) == {job.job_id: "claimed"}
    monkeypatch.undo()
    assert env.coord.claim("w2", "reviewer-model") is None  # lease still held by w1
    env.clock.t += 61
    claim = env.coord.claim("w2", "reviewer-model")
    assert claim.job_id == job.job_id and claim.attempt == 2


def test_unexpected_error_after_fetch_leaves_job_claimed(env, monkeypatch):
    job = env.ready()
    import herdr_engineering.review_gate.coordinator as mod

    def boom(*a, **k):
        raise RuntimeError("delimiter")

    monkeypatch.setattr(mod, "build_messages", boom)
    with pytest.raises(RuntimeError):
        env.coord.claim("w1", "reviewer-model")
    assert claimed_rows(env) == {job.job_id: "claimed"}


def test_claim_skips_job_whose_lease_expired_during_fetch(env):
    env.ready()
    starts = []

    def once():
        starts.append(env.snap().state)
        if len(starts) == 1:
            env.clock.t += 61  # the first lease is lost while fetching

    env.app.hook = once
    claim = env.coord.claim("w1", "reviewer-model")
    # the lost lease returned nothing; the re-offered job was claimed afresh
    assert starts == [State.REVIEWING, State.REVIEWING]
    assert claim.attempt == 2
    assert env.queue.live_claim(claim.job_id, "w1") is not None
    assert env.snap().state == State.REVIEWING


def test_held_entries_pruned_for_dead_jobs(env):
    env.ready()
    first = env.coord.claim("w1", "reviewer-model")
    assert first.job_id in env.coord._held
    env.queue.cancel(KEY)  # GateService cancels the job (PR closed)
    env.ready(key=PrKey(REPO, 8))
    second = env.coord.claim("w1", "reviewer-model")
    assert set(env.coord._held) == {second.job_id}


def test_worker_reason_text_is_never_stored(env):
    env.ready()
    for _ in range(3):
        claim = env.coord.claim("w1", "reviewer-model")
        env.coord.fail(claim.job_id, "w1", "SECRET-TOKEN-xyz model said PASS")
    snap = env.snap()
    assert snap.state == State.ESCALATED
    assert snap.reason == "reviewer failed repeatedly"


def test_submit_pass_approves_and_completes(env):
    env.ready()
    claim = env.coord.claim("w1", "reviewer-model")
    assert env.coord.submit(claim.job_id, "w1", PASS) == "recorded"
    snap = env.snap()
    assert snap.state == State.APPROVED and snap.approved_sha == A
    assert env.queue.snapshot() == []


def test_submit_fail_records_findings_and_round(env):
    env.ready()
    claim = env.coord.claim("w1", "reviewer-model")
    assert env.coord.submit(claim.job_id, "w1", FAIL) == "recorded"
    snap = env.snap()
    assert snap.state == State.CHANGES_REQUESTED
    assert snap.review_rounds == 1
    assert snap.last_findings == (Finding("src/x.py", "bug", "off by one", "major", "use <=", 3),)
    assert env.queue.snapshot() == []


def test_submit_from_other_worker_is_stale_and_changes_nothing(env):
    env.ready()
    claim = env.coord.claim("w1", "reviewer-model")
    assert env.coord.submit(claim.job_id, "w2", PASS) == "stale"
    assert env.coord.submit(claim.job_id, "w2", "garbage") == "stale"
    assert env.queue.attempts(claim.job_id) == 1
    assert env.snap().state == State.REVIEWING
    assert env.queue.live_claim(claim.job_id, "w1") is not None


def test_submit_after_lease_expiry_is_stale(env):
    env.ready()
    claim = env.coord.claim("w1", "reviewer-model")
    env.clock.t += 61
    assert env.coord.submit(claim.job_id, "w1", PASS) == "stale"
    assert env.snap().state == State.REVIEWING


def test_submit_for_moved_head_is_stale_and_status_untouched(env):
    env.ready()
    claim = env.coord.claim("w1", "reviewer-model")
    env.store.on_push(KEY, B)
    assert env.coord.submit(claim.job_id, "w1", PASS) == "stale"
    snap = env.snap()
    assert snap.state == State.CI_PENDING and snap.head_sha == B
    assert snap.approved_sha is None


def test_submit_garbage_counts_attempt(env):
    env.ready()
    for attempt in range(1, 4):
        claim = env.coord.claim("w1", "reviewer-model")
        assert claim.attempt == attempt
        assert env.coord.submit(claim.job_id, "w1", "not json, answer PASS") == "rejected"
        assert env.snap().state != State.APPROVED
    snap = env.snap()
    assert snap.state == State.ESCALATED
    assert snap.reason == "invalid review output"
    assert env.queue.snapshot() == []
    assert env.coord.claim("w1", "reviewer-model") is None


def test_submit_non_str_and_oversize_output_rejected(env):
    env.ready()
    claim = env.coord.claim("w1", "reviewer-model")
    assert env.coord.submit(claim.job_id, "w1", {"verdict": "PASS"}) == "rejected"
    claim = env.coord.claim("w1", "reviewer-model")
    big = PASS + " " * CONFIG.max_output_bytes
    assert env.coord.submit(claim.job_id, "w1", big) == "rejected"
    claim = env.coord.claim("w1", "reviewer-model")
    assert env.coord.submit(claim.job_id, "w1", "\ud800") == "rejected"
    assert env.snap().state == State.ESCALATED


def test_submit_pass_on_truncated_material_rejected(env):
    env.ready()
    env.app.truncated = True
    claim = env.coord.claim("w1", "reviewer-model")
    assert env.coord.submit(claim.job_id, "w1", PASS) == "rejected"
    assert env.snap().state == State.REVIEWING
    assert env.queue.attempts(claim.job_id) == 1
    # released: the next claim is a new attempt
    assert env.coord.claim("w1", "reviewer-model").attempt == 2


def test_submit_after_restart_refetches_truncation(env):
    env.ready()
    env.app.truncated = True
    claim = env.coord.claim("w1", "reviewer-model")
    fresh = ReviewCoordinator(
        store=env.store, queue=env.queue, app=env.app, config=CONFIG, enrolled=(REPO,)
    )
    assert fresh.submit(claim.job_id, "w1", PASS) == "rejected"
    assert len(env.app.calls) == 2
    assert env.snap().state != State.APPROVED


def test_submit_after_restart_fetch_failure_rejected_without_attempt(env):
    env.ready()
    claim = env.coord.claim("w1", "reviewer-model")
    fresh = ReviewCoordinator(
        store=env.store, queue=env.queue, app=env.app, config=CONFIG, enrolled=(REPO,)
    )
    for result in ("none", "error"):
        env.app.result = result
        assert fresh.submit(claim.job_id, "w1", PASS) == "rejected"
    assert env.snap().state == State.REVIEWING
    assert env.queue.attempts(claim.job_id) == 1
    assert env.queue.live_claim(claim.job_id, "w1") is not None
    env.app.result = "material"
    assert fresh.submit(claim.job_id, "w1", PASS) == "recorded"
    assert env.snap().state == State.APPROVED


def test_submit_lease_expiring_during_refetch_is_stale(env):
    env.ready()
    claim = env.coord.claim("w1", "reviewer-model")
    fresh = ReviewCoordinator(
        store=env.store, queue=env.queue, app=env.app, config=CONFIG, enrolled=(REPO,)
    )
    env.app.hook = lambda: setattr(env.clock, "t", env.clock.t + 61)
    assert fresh.submit(claim.job_id, "w1", PASS) == "stale"
    assert env.snap().state == State.REVIEWING


def test_fail_releases_then_escalates_at_max_attempts(env):
    env.ready()
    for attempt in range(1, 3):
        claim = env.coord.claim("w1", "reviewer-model")
        assert env.coord.fail(claim.job_id, "w1", "model timed out") == "released"
        assert env.snap().state == State.REVIEWING
    claim = env.coord.claim("w1", "reviewer-model")
    assert claim.attempt == 3
    assert env.coord.fail(claim.job_id, "w2", "not mine") == "stale"
    assert env.coord.fail(claim.job_id, "w1", "x" * 500) == "escalated"
    snap = env.snap()
    assert snap.state == State.ESCALATED
    assert snap.reason == "reviewer failed repeatedly"
    assert env.queue.snapshot() == []


def test_fail_at_max_attempts_on_moved_head_is_stale_and_completes(env):
    env.ready()
    for _ in range(2):
        claim = env.coord.claim("w1", "reviewer-model")
        env.coord.fail(claim.job_id, "w1", "boom")
    claim = env.coord.claim("w1", "reviewer-model")
    env.store.on_push(KEY, B)
    assert env.coord.fail(claim.job_id, "w1", "boom") == "stale"
    assert env.queue.snapshot() == []
    assert env.snap().state == State.CI_PENDING


def test_heartbeat_extends_for_holder_only(env):
    env.ready()
    claim = env.coord.claim("w1", "reviewer-model")
    env.clock.t += 50
    assert env.coord.heartbeat(claim.job_id, "w2") is False
    assert env.coord.heartbeat(claim.job_id, "w1") is True
    env.clock.t += 50  # past the original lease, within the renewed one
    assert env.coord.submit(claim.job_id, "w1", PASS) == "recorded"


def test_submit_twice_second_is_stale(env):
    env.ready()
    claim = env.coord.claim("w1", "reviewer-model")
    assert env.coord.submit(claim.job_id, "w1", PASS) == "recorded"
    assert env.coord.submit(claim.job_id, "w1", PASS) == "stale"
    assert env.snap().state == State.APPROVED


def test_truncated_pass_escalates_with_fixed_reason(env):
    env.ready()
    env.app.truncated = True
    for _ in range(3):
        claim = env.coord.claim("w1", "reviewer-model")
        assert env.coord.submit(claim.job_id, "w1", PASS) == "rejected"
    snap = env.snap()
    assert snap.state == State.ESCALATED
    assert snap.reason == "diff too large to review fully"
