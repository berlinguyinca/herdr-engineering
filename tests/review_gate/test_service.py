import threading

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.review_gate.config import AppConfig, RepoEnrollment, ReviewGateConfig
from herdr_engineering.review_gate.github_app import GitHubAppError, PrInfo
from herdr_engineering.review_gate.models import Author, Finding, PrKey, State, Verdict
from herdr_engineering.review_gate.queue import ReviewQueue
from herdr_engineering.review_gate.registry import AuthorRegistry
from herdr_engineering.review_gate.service import STATUS_CONTEXT, GateService
from herdr_engineering.review_gate.store import PipelineStore

REPO = "github.com/acme/widgets"
OTHER = "github.com/acme/gadgets"
UNENROLLED = "github.com/acme/secret"
A = "a" * 40
B = "b" * 40
KEY = PrKey(REPO, 7)


class FakeApp:
    def __init__(self):
        self.prs: dict[str, list[PrInfo]] = {}
        self.calls: list[tuple] = []
        self.merge_result = True
        self.fail_open_prs: set[str] = set()

    def open_prs(self, repo):
        self.calls.append(("open_prs", repo))
        if repo in self.fail_open_prs:
            raise GitHubAppError("unavailable", status=503)
        return list(self.prs.get(repo, []))

    def set_status(self, repo, sha, state, description, *, context="herdr/review-gate"):
        self.calls.append(("set_status", repo, sha, state, description, context))

    def merge_pr(self, repo, number, sha):
        self.calls.append(("merge_pr", repo, number, sha))
        return self.merge_result

    def update_branch(self, repo, number, expected_head_sha):
        self.calls.append(("update_branch", repo, number, expected_head_sha))

    def patch_id(self, repo, base_sha, head_sha):
        self.calls.append(("patch_id", repo, base_sha, head_sha))
        return "p1"

    def statuses(self, sha=None):
        return [c for c in self.calls if c[0] == "set_status" and (sha is None or c[2] == sha)]

    def last_status(self, sha):
        return self.statuses(sha)[-1][3]

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


def pr(number=7, sha=A, *, draft=False, ref="feat/x"):
    return PrInfo(
        number=number, head_sha=sha, head_ref=ref, base_ref="main", author_login="bot", draft=draft
    )


def ci_row(sha, state, *, repo="acme/widgets", event="pull_request", n=1):
    return {
        "revision": sha,
        "state": state,
        "event": event,
        "run_number": n,
        "repository": repo,
        "provider_repo_id": 11,
        "forge_url": f"https://github.com/{repo}/pull/7",
    }


class CI:
    def __init__(self):
        self.rows: dict[str, list[dict]] = {}
        self.stale = False

    def __call__(self):
        return {"pipelines": self.rows, "stale": self.stale, "available": True}

    def set(self, *rows, repo="acme/widgets"):
        self.rows[repo] = list(rows)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(repos=(REPO,)):
    cp = ControlPlaneRepo()
    app = FakeApp()
    store = PipelineStore(cp)
    queue = ReviewQueue(cp)
    registry = AuthorRegistry(cp)
    ci = CI()
    config = ReviewGateConfig(
        enabled=True,
        app=AppConfig(1, 2, "/nonexistent/key.pem"),
        repos=tuple(RepoEnrollment(repo=r) for r in repos),
        poll_seconds=5,
    )
    svc = GateService(
        config=config,
        app=app,
        store=store,
        queue=queue,
        registry=registry,
        ci_snapshot=ci,
        now=Clock(),
    )
    return svc, app, store, queue, registry, ci


def approve(store, key, sha):
    assert store.on_review_started(key, sha, "reviewer-m")
    assert store.on_verdict(key, Verdict("PASS", sha, (), "reviewer-m"))


def test_new_head_sets_pending_status_first():
    svc, app, store, _, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    summary = svc.tick()
    after_list = app.calls[1:]
    assert after_list[0][0] == "set_status"
    assert after_list[0][2:4] == (A, "pending")
    assert after_list[0][5] == STATUS_CONTEXT == "herdr/review-gate"
    assert store.snapshot(KEY).head_sha == A
    assert summary["prs"] == 1 and summary["errors"] == 0


def test_failed_pending_status_records_nothing_for_new_head():
    """The pending status for a new head is a precondition for everything else."""
    svc, app, store, queue, registry, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))

    def down(*a, **kw):
        app.calls.append(("set_status_failed",))
        raise GitHubAppError("unavailable", status=502)

    app.set_status = down
    summary = svc.tick()
    assert summary["errors"] == 1 and summary["enqueued"] == 0
    assert app.calls == [("open_prs", REPO), ("set_status_failed",)]
    assert store.snapshot(KEY).head_sha is None
    assert queue.snapshot() == []
    assert registry._pinned(KEY) is None


def test_green_ci_enqueues_exactly_one_review():
    svc, app, store, queue, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    first = svc.tick()
    second = svc.tick()
    assert first["enqueued"] == 1 and second["enqueued"] == 0
    jobs = queue.snapshot()
    assert [(j["key"], j["sha"]) for j in jobs] == [(str(KEY), A)]
    # The store has no REVIEW_QUEUED event: the state stays CI_GREEN, which
    # on_review_started accepts.
    assert store.snapshot(KEY).state == State.CI_GREEN
    assert app.last_status(A) == "pending"


def test_red_ci_enqueues_nothing_and_sets_failure():
    svc, app, store, queue, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "failure", n=3))
    summary = svc.tick()
    assert summary["enqueued"] == 0
    assert queue.snapshot() == []
    assert store.snapshot(KEY).state == State.CI_RED
    assert app.last_status(A) == "failure"
    assert "run 3 failure" in app.statuses(A)[-1][4]


def test_stale_ci_snapshot_enqueues_nothing():
    svc, app, store, queue, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    ci.stale = True
    assert svc.tick()["enqueued"] == 0
    assert queue.snapshot() == []
    assert store.snapshot(KEY).state == State.CI_PENDING
    assert app.last_status(A) == "pending"


def test_snapshot_without_repo_is_pending():
    svc, app, store, queue, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success", repo="acme/other"), repo="acme/other")
    assert svc.tick()["enqueued"] == 0
    assert queue.snapshot() == []
    assert store.snapshot(KEY).state == State.CI_PENDING


def test_ci_snapshot_raising_is_treated_as_stale():
    svc, app, store, queue, _, _ = make()
    app.prs[REPO] = [pr()]

    def boom():
        raise RuntimeError("ci down")

    svc._ci_snapshot = boom
    assert svc.tick()["enqueued"] == 0
    assert queue.snapshot() == []
    assert store.snapshot(KEY).state == State.CI_PENDING


def test_approved_green_pr_is_merged_with_head_sha():
    svc, app, store, _, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    svc.tick()
    approve(store, KEY, A)
    summary = svc.tick()
    assert summary["merged"] == 1
    assert app.named("merge_pr") == [("merge_pr", REPO, 7, A)]
    merge_at = app.calls.index(("merge_pr", REPO, 7, A))
    assert app.calls[merge_at - 1][0] == "set_status" and app.calls[merge_at - 1][3] == "success"
    assert store.snapshot(KEY).state == State.MERGED


def test_approved_but_head_moved_is_not_merged():
    svc, app, store, _, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    svc.tick()
    approve(store, KEY, A)
    app.merge_result = False  # GitHub refused: head moved / not mergeable
    summary = svc.tick()
    assert summary["merged"] == 0
    assert store.snapshot(KEY).state == State.APPROVED
    assert app.last_status(A) == "pending"
    assert app.named("update_branch") == [("update_branch", REPO, 7, A)]
    svc.tick()
    assert len(app.named("update_branch")) == 1  # once per head SHA
    # The new head then arrives: never merged on the old SHA, back to CI.
    app.merge_result = True
    app.prs[REPO] = [pr(sha=B)]
    n_merges = len(app.named("merge_pr"))
    svc.tick()
    assert len(app.named("merge_pr")) == n_merges
    assert store.snapshot(KEY).state == State.CI_PENDING


def test_merge_raising_reverts_status_to_pending():
    svc, app, store, _, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    svc.tick()
    approve(store, KEY, A)

    def broken(repo, number, sha):
        app.calls.append(("merge_pr", repo, number, sha))
        raise GitHubAppError("unavailable", status=502)

    app.merge_pr = broken
    summary = svc.tick()
    assert summary["errors"] == 1 and summary["merged"] == 0
    assert app.last_status(A) == "pending"
    assert store.snapshot(KEY).state == State.APPROVED


def test_push_during_review_cancels_job_and_resets_status():
    svc, app, store, queue, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    svc.tick()
    job = queue.claim("w1", "reviewer-m")
    assert job is not None and job.sha == A
    assert store.on_review_started(KEY, A, "reviewer-m")
    app.prs[REPO] = [pr(sha=B)]
    ci.set(ci_row(A, "success"))  # nothing for B yet
    svc.tick()
    assert queue.snapshot() == []  # the A job was cancelled
    assert queue.complete(job.job_id, "w1") is False
    snap = store.snapshot(KEY)
    assert snap.state == State.CI_PENDING and snap.head_sha == B
    assert app.statuses(B)[0][3] == "pending"


def test_new_head_after_approval_is_rereviewed():
    # Replaces test_rebase_with_same_patch_id_keeps_approval_and_merges: there
    # is no automatic carry-over (no base SHA available), so every new head is
    # reviewed again and patch_id is never consulted.
    svc, app, store, queue, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    svc.tick()
    assert queue.claim("w1", "reviewer-m") is not None
    approve(store, KEY, A)
    app.merge_result = False
    svc.tick()
    app.prs[REPO] = [pr(sha=B)]
    ci.set(ci_row(A, "success"), ci_row(B, "success", n=2))
    summary = svc.tick()
    assert summary["merged"] == 0 and summary["enqueued"] == 1
    assert [j["sha"] for j in queue.snapshot()] == [B]
    assert store.snapshot(KEY).state == State.CI_GREEN
    assert app.named("patch_id") == []
    assert app.last_status(B) == "pending"


def test_escalated_pr_stays_failure_until_resume():
    svc, app, store, queue, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    svc.tick()
    assert store.on_review_started(KEY, A, "reviewer-m")
    bad = Finding("a.py", "bug", "nope", "high", "fix it", 1)
    store._review_cap = 1  # escalate on the first FAIL
    assert store.on_verdict(KEY, Verdict("FAIL", A, (bad,), "reviewer-m"))
    assert store.snapshot(KEY).state == State.ESCALATED
    svc.tick()
    assert app.last_status(A) == "failure"
    assert "escalated" in app.statuses(A)[-1][4]
    # A push keeps it escalated: pending first for the new head, then failure.
    app.prs[REPO] = [pr(sha=B)]
    ci.set(ci_row(B, "success", n=2))
    summary = svc.tick()
    assert summary["enqueued"] == 0
    assert [s[3] for s in app.statuses(B)] == ["pending", "failure"]
    assert store.snapshot(KEY).state == State.ESCALATED
    svc.tick()
    assert app.last_status(B) == "failure"
    assert store.resume(KEY)
    assert svc.tick()["enqueued"] == 1
    assert app.last_status(B) == "pending"


def test_changes_requested_holds_failure():
    svc, app, store, _, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    svc.tick()
    assert store.on_review_started(KEY, A, "reviewer-m")
    bad = Finding("a.py", "bug", "nope", "high", "fix it", 1)
    assert store.on_verdict(KEY, Verdict("FAIL", A, (bad,), "reviewer-m"))
    svc.tick()
    assert app.last_status(A) == "failure"
    assert "changes requested" in app.statuses(A)[-1][4]


def test_one_repo_error_does_not_stop_other_repos():
    svc, app, store, queue, _, ci = make(repos=(REPO, OTHER))
    app.fail_open_prs.add(REPO)
    app.prs[OTHER] = [pr(number=3)]
    ci.set(ci_row(A, "success", repo="acme/gadgets"), repo="acme/gadgets")
    summary = svc.tick()
    assert summary["errors"] == 1
    assert summary["repos"] == 2
    assert summary["enqueued"] == 1
    assert [j["key"] for j in queue.snapshot()] == [f"{OTHER}#3"]


def test_one_pr_error_does_not_stop_other_prs():
    svc, app, store, queue, _, ci = make()
    app.prs[REPO] = [pr(number=1, sha="not-a-sha"), pr(number=2)]
    real = app.set_status

    def picky(repo, sha, state, description, *, context="herdr/review-gate"):
        if sha == "not-a-sha":
            raise ValueError("sha must be a full 40 (or 64) hex commit id")
        real(repo, sha, state, description, context=context)

    app.set_status = picky
    ci.set(ci_row(A, "success"))
    summary = svc.tick()
    assert summary["errors"] == 1 and summary["enqueued"] == 1
    # Status failed first, so nothing was recorded for PR 1.
    assert store.snapshot(PrKey(REPO, 1)).head_sha is None


def test_draft_pr_ignored():
    svc, app, store, queue, _, ci = make()
    app.prs[REPO] = [pr(draft=True)]
    ci.set(ci_row(A, "success"))
    summary = svc.tick()
    assert summary["skipped_drafts"] == 1 and summary["prs"] == 0
    assert app.calls == [("open_prs", REPO)]
    assert store.snapshot(KEY).head_sha is None
    assert queue.snapshot() == []


def test_unenrolled_repo_never_touched():
    svc, app, _, _, _, ci = make()
    app.prs[UNENROLLED] = [pr()]
    ci.set(ci_row(A, "success", repo="acme/secret"), repo="acme/secret")
    svc.tick()
    assert all(c[1] != UNENROLLED for c in app.calls)
    assert app.calls == [("open_prs", REPO)]


def test_disabled_config_does_nothing():
    svc, app, _, _, _, _ = make()
    svc._config = ReviewGateConfig(enabled=False, app=None, repos=svc._config.repos)
    app.prs[REPO] = [pr()]
    assert svc.tick()["repos"] == 0
    assert app.calls == []


def test_known_author_is_pinned_with_model_and_unknown_is_not():
    svc, app, _, queue, registry, ci = make()
    registry._repo.upsert_session(
        "s1", host_id="h1", branch="feat/x", pane_id="p1", model="author-m"
    )
    app.prs[REPO] = [pr(), pr(number=8, ref="nobody")]
    ci.set(ci_row(A, "success"))
    svc.tick()
    # An unknown author is not pinned (a first-wins "unknown" pin would disable
    # same-model reviewer exclusion for ever).
    assert registry._pinned(PrKey(REPO, 8)) is None
    pinned = registry._pinned(KEY)
    assert pinned == Author(
        kind="collab", model="author-m", host_id="h1", pane_id="p1", session_id="s1"
    )
    jobs = {j["key"]: j for j in queue.snapshot()}
    assert jobs[f"{REPO}#8"]["author_model"] is None
    assert jobs[str(KEY)]["author_model"] == "author-m"
    # The pin survives the session moving on.
    registry._repo.upsert_session("s1", host_id="h1", branch="other", pane_id="p1", model="x")
    assert registry.author_of(KEY, "feat/x").model == "author-m"


def test_tick_never_raises():
    svc, app, _, _, _, _ = make()

    def explode(repo):
        raise RuntimeError("boom")

    app.open_prs = explode
    summary = svc.tick()
    assert summary["errors"] == 1


def test_start_stop_runs_ticks_in_daemon_thread():
    svc, app, _, _, _, _ = make()
    ticked = threading.Event()
    real = svc.tick

    def counting():
        ticked.set()
        return real()

    svc.tick = counting
    svc.start()
    svc.start()  # second start is a no-op
    assert ticked.wait(5)
    thread = svc._thread
    assert thread is not None and thread.daemon
    svc.stop()
    assert not thread.is_alive()


def test_red_ci_status_is_not_reposted_every_tick():
    svc, app, _, _, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "failure", n=3))
    svc.tick()
    svc.tick()
    svc.tick()
    assert [s[3] for s in app.statuses(A)] == ["pending", "failure"]


def test_ci_red_at_fix_cap_escalates_with_failure_status():
    svc, app, store, queue, _, ci = make()
    store._ci_fix_cap = 1
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "failure"))
    svc.tick()
    assert store.snapshot(KEY).state == State.ESCALATED
    assert app.last_status(A) == "failure"
    assert "escalated" in app.statuses(A)[-1][4]
    assert queue.snapshot() == []


def finish_review_cap_escalation(store, sha):
    store._review_cap = 1
    assert store.on_review_started(KEY, sha, "reviewer-m")
    bad = Finding("a.py", "bug", "nope", "high", "fix it", 1)
    assert store.on_verdict(KEY, Verdict("FAIL", sha, (bad,), "reviewer-m"))
    assert store.snapshot(KEY).state == State.ESCALATED


def live_jobs(queue):
    return [(j["key"], j["sha"]) for j in queue.snapshot()]


def test_resume_without_push_after_review_cap_rearms_one_job():
    svc, app, store, queue, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    svc.tick()
    job = queue.claim("w", "reviewer-m")
    assert queue.complete(job.job_id, "w")
    finish_review_cap_escalation(store, A)
    svc.tick()
    assert store.resume(KEY)
    summary = svc.tick()
    assert summary["enqueued"] == 1
    assert live_jobs(queue) == [(str(KEY), A)]
    assert store.snapshot(KEY).state == State.CI_GREEN
    assert app.statuses(A)[-1][3:5] == ("pending", "review gate: CI green, review queued")
    svc.tick()
    assert live_jobs(queue) == [(str(KEY), A)]


def test_head_a_b_a_ends_with_one_live_job_for_a():
    svc, app, store, queue, _, ci = make()
    ci.set(ci_row(A, "success"), ci_row(B, "success", n=2))
    app.prs[REPO] = [pr(sha=A)]
    svc.tick()
    app.prs[REPO] = [pr(sha=B)]
    svc.tick()
    assert live_jobs(queue) == [(str(KEY), B)]
    app.prs[REPO] = [pr(sha=A)]
    svc.tick()
    assert live_jobs(queue) == [(str(KEY), A)]
    assert store.snapshot(KEY).state == State.CI_GREEN
    svc.tick()
    assert live_jobs(queue) == [(str(KEY), A)]


def test_ci_green_without_live_job_says_cannot_queue():
    svc, app, store, queue, _, ci = make()
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    svc.tick()
    # A worker completed the job but never recorded a verdict.
    job = queue.claim("w", "reviewer-m")
    assert queue.complete(job.job_id, "w")
    svc.tick()
    svc.tick()
    assert store.snapshot(KEY).state == State.CI_GREEN
    descs = [s[3:5] for s in app.statuses(A)]
    wedged = ("failure", "review gate: cannot queue review; push a commit")
    assert descs[-1] == wedged
    assert descs.count(wedged) == 1  # deduped


def test_refused_merge_backs_off_exponentially():
    svc, app, store, _, _, ci = make()
    clock = svc._now
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    svc.tick()
    approve(store, KEY, A)
    app.merge_result = False
    base_status = len(app.statuses(A))
    svc.tick()  # attempt 1: success, merge, pending(retry), update_branch
    assert len(app.named("merge_pr")) == 1
    assert [s[3] for s in app.statuses(A)[base_status:]] == ["success", "pending"]
    assert "retrying at" in app.statuses(A)[-1][4]
    assert len(app.named("update_branch")) == 1
    for _ in range(5):  # inside the 60 s window
        clock.t += 10
        svc.tick()
    assert len(app.named("merge_pr")) == 1
    assert len(app.statuses(A)) == base_status + 2
    clock.t += 11  # 61 s after the refusal
    svc.tick()
    assert len(app.named("merge_pr")) == 2
    assert len(app.statuses(A)) == base_status + 4
    assert len(app.named("update_branch")) == 1  # once per (key, sha)
    clock.t += 119  # next window is 120 s
    svc.tick()
    assert len(app.named("merge_pr")) == 2
    clock.t += 2
    svc.tick()
    assert len(app.named("merge_pr")) == 3
    for _ in range(10):  # the delay caps at 900 s
        clock.t += 901
        svc.tick()
    assert len(app.named("merge_pr")) == 13
    # A new head resets the backoff.
    app.merge_result = True
    app.prs[REPO] = [pr(sha=B)]
    ci.set(ci_row(B, "success", n=2))
    svc.tick()
    approve(store, KEY, B)
    svc.tick()
    assert app.named("merge_pr")[-1] == ("merge_pr", REPO, 7, B)
    assert store.snapshot(KEY).state == State.MERGED


def test_closed_pr_job_is_cancelled():
    svc, app, _, queue, _, ci = make()
    app.prs[REPO] = [pr(), pr(number=8)]
    ci.set(ci_row(A, "success"))
    svc.tick()
    assert len(queue.snapshot()) == 2
    app.prs[REPO] = [pr(number=8)]  # PR 7 closed
    svc.tick()
    assert live_jobs(queue) == [(f"{REPO}#8", A)]


def test_author_resolved_once_per_head():
    svc, app, _, _, registry, ci = make()
    calls = []
    real = registry.author_of

    def counting(key, head_ref):
        calls.append((key, head_ref))
        return real(key, head_ref)

    registry.author_of = counting
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    for _ in range(4):
        svc.tick()
    assert len(calls) == 1


def test_stop_times_out_on_a_stuck_tick(caplog):
    svc, _, _, _, _, _ = make()
    release = threading.Event()
    entered = threading.Event()

    def stuck():
        entered.set()
        release.wait(30)
        return {}

    svc.tick = stuck
    svc._stop_timeout = 0.2
    svc.start()
    assert entered.wait(5)
    with caplog.at_level("WARNING"):
        svc.stop()
    assert "still running" in caplog.text
    release.set()
    svc._thread.join(5)


def test_tick_counts_a_durable_failure_and_retries_next_tick():
    class FlakySink:
        fail = True

        def record_required(self, ev):
            if self.fail:
                raise RuntimeError("durable event insert was not acknowledged")

        def record(self, ev):  # best-effort path: must not be what the gate uses
            pass

    sink = FlakySink()
    cp = ControlPlaneRepo(sink=sink)
    app, ci = FakeApp(), CI()
    store = PipelineStore(cp)
    svc = GateService(
        config=ReviewGateConfig(
            enabled=True,
            app=AppConfig(1, 2, "/nonexistent/key.pem"),
            repos=(RepoEnrollment(repo=REPO),),
            poll_seconds=5,
        ),
        app=app,
        store=store,
        queue=ReviewQueue(cp),
        registry=AuthorRegistry(cp),
        ci_snapshot=ci,
        now=Clock(),
    )
    app.prs[REPO] = [pr()]
    assert svc.tick()["errors"] == 1
    assert store.snapshot(KEY).head_sha is None
    assert cp.stream("pr_gate", str(KEY)) == []
    sink.fail = False
    assert svc.tick()["errors"] == 0
    assert store.snapshot(KEY).head_sha == A
