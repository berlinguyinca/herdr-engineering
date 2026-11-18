# Handoff Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Herdr-hosted controller that watches every managed session's context use, drains it at the limit, hands it to a same-harness successor below it, and falls back to compaction only when a handoff cannot be completed.

**Architecture:** New package `herdr_engineering/handoff_controller/`. A polling loop (Herdr offers no output-changed event; `events.subscribe` only has `pane.output_matched`) is hosted by a Herdr plugin `[[startup]]` command, the pattern `deploy/herdr-cp-feed-plugin/herdr-plugin.toml` already uses. Each tick: list agent panes, read context via plan 0 adapters, and advance a per-session state machine (`idle -> draining -> briefing -> takeover -> done | failed`). Every step appends a `HandoffEvent` to the shared store's `events.jsonl`. It calls `herdr-handoff` (plan 1) for create/takeover and the subagent gate (plan 2) for waiting.

**Tech Stack:** Python 3.13, `herdr` CLI v0.9.1 via `herdr_pane_client.py` and `herdr_adapter.py`, `herdr-handoff` 0.5.0, git, `gh`, pytest with real git repos and real Herdr panes.

**Spec:** `docs/superpowers/specs/2026-11-18-auto-handoff-design.md` (sections "Harness-driven controller", "Drain order", "Briefing fallback", "Takeover", "Event log", "Shared store").

## Global Constraints

- Drain limit 80%; hard ceiling = limit + 12 points; drain timeout 30 min; briefing timeout 5 min (`ControllerConfig`, index).
- Type the drain order only when the session is idle with an empty input: `herdr agent wait --until idle` plus a visible-screen check via the adapter's `is_ready_for_input`. Type with `herdr agent prompt` (it uses bracketed paste and refuses a blocked agent with `agent_blocked`), never `pane run`.
- Typed command limit 900 bytes.
- Drain order: start no new work; wait for subagents; commit and push to the feature branch (never main); write the briefing with `herdr-handoff create`; stop.
- Fallback: a second model (headless harness call) writes the briefing from transcript, `git status`/`git log` and `gh pr list`; the record is marked `fallback`.
- Controller makes a WIP commit when uncommitted changes remain; pushes where allowed. In herdr-engineering (no push approval) it records unpushed commits in the briefing and proceeds (uses `--allow-dirty-source`, plan 1 task 6).
- Takeover: `herdr-handoff takeover --direction down`, same tab, same harness, proof of life observed by Herdr, clean exit then close, record ids.
- On any failure the source keeps running and nothing is closed. Retries bounded (`max_attempts = 3`) and idempotent.
- Last resort: compaction, then tell the compacted session the briefing path.
- Event colours: green handed off, orange failed and continued in compaction, red stuck with reason.
- All four harnesses, every session, default on; per-session off switch (`herdr-eng handoff auto off`), `HERDR_HANDOFF_AUTO=0` globally.
- Real deps, no mocks. Nothing under `/tmp`, `/private/tmp`, scratchpad; tests use `.herdr-test/`. Never push herdr-engineering without approval. No tokens. Woodpecker `unit-gates` is slow; keep new tests fast.

## Review Focus

- Session already at 95% when the controller first sees it (started after a long compaction): it must drain once, not loop, and a session past the ceiling goes straight to the last-resort path if the drain order cannot be typed.
- Session is mid-tool-call or showing an approval prompt (`blocked`): nothing is typed; the controller waits, and after the drain timeout records `handoff.stuck` with reason `blocked`.
- Controller restarted mid-handoff: state is rebuilt from `events.jsonl` and the store (the idempotent retry), with no second child pane.
- A user types into the pane while the order is being typed: the screen check fails, the controller backs off and retries within the bound.
- The harness writes the briefing to the wrong store or never runs `herdr-handoff create`: the briefing timeout fires and the fallback writer produces it.

---

### Task 1: Config, event log, chain colour

**Files:** Create `handoff_controller/__init__.py`, `config.py`, `events.py`; Test `tests/test_handoff_controller_events.py`.

**Interfaces:** Produces `ControllerConfig`, `Colour`, `HandoffEvent`, `EventLog`, `chain_colour` (index).

- [ ] **Step 1: Failing tests:** `test_ceiling_is_limit_plus_12`, `test_event_roundtrip_jsonl`, `test_append_is_one_line_and_concurrent_appends_do_not_interleave` (two processes appending 200 events each into a fixed-path file; every line parses), `test_read_by_chain_id`, `test_chain_colour_green_after_succeeded`, `test_chain_colour_orange_after_failed_compacted`, `test_chain_colour_red_after_stuck_then_none_if_no_terminal`, `test_unknown_event_name_rejected`.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement (`os.open(..., O_APPEND)`, single `os.write`). **Step 4:** PASS.
- [ ] **Step 5: Commit** `feat(handoff): controller config and event log`.

### Task 2: Per-session switch

**Files:** Create `handoff_controller/switch.py`; Modify `herdr_engineering/cli.py` (handoff `auto` handler ~1504 gains `off`/`on` actions); Test `tests/test_handoff_controller_switch.py`.

**Interfaces:** Produces `is_enabled`, `set_enabled(pane_id, enabled, state_dir)`, CLI `herdr-eng handoff auto off|on [--pane ID]` (reads `HERDR_PANE_ID`).

- [ ] **Step 1: Failing tests:** `test_default_on`, `test_env_zero_turns_off`, `test_pane_marker_turns_off_only_that_pane`, `test_on_removes_marker`, CLI subprocess smoke.
- [ ] **Step 2-4:** Run FAIL, implement (marker file `state/handoff-controller/off/<pane_id>`), run PASS. **Step 5: Commit** `feat(handoff): per-session auto-handoff switch`.

### Task 3: Trigger decision

**Files:** Create `handoff_controller/trigger.py`; Test `tests/test_handoff_controller_trigger.py`.

**Interfaces:** Consumes plan 0 `ContextReading`. Produces `class Decision(StrEnum)`: `NONE`, `DRAIN`, `OVER_CEILING`; `decide(reading: ContextReading | None, cfg: ControllerConfig) -> Decision`.

- [ ] **Step 1: Failing tests:** `test_none_when_reading_missing`, `test_none_below_limit`, `test_drain_at_exact_limit`, `test_over_ceiling_at_limit_plus_12`.
- [ ] **Step 2-4:** FAIL, implement, PASS. **Step 5: Commit** `feat(handoff): trigger decision`.

### Task 4: Typing safety and drain

**Files:** Create `handoff_controller/drain.py`; Test `tests/test_handoff_controller_drain.py`.

**Interfaces:** Consumes `HarnessAdapter.is_ready_for_input`, `drain_input`, `SubagentGate` (plan 2), `herdr agent wait`, `herdr agent prompt`. Produces `drain(ref: SessionRef, adapter, gate: SubagentGate, herdr: HerdrCli, cfg) -> DrainResult` with `DrainResult(outcome: Literal["typed","blocked","timeout"], waited_s: float)`.

- [ ] **Step 1: Failing tests** in real Herdr panes running a small real TUI-shaped script (a Python script that prints a prompt and reads input, living in `tests/fixtures/`): `test_types_order_when_idle_and_empty`, `test_does_not_type_when_input_has_text` (pre-type text without Enter), `test_does_not_type_when_blocked`, `test_times_out_after_cfg_drain_timeout_s` (cfg with 2s), `test_order_is_the_gate_text` (screen shows exactly `gate.order_text`).
- [ ] **Step 2-4:** FAIL, implement (wait idle, read visible, check, `agent prompt`; no typing otherwise), PASS. **Step 5: Commit** `feat(handoff): safe drain typing`.

### Task 5: WIP commit

**Files:** Create `handoff_controller/wip.py`; Test `tests/test_handoff_controller_wip.py`.

**Interfaces:** Produces `wip_commit(worktree: Path, *, push_allowed: bool) -> WipResult` with `WipResult(committed: bool, sha: str | None, pushed: bool, unpushed: tuple[str, ...])`. Never commits on `main`/`master`; on those branches returns `committed=False` and the controller records `handoff.stuck` reason `on_main_with_changes`. Commit message `wip: auto-handoff checkpoint`; stages tracked changes only (`git add -u`); untracked files are listed in the briefing, never committed.

- [ ] **Step 1: Failing tests** in real git repos with a bare remote: `test_clean_tree_no_commit`, `test_tracked_change_committed_and_pushed`, `test_push_not_allowed_records_unpushed_sha`, `test_never_commits_on_main`, `test_untracked_files_not_committed`.
- [ ] **Step 2-4:** FAIL, implement, PASS. **Step 5: Commit** `feat(handoff): WIP commit on the feature branch`.

### Task 6: Briefing wait and second-model fallback

**Files:** Create `handoff_controller/briefing.py`; Test `tests/test_handoff_controller_briefing.py`.

**Interfaces:** Produces `wait_for_briefing(store_root: Path, since: str, pane_id: str, timeout_s: float) -> str | None` (the new `handoff_<hex>` whose `source.pane_id` matches, created after `since`); `write_fallback_briefing(ref: SessionRef, worktree: Path, store_root: Path, harness_runner) -> str` that calls the same harness headlessly (`claude -p`, `codex exec`, `pi -p`, `opencode run`; the exact flags measured in plan 0 task 7), with transcript tail, `git status --short`, `git log -n 20 --oneline`, `gh pr list --json number,title,headRefName` as input, then `herdr-handoff create --from-json` with `provenance.fallback = true`.

- [ ] **Step 1: Failing tests:** `test_wait_returns_id_when_record_appears`, `test_wait_none_on_timeout`, `test_wait_ignores_record_from_other_pane`, `test_fallback_creates_record_marked_fallback` (runner is a real script that prints a JSON briefing; this is a real subprocess, not a mock of the controller), `test_fallback_input_includes_git_status_and_pr_list` (real repo, `gh` replaced by the repo's existing `tests/e2e/fake_gh` executable already used by the suite).
- [ ] **Step 2-4:** FAIL, implement, PASS. **Step 5: Commit** `feat(handoff): briefing wait and fallback writer`.

### Task 7: Takeover, retries, compaction last resort

**Files:** Create `handoff_controller/takeover.py`, `compaction.py`; Test `tests/test_handoff_controller_takeover.py`.

**Interfaces:** Consumes `herdr-handoff takeover --direction down --source-exit-text ... --allow-dirty-source` (plan 1), adapter `exit_plan`/`compact_input`. Produces `run_takeover(ref, adapter, handoff_id, cfg, log: EventLog, herdr_handoff: Runner) -> HandoffEvent` returning the terminal event; `compact_with_pointer(ref, adapter, handoff_id_or_path) -> bool`. Rules: attempt up to `cfg.max_attempts`; between attempts check the store: if the record already has `successor_id`, do not launch again. Success: `handoff.succeeded` (GREEN) and `gate.finish`. Takeover refused: source untouched, retry, then compaction with the pointer text "Your briefing is <path>; read it after compaction" typed through the adapter; success there is `handoff.failed_compacted` (ORANGE). Compaction impossible: `handoff.stuck` (RED) with the reason string.

- [ ] **Step 1: Failing tests** (real Herdr panes, real `herdr-handoff` 0.5.0): `test_success_emits_succeeded_and_closes_source_after_exit`, `test_refusal_keeps_source_and_retries_then_compacts` (source worktree dirty and push not allowed with `--allow-dirty-source` absent to force `source_dirty`), `test_retry_after_crash_does_not_launch_second_child`, `test_compaction_pointer_typed_after_compact`, `test_no_way_forward_is_red_with_reason`.
- [ ] **Step 2-4:** FAIL, implement, PASS. **Step 5: Commit** `feat(handoff): takeover with retries and compaction fallback`.

### Task 8: Controller loop and host

**Files:** Create `handoff_controller/run.py`, `deploy/herdr-handoff-controller-plugin/herdr-plugin.toml` (`[[startup]]` command `herdr-eng handoff controller run`; `[[actions]]` `handoff-controller-status`; `min_herdr_version = "0.9.1"`, as in `deploy/herdr-agent-ops-plugin/herdr-plugin.toml`); Modify `herdr_engineering/cli.py` (handoff subparser: `controller run [--once] [--interval 5]`); Test `tests/test_handoff_controller_run.py`.

**Interfaces:** Produces `run_once`, `serve` (index). Skips a pane with `is_enabled` False (event `handoff.skipped`, reason `switch_off`) or `read_context` None (reason `no_window`/`no_record`), once per pane per reason. Tick interval 5s. The side pane (plan 4) creation hook: on the first managed session in a tab, call `side_pane.ensure(tab_id)` when that module exists.

- [ ] **Step 1: Failing tests:** `test_run_once_drains_session_at_limit` (a real pane running a script that appends usage lines to a fixture Claude transcript to reach 81%), `test_run_once_ignores_session_below_limit`, `test_skipped_event_once_per_reason`, `test_restart_resumes_from_event_log`, manifest parse test `test_plugin_manifest_valid` (via `herdr plugin link` dry run or the existing manifest tests' loader).
- [ ] **Step 2-4:** FAIL, implement, PASS.
- [ ] **Step 5: Commit** `feat(handoff): controller loop and Herdr plugin host`.

### Task 9: Shared store wiring and migration

**Files:** Modify `handoff_controller/run.py` and `briefing.py` to pass `--root` = `shared_store_root()`; Modify `herdr_engineering/control_plane/handoffs.py` only if it reads per-repo roots (check `HandoffRegistry.list`); Create `scripts/handoff_import_legacy.py`; Test `tests/test_handoff_controller_store.py`.

- [ ] **Step 1: Failing tests:** `test_records_from_two_repos_land_in_one_store`, `test_legacy_records_imported_once` (uses `HandoffStore.import_legacy`, plan 1).
- [ ] **Step 2-4:** FAIL, implement, PASS. **Step 5: Commit** `feat(handoff): controller uses the shared store`.

### Task 10: End-to-end and release notes

**Files:** Create `tests/e2e/test_handoff_controller_live.py` (marker `herdr_live`); Modify `CHANGELOG.md`, `docs/operations/handoff.md`, `docs/operations/handoff-auto-triggers.md`.

- [ ] **Step 1: Write** the E2E: real Herdr, a real Claude pane with a fixture transcript driven to 81%; assert the order is typed, a briefing record appears, the child pane is below the source, the source process ended and its pane closed, events end `handoff.succeeded`, and `chain_colour == GREEN`.
- [ ] **Step 2:** Run `pytest tests/e2e/test_handoff_controller_live.py -m herdr_live -v`; PASS. Run the whole controller suite once with `pytest tests/test_handoff_controller_*.py -q`; PASS with 80%+ coverage on the package (`--cov=herdr_engineering.handoff_controller --cov-fail-under=80`).
- [ ] **Step 3:** Document: the 80% limit, the switch, the fallback marker, the colours. State that plan 5 must ship in the same release.
- [ ] **Step 4: Commit** `docs(handoff): automatic handoff controller`.
