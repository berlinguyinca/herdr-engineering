# Review Gate Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundation of the automated PR review and merge gate: author registry, GitHub App client, per-PR state machine, CI-green trigger, review queue, and a gate service that sets a required GitHub status and merges.

**Architecture:** A new package `herdr_engineering/review_gate/` holds pure logic (models, fingerprint, merge decision, trigger rules) and event-sourced stores over the existing `ControlPlaneRepo.record_event` log (the same pattern as `control_plane/reviews.py`; no new tables, no new broker). A `GateService.tick()` polls enrolled repos through a GitHub App client, advances each PR's state keyed by head SHA, and sets the `herdr/review-gate` commit status. Reviewer workers, author/fixer notification, the pool, main-branch verification and the dashboard are later plans (see Roadmap) and plug into the queue and store defined here.

**Tech Stack:** Python 3.12, pytest, ruff, PyJWT[crypto] (App JWT, RS256), stdlib `urllib`, existing `ControlPlaneRepo` and `CIMonitor`.

**Spec:** `docs/superpowers/specs/2026-10-22-automated-review-merge-gate-design.md`

## Global Constraints

- Everything is keyed by (repo, PR number, head SHA). A verdict, check or approval is only accepted if its SHA equals the PR's current head.
- Repo identity is the canonical form `github.com/<owner>/<name>` (as in `github.py` `canonical_repository`).
- Gate status context is exactly `herdr/review-gate`. Status is set to `pending` immediately on every new head SHA.
- Round cap default 4 (review rounds); CI-fix cap default 3; both configurable per repo. Only completed review rounds count toward the review cap.
- Escalation is cleared only by an operator `resume` action, which resets both counters.
- The reviewer model must differ from the authoring model; an unknown or human author accepts any reviewer.
- Credentials: a dedicated GitHub App. The private key is read from a 0600 file named in the fleet config and is never logged or placed in an event payload. Run `redact_text` over anything stored.
- Woodpecker is truth for CI; missing or stale CI data is "not green".
- Unmanaged repos are untouched: only repos listed in `control_plane.review_gate.repos` are enrolled.
- Code style follows the repo: `ruff` clean, tests under `tests/review_gate/`, run with `python3 -m pytest`.

## Review Focus

- A push lands while a review is in flight: the in-flight job is cancelled and its late verdict is ignored (tests in Tasks 2 and 6).
- A verdict arrives for an old SHA after a force-push: no state change and no status change (Task 2).
- Rebase with an identical patch-id carries the approval over; a changed diff does not (Task 7).
- Woodpecker data is stale or the API is down: the PR stays pending, never green (Task 5).
- The author session is gone or ambiguous (two sessions on one branch): the registry returns the most recently active one, or `unknown` (Task 3).
- The same finding returns after a fix attempt, with a different line number: it still counts as a repeat (Task 1).
- A claimed review job's worker dies: the lease expires and the job returns to the queue exactly once (Task 6).

---

### Task 1: Models, fingerprint and merge decision (pure)

**Files:**
- Create: `herdr_engineering/review_gate/__init__.py`, `models.py`, `fingerprint.py`, `decision.py`
- Test: `tests/review_gate/test_fingerprint.py`, `tests/review_gate/test_decision.py`

**Interfaces:**
- Produces (`models.py`):
  - `class State(StrEnum)`: `CI_PENDING, CI_GREEN, REVIEW_QUEUED, REVIEWING, CHANGES_REQUESTED, APPROVED, MERGED, CI_RED, SUPERSEDED, ESCALATED` (lowercase string values).
  - `@dataclass(frozen=True) class PrKey: repo: str; number: int` with `__str__` -> `"<repo>#<number>"` and `PrKey.parse(text) -> PrKey`.
  - `@dataclass(frozen=True) class Finding: path: str; category: str; message: str; severity: str; required_change: str; line: int | None = None`.
  - `@dataclass(frozen=True) class Verdict: verdict: Literal["PASS","FAIL"]; sha: str; findings: tuple[Finding, ...]; reviewer_model: str`.
  - `@dataclass(frozen=True) class Author: kind: Literal["collab","pi","human","unknown"]; model: str | None = None; host_id: str | None = None; pane_id: str | None = None; session_id: str | None = None; mission_id: str | None = None`.
  - `@dataclass(frozen=True) class CheckRun: name: str; status: Literal["pass","fail","pending","skipped"]; sha: str`.
- Produces (`fingerprint.py`): `finding_fingerprint(f: Finding) -> str` (hex sha256 of normalised path, category, message; line excluded; message lower-cased with whitespace collapsed and digits runs replaced by `#`).
- Produces (`decision.py`): `@dataclass(frozen=True) class MergeDecision: decision: Literal["merge","wait","reject"]; reasons: tuple[str, ...]` and `decide_merge(*, head_sha: str, checks: Sequence[CheckRun], required_checks: Sequence[str], verdict: Verdict | None) -> MergeDecision`.

- [ ] **Step 1: Write failing tests.** `test_fingerprint.py`: `test_line_number_is_ignored` (same finding, lines 10 vs 99 -> equal), `test_digits_and_whitespace_normalised` ("Unused  var x1" vs "unused var x2" -> equal), `test_different_path_differs`. `test_decision.py`: `test_merge_when_all_required_pass_and_review_passes`; `test_reject_on_review_fail`; `test_reject_when_check_sha_differs_from_head`; `test_reject_when_review_sha_differs_from_head`; `test_wait_when_required_check_missing`; `test_wait_when_required_check_pending`; `test_wait_when_no_verdict`; `test_skipped_required_check_is_not_a_pass` (wait). Reasons are collected in fixed order, rejects before waits.
- [ ] **Step 2:** Run `python3 -m pytest tests/review_gate -q`; expect import errors/FAIL.
- [ ] **Step 3:** Implement the interfaces above. `decide_merge` ports the rule order from `pi-engineering/src/merge/mergeGate.ts` (reject reasons, then wait reasons, else merge) without the failure-baseline classification (deferred to the main-verification plan).
- [ ] **Step 4:** Run the tests; expect all PASS. Run `ruff check herdr_engineering/review_gate tests/review_gate`.
- [ ] **Step 5:** Create branch `feat/review-gate-core` from `main`, then `git add herdr_engineering/review_gate tests/review_gate && git commit -m "feat(review-gate): models, fingerprint, merge decision"`.

### Task 2: PipelineStore (event-sourced state machine)

**Files:**
- Create: `herdr_engineering/review_gate/store.py`
- Test: `tests/review_gate/test_store.py`

**Interfaces:**
- Consumes: Task 1 models; `ControlPlaneRepo.record_event(entity_type, entity_id, event_type, payload=None, ...)` and `ControlPlaneRepo.stream(entity_type, entity_id)`.
- Produces: `@dataclass(frozen=True) class PrSnapshot: key: PrKey; head_sha: str | None; state: State; review_rounds: int; ci_fix_rounds: int; last_findings: tuple[Finding, ...]; approved_sha: str | None; approved_patch_id: str | None; reason: str | None`.
- Produces: `class PipelineStore` with `__init__(self, repo: ControlPlaneRepo, *, review_cap: int = 4, ci_fix_cap: int = 3)` and methods, all returning `bool` (False = ignored because the SHA is not current or the transition is illegal):
  - `snapshot(key: PrKey) -> PrSnapshot` (state `CI_PENDING`, head None for an unknown PR)
  - `on_push(key, sha: str) -> bool` (new head: supersedes the prior head's non-terminal state, resets to `CI_PENDING`, keeps counters)
  - `on_ci_green(key, sha)`, `on_ci_red(key, sha, failures: Sequence[str])`
  - `on_review_started(key, sha, reviewer_model: str)`
  - `on_verdict(key, verdict: Verdict, *, patch_id: str | None = None)` (PASS -> `APPROVED`; FAIL -> `CHANGES_REQUESTED`, increments `review_rounds`; escalates when rounds >= cap or any finding fingerprint repeats from `last_findings`)
  - `on_merged(key, sha)`, `resume(key)` (clears `ESCALATED`, zeroes counters)
  - `carry_over_approval(key, new_sha: str, patch_id: str) -> bool` (re-approves the new head when `patch_id == approved_patch_id`)
  - Event type names equal the method names without `on_`; entity type `"pr_gate"`, entity id `str(key)`.

- [ ] **Step 1: Write failing tests** in `test_store.py` using a real `ControlPlaneRepo()`: `test_push_then_green_then_review_then_pass_reaches_approved`; `test_verdict_for_old_sha_is_ignored` (push A, push B, verdict on A returns False, state still `CI_PENDING`); `test_push_during_review_supersedes` (state back to `CI_PENDING`, `review_rounds` unchanged); `test_ci_red_does_not_consume_review_round` and `test_ci_red_three_times_escalates`; `test_fourth_failing_verdict_escalates` (cap 4: after the 4th FAIL state is `ESCALATED`); `test_repeated_fingerprint_escalates_with_moved_line`; `test_resume_clears_escalation_and_counters`; `test_carry_over_same_patch_id_reapproves_new_head` and `test_carry_over_different_patch_id_does_not`; `test_snapshot_rebuilds_from_events_after_restart` (new `PipelineStore` on the same repo returns the same snapshot).
- [ ] **Step 2:** Run `python3 -m pytest tests/review_gate/test_store.py -q`; expect FAIL.
- [ ] **Step 3:** Implement `PipelineStore.snapshot` as a left fold over `repo.stream("pr_gate", str(key))`; each `on_*` method validates against `snapshot` then records exactly one event. Serialise with a `threading.RLock` per store. Redact finding text with `control_plane.redact.redact_text` before recording.
- [ ] **Step 4:** Run tests; expect PASS.
- [ ] **Step 5:** `git commit -m "feat(review-gate): event-sourced PR state machine"`.

### Task 3: Author registry

**Files:**
- Create: `herdr_engineering/review_gate/registry.py`
- Test: `tests/review_gate/test_registry.py`

**Interfaces:**
- Consumes: `ControlPlaneRepo.list_sessions(status=None)` (session dicts carry `session_id`, `host_id`, `mission_id`, `model`, `branch`, `pane_id`, `last_seen`-style activity field; read `herdr_engineering/control_plane/repo.py:808` `upsert_session` for the exact keys); Task 1 `Author`.
- Produces: `class AuthorRegistry`: `__init__(self, repo)`; `author_of(self, key: PrKey, head_ref: str) -> Author` (finds sessions whose `branch == head_ref`; several -> most recently active; none -> `Author(kind="unknown")`); `pin(self, key: PrKey, author: Author) -> None` (records event `author_pinned` on stream `pr_gate_author:<key>` so the first resolution survives the session's later death); on later lookups a pinned author wins.
- The `kind` is `"collab"` when the session's mission or fleet marks it as a collab agent, `"pi"` when it is a pi mission session, else `"human"` only when the PR author login is not a known agent login (Task 7 passes that flag); otherwise `"unknown"`.

- [ ] **Step 1: Write failing tests:** `test_unknown_when_no_session_matches`; `test_matches_session_by_branch_and_records_model`; `test_two_sessions_same_branch_picks_most_recent`; `test_pinned_author_survives_session_removal`; `test_pin_is_idempotent`.
- [ ] **Step 2:** Run the file; expect FAIL.
- [ ] **Step 3:** Implement. Matching is by session `branch` only, with the PR's head ref (my call: sessions do not record their repo, so ambiguity across repos is resolved by also requiring the session's `cwd` basename to equal the repo name when `cwd` is present).
- [ ] **Step 4:** Run tests; expect PASS.
- [ ] **Step 5:** `git commit -m "feat(review-gate): author registry"`.

### Task 4: Enrollment config

**Files:**
- Create: `herdr_engineering/review_gate/config.py`
- Modify: `herdr_engineering/fleet_config.py` (accept and validate `control_plane.review_gate`; follow how `ci_actions` is handled near `CONTROL_PLANE_SECTION`)
- Test: `tests/review_gate/test_config.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class RepoEnrollment: repo: str; required_events: tuple[str, ...] = ("pull_request",); required_checks: tuple[str, ...] = (); main_pipeline: str | None = None; disabled_tests_file: str | None = None; review_cap: int = 4; ci_fix_cap: int = 3`.
- Produces: `@dataclass(frozen=True) class AppConfig: app_id: int; installation_id: int; private_key_file: str`.
- Produces: `@dataclass(frozen=True) class ReviewGateConfig: enabled: bool; app: AppConfig | None; repos: tuple[RepoEnrollment, ...]; poll_seconds: int = 15`; `load_review_gate_config(cfg: dict) -> ReviewGateConfig` reading `cfg["control_plane"]["review_gate"]`; disabled and empty when absent.
- Validation: repo must canonicalise to `github.com/...`; caps >= 1; `poll_seconds` >= 5; the key file must exist with mode 0600 or the loader raises `ValueError("private_key_file must be mode 0600")`; unknown keys raise.

- [ ] **Step 1: Write failing tests:** `test_absent_section_is_disabled`; `test_repo_defaults`; `test_bad_repo_rejected`; `test_loose_key_permissions_rejected` (tmp file 0644); `test_unknown_key_rejected`; `test_fleet_config_accepts_section` (run the fleet-config validator on a dict containing the section).
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement the loader and teach `fleet_config.py` the section.
- [ ] **Step 4:** Run `python3 -m pytest tests/review_gate/test_config.py tests/test_fleet_config.py -q`; expect PASS.
- [ ] **Step 5:** `git commit -m "feat(review-gate): per-repo enrollment config"`.

### Task 5: CI-green trigger rule (pure)

**Files:**
- Create: `herdr_engineering/review_gate/trigger.py`
- Test: `tests/review_gate/test_trigger.py`

**Interfaces:**
- Consumes: Woodpecker rows as dicts from `CIMonitor.snapshot()["pipelines"]` (keys per `ci.py` `PipelineView`: `revision`, `state`, `event`, `run_number`, `finished_at`); `RepoEnrollment`.
- Produces: `ci_verdict_for(sha: str, rows: Sequence[dict], enrollment: RepoEnrollment, *, snapshot_stale: bool) -> CiVerdict` where `CiVerdict` is a frozen dataclass `(result: Literal["green","red","pending"], failures: tuple[str, ...])`. Rules: only rows with `revision == sha` and `event in enrollment.required_events` count; none -> pending; any non-terminal -> pending; any of `failure/error/killed` -> red (failures are `"run <n> <state>"`); all `success` -> green; `snapshot_stale` -> pending always; `declined/skipped/canceled` -> pending (my call: needs a human retry, never green).

- [ ] **Step 1: Write failing tests:** `test_no_matching_rows_is_pending`; `test_running_is_pending`; `test_all_success_is_green`; `test_one_failure_is_red_with_failures_listed`; `test_other_sha_rows_ignored`; `test_non_required_event_ignored`; `test_stale_snapshot_is_never_green`; `test_cancelled_is_pending`.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement `ci_verdict_for`.
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** `git commit -m "feat(review-gate): CI-green trigger rule"`.

### Task 6: Review queue

**Files:**
- Create: `herdr_engineering/review_gate/queue.py`
- Test: `tests/review_gate/test_queue.py`

**Interfaces:**
- Consumes: `ControlPlaneRepo` events; Task 1 models.
- Produces: `@dataclass(frozen=True) class ReviewJob: job_id: str; key: PrKey; sha: str; author_model: str | None; enqueued_at: str`.
- Produces: `class ReviewQueue`: `__init__(self, repo, *, lease_seconds: int = 900, now=None)`; `enqueue(key, sha, author_model) -> ReviewJob | None` (None when an identical (key, sha) job is already queued, claimed or done; a job for an older SHA of the same key is cancelled first); `claim(worker_id: str, reviewer_model: str) -> ReviewJob | None` (oldest first; skips jobs whose `author_model == reviewer_model` when `author_model` is set; re-offers a job whose lease has expired); `complete(job_id: str, worker_id: str) -> bool` (False if the lease was lost); `cancel(key: PrKey, *, except_sha: str | None = None) -> int`; `depth() -> int` (queued, unclaimed); `snapshot() -> list[dict]`. Entity type `"review_job"`.

- [ ] **Step 1: Write failing tests:** `test_enqueue_is_idempotent_per_sha`; `test_new_sha_cancels_old_job`; `test_claim_is_fifo`; `test_claim_skips_job_authored_by_same_model`; `test_expired_lease_requeues_once` (fake clock; two claims after expiry yield one job, the dead worker's `complete` returns False); `test_complete_by_wrong_worker_rejected`; `test_depth_counts_only_unclaimed`; `test_concurrent_claims_never_double_assign` (8 threads, 3 jobs -> exactly 3 distinct assignments).
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement with a lock plus a fold over the `review_job` streams.
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** `git commit -m "feat(review-gate): durable review queue with leases"`.

### Task 7: GitHub App client

**Files:**
- Create: `herdr_engineering/review_gate/github_app.py`
- Modify: `pyproject.toml` (add `PyJWT[crypto]>=2.8` to a new extra `review-gate`; add the same line to the control-plane image requirements in `deploy/control-plane/` where `collab` deps are installed)
- Test: `tests/review_gate/test_github_app.py`

**Interfaces:**
- Consumes: `AppConfig`; `urllib` opener refusing redirects (reuse `_NoRedirect` from `herdr_engineering/github.py`).
- Produces: `class GitHubAppError(RuntimeError)` with `.reason` in `{"authentication","permission","not_found","rate_limited","conflict","unavailable","invalid_response"}`.
- Produces: `@dataclass(frozen=True) class PrInfo: number: int; head_sha: str; head_ref: str; base_ref: str; author_login: str; draft: bool`.
- Produces: `class GitHubAppClient`: `__init__(self, config: AppConfig, *, client=None, now=time.time)`; `installation_token() -> str` (App JWT RS256, `iss=app_id`, 9-minute life; installation token cached and refreshed 60 s before expiry); `open_prs(repo: str) -> list[PrInfo]`; `set_status(repo, sha, state: Literal["pending","success","failure","error"], description: str, *, context="herdr/review-gate", target_url=None) -> None`; `merge_pr(repo, number, sha, *, method="squash") -> bool` (sends `sha` so GitHub refuses a moved head; returns False on 409/405 instead of raising); `update_branch(repo, number, expected_head_sha) -> None`; `patch_id(repo, base_sha: str, head_sha: str) -> str` (sha256 of the compare endpoint's per-file patches, ignoring hunk line offsets).
- Every error message passes through `redact_text` and never contains the token or key.

- [ ] **Step 1: Write failing tests** with a fake opener that records requests: `test_jwt_is_rs256_with_app_id_issuer` (decode with the public key from a generated test keypair); `test_installation_token_cached_until_near_expiry` (one token request across two calls, a second after the clock jumps); `test_set_status_posts_context_and_state`; `test_merge_sends_sha_and_returns_false_on_409`; `test_open_prs_maps_fields`; `test_patch_id_ignores_hunk_offsets` (same change at different line numbers -> equal; different change -> differs); `test_errors_never_contain_token`; `test_401_maps_to_authentication`; `test_redirect_is_refused`.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement against GitHub REST (`/app/installations/{id}/access_tokens`, `/repos/{o}/{r}/pulls?state=open`, `/statuses/{sha}`, `/pulls/{n}/merge`, `/pulls/{n}/update-branch`, `/compare/{base}...{head}`).
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** `git commit -m "feat(review-gate): GitHub App client"`.

### Task 8: GateService (tick loop)

**Files:**
- Create: `herdr_engineering/review_gate/service.py`
- Test: `tests/review_gate/test_service.py`

**Interfaces:**
- Consumes: Tasks 1-7; `CIMonitor.snapshot(start=False)` returning `{"pipelines": {...}, "stale": bool, ...}` (see `control_plane/ci_monitor.py:68`).
- Produces: `class GateService`: `__init__(self, *, config: ReviewGateConfig, app: GitHubAppClient, store: PipelineStore, queue: ReviewQueue, registry: AuthorRegistry, ci_snapshot: Callable[[], dict])`; `tick() -> dict` (summary counts, never raises; one repo's failure is recorded and does not stop the others); `start()` / `stop()` run `tick` every `config.poll_seconds` in a daemon thread.
- Per-PR logic in `tick`: (1) new head SHA -> `store.on_push`, `queue.cancel(key, except_sha=sha)`, if the new diff has the same `patch_id` as `approved_patch_id` call `store.carry_over_approval` else `set_status(pending)`; if the branch is behind base and checks are strict, `update_branch`. (2) state `CI_PENDING` -> `ci_verdict_for`; green -> `store.on_ci_green` + `queue.enqueue(key, sha, author.model)` and state `REVIEW_QUEUED`; red -> `store.on_ci_red` + status `failure`. (3) state `APPROVED` -> `decide_merge`; `merge` -> status `success`, `app.merge_pr`, `store.on_merged`; `reject` -> status `failure`. (4) `CHANGES_REQUESTED` and `ESCALATED` hold status `failure` (description says why). The author is resolved once through `registry.author_of` and pinned.
- Draft PRs are skipped.

- [ ] **Step 1: Write failing tests** with a fake `GitHubAppClient` and a hand-built CI snapshot: `test_new_head_sets_pending_status_first`; `test_green_ci_enqueues_exactly_one_review`; `test_red_ci_enqueues_nothing_and_sets_failure`; `test_stale_ci_snapshot_enqueues_nothing`; `test_approved_green_pr_is_merged_with_head_sha`; `test_approved_but_head_moved_is_not_merged`; `test_push_during_review_cancels_job_and_resets_status`; `test_rebase_with_same_patch_id_keeps_approval_and_merges`; `test_escalated_pr_stays_failure_until_resume`; `test_one_repo_error_does_not_stop_other_repos`; `test_draft_pr_ignored`; `test_unenrolled_repo_never_touched`.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement as specified.
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** `git commit -m "feat(review-gate): gate service tick loop"`.

### Task 9: Wire into the control plane and expose read/resume API

**Files:**
- Modify: `herdr_engineering/control_plane/api.py` (construct `GateService` next to `CIMonitor` at ~line 145 when `load_review_gate_config(...).enabled`; add routes near the `/api/v1/ci/` and `/api/v1/reviews` auth handling at ~line 669)
- Test: `tests/control_plane/test_review_gate_api.py`

**Interfaces:**
- Produces routes: `GET /api/v1/review-gate/prs` (viewer; list of `PrSnapshot` as dicts), `GET /api/v1/review-gate/queue` (viewer; `ReviewQueue.snapshot()` plus `depth`), `POST /api/v1/review-gate/prs/<repo>/<number>/resume` (operator only; calls `PipelineStore.resume`). With the gate disabled, all three return 404 and no thread starts.
- Server shutdown stops the gate thread.

- [ ] **Step 1: Write failing tests** in the style of `tests/control_plane/test_ci_api.py`: `test_disabled_returns_404`; `test_viewer_can_list_prs_and_queue`; `test_viewer_cannot_resume` (403); `test_operator_resume_clears_escalation`; `test_shutdown_stops_gate_thread`.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement the wiring and routes.
- [ ] **Step 4:** Run the full suite: `python3 -m pytest -q` and `ruff check .`; expect PASS with no new failures.
- [ ] **Step 5:** `git commit -m "feat(review-gate): wire gate service and API into the control plane"`.

---

## Roadmap (separate plans, written after this one lands)

2. **Reviewer and notification.** Reviewer worker (started like `collab-spawn.py` agents, read-only sandboxed checkout, structured `review_result`), claim/result API endpoints, delivery of findings or CI failures to the author (`repo.send_message` for sessions; fresh fixer agent when the author is gone or unknown), escalation notice to the user.
3. **Reviewer pool.** Min/max workers scaled by `ReviewQueue.depth()`, retirement of idle workers, round-robin hosts.
4. **Main-branch verification.** One full run at a time on latest main, explicit disabled-tests file enforcement, notify author and user with the commit range since the last green main run.
5. **Dashboard page.** Queue, reviewers, per-PR state and round counts, resume button.

## Self-review notes

- Spec coverage for this plan: trigger (Task 5, 8), queue with supersede and leases (6), gate status and merge via App (7, 8), registry and unknown/human authors (3), strict-branch update and patch-id carry-over (7, 8), fingerprint and caps and escalation and resume (1, 2, 9), enrollment and expected CI (4, 5), GitHub App credentials (4, 7). Items for later plans are listed in the Roadmap.
- Calls made in this plan that the user did not confirm: polling every 15 s instead of webhooks; trigger keyed on Woodpecker `event` types because pipeline rows carry no workflow list; cancelled or skipped CI counts as pending, never green; registry matches sessions by branch plus cwd basename.
