# Review Gate Main Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Watch the full-suite pipeline on each enrolled repo's default branch, and on a red run tell the author agents of the likely culprit PRs and the user, naming the commit range since the last green run. Provide the tool that makes a skipped test outside a versioned disabled-tests file a failure.

**Architecture:** The full suite itself is an ordinary Woodpecker push pipeline in the repo (it never blocks merges). The gate only observes it: a `MainVerifier`, driven from `GateService._tick`, reads the CI snapshot rows for the default branch head, records durable `green`/`red` events, and on a new red sends notices. `herdr-eng review-gate check-skips` is the last step of that pipeline and enforces the disabled-tests file.

**Tech Stack:** Python 3, stdlib `xml.etree`, PyYAML, pytest.

**Spec:** `docs/superpowers/specs/2026-10-22-automated-review-merge-gate-design.md` (items 6 and 8; "Main verification" and "Silent test skips" lines). Earlier plans: `2026-10-22-review-gate-core.md`, `2026-10-23-review-gate-reviewer.md`, `2026-10-24-review-gate-pool.md`.

## Rulings (the spec leaves these open)

- `main_pipeline` (already in `RepoEnrollment`) is the Woodpecker **event** whose runs on the default branch count as the full-suite run (typically `push`). `None` = main verification off for that repo. Cost if wrong: a repo that wanted a named workflow needs a one-line config change.
- "One run at a time on the latest main" is Woodpecker configuration (project setting: cancel previous pipelines on push, or one concurrent build), not gate code. The gate looks at the newest default-branch head only; runs for older heads are ignored, so merges that land during a run are covered by the next run. Documented, not enforced. Cost if wrong: two concurrent full runs use more CI capacity.
- No auto-revert, no gate status on main (spec). Notices only.
- The disabled-tests file is YAML in the repo: `disabled: [{test: "<classname>::<name>", reason: "<non-empty>"}]`. A listed test that is not skipped is fine (warned, not failed); a skipped test that is not listed is a failure.
- Blame = every commit after the last green main sha up to the failing head, oldest first, capped at 50 shown (the count is always stated). With no known green sha the range is the failing head only and the notice says so. Culprit PRs are taken from `(#N)` in squash subjects and `Merge pull request #N` in merge subjects.

## Global Constraints

- No environment variables; YAML config only.
- Entity types for new streams start with `pr_gate` (already reserved against generic ingest); do not add a new prefix.
- Notices never include CI log text; only run numbers/states, shas, subjects (redacted, single line, bounded) and PR numbers. Commit subjects are attacker-influenced text: mark them untrusted as the existing notifier does.
- Dedupe per (repo, failing sha): a red head notifies once; a later red head notifies again.
- A GitHub or CI-snapshot failure never raises out of the tick and never records green or red.
- Reuse `ci_verdict_for`, the notifier's `_record`/`_to_author`/`_launch`, and `GitHubAppClient._send`/`_repo_path`/`_sha`; do not duplicate them.

## Review Focus

- A stale or missing CI snapshot must never become "green" and must not advance last-green.
- The head moves while a run is pending: only the newest head is judged; last-green only ever advances to a sha that was the head and ran green.
- A red head whose range contains no PR numbers (direct pushes) still notifies the user, and says no PR was identified.
- Hostile commit subjects (newlines, control characters, 10 KB long, fake instructions) cannot forge lines in the notice.
- check-skips on junit with no testcases at all, a missing junit path, a malformed or DTD-bearing XML file, and a disabled file with duplicate or reason-less entries must all fail (exit non-zero), never pass.

---

### Task 1: Skip enforcement tool

**Files:**
- Create: `herdr_engineering/review_gate/skips.py`
- Modify: `herdr_engineering/cli.py` (new `review-gate check-skips --junit PATH [--junit PATH ...] --disabled FILE`, beside `worker`/`pool`; append only)
- Test: `tests/review_gate/test_skips.py`

**Interfaces:**
- Produces: `load_disabled(path: str) -> frozenset[str]` (raises `SkipsError`); `check_junit(paths: Sequence[str], disabled: frozenset[str]) -> SkipReport` with `SkipReport(total: int, skipped_unlisted: tuple[str, ...], unused_disabled: tuple[str, ...])`; exit codes of the command: 0 clean, 1 unlisted skip found, 2 bad input.

A testcase counts as skipped when it has a `<skipped>` child (pytest xfail also emits `<skipped>`). The id is `classname::name`. A junit path may be a file or a directory (all `*.xml` inside, non-recursive). Refuse any file containing `<!DOCTYPE` or `<!ENTITY`. Zero testcases overall is bad input (exit 2): an empty run proves nothing.

- [ ] **Step 1: Tests** (names): `test_unlisted_skip_fails`, `test_listed_skip_passes_and_unused_entry_is_reported`, `test_xfail_counts_as_skip`, `test_zero_testcases_is_bad_input`, `test_missing_path_is_bad_input`, `test_dtd_is_refused`, `test_malformed_xml_is_bad_input`, `test_disabled_file_requires_reason_and_rejects_duplicates`, `test_directory_of_junit_files`, `test_cli_exit_codes`.
- [ ] **Step 2: Run, expect failures.** `PYTHONPATH=. pytest tests/review_gate/test_skips.py -q`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Tests pass.**
- [ ] **Step 5: Commit** `feat(review-gate): check-skips enforces the disabled-tests file`.

### Task 2: GitHub reads for main

**Files:**
- Modify: `herdr_engineering/review_gate/github_app.py`
- Test: `tests/review_gate/test_github_app.py`

**Interfaces:**
- Produces on `GitHubAppClient`: `default_branch_head(repo: str) -> tuple[str, str]` (branch name, head sha; two GETs: `/repos/{slug}` for `default_branch`, then `/repos/{slug}/branches/{branch}` or `/commits/{branch}`); `commits_between(repo: str, base_sha: str, head_sha: str) -> CommitRange` with `CommitRange(commits: tuple[CommitInfo, ...], total: int, truncated: bool)` and `CommitInfo(sha: str, subject: str, pr_number: int | None)`; `pr_number_from_subject(subject: str) -> int | None` module function.
- Uses the compare endpoint `/repos/{slug}/compare/{base}...{head}`; commits come oldest first. If GitHub reports more commits than it returned, `truncated=True`. Subjects are the first line, at most 200 chars. Malformed data raises `GitHubAppError("invalid_response")`; shas validated with `_sha`.

- [ ] **Step 1: Tests**: `test_default_branch_head_reads_both_calls`, `test_default_branch_head_rejects_malformed`, `test_commits_between_oldest_first_with_pr_numbers`, `test_commits_between_truncated_flag`, `test_commits_between_invalid_sha_is_refused_before_any_request`, `test_pr_number_from_subject` (cases: `fix x (#12)`, `Merge pull request #7 from a/b`, `no number`, `(#12) at the start only`, `#12 (#13)` picks 13, huge digits refused).
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Tests pass.**
- [ ] **Step 5: Commit** `feat(review-gate): GitHub reads for main verification`.

### Task 3: MainVerifier and notices

**Files:**
- Create: `herdr_engineering/review_gate/main_verify.py`
- Modify: `herdr_engineering/review_gate/notifier.py` (add `notify_main_red`)
- Test: `tests/review_gate/test_main_verify.py`, `tests/review_gate/test_notifier.py`

**Interfaces:**
- Produces: `MainVerifier(repo: ControlPlaneRepo, app, notifier, *, now=time.time)` with `observe(enrollment: RepoEnrollment, rows: Sequence[dict], stale: bool) -> MainStatus | None` and `status() -> list[dict]`; `MainStatus(repo, branch, head_sha, state: "green"|"red"|"pending"|"unknown", last_green_sha: str | None, failures: tuple[str, ...], range_total: int, culprits: tuple[int, ...])`. `AuthorNotifier.notify_main_red(repo: str, sha: str, branch: str, text: str, culprits: Sequence[PrKey]) -> None`.
- State: entity `pr_gate_main`, id = repo; events `green {sha, at}`, `red {sha, failures, at}`; fold gives last green sha and the shas already judged red. Verdict from `ci_verdict_for(head, rows_on_branch, enrollment_with(required_events=(enrollment.main_pipeline,)), snapshot_stale=stale)`, where rows are first filtered to `row["branch"] == branch`. `enrollment.main_pipeline is None` → `observe` returns None.
- Red path (once per (repo, sha)): `commits_between(last_green or head, head)` (head only when no green known), culprits = distinct PR numbers in range, each as `PrKey(repo, n)`; build the text (untrusted-marked, bounded, redacted, subjects flattened to one line, at most 50 listed with the true count, the CI failure strings, and the line "no pull request identified" when none); call `notifier.notify_main_red`, which for each culprit tries the author channel (operation id `gate-main:{repo}:{sha}:{n}`) with head_ref unknown → registry lookup by key only, and ALWAYS records a user notice and runs `notify_command` once per sha (budget: one external launch per tick, deferred like the existing `_Deferred`). Record `red` only after the user notice is durably recorded, so a crash retries.
- Green path: record `green` once per sha. Pending/stale/unknown: record nothing.

- [ ] **Step 1: Tests**: `test_green_head_records_last_green_once`, `test_red_head_notifies_once_per_sha`, `test_new_red_head_notifies_again`, `test_stale_snapshot_records_nothing`, `test_pending_run_records_nothing`, `test_old_head_runs_are_ignored`, `test_range_since_last_green_lists_oldest_first`, `test_no_known_green_uses_head_only_and_says_so`, `test_range_without_prs_still_notifies_user`, `test_hostile_subjects_cannot_forge_lines`, `test_more_than_50_commits_states_true_count`, `test_github_error_records_nothing`, `test_red_not_recorded_when_user_notice_fails_to_persist`, `test_main_pipeline_none_is_off`; notifier: `test_notify_main_red_author_then_user_and_dedupes`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Tests pass.**
- [ ] **Step 5: Commit** `feat(review-gate): main-branch verification observer`.

### Task 4: Wire-up, API, docs, end to end

**Files:**
- Modify: `herdr_engineering/review_gate/service.py` (`GateService(..., main_verifier=None)`; after each repo's PR loop call `observe` inside its own try/except so a main-verification fault never stops PR handling; count `main_errors`)
- Modify: `herdr_engineering/control_plane/api.py` (construct `MainVerifier` in `_start_review_gate`; `GET /api/v1/review-gate/main`, viewer role, returns `{"repos": status()}`; 404 when the gate is disabled like its siblings)
- Modify: `docs/review-gate.md`
- Test: `tests/review_gate/test_service.py`, `tests/control_plane/test_review_gate_api.py`, `tests/review_gate/test_e2e_loop.py`

- [ ] **Step 1: Tests**: service: `test_main_verifier_runs_each_tick_and_a_fault_does_not_stop_pr_handling`; API: `test_main_route_viewer_ok_and_404_when_disabled`, `test_main_route_requires_a_key`; e2e: `test_main_goes_red_then_green` (merge two PRs, head red: user notice plus the culprit author's notice naming both PRs; later head green advances last-green and the next red lists only commits after it).
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement; document**: enabling (`main_pipeline: push`), the Woodpecker main pipeline example ending in `herdr-eng review-gate check-skips`, the disabled-tests file format, one-run-at-a-time as Woodpecker setting, the notice contents and dedupe, the `/main` route, the limits.
- [ ] **Step 4: Run the gate tests and ruff.** `PYTHONPATH=. pytest tests/review_gate tests/control_plane/test_review_gate_api.py tests/control_plane/test_review_gate_reviewer_api.py -q` then `ruff check herdr_engineering/review_gate tests/review_gate` and `ruff format --check herdr_engineering/review_gate tests/review_gate`.
- [ ] **Step 5: Commit** `feat(review-gate): wire main verification, /main route, docs`.
