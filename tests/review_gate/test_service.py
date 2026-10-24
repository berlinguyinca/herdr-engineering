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


def pr(number=7, sha=A, *, draft=False, ref="feat/x", base="main"):
    return PrInfo(
        number=number, head_sha=sha, head_ref=ref, base_ref=base, author_login="bot", draft=draft
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


def approve(store, key, sha, base="main"):
    assert store.on_review_started(key, sha, "reviewer-m")
    assert store.on_verdict(key, Verdict("PASS", sha, (), "reviewer-m"), base_ref=base)


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


class MaterialApp(FakeApp):
    """FakeApp that also serves review material (``None`` while ``closed``)."""

    def __init__(self):
        super().__init__()
        self.closed = False

    def review_material(self, repo, number, expected_head_sha, *, max_bytes):
        from herdr_engineering.review_gate.github_app import ReviewMaterial

        if self.closed:
            return None
        return ReviewMaterial("t", "b", "main", expected_head_sha, (), False)


def _coordinator(store, queue, app):
    from herdr_engineering.review_gate.config import ReviewerConfig
    from herdr_engineering.review_gate.coordinator import ReviewCoordinator

    return ReviewCoordinator(
        store=store, queue=queue, app=app, config=ReviewerConfig(), enrolled=(REPO,)
    )


def _live_jobs(queue):
    return [j for j in queue.snapshot() if j["key"] == str(KEY) and j["sha"] == A]


def test_pr_closed_during_review_and_reopened_is_requeued():
    svc, app, store, queue, _, ci = make()
    app.__class__ = MaterialApp
    app.closed = False
    coord = _coordinator(store, queue, app)
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    svc.tick()
    svc.tick()
    app.closed = True  # PR closed while the reviewer fetches
    assert coord.claim("w1", "reviewer-m") is None
    assert store.snapshot(KEY).state == State.REVIEWING
    app.prs[REPO] = []
    svc.tick()  # PR left the open list: job cancelled
    assert _live_jobs(queue) == []
    app.prs[REPO] = [pr()]
    app.closed = False
    svc.tick()  # reopened at the same sha
    svc.tick()
    assert len(_live_jobs(queue)) == 1
    claim = coord.claim("w2", "reviewer-m")
    assert claim is not None and claim.sha == A
    out = '{"verdict": "PASS", "findings": []}'
    assert coord.submit(claim.job_id, "w2", out, claim_token=claim.claim_token) == "recorded"
    assert store.snapshot(KEY).state == State.APPROVED


def test_reviewing_pr_with_live_job_is_not_rearmed():
    svc, app, store, queue, _, ci = make()
    app.__class__ = MaterialApp
    app.closed = False
    coord = _coordinator(store, queue, app)
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    svc.tick()
    svc.tick()
    claim = coord.claim("w1", "reviewer-m")
    assert store.snapshot(KEY).state == State.REVIEWING
    before = _live_jobs(queue)
    summary = svc.tick()
    assert summary["enqueued"] == 0
    after = _live_jobs(queue)
    assert [j["job_id"] for j in after] == [j["job_id"] for j in before] == [claim.job_id]
    assert after[0]["state"] == "claimed"


class RecordingNotifier:
    def __init__(self, raises=False):
        self.calls = []
        self.raises = raises

    def notify(self, key, snap, head_ref, failures=()):
        self.calls.append((key, snap.state, snap.head_sha, head_ref, tuple(failures)))
        if self.raises:
            raise RuntimeError("notice write failed")


def make_notified(notifier):
    svc, app, store, queue, registry, ci = make()
    svc._notifier = notifier
    return svc, app, store, queue, registry, ci


def test_service_accepts_notifier_argument():
    cp = ControlPlaneRepo()
    notifier = RecordingNotifier()
    svc = GateService(
        config=ReviewGateConfig(enabled=True, app=None, repos=(RepoEnrollment(repo=REPO),)),
        app=FakeApp(),
        store=PipelineStore(cp),
        queue=ReviewQueue(cp),
        registry=AuthorRegistry(cp),
        ci_snapshot=CI(),
        notifier=notifier,
    )
    assert svc._notifier is notifier


def test_service_notifies_ci_red_with_failures_then_on_later_ticks():
    notifier = RecordingNotifier()
    svc, app, store, _, _, ci = make_notified(notifier)
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "failure", n=3))
    svc.tick()
    assert len(notifier.calls) == 1
    key, state, sha, ref, failures = notifier.calls[0]
    assert (key, state, sha, ref) == (KEY, State.CI_RED, A, "feat/x")
    assert failures == ("run 3 failure",)
    assert app.statuses(A)[-1][4] == "CI failed: run 3 failure"
    svc.tick()
    assert len(notifier.calls) == 2 and notifier.calls[1][4] == ()
    assert [s[3] for s in app.statuses(A)] == ["pending", "failure"]


def test_service_notifies_changes_requested_and_escalated_only():
    notifier = RecordingNotifier()
    svc, app, store, _, _, ci = make_notified(notifier)
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "success"))
    svc.tick()
    assert notifier.calls == []  # CI_GREEN: no notification
    assert store.on_review_started(KEY, A, "reviewer-m")
    svc.tick()
    assert notifier.calls == []  # REVIEWING
    bad = Finding("a.py", "bug", "nope", "high", "fix it", 1)
    assert store.on_verdict(KEY, Verdict("FAIL", A, (bad,), "reviewer-m"))
    svc.tick()
    assert [c[1] for c in notifier.calls] == [State.CHANGES_REQUESTED]
    assert "changes requested" in app.statuses(A)[-1][4]


def test_service_notifies_ci_fix_cap_escalation_with_failures():
    notifier = RecordingNotifier()
    svc, app, store, _, _, ci = make_notified(notifier)
    store._ci_fix_cap = 1
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "failure"))
    svc.tick()
    assert [c[1] for c in notifier.calls] == [State.ESCALATED]
    assert notifier.calls[0][4] == ("run 1 failure",)
    assert "escalated" in app.statuses(A)[-1][4]


def test_service_calls_notifier_and_survives_its_exception(caplog):
    notifier = RecordingNotifier(raises=True)
    svc, app, store, _, _, ci = make_notified(notifier)
    app.prs[REPO] = [pr()]
    ci.set(ci_row(A, "failure", n=3))
    summary = svc.tick()
    assert summary["errors"] == 0
    assert len(notifier.calls) == 1
    assert store.snapshot(KEY).state == State.CI_RED
    assert app.last_status(A) == "failure"
    assert f"notification failed for {REPO}#7" in caplog.text
    assert "notice write failed" not in caplog.text
    svc.tick()  # retried on the next tick
    assert len(notifier.calls) == 2


class CountingRun:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        import subprocess

        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def test_one_external_launch_per_tick_and_backlog_drains():
    from herdr_engineering.review_gate.config import NotifyConfig
    from herdr_engineering.review_gate.notifier import ENTITY, AuthorNotifier

    svc, app, store, _, registry, ci = make()
    run = CountingRun()
    svc._notifier = AuthorNotifier(
        store._repo,
        config=NotifyConfig(fixer_command=("/usr/local/bin/fixer", "{number}")),
        registry=registry,
        dispatcher=lambda *a: {"status": "delivered"},
        run=run,
    )
    c = "c" * 40
    app.prs[REPO] = [pr(7, A), pr(8, B), pr(9, c)]
    ci.set(ci_row(A, "failure", n=3), ci_row(B, "failure", n=4), ci_row(c, "failure", n=5))

    def channels(number):
        stream = store._repo.stream(ENTITY, f"{REPO}#{number}")
        return [e["payload"]["channel"] for e in stream]

    svc.tick()
    assert run.calls == [["/usr/local/bin/fixer", "7"]]
    assert channels(7) == ["author", "fixer"]
    assert channels(8) == ["author"] and channels(9) == ["author"]  # deferred: no notice
    svc.tick()
    assert [c[1] for c in run.calls] == ["7", "8"]
    assert channels(9) == ["author"]
    svc.tick()
    svc.tick()
    svc.tick()
    assert [c[1] for c in run.calls] == ["7", "8", "9"]
    for n in (7, 8, 9):
        assert channels(n) == ["author", "fixer"]
    for sha in (A, B, c):
        assert app.last_status(sha) == "failure"


# ------------------------------------------------------------ base retarget
def _approved_on(base, pr_base):
    svc, app, store, queue, _, ci = make()
    app.prs[REPO] = [pr(base=pr_base)]
    ci.set(ci_row(A, "success"))
    svc.tick()
    svc.tick()
    approve(store, KEY, A, base=base)
    queue.cancel(KEY)  # the coordinator completes the job when it records the verdict
    return svc, app, store, queue


def test_retargeted_pr_is_not_merged_and_is_reviewed_again():
    svc, app, store, queue = _approved_on("feature-x", "main")
    before = len([j for j in queue.snapshot() if j["sha"] == A])
    svc.tick()
    assert app.named("merge_pr") == []
    assert "success" not in [c[3] for c in app.statuses(A)]
    assert app.statuses(A)[-1][3:5] == ("pending", "review gate: base branch changed, re-reviewing")
    snap = store.snapshot(KEY)
    assert snap.state == State.CI_GREEN and snap.approved_sha is None
    assert snap.reason == "base branch changed"
    assert [j["state"] for j in queue.snapshot() if j["sha"] == A] == ["queued"]
    assert before == 0
    svc.tick()  # stays queued; no second rearm, no merge
    assert len([j for j in queue.snapshot() if j["sha"] == A]) == 1
    assert app.named("merge_pr") == []


def test_approval_for_the_same_base_merges():
    svc, app, store, _ = _approved_on("main", "main")
    svc.tick()
    assert app.named("merge_pr") == [("merge_pr", REPO, 7, A)]


def test_approval_without_a_recorded_base_fails_closed():
    svc, app, store, queue = _approved_on(None, "main")  # an old or forged approval
    assert store.snapshot(KEY).approved_base_ref is None
    svc.tick()
    assert app.named("merge_pr") == []
    assert store.snapshot(KEY).state == State.CI_GREEN
    assert [j["state"] for j in queue.snapshot() if j["sha"] == A] == ["queued"]
