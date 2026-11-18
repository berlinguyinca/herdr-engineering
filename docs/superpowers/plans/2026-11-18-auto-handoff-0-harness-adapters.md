# Harness Adapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One adapter per harness (Claude Code, Codex, Pi, OpenCode) that reads context use and window, builds the typed drain order, detects an input-ready session, and exits it cleanly.

**Architecture:** New package `herdr_engineering/handoff_adapters/` with a `HarnessAdapter` protocol (see the index's Cross-plan interfaces) and four implementations. Each reads the harness's own session record; nothing is inferred from a model name. Where a capability is unverified, a verification task measures it from real records. The context *window* is the one exception to never-guess (user rule: every session, every harness, non-negotiable): when no recorded window field exists, the adapter uses `herdr_engineering/handoff_adapters/windows.py` `MODEL_WINDOWS` (model id → window, seeded from autospec_context_monitor's MODEL_MAX) and, for an unknown model, `FALLBACK_WINDOW = 128_000` (smallest common window, so it hands off early, never late). The `ContextReading` carries `window_source: Literal['recorded','model_table','fallback']`.

**Tech Stack:** Python 3.13 standard library (`json`, `sqlite3` read-only, `pathlib`), pytest. Real record fixtures copied from real sessions (redacted), no mocks.

**Spec:** `docs/superpowers/specs/2026-11-18-auto-handoff-design.md` (sections "Harness-driven controller", "Typed-command limit 900 bytes", "My calls").

## Global Constraints

- Context use and window come from the harness's own records; the `_window` 200k/1M guess in `handoff_context_hook.py` is not reused.
- Typed command limit: 900 bytes (`MAX_TYPED_BYTES = 900`).
- Drain limit 80%, ceiling = limit + 12 points, drain timeout 30 min, briefing timeout 5 min (consumed by plan 3; adapters only report).
- Drain order, in this order: start no new work; wait for subagents; commit and push to the feature branch (never main); write the briefing with `herdr-handoff create`; stop.
- All four harnesses, every session. Pi, Codex and OpenCode are "Not verified" in `docs/operations/handoff-auto-triggers.md`; each capability gets a measuring task.
- Real deps, no mocks. Nothing under `/tmp`, `/private/tmp` or the scratchpad; fixtures live in `tests/fixtures/handoff_adapters/`.
- Never use or write CI tokens. Woodpecker CI.

## Review Focus

- A record with no usage yet (new session, no assistant turn): `read_context` returns `None`, never 0%.
- A record file being appended mid-read (half-written last JSON line): the partial line is skipped, not raised.
- Session switched model mid-run to a different window: the reading uses the window recorded with the latest usage entry.
- Two sessions in one cwd (several panes in one repo): `locate` picks by pane/session id, not "newest file in cwd".
- Drain order over 900 bytes after adding a path or branch name: `TypedInput` raises rather than truncating.

---

### Task 1: Types, drain order text, adapter registry

**Files:**
- Create: `herdr_engineering/handoff_adapters/__init__.py`, `base.py`, `drain.py`
- Test: `tests/test_handoff_adapters_base.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Harness`, `SessionRef`, `ContextReading`, `ExitPlan`, `TypedInput`, `MAX_TYPED_BYTES`, `HarnessAdapter`, `adapter_for`, `drain_order` (all as in the index).

- [ ] **Step 1: Write failing tests** `test_percent_is_used_over_window`, `test_typed_input_over_900_bytes_raises_value_error` (901-byte ASCII and a 301-char multibyte string both raise), `test_drain_order_known_lists_steps_in_spec_order` (asserts index of "no new work" < "subagents" < "commit" < "herdr-handoff create" < "stop"), `test_drain_order_unknown_asks_to_list_and_wait_for_background_agents`, `test_drain_order_fits_900_bytes_for_every_harness`, `test_adapter_for_returns_adapter_per_harness` (skipped until tasks 2-5 land: parametrize over the four).
- [ ] **Step 2: Run** `pytest tests/test_handoff_adapters_base.py -v`; expect ImportError.
- [ ] **Step 3: Implement** the frozen dataclasses and enum in `base.py`; `TypedInput.__post_init__` checks `len(text.encode()) <= MAX_TYPED_BYTES`. `drain.py` holds the two fixed strings; the unknown variant starts the subagent step with "List your background agents, then wait for them to finish". `adapter_for` maps by dict, lazily importing each adapter module.
- [ ] **Step 4: Run** the tests; expect PASS (registry test skipped).
- [ ] **Step 5: Commit** `feat(handoff): adapter types and fixed drain order`.

### Task 2: Claude Code adapter

**Files:**
- Create: `herdr_engineering/handoff_adapters/claude.py`
- Test: `tests/test_handoff_adapters_claude.py`, fixtures `tests/fixtures/handoff_adapters/claude/*.jsonl`

**Interfaces:**
- Consumes: base types.
- Produces: `class ClaudeAdapter(HarnessAdapter)`.

- [ ] **Step 1: Write failing tests** against a real redacted transcript: `test_read_context_sums_input_cache_read_and_cache_creation_of_last_assistant_usage` (same sum as `handoff_context_hook.context_tokens`), `test_read_context_none_before_first_assistant_turn`, `test_partial_last_line_is_skipped`, `test_locate_picks_transcript_by_session_id_not_newest` (two transcripts in `~/.claude/projects/<cwd-slug>/`; the slug replaces `/` and `.` with `-`).
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement** `locate` (transcript directory from the cwd slug; session id from the pane's `herdr agent get` `agent_session` where present), `read_context` (window from Task 6's finding), `drain_input` (`TypedInput(order, ("Enter",))`), `is_ready_for_input` (agent status `idle` or `done` and the last visible line is the prompt box with empty input; the prompt marker is taken from the Task 7 measurement), `exit_plan` (`ExitPlan("/exit", ("Enter",), 15.0)`), `compact_input` (`/compact`).
- [ ] **Step 4: Run**; expect PASS.
- [ ] **Step 5: Commit** `feat(handoff): Claude Code adapter`.

### Task 3: Codex adapter

**Files:** Create `handoff_adapters/codex.py`; Test `tests/test_handoff_adapters_codex.py`, fixtures from `~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl`.

**Interfaces:** Produces `class CodexAdapter(HarnessAdapter)`.

- [ ] **Step 1: Write failing tests**: `test_read_context_uses_last_token_count_event` (events `type":"token_count"`, `info.last_token_usage.total_tokens` over `info.model_context_window`; both verified present in real rollouts, e.g. window 828400), `test_none_when_no_token_count_event`, `test_partial_last_line_is_skipped`, `test_locate_matches_rollout_by_session_id_in_filename`.
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement** as above. Whether `last_token_usage` or `total_token_usage` equals the current context size is measured in Task 6 against a rollout where compaction occurred; use the result.
- [ ] **Step 4: Run**; expect PASS. **Step 5: Commit** `feat(handoff): Codex adapter`.

### Task 4: Pi adapter

**Files:** Create `handoff_adapters/pi.py`; Test `tests/test_handoff_adapters_pi.py`, fixtures from `~/.pi/agent/sessions/<cwd-slug>/<timestamp>_<uuid>.jsonl`.

**Interfaces:** Produces `class PiAdapter(HarnessAdapter)`.

- [ ] **Step 1: Write failing tests**: `test_read_context_uses_last_message_usage_total_tokens` (messages carry `usage.totalTokens`, verified in a real record), `test_window_from_model_table_when_unrecorded` and `test_unknown_model_uses_fallback_window` (window_source `model_table` / `fallback`, never None), `test_locate_by_session_uuid`.
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement.** Window source comes from Task 6; Pi also exposes `contextHandoffTrigger` in pi-engineering (per `docs/operations/handoff-auto-triggers.md`), which is consulted only if it reports the window.
- [ ] **Step 4: Run**; PASS. **Step 5: Commit** `feat(handoff): Pi adapter`.

### Task 5: OpenCode adapter

**Files:** Create `handoff_adapters/opencode.py`; Test `tests/test_handoff_adapters_opencode.py`, fixture a real `opencode.db` subset built by a script that copies rows for one session into a fixture db.

**Interfaces:** Produces `class OpenCodeAdapter(HarnessAdapter)`; `drain_input` for OpenCode has no flag, it types the order like the others.

- [ ] **Step 1: Write failing tests**: `test_read_context_from_latest_assistant_message_tokens` (table `message`, JSON `data` with `tokens.total`, `modelID`, `providerID`), `test_db_opened_read_only` (a concurrent writer holding a transaction does not block or raise; open with `file:...?mode=ro`), `test_none_without_window`.
- [ ] **Step 2: Run**; FAIL. **Step 3: Implement** with `sqlite3` read-only URI; window from Task 6. **Step 4: Run**; PASS. **Step 5: Commit** `feat(handoff): OpenCode adapter`.

### Task 6: Verify and record context windows for Claude, Pi, OpenCode (and Codex current-size semantics)

**Files:** Create `docs/operations/handoff-harness-capabilities.md` (measured table); Modify `docs/operations/handoff-auto-triggers.md` (replace "Not verified" with the measured result); Modify the adapters from tasks 2-5.

**Interfaces:** Consumes the four adapters. Produces the window source per harness, recorded in the doc.

- [ ] **Step 1: Measure.** Write `scripts/measure_harness_windows.py` that, for each harness, scans its own session records and prints where a window or a model's limit appears (Claude: transcript fields and the statusline JSON; Pi: `~/.pi/agent/` config and records; OpenCode: `opencode.db` tables `session`, `session_context_epoch`, provider config; Codex: confirm `model_context_window` and whether `last_token_usage` drops after a compaction).
- [ ] **Step 2: Run it** and paste the real output for each harness into the capabilities doc. Expected: a documented source for each window, or an explicit "none found".
- [ ] **Step 3: Wire** each adapter's window source to the measured field. A harness with no recorded window uses `MODEL_WINDOWS` then `FALLBACK_WINDOW`; `read_context` never returns None because of a missing window (only for no usage yet). Add a test per harness `test_read_context_window_matches_recorded_field`.
- [ ] **Step 4: Run** `pytest tests/test_handoff_adapters_*.py -v`; PASS. **Step 5: Commit** `docs(handoff): measured context window sources per harness`.

### Task 7: Verify idle/prompt detection and clean exit per harness in real Herdr panes

**Files:** Create `tests/e2e/test_handoff_adapters_live.py` (marker `herdr_live`, skipped without a `herdr` socket); Modify adapters' `is_ready_for_input` and `exit_plan` to the measured values; Modify the capabilities doc.

**Interfaces:** Consumes `HerdrPaneClient` (`herdr_engineering/herdr_pane_client.py`: `read_visible`, `send_text`, `send_keys`) and `herdr pane process-info`.

- [ ] **Step 1: Write the live test** per installed harness: open a pane with `herdr pane split --no-focus`, start the harness, wait `herdr agent wait --until idle`, read `pane read --source visible`, assert `is_ready_for_input` is True with an empty prompt and False while the prompt holds typed text (type text without Enter first).
- [ ] **Step 2: Add** `test_exit_plan_ends_the_process`: send the `ExitPlan`, poll `herdr pane process-info` until the harness process ends within `process_end_timeout_s`, then `herdr pane close`.
- [ ] **Step 3: Run** `pytest tests/e2e/test_handoff_adapters_live.py -m herdr_live -v` on this machine; record per harness the exact prompt marker and exit command that worked. A command that fails is replaced by the one that works, found from the harness's own `/help`; if none ends the process, `exit_plan` uses the keys that do (e.g. `ctrl-d`), and the finding is written to the doc.
- [ ] **Step 4: Update** the adapters and the doc; un-skip the registry test from Task 1. **Step 5: Commit** `feat(handoff): measured idle detection and clean exit for all harnesses`.

### Task 8: Smoke command

**Files:** Modify `herdr_engineering/cli.py` (handoff subparser near line 509-531; handler beside `_cmd_handoff_harness` ~line 1493); Test `tests/test_handoff_adapters_cli.py`.

**Interfaces:** Produces `herdr-eng handoff context [--pane ID] [--json]` printing `{harness, used_tokens, window_tokens, percent, source}` or `{"unknown": true}`.

- [ ] **Step 1: Write failing test** running the CLI as a subprocess against a fixture transcript; asserts JSON fields and exit 0, and `{"unknown": true}` with exit 0 when no record.
- [ ] **Step 2: Run**; FAIL. **Step 3: Implement** using `handoff_harness.detect` then `adapter_for(...).locate/read_context`. **Step 4: Run**; PASS. **Step 5: Commit** `feat(handoff): herdr-eng handoff context`.
