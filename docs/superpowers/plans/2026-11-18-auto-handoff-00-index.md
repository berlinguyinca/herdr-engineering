# Automatic Handoff: Plan Index

**Spec:** `docs/superpowers/specs/2026-11-18-auto-handoff-design.md` (mockups in `2026-11-18-auto-handoff-mockups/`).

Six plans, one PR each, in this order. Each produces working, testable software on its own.

| # | Plan | Repo | Depends on | Ships |
|---|------|------|-----------|-------|
| 0 | `2026-11-18-auto-handoff-0-harness-adapters.md` | herdr-engineering | none | Per-harness adapters: context use/window, drain typing, idle detection, clean exit |
| 1 | `2026-11-18-auto-handoff-1-herdr-handoff.md` | herdr-handoff (`/Users/wohlgemuth/IdeaProjects/herdr-handoff`), then a pin bump in herdr-engineering `lock/upstreams.yaml` | none | `herdr-handoff` 0.5.0: down placement, predecessor/successor ids, pane/session ids, clean source exit, shared store, doc fix |
| 2 | `2026-11-18-auto-handoff-2-subagent-registry.md` | herdr-engineering | 0 | Registry wired into drain/takeover; hooks installed by the installer |
| 3 | `2026-11-18-auto-handoff-3-controller.md` | herdr-engineering | 0, 1, 2 | The controller, event log, shared store use, compaction last resort |
| 4 | `2026-11-18-auto-handoff-4-side-pane.md` | herdr-engineering | 3 (event log) | Side pane TUI: Summary / Handoffs / CI |
| 5 | `2026-11-18-auto-handoff-5-retire-old-policies.md` | herdr-engineering + autospec | 3 | Old reminder hooks and autospec context monitor retired for Herdr sessions. **Same release as plan 3.** |

Plans 0 and 1 are independent and can run in parallel. Plan 4 only needs the event-log reader from plan 3. Plan 5 must not merge before plan 3 is released, and must release with it, otherwise sessions have no policy at all.

## Cross-plan interfaces

Every plan uses these names verbatim. A change here is a change in every plan that lists it.

| Owner | Module path | Name and signature | Notes |
|-------|-------------|--------------------|-------|
| 0 | `herdr_engineering/handoff_adapters/base.py` | `class Harness(StrEnum)`: `CLAUDE="claude"`, `CODEX="codex"`, `PI="pi"`, `OPENCODE="opencode"` | Same strings as `handoff_harness.Detection.harness` |
| 0 | same | `@dataclass(frozen=True) class SessionRef`: `harness: Harness`, `pane_id: str`, `cwd: Path`, `session_id: str \| None`, `record_path: Path \| None` | `record_path` is the transcript/rollout/db the adapter reads |
| 0 | same | `@dataclass(frozen=True) class ContextReading`: `used_tokens: int`, `window_tokens: int`, `source: str`, `window_source: Literal['recorded','model_table','fallback']`, `measured_at: float`; property `percent -> float` | `percent` = used / window * 100. Window is read when recorded, else `MODEL_WINDOWS` (`handoff_adapters/windows.py`), else `FALLBACK_WINDOW = 128_000`; never skipped |
| 0 | same | `@dataclass(frozen=True) class ExitPlan`: `text: str \| None`, `keys: tuple[str, ...]`, `process_end_timeout_s: float` | |
| 0 | same | `class HarnessAdapter(Protocol)`: `harness: Harness`; `locate(pane_id: str, cwd: Path) -> SessionRef \| None`; `read_context(ref: SessionRef) -> ContextReading \| None`; `drain_input(order: str) -> TypedInput`; `is_ready_for_input(screen: str, agent_status: str) -> bool`; `exit_plan() -> ExitPlan`; `compact_input() -> TypedInput` | |
| 0 | same | `@dataclass(frozen=True) class TypedInput`: `text: str`, `submit_keys: tuple[str, ...]`; `MAX_TYPED_BYTES = 900` | Constructor raises `ValueError` over 900 UTF-8 bytes |
| 0 | `herdr_engineering/handoff_adapters/__init__.py` | `adapter_for(harness: Harness) -> HarnessAdapter` | |
| 0 | `herdr_engineering/handoff_adapters/drain.py` | `DRAIN_ORDER_KNOWN: str`, `DRAIN_ORDER_UNKNOWN_SUBAGENTS: str`, `drain_order(subagents_known: bool, handoff_target: Harness) -> str` | Fixed text, one source of truth for plans 2 and 3 |
| 1 | `herdr_handoff.py` | envelope keys `predecessor_id: str \| None`, `successor_id: str \| None`; `source` and `target` gain `pane_id`, `session_id` (both `str \| None`) | Schema version stays 2; keys are optional |
| 1 | `herdr_handoff.py` | takeover block gains `direction: "down" \| "right"`; `placement` stays `"split"`; phase `source_exit_failed` added to `TAKEOVER_PHASES` | |
| 1 | `herdr_handoff_cli.py` | `takeover ... --direction {down,right}` (default `down`), `--source-exit-text TEXT`, `--source-exit-key KEY` (repeatable), `--source-exit-timeout SECONDS` (default 30), status `source_exit_failed` | Exit code 1, source and child stay up |
| 1 | `herdr_handoff.py` | `shared_store_root(env: Mapping[str,str]) -> Path`: `HERDR_HANDOFF_STORE`, else `~/.local/share/herdr-handoff/store` | `--root` and the old env vars still win |
| 1 | `herdr_handoff.py` | `HandoffStore.import_legacy(legacy_root: Path) -> list[str]` | Copies records not yet present; returns the ids |
| 2 | `herdr_engineering/handoff_subagent_registry.py` | `@dataclass(frozen=True) class SubagentStatus`: `known: bool`, `live: int` | `known=False` means no hook data for this pane |
| 2 | same | `status(pane_id: str, env: Mapping[str,str], now: float \| None = None) -> SubagentStatus` | |
| 2 | same | `wait_until_idle(pane_id, env, timeout_s: float, poll_s: float = 2.0) -> bool` | True when `live == 0` before the timeout |
| 2 | same | `purge_pane(pane_id: str, env: Mapping[str,str]) -> int` | Called after a source pane closes |
| 3 | `herdr_engineering/handoff_controller/config.py` | `@dataclass(frozen=True) class ControllerConfig`: `drain_limit_pct: float = 80.0`, `ceiling_margin_pts: float = 12.0`, `drain_timeout_s: float = 1800.0`, `briefing_timeout_s: float = 300.0`, `max_attempts: int = 3`; property `ceiling_pct -> float` | |
| 3 | `herdr_engineering/handoff_controller/events.py` | `class Colour(StrEnum)`: `GREEN`, `ORANGE`, `RED` | green handed off, orange failed and continued in compaction, red stuck |
| 3 | same | `@dataclass(frozen=True) class HandoffEvent`: `event: str`, `at: str` (UTC ISO), `chain_id: str`, `pane_id: str`, `session_id: str \| None`, `harness: Harness`, `handoff_id: str \| None`, `predecessor_id: str \| None`, `successor_id: str \| None`, `colour: Colour \| None`, `reason: str \| None`, `context_pct: float \| None` | One JSON object per line |
| 3 | same | event names: `handoff.triggered`, `handoff.drain_started`, `handoff.drained`, `handoff.briefing_written`, `handoff.briefing_fallback`, `handoff.wip_committed`, `handoff.takeover_started`, `handoff.succeeded` (GREEN), `handoff.failed_compacted` (ORANGE), `handoff.stuck` (RED), `handoff.skipped` | |
| 3 | same | `class EventLog`: `__init__(store_root: Path)`; `append(e: HandoffEvent) -> None`; `read(chain_id: str \| None = None) -> list[HandoffEvent]`; file `<store_root>/events.jsonl` | Append with `O_APPEND`, one line per write |
| 3 | same | `chain_colour(events: Sequence[HandoffEvent]) -> Colour \| None` | Last terminal event wins |
| 3 | `herdr_engineering/handoff_controller/switch.py` | `is_enabled(pane_id: str, env: Mapping[str,str], state_dir: Path) -> bool`; CLI `herdr-eng handoff auto off\|on [--pane ID]` | Off when `HERDR_HANDOFF_AUTO=0` or the per-pane marker exists |
| 3 | `herdr_engineering/handoff_controller/run.py` | `run_once(cfg, herdr, store_root, env) -> list[HandoffEvent]`; `serve(cfg, ...) -> None`; CLI `herdr-eng handoff controller run [--once]` | |
| 4 | `herdr_engineering/side_pane/model.py` | `@dataclass(frozen=True) class SidePaneModel`: `session: SessionRef \| None`, `chain: tuple[ChainRow, ...]`, `totals: Totals`, `ci: tuple[CiCard, ...]`, `summary: SummaryView`, `attention: bool` | `attention` drives the tab-label dot |
| 4 | `herdr_engineering/side_pane/app.py` | `run(argv: Sequence[str]) -> int`; CLI `herdr-eng side-pane run`, `herdr-eng side-pane ensure --tab TAB_ID` | |
| 5 | autospec `autospec_context_monitor/__main__.py` | env `AUTOSPEC_CONTEXT_MONITOR_SKIP_HERDR` (default `1`) and `is_herdr_managed(env: Mapping[str,str]) -> bool` (true when `HERDR_PANE_ID` is set) | Skips the monitor loop, logs `herdr_managed` |

Paths for tests: nothing may write under `/tmp`, `/private/tmp` or the harness scratchpad. CI is Linux where `tmp_path` lives under `/tmp`, so tests that exercise the write guard or run real harness-state fixtures use a fixed directory under the repo, `.herdr-test/<topic>/` (gitignored), created and removed by a fixture, following `tests/test_workflow_guard.py` (real git repos, real subprocesses, real settings under a temp HOME). Other tests may use `tmp_path`.

Test layers per plan: unit, integration (real git, real processes), smoke (one real CLI invocation), E2E (real Herdr panes where a Herdr is available, else the `tests/e2e/harness.py` pattern). CI is Woodpecker (`.woodpecker/unit-gates.yml`); no tokens.
