# Session Side Pane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A narrow Herdr pane, one per tab, showing the focused session's Summary, Handoffs tree and CI status, read-only.

**Architecture:** New package `herdr_engineering/side_pane/`: a pure model builder (`model.py`) from the event log, handoff store, `herdr` state and CI data; three tab renderers; a thin runner on `pane_viewer.run_viewer` (SIGWINCH reflow, `_parse_keys`) styled like `agent_ops_tui.py` (pure `render`, reducer, standard library only). Registered as a Herdr plugin pane (`deploy/herdr-session-pane-plugin/herdr-plugin.toml`, `[[panes]]` with `placement = "split"`), opened by the controller with `herdr plugin pane open --plugin ID --entrypoint ID --placement split --direction right --target-pane <pane> --no-focus`. Collapse is resize/zoom only (Herdr has no collapse): `herdr pane resize --direction` and `herdr pane zoom`.

**Tech Stack:** Python 3.13 stdlib, existing `pane_viewer.py`, `agent_ops_tui.py` helpers, `summary_report.py` (`collect_evidence`), `ci.py` (`WoodpeckerProvider.public_pipelines`, `PipelineView`), `control_plane/pr_trail.py` (`build`), `control_plane/handoffs.py` (`HandoffRegistry.chain`).

**Spec:** `docs/superpowers/specs/2026-11-18-auto-handoff-design.md` (section "Side pane") and mockups `2026-11-18-auto-handoff-mockups/handoff-layout.html`, `ci-tab.html`.

## Global Constraints

- One pane per tab, created by the controller with the first managed session in that tab; it follows the focused session's chain.
- Tabs: Summary / Handoffs (tree, totals, subagent-wait label) / CI (a card per PR/branch).
- Merged PRs collapse to one line; a check that never started shows grey "not started".
- Informational only: no click actions. The tab label shows a dot when something is running or failing (`attention`).
- Tree colours from the event log: green handed off, orange failed and continued in compaction, red stuck with reason.
- CI data tokenless only (`public_pipelines`); never use or write Woodpecker tokens.
- Real deps, no mocks; nothing under `/tmp`, `/private/tmp`, scratchpad (use `.herdr-test/`). Never push without approval.

## Review Focus

- Pane narrower than the longest branch or PR title (e.g. 30 columns): truncation with an ellipsis, no wrapped tree glyphs, no crash.
- Focused session has no handoff chain yet: Handoffs tab shows "no handoffs", not an empty or broken tree.
- Woodpecker unreachable or returns a pipeline with zero workflows: CI card shows "unavailable" or grey "not started", never an exception.
- Focus moves to a pane in another tab (a second side pane exists there): each pane follows its own tab's focus, no cross-talk.
- Event log has a chain with a dangling predecessor (record imported from another repo): rendered as a root, flagged `?`.

---

### Task 1: Model from events and store

**Files:** Create `side_pane/__init__.py`, `side_pane/model.py`; Test `tests/test_side_pane_model.py`.

**Interfaces:** Consumes `EventLog.read`, `chain_colour`, `HandoffRegistry.chain` (CHAIN_MAX = 10), `SubagentStatus` (plan 2). Produces `SidePaneModel` (index), `ChainRow(handoff_id: str, depth: int, harness: Harness, colour: Colour | None, label: str, reason: str | None, dangling: bool)`, `Totals(handoffs: int, compactions: int, failures: int)`, `build_model(session: SessionRef | None, store_root: Path, env, now: float) -> SidePaneModel`; `attention` is True when any chain is not terminal-green or a CI card is running/failing.

- [ ] **Step 1: Failing tests** with a real event log and real handoff records in `.herdr-test/store/`: `test_chain_rows_follow_predecessor_links`, `test_colours_from_events`, `test_totals`, `test_dangling_predecessor_marked`, `test_no_session_gives_empty_model`, `test_attention_true_while_draining`, `test_waiting_for_subagents_label` (registry live == 2 gives label "waiting for 2 subagents").
- [ ] **Step 2:** `pytest tests/test_side_pane_model.py -v`; FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5: Commit** `feat(side-pane): session model from the event log`.

### Task 2: CI cards

**Files:** Create `side_pane/ci_tab.py`; Test `tests/test_side_pane_ci.py`.

**Interfaces:** Consumes `ci.WoodpeckerProvider.public_pipelines(repository)` and `pipeline(repository, number)` (workflows/tasks), `pr_trail.build` for the PR and merge state, `gh pr list --json` for open PRs. Produces `@dataclass(frozen=True) CiCard(ref: str, title: str, state: Literal["running","passed","failed","not_started","merged","unavailable"], workflows: tuple[str, ...], url: str | None)`; `build_cards(repo: str, branches: Sequence[str], provider) -> tuple[CiCard, ...]`. Merged PRs become one-line cards (`state="merged"`, no workflows). A required check that never started (e.g. no pipeline for the head SHA) is `not_started`.

- [ ] **Step 1: Failing tests** against a real local HTTP server serving recorded Woodpecker JSON (`tests/fixtures/woodpecker/*.json`, a stdlib `http.server` on an ephemeral port; no mocks of the provider class): `test_running_pipeline_card`, `test_failed_workflow_listed`, `test_merged_pr_collapses_to_one_line`, `test_no_pipeline_is_not_started`, `test_provider_down_is_unavailable`.
- [ ] **Step 2-4:** FAIL, implement, PASS. **Step 5: Commit** `feat(side-pane): CI cards`.

### Task 3: Summary and Handoffs renderers

**Files:** Create `side_pane/summary_tab.py`, `side_pane/handoffs_tab.py`, `side_pane/render.py`; Test `tests/test_side_pane_render.py`.

**Interfaces:** Consumes `SidePaneModel`, `summary_report.collect_evidence` for the Summary text (goal, last action, repo state; read-only, no model call), `agent_ops_tui.Canvas` where reusable. Produces pure `render(model: SidePaneModel, tab: Tab, width: int, height: int, now: float) -> list[str]` and `class Tab(StrEnum)`: `SUMMARY`, `HANDOFFS`, `CI`; `tab_label(model) -> str` (adds a dot when `attention`).

- [ ] **Step 1: Failing tests:** `test_handoffs_tree_glyphs_and_colour_codes`, `test_render_truncates_to_width_30`, `test_every_line_at_most_width`, `test_tab_label_dot_when_attention`, `test_empty_chain_message`, `test_ci_tab_one_line_for_merged`, golden-file snapshots for widths 30, 44, 60 (files under `tests/fixtures/side_pane/`).
- [ ] **Step 2-4:** FAIL, implement, PASS. **Step 5: Commit** `feat(side-pane): tab renderers`.

### Task 4: Runner, focus following, keys

**Files:** Create `side_pane/app.py`; Modify `herdr_engineering/cli.py` (new `side-pane` subparser: `run`, `ensure --tab TAB_ID`); Test `tests/test_side_pane_app.py`.

**Interfaces:** Produces `run(argv: Sequence[str]) -> int` using `pane_viewer.run_viewer(render, ...)`; refresh every 2s and on key; keys: `1`/`2`/`3` or Tab to switch, `q` ignored (pane stays), `z` zoom toggle (`herdr pane zoom --toggle`), `[` / `]` resize (`herdr pane resize --direction left|right --amount 0.1`). Focus following: poll `herdr pane current`/`agent list` through `herdr_pane_client`, take the focused pane in its own tab, `SessionRef` via plan 0 `adapter.locate`.

- [ ] **Step 1: Failing tests** with a real pty (`pty.fork`) and the real runner: `test_tab_keys_switch_tabs`, `test_resize_reflows_on_sigwinch`, `test_follows_focus_change` (real Herdr: two panes in a tab, focus the second, the model's session id changes), `test_q_does_not_exit`.
- [ ] **Step 2-4:** FAIL, implement, PASS. **Step 5: Commit** `feat(side-pane): runner with focus following`.

### Task 5: Plugin manifest and one-per-tab creation

**Files:** Create `deploy/herdr-session-pane-plugin/herdr-plugin.toml` (`[[panes]]` id `session-pane`, title "Session", `placement = "split"`, command `herdr-eng side-pane run`; `min_herdr_version = "0.9.1"`), `side_pane/ensure.py`; Modify `handoff_controller/run.py` (call `ensure` on first managed session in a tab); Test `tests/test_side_pane_ensure.py`.

**Interfaces:** Produces `ensure(tab_id: str, target_pane: str, herdr) -> str` returning the side pane id; idempotent, a second call for the same tab opens nothing (detect by pane title `Session` in `herdr pane list` for that tab).

- [ ] **Step 1: Failing tests** (real Herdr): `test_ensure_opens_one_pane_per_tab`, `test_ensure_twice_is_noop`, `test_two_tabs_get_two_panes`, `test_opens_without_stealing_focus`.
- [ ] **Step 2-4:** FAIL, implement via `herdr plugin pane open ... --no-focus`, PASS. **Step 5: Commit** `feat(side-pane): plugin manifest and one pane per tab`.

### Task 6: Smoke, E2E and docs

**Files:** Create `tests/e2e/test_side_pane_live.py` (marker `herdr_live`); Modify `docs/operations/handoff.md`.

- [ ] **Step 1: Write the E2E:** start a controller-managed session, drive a handoff (plan 3 fixture), read the side pane via `herdr pane read`, assert the tree shows the green row and the tab label has no dot after success, then a failing chain shows red with its reason.
- [ ] **Step 2:** Run `pytest tests/test_side_pane_*.py -q --cov=herdr_engineering.side_pane --cov-fail-under=80`; then the E2E; PASS.
- [ ] **Step 3: Commit** `test(side-pane): live E2E and docs`.
