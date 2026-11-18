# Retire the Old Context Policies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the two policies the controller replaces: the herdr-engineering 50% reminder hooks (including the `/handoff` text from PR #882) and the autospec tmux context monitor for Herdr-managed sessions.

**Architecture:** herdr-engineering deletes `handoff_context_hook.py`, its CLI entry and its `UserPromptSubmit` hook, and removes the `/handoff` lines from the managed `SessionStart` and `PreCompact` messages in `workflow_hooks.py`; a one-shot migration strips already-installed hooks. autospec (`/Users/wohlgemuth/IdeaProjects/autospec`, `packages/autospec_context_monitor`) gains its own setting that skips the monitor when the session is Herdr-managed. Both ship in the same release as plan 3.

**Tech Stack:** Python 3.13, pytest, real settings files under a temp HOME; autospec package tests (`packages/autospec_context_monitor/tests`).

**Spec:** `docs/superpowers/specs/2026-11-18-auto-handoff-design.md` (section "Retired policies").

## Global Constraints

- Ships in the same release as plan 3; do not merge before plan 3 is releasable, and release both together. Between releases a session must never be left with no policy.
- autospec's monitor (50% compact / 80% rollover) is disabled only for Herdr-managed sessions, through its own setting; tmux sessions outside Herdr keep it. The existing global kill switch `~/.autospec/no-auto-rollover.flag` (`__main__.py:67,761`) stays.
- Foreign hooks in `~/.claude/settings.json` untouched; writes atomic and backed up.
- Real deps, no mocks; nothing under `/tmp`, scratchpad. Never push herdr-engineering without approval; the autospec repo has its own AGENTS rules, read them first.

## Review Focus

- A user's `settings.json` still holding the old `UserPromptSubmit` hook (command text `herdr-eng handoff context-hook claude`) after upgrade: it must keep exiting 0 silently, not error, because the subcommand is gone; the migration then removes it.
- A user who set `HERDR_HANDOFF_AUTO=0` to silence the old reminder: still honoured as the new global off switch (plan 3).
- Hand-edited `PreCompact` entry mixed with foreign hooks: only the managed one (`herdr-workflow:managed`) changes.
- autospec started inside Herdr with `HERDR_PANE_ID` unset (remote shell): monitor runs as before; the skip is documented as keyed on that variable.
- Herdr session on a harness where the controller is switched off for that pane: the autospec monitor stays off too (no policy) and the docs say so.

---

### Task 1: Remove the herdr-engineering 50% reminder hook

**Files:**
- Delete: `herdr_engineering/handoff_context_hook.py`, `tests/test_handoff_context_hook.py`
- Modify: `herdr_engineering/handoff_auto_setup.py` (HOOKS lines 14-21: drop `UserPromptSubmit`, `HOOK_EVENT`, `HOOK_COMMAND`), `herdr_engineering/cli.py` (remove the `context-hook` parser ~509-531 and `_cmd_handoff_context_hook` ~1488; keep a hidden `context-hook` that exits 0 printing nothing, for already-installed hooks), `docs/operations/handoff-auto-triggers.md`
- Test: `tests/test_handoff_auto_setup.py`, `tests/test_handoff_context_hook_retired.py`

**Interfaces:** Consumes plan 2's `install_subagent_hooks`. Produces `HOOKS = {"SubagentStart": ..., "SubagentStop": ...}`; `herdr-eng handoff context-hook claude` exits 0 with empty stdout.

- [ ] **Step 1: Failing tests:** `test_hooks_map_has_only_subagent_events`, `test_legacy_context_hook_command_exits_zero_silently` (subprocess with a transcript over 50%), `test_install_removes_legacy_userpromptsubmit_hook` (settings with the old marked entry plus a foreign entry; after install only the foreign one and the subagent hooks remain), `test_module_gone` (`importlib.util.find_spec("herdr_engineering.handoff_context_hook") is None`).
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement; installer prunes any entry whose command contains `context-hook` and marker `herdr-handoff-auto`. **Step 4:** PASS.
- [ ] **Step 5: Commit** `refactor(handoff): remove the 50% context reminder hook`.

### Task 2: Strip the `/handoff` text from SessionStart and PreCompact

**Files:** Modify `herdr_engineering/workflow_hooks.py` (`REMINDER` line ~42 loses its `/handoff` bullet; `PRECOMPACT_MESSAGE` line ~44 is replaced by nothing: the managed `PreCompact` event is removed from `EVENTS` line 33 unless the guard needs it), `docs/operations/workflow-hooks.md` (rows at lines 13-14), `CHANGELOG.md`; Test `tests/test_workflow_hooks.py`.

**Interfaces:** Produces `EVENTS = ("SessionStart", "PreToolUse")`; `workflow install` removes a previously managed `PreCompact` hook (marker `herdr-workflow:managed`) and touches no other.

- [ ] **Step 1: Failing tests:** `test_reminder_does_not_mention_handoff`, `test_precompact_not_installed`, `test_install_removes_old_managed_precompact_only` (foreign `PreCompact` preserved), `test_second_install_noop`.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement; update docs table. **Step 4:** PASS.
- [ ] **Step 5: Commit** `refactor(workflow): drop the /handoff reminder text`.

### Task 3: autospec monitor skips Herdr-managed sessions

**Files (repo `/Users/wohlgemuth/IdeaProjects/autospec`, branch per its rules):** Modify `packages/autospec_context_monitor/autospec_context_monitor/__main__.py` (kill-switch check at ~line 761 gains a second check; `_KILL_SWITCH` at line 67), `packages/autospec_context_monitor/autospec_context_monitor/doctor.py` (report the setting); Test `packages/autospec_context_monitor/tests/test_herdr_skip.py`; docs in the package README or the monitor skill (`autospec-rollover-status` reports the skip).

**Interfaces:** Produces `is_herdr_managed(env: Mapping[str,str]) -> bool` (true when `HERDR_PANE_ID` non-empty) and env `AUTOSPEC_CONTEXT_MONITOR_SKIP_HERDR` (default `1`; `0` keeps the monitor running inside Herdr). On skip the monitor logs `{"event": "herdr_managed"}` and exits 0 before touching tmux.

- [ ] **Step 1: Failing tests:** `test_is_herdr_managed_true_with_pane_id`, `test_false_without_pane_id`, `test_skip_disabled_by_setting_zero`, `test_monitor_main_exits_zero_and_logs_herdr_managed` (run `python -m autospec_context_monitor` as a subprocess with `HERDR_PANE_ID=w1:p1`; assert exit 0 and the log line).
- [ ] **Step 2:** Run `pytest packages/autospec_context_monitor/tests/test_herdr_skip.py -v`; FAIL. **Step 3:** Implement. **Step 4:** PASS, then the whole package suite.
- [ ] **Step 5: Commit** `feat(context-monitor): skip Herdr-managed sessions`.

### Task 4: Release gating and end-to-end proof

**Files:** Create `tests/test_release_policy_pairing.py` (herdr-engineering); Modify `CHANGELOG.md`, `docs/operations/handoff.md`.

- [ ] **Step 1: Failing test** `test_controller_plugin_present_when_old_hooks_removed`: asserts `deploy/herdr-handoff-controller-plugin/herdr-plugin.toml` exists and `handoff_context_hook` does not, so the two cannot be split across commits.
- [ ] **Step 2:** Run; FAIL until plan 3 is on the branch; PASS after merging plan 3 into the release branch.
- [ ] **Step 3: Live check:** in a Herdr pane run a session past 50%: no reminder text appears; the controller acts at 80%. Record the evidence in the PR.
- [ ] **Step 4:** Release plan 3 and plan 5 together; reinstall (`herdr-eng` is a copy under `~/.local/share/herdr-engineering/releases/<name>/venv`) and run `herdr-eng handoff auto check`.
- [ ] **Step 5: Commit** `docs: retire the old context policies`.
