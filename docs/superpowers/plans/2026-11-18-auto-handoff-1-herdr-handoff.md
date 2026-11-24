# herdr-handoff: Down Placement, Chain Ids, Clean Exit, Shared Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `herdr-handoff` able to hand a session to a successor below the source, link the two records, exit the source cleanly before closing its pane, and keep records in one shared store.

**Architecture:** Changes stay inside `src/herdr_handoff.py` (envelope, store, launch plan) and `src/herdr_handoff_cli.py` (`takeover`, `confirm_successor`, `close_pane`). Envelope schema stays version 2; every new key is optional so existing records validate. Repo: `/Users/wohlgemuth/IdeaProjects/herdr-handoff`. Branch `feat/auto-handoff-ids` from an up-to-date `origin/master`.

**Tech Stack:** Python (single-module CLI), pytest (`integration` marker opt-in), Woodpecker (`.woodpecker/unit-gates.yml`, `scripts/ci/`), real git repos and real `herdr` for integration.

**Spec:** `docs/superpowers/specs/2026-11-18-auto-handoff-design.md` in herdr-engineering (sections "Repos and order", "Takeover below", "Shared store", "Clean exit", "Proof of life").

## Global Constraints

- Placement "down": `herdr pane split --direction down`, same tab, same harness.
- Record `predecessor_id` / `successor_id` plus source and child pane and session ids.
- Clean exit: harness-appropriate exit, wait for the process to end, then close the pane. On any failure the source keeps running and nothing is closed.
- Typed command limit `MAX_TYPED_COMMAND = 900`.
- Shared handoff store across repos; existing per-repo `.herdr/handoffs` records keep working.
- Docs and code must agree on proof of life.
- No mocks; real git repos and real processes. Nothing under `/tmp`, `/private/tmp`, scratchpad (use a fixed dir under the repo, e.g. `.herdr-test/`, gitignored). No CI tokens.
- Never commit to `master`; PR, review, CI green, squash merge.

## Review Focus

- Source harness already exited or crashed before the exit step: takeover must treat "pane has no process" as exited, not fail.
- Source harness ignores the exit command (hung, modal open): refuse with `source_exit_failed`, both panes stay.
- Two roots disagree (an old per-repo record and the shared store hold the same id): the shared store copy wins and nothing is duplicated.
- A takeover run twice for the same handoff id (retry after a crash between launch and close): idempotent, no second child pane.
- `predecessor_id` pointing at a record that is not in this store (imported from another repo): validates, shows as dangling, never raises.

---

### Task 1: Bring the checkout level with origin/master and reconcile the installed CLI

**Files:** Modify `src/herdr_handoff_cli.py` (only if the diff below is wanted); no new files.

**Interfaces:** Produces a clean base: `__version__ == "0.4.0"` with the dirty-source guard (`source_worktree_state`, `worktree_close_refusal`, `decide_takeover_close`).

Spec correction (to be reported): `origin/master` already contains `feat/takeover-dirty-source-guard` (PR #7, d57207b) and is 0.4.0; only the local `master` is stale at 0.3.0.

- [ ] **Step 1:** `git -C /Users/wohlgemuth/IdeaProjects/herdr-handoff fetch && git switch master && git merge --ff-only origin/master`; expect `src/herdr_handoff.py` `__version__ = "0.4.0"`.
- [ ] **Step 2:** Diff `src/herdr_handoff_cli.py` against `~/.local/share/uv/tools/herdr-handoff/lib/python3.14/site-packages/herdr_handoff_cli.py`; the known difference is one added line `dashboard = "not_configured"` near line 577 of the installed copy. Decide by reading it in context: if it fixes an unbound `dashboard` variable on the `create` path, add it with a test `test_create_without_report_dashboard_does_not_raise`; otherwise leave the installed copy to be overwritten.
- [ ] **Step 3:** `git switch -c feat/auto-handoff-ids`; run `pytest -q`; expect all pass.
- [ ] **Step 4: Commit** (only if Step 2 changed code) `fix: dashboard sync status default`.

### Task 2: Envelope chain ids and pane/session ids

**Files:** Modify `src/herdr_handoff.py` (`build_handoff`, `validate_envelope`, `source`/`target` blocks), `schemas/` (the envelope schema file); Test `tests/test_envelope_v2.py`, `tests/test_chain_ids.py`.

**Interfaces:** Produces envelope keys `predecessor_id`, `successor_id`, `source.pane_id`, `source.session_id`, `target.pane_id`, `target.session_id` (all `str | None`, optional); `build_handoff(..., predecessor_id: str | None = None, source_pane_id=None, source_session_id=None)`. CLI `create --predecessor-id`, `--source-pane`, `--source-session`.

- [ ] **Step 1: Failing tests:** `test_build_handoff_records_predecessor_and_pane_ids`, `test_old_record_without_new_keys_still_validates`, `test_predecessor_id_must_match_handoff_pattern` (`handoff_<32hex>`; else `ValueError`), `test_dangling_predecessor_validates`.
- [ ] **Step 2:** Run `pytest tests/test_chain_ids.py -v`; FAIL.
- [ ] **Step 3:** Implement; reject unknown keys still; add the keys to the JSON schema.
- [ ] **Step 4:** Run `pytest -q`; PASS.
- [ ] **Step 5: Commit** `feat: predecessor/successor and pane/session ids in the envelope`.

### Task 3: Fill session and pane ids from Herdr at takeover; link successor

**Files:** Modify `src/herdr_handoff_cli.py` (`takeover`, `_announce_pane`, `_agent_state`), `src/herdr_handoff.py` (`HandoffStore.record_takeover`, new `HandoffStore.link_successor(predecessor_id: str, successor_id: str) -> None`); Test `tests/test_takeover.py`.

**Interfaces:** Consumes Task 2 keys. Produces: after a confirmed takeover the source record has `successor_id` set and `target.pane_id`, `target.session_id` (from `herdr agent get` `agent_session`); the successor record (when created by `create --predecessor-id`) has `source.pane_id`.

- [ ] **Step 1: Failing test** `test_takeover_fills_pane_and_session_ids` against a real Herdr with a fake-free harness command (`sh -c 'sleep 30'` as the harness would not be "agent"; use the existing real-herdr pattern in `tests/test_takeover.py` where present, otherwise the `integration` marker).
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement. **Step 4:** Run; PASS.
- [ ] **Step 5: Commit** `feat: record pane/session ids and link successor on takeover`.

### Task 4: Placement "down"

**Files:** Modify `src/herdr_handoff.py` (`build_launch_plan` hardcodes `--direction right --focus`; `_TAKEOVER_KEYS`; `validate_takeover`), `src/herdr_handoff_cli.py` (`_place`, takeover parser); Test `tests/test_takeover.py`.

**Interfaces:** Produces `build_launch_plan(..., direction: str = "down")`; takeover block key `direction` in `{"down","right"}`; `placement` remains `"split"` (so `decide_takeover_close` is unchanged); CLI `takeover --direction {down,right}` default `down`. Default changes from right to down: documented in README and CHANGELOG as intentional.

- [ ] **Step 1: Failing tests** `test_launch_plan_splits_down_by_default`, `test_direction_right_still_available`, `test_invalid_direction_rejected`, `test_takeover_block_records_direction`.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement. **Step 4:** Run; PASS.
- [ ] **Step 5: Commit** `feat: takeover splits below the source by default`.

### Task 5: Clean source exit before the pane closes

**Files:** Modify `src/herdr_handoff_cli.py` (`takeover` between confirm and `close_pane`; new `exit_source(pane, text, keys, timeout, runner)`), `src/herdr_handoff.py` (`TAKEOVER_PHASES` add `source_exit_failed`); Test `tests/test_source_exit.py`.

**Interfaces:** Produces `exit_source(pane: str, *, text: str | None, keys: Sequence[str], timeout: float) -> bool` and the CLI flags listed in the index. Behaviour: send text (`herdr pane send-text`) then keys (`herdr pane send-keys`), poll `herdr pane process-info <pane>` until no harness process, return True; "no process already" is True. When no exit flags are given, behaviour is today's (direct close), so manual use is unchanged.

- [ ] **Step 1: Failing tests** with real `sh` panes: `test_exit_source_returns_true_when_process_ends` (pane running `sh -c 'read x'`, exit text `""` plus key `Enter`), `test_exit_source_false_on_timeout_and_source_stays` (pane running `sleep 300` ignores input), `test_takeover_does_not_close_when_exit_fails` (status `source_exit_failed`, exit 1, both panes listed), `test_already_exited_source_counts_as_exited`.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement. **Step 4:** Run; PASS.
- [ ] **Step 5: Commit** `feat: clean harness exit before closing the source pane`.

### Task 6: Allow recorded unpushed work (controller-approved close)

**Files:** Modify `src/herdr_handoff_cli.py` (`takeover`: when `--allow-dirty-source` closes over unpushed commits, write them into the record), `src/herdr_handoff.py` (`record_takeover` evidence); Test `tests/test_source_worktree_guard.py`.

**Interfaces:** Produces takeover evidence key `unpushed_commits: list[str]` (short SHAs from `git rev-list HEAD --not --remotes`) and `dirty_allowed: bool`. Needed by herdr-engineering, where pushes need explicit user approval so the WIP commit stays local.

- [ ] **Step 1: Failing tests** in a real git repo with one local-only commit: `test_allow_dirty_records_unpushed_shas`, `test_without_flag_still_refuses_source_dirty` (existing behaviour intact).
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement. **Step 4:** Run; PASS.
- [ ] **Step 5: Commit** `feat: record unpushed commits when the close is allowed over them`.

### Task 7: Shared store

**Files:** Modify `src/herdr_handoff.py` (new `shared_store_root`, `HandoffStore.import_legacy`), `src/herdr_handoff_cli.py` (`_default_root`, new `store import-legacy` command); Test `tests/test_shared_store.py`.

**Interfaces:** Produces `shared_store_root(env) -> Path` and `HandoffStore.import_legacy` per the index. Root precedence: `--root`, `HERDR_HANDOFF_FILE`, `HERDR_HANDOFF_DIR`, `HERDR_HANDOFF_STORE`, `~/.local/share/herdr-handoff/store`. A repo that already has `.herdr/handoffs/.outbox` is imported on first use, never deleted. `list` reads the shared root; legacy records still resolve by id.

- [ ] **Step 1: Failing tests** (store dir under a fixed non-/tmp path via `HERDR_HANDOFF_STORE`): `test_default_root_is_shared_not_cwd_relative`, `test_import_legacy_copies_missing_and_skips_present`, `test_legacy_record_resolves_by_id`, `test_import_is_idempotent`.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement with atomic writes (write temp then `os.replace`). **Step 4:** Run; PASS.
- [ ] **Step 5: Commit** `feat: shared handoff store with legacy import`.

### Task 8: Proof of life: make docs and code agree

**Files:** Modify `README.md` (lines ~35, 100-104), `skill/herdr-handoff/SKILL.md` (lines ~64-67, 91-94), `src/herdr_handoff_cli.py` (`confirm_successor`); Test `tests/test_takeover_documentation.py`.

The mismatch: README and SKILL.md say confirmation comes from the successor running `herdr-handoff confirm HANDOFF_ID`; the code (`confirm_successor`) decides from Herdr's own observation (agent in the child pane in a settled state plus the handoff id on its screen) and never waits for the successor's command. Decision: the docs follow the code; `confirm` stays as an optional, idempotent record of the successor's own acknowledgement. A real record (`screenwright/.herdr/handoffs/outbox/handoff_732590cf...json`) shows `phase: unconfirmed` with "agent=claude/working, handoff id not seen in its output" although the successor was running; so the id match is hardened too.

- [ ] **Step 1: Failing tests:** `test_docs_state_proof_is_herdr_observation` (README and SKILL.md contain no sentence saying the successor "confirms itself" as the close condition; assert the phrase "herdr observes" appears), `test_confirm_successor_accepts_id_seen_in_recent_scrollback` (successor prints the id, then the screen scrolls past it; `herdr pane read --source recent` still matches), `test_confirm_successor_rejects_id_only_in_the_typed_command_echo` (id present only in the launch line is not proof).
- [ ] **Step 2:** Run; FAIL.
- [ ] **Step 3:** Rewrite the doc passages; in `confirm_successor` also search `pane read --source recent --lines 200` after `wait-output` times out; define "seen" as the id on a line that is not the typed launch command.
- [ ] **Step 4:** Run `pytest -q`; PASS.
- [ ] **Step 5: Commit** `fix: docs and code agree that herdr observation is the proof of life`.

### Task 9: Release 0.5.0 and pin in herdr-engineering

**Files:** Modify `pyproject.toml`, `src/herdr_handoff.py` (`__version__ = "0.5.0"`), `CHANGELOG.md` (herdr-handoff); in herdr-engineering: `lock/upstreams.yaml` (revision), `CHANGELOG.md`; Test `tests/test_install_handoff_script.py` (existing, herdr-engineering) and a smoke `tests/test_cli_handoff.py::test_version_and_takeover_help_list_direction_and_exit_flags`.

**Interfaces:** Produces `herdr-handoff --version` = 0.5.0 and the flags in the index. herdr-engineering's `check_handoff_dependency` requires `takeover`, `confirm`, `create`, `launch` in `--help`; extend it to require `--direction`.

- [ ] **Step 1: Failing smoke test** in herdr-handoff (subprocess `herdr-handoff takeover --help` contains `--direction`, `--source-exit-text`).
- [ ] **Step 2:** Run; FAIL. **Step 3:** Bump version, CHANGELOG, open PR, wait for Woodpecker green (blocking wait, no token), squash merge.
- [ ] **Step 4:** In herdr-engineering branch `chore/pin-herdr-handoff-0.5.0`: update `lock/upstreams.yaml` to the merge SHA and the capability check; run `bash scripts/install-handoff.sh --plan` then install; expect the script to attest 0.5.0. Do not push without approval.
- [ ] **Step 5: Commit** `chore: pin herdr-handoff 0.5.0`.

### Task 10: End-to-end in real Herdr panes

**Files:** Create `tests/e2e/test_takeover_down.py` (marker `integration`, skipped without a Herdr socket).

- [ ] **Step 1: Write** the test: real workspace, a source pane running a harness-shaped long-lived process, `herdr-handoff create` then `takeover --direction down --source-exit-text /exit`; assert the child appears below the source (`herdr pane layout`), the source process ended, the source pane closed, the source record has `successor_id`, `target.pane_id`.
- [ ] **Step 2:** Run `pytest tests/e2e -m integration -v`; expect PASS on a machine with Herdr 0.9.1.
- [ ] **Step 3: Commit** `test: down takeover end to end`.
