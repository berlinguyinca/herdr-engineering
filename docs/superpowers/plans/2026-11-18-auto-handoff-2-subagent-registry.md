# Subagent Registry Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing subagent registry the controller's source for "are background agents still running", with its Claude hooks installed by the installer rather than opt-in.

**Architecture:** `herdr_engineering/handoff_subagent_registry.py` (106 lines: `sanitize`, `record`, `run_subagent_hook`, `live_entries`, `run_subagents`; state `$HERDR_ENGINEERING_STATE/subagents/<pane_id>/<agent_id>`, stale after 6h, keyed by `HERDR_PANE_ID`) gains a typed status, a bounded wait and a purge. The hook installer `handoff_auto_setup.py` already exists (HOOKS map, lines 14-21) but is opt-in via `herdr-eng handoff auto install`; it is trimmed to the two Subagent hooks and run by `scripts/install.sh`.

**Tech Stack:** Python 3.13 standard library, pytest, real files and real subprocess hook calls.

**Spec:** `docs/superpowers/specs/2026-11-18-auto-handoff-design.md` (section "Subagent registry").

## Global Constraints

- When the registry has no data for a pane, the drain order asks the session to list and wait for its background agents (`drain_order(subagents_known=False, ...)`, plan 0).
- Registry stale after 6 hours (existing `live_entries` behaviour; unchanged).
- Subagent hooks must never fail a session: they always exit 0 and are guarded by `[ -x ... ] && ... || true`.
- Managed hooks carry marker `herdr-handoff-auto`; foreign hooks in `~/.claude/settings.json` are untouched, writes atomic and backed up (existing `fabric_join_setup` helpers).
- Only Claude Code emits SubagentStart/SubagentStop. Codex, Pi and OpenCode have no registry data, so they always take the "unknown" branch (spec gap, listed in the final report).
- Real deps, no mocks; nothing under `/tmp`, `/private/tmp`, scratchpad. No CI tokens.

## Review Focus

- A subagent that crashed without SubagentStop: its entry goes stale at 6h; the wait must not block the drain timeout (30 min) on it, so the controller times out and proceeds.
- A pane id reused after a close: leftover entries from the closed pane must not make a new pane wait (purge on close).
- Two hook calls racing on the same agent id: no exception, one file.
- `HERDR_PANE_ID` unset (hook run outside Herdr): `record` is a no-op and `status` reports `known=False`.
- `settings.json` invalid JSON: installer reports it and does not rewrite (existing contract), and the controller then treats the pane as unknown.

---

### Task 1: Typed status, wait and purge

**Files:**
- Modify: `herdr_engineering/handoff_subagent_registry.py`
- Test: `tests/test_handoff_subagent_registry.py` (add cases beside the existing `test_start_creates_file_and_stop_removes_it`)

**Interfaces:**
- Consumes: existing `live_entries(env, pane, now) -> list[dict]`.
- Produces: `SubagentStatus`, `status`, `wait_until_idle`, `purge_pane` (signatures in the index). `known` is True when the pane's directory exists (any hook ever fired for it), even with zero live agents; a pane never seen is `known=False`.

- [ ] **Step 1: Write failing tests** with a real state dir under `.herdr-test/subagents/` (fixture creates and removes it): `test_status_unknown_for_unseen_pane`, `test_status_known_zero_after_stop`, `test_status_counts_live_agents`, `test_stale_entry_not_counted` (entry `started` 7h ago), `test_wait_until_idle_true_when_last_agent_stops` (a real thread calls the stop hook after 0.3s; `poll_s=0.05`), `test_wait_until_idle_false_on_timeout`, `test_purge_pane_removes_entries_and_returns_count`.
- [ ] **Step 2:** `pytest tests/test_handoff_subagent_registry.py -v`; expect the new tests FAIL (name errors).
- [ ] **Step 3: Implement** the three functions and dataclass; `wait_until_idle` uses a monotonic clock and `time.sleep(poll_s)`.
- [ ] **Step 4:** Run; PASS.
- [ ] **Step 5: Commit** `feat(handoff): typed subagent status, wait and purge`.

### Task 2: Registry CLI for the controller

**Files:**
- Modify: `herdr_engineering/cli.py` (handoff subparser ~509-531; `_cmd_handoff_subagents` ~1525)
- Test: `tests/test_handoff_subagents_cli.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `herdr-eng handoff subagents --pane ID --status --json` printing `{"known": bool, "live": int}`; `--wait SECONDS` exits 0 when idle, 3 on timeout; `--purge`.

- [ ] **Step 1: Failing test** `test_cli_status_json_and_wait_exit_codes` (subprocess with `HERDR_ENGINEERING_STATE` set to the fixed dir; seeds an entry via `handoff subagent-hook start` with the hook payload on stdin, asserts `{"known": true, "live": 1}`, then `--wait 0.2` exits 3, then stop hook and `--wait 2` exits 0).
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement. **Step 4:** Run; PASS.
- [ ] **Step 5: Commit** `feat(handoff): subagents status/wait/purge on the CLI`.

### Task 3: Install the hooks by default

**Files:**
- Modify: `herdr_engineering/handoff_auto_setup.py` (drop `UserPromptSubmit` only after plan 5; here add `install_subagent_hooks(home)` that installs just `SubagentStart`/`SubagentStop`), `scripts/install.sh` (new step beside 7c at line ~345, flag `HERDR_INSTALL_SUBAGENT_HOOKS`, default 1, opt out `--no-subagent-hooks`), `docs/operations/handoff-auto-triggers.md`
- Test: `tests/test_handoff_auto_setup.py`

**Interfaces:**
- Consumes: existing `install/uninstall/check(home)`, `Item`.
- Produces: `install_subagent_hooks(home: Path | None = None) -> list[Item]`; CLI `herdr-eng handoff auto install --subagents-only`; `scripts/install.sh --plan` mentions the step.

- [ ] **Step 1: Failing tests** with a real temp HOME under `.herdr-test/home/`: `test_install_subagent_hooks_writes_start_and_stop_only`, `test_second_install_is_noop`, `test_foreign_hooks_untouched`, `test_invalid_json_not_rewritten`, `test_installed_start_hook_registers_agent` (run the exact command string from settings.json through `sh -c` with a Claude hook payload on stdin and `HERDR_PANE_ID=w1:p1`; assert the registry file appears), plus an `install.sh --plan` smoke asserting the step is listed.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement (reuse `HOOKS` entries `SubagentStart`, `SubagentStop`). **Step 4:** Run; PASS.
- [ ] **Step 5: Commit** `feat(handoff): install subagent registry hooks by default`.

### Task 4: Drain and takeover use the registry

**Files:**
- Create: `herdr_engineering/handoff_subagent_gate.py`
- Test: `tests/test_handoff_subagent_gate.py`

**Interfaces:**
- Consumes: Task 1 (`status`, `wait_until_idle`, `purge_pane`), plan 0 `drain_order`.
- Produces: `@dataclass(frozen=True) class SubagentGate`: `order_text: str`, `known: bool`, `live: int`; `prepare_drain(pane_id: str, harness: Harness, env: Mapping[str,str]) -> SubagentGate` (`order_text = drain_order(known, harness)`); `finish(pane_id, env) -> None` (calls `purge_pane`, to be called after the source pane closes). Plan 3 calls these two functions; it does not touch the registry directly.

- [ ] **Step 1: Failing tests** `test_prepare_drain_known_zero_uses_known_order`, `test_prepare_drain_unseen_pane_uses_unknown_order`, `test_non_claude_harness_always_unknown`, `test_finish_purges`.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement. **Step 4:** Run; PASS.
- [ ] **Step 5: Commit** `feat(handoff): subagent gate for the drain order`.

### Task 5: Live check with a real Claude subagent

**Files:** Create `tests/e2e/test_subagent_registry_live.py` (marker `herdr_live`, skipped without Herdr and `claude`).

- [ ] **Step 1: Write** the test: open a pane, run `claude -p` is not enough (hooks fire for interactive subagents); start interactive Claude in a pane, ask it to run one background agent that sleeps, poll `herdr-eng handoff subagents --pane <id> --status --json` until `live == 1`, then until `live == 0`.
- [ ] **Step 2:** Run on this machine with hooks installed by Task 3; PASS; record the observed hook payload keys in `docs/operations/handoff-auto-triggers.md`.
- [ ] **Step 3: Commit** `test(handoff): subagent registry observed live`.
