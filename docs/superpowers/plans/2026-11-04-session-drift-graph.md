# Session Drift Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a node-and-arrow drift graph in a pane under the `/summary` pane, harness-neutral, correct under resize, closing together with the summary pane.

**Architecture:** The history layer exposes every real user prompt. The summarizing model proposes labelled segments in an optional `drift-graph` block; `publish` validates it against the real prompts, computes times and counts, writes a sidecar JSON, and strips the block from the report. Two small viewers (summary, graph) share one terminal loop that reflows on SIGWINCH and closes its pane and its peer on exit.

**Tech Stack:** Python 3.12+ standard library only (`signal`, `termios`, `select`, `os`), pytest, existing `herdr` CLI.

**Spec:** `docs/superpowers/specs/2026-11-04-session-drift-graph-design.md`

## Global Constraints

- Standard library only (as `md_render.py`).
- The graph path reads session data only through `session_history.read_history`; never a harness transcript.
- Failure is soft: any problem with the block, sidecar or graph pane leaves the summary published exactly as before.
- Segments 2 to 8; label at most 40 characters; drift note at most 120; prompt text clipped to 120; fewer than 5 real prompts means no graph.
- Text of at most 400 prompts is shown to the model (first 50, last 50, evenly spaced between); every prompt keeps `n` and `ts`.
- Graph width floor 30 columns; below it a numbered list. `NO_COLOR` honoured; ASCII glyphs when the locale is not UTF-8.
- Graph pane is split **down** from the summary pane at about 30% height with `--no-focus`; closing either pane closes both.
- Sidecar is `<report>.graph.json` next to the cached report file. The `drift-graph` block never reaches the stored report, the dashboard or a viewer.
- Prerequisite: PR #597 (`md_render.py`, `view_report`) is merged. If it is not, rebase this branch on `feat/summary-pane-render` first.

## Review Focus

1. Malformed, duplicated or oversized `drift-graph` block: the block is still stripped (nobody sees raw JSON), the graph is dropped, publish succeeds.
2. Prompt timestamps missing, ISO, or OpenCode epoch milliseconds: normalised; a missing time is drawn blank, never guessed.
3. Widths 0, 12, 29, 30 and 200, and wide (CJK) labels: no line exceeds the width, no exception.
4. Non-UTF-8 locale: ASCII glyphs, no mojibake.
5. Peer pane already gone, or a burst of resizes: no error, exit 0, no stale screen.

---

### Task 0: Base the branch on the viewer work

**Files:** none.

- [ ] **Step 1:** `git fetch origin`. If `gh pr view 597 --repo berlinguyinca/herdr-engineering --json state` says MERGED, run `git rebase origin/main`; otherwise `git rebase feat/summary-pane-render`.
- [ ] **Step 2:** Run `python3 -m pytest tests/test_md_render.py tests/test_summary_report.py -q`. Expected: all pass (confirms `view_report` and `md_render` are present).

### Task 1: Real prompts in the history

**Files:**
- Modify: `herdr_engineering/session_history.py` (add functions; `read_history` return dict)
- Test: `tests/test_session_history.py`

**Interfaces:**
- Produces: `is_human_prompt(text: str) -> bool`; `normalize_ts(value: Any) -> str | None` (UTC `YYYY-MM-DDTHH:MM:SSZ`); `build_prompts(events: list[dict], *, text_limit: int = 400) -> tuple[list[dict], int]` returning prompts `{"n": int, "ts": str|None, "text": str}` (`text` omitted when not sampled; at most 120 characters) and the true total. `read_history` result gains `"prompts"` and `"prompts_total"`, and `"dispatch_ts": list[str|None]` (timestamps of `child_dispatch` events).

- [ ] **Step 1: Failing tests.** `test_is_human_prompt_rejects_injected_wrappers`: false for `""`, `"<task-notification>x"`, `"<system-reminder>x"`, `"<environment_context>x"`, `"<command-name>/x"`, `"<local-command-stdout>"`; true for `"fix the usb stick script"` and for text that merely contains `<` later. `test_normalize_ts`: ISO with `Z` and with `+00:00`, `1767225600000` (ms) and `1767225600` (s) all give `2026-01-01T00:00:00Z`; `None`, `""`, `"garbage"` give `None`. `test_prompts_numbered_among_real_prompts_only` and `test_prompts_survive_the_event_trim` (build a long session with `max_events=50`; assert `prompts_total` equals the real count and every `n` is present). `test_prompts_text_sampling`: 500 prompts gives text on exactly 400 (first 50, last 50), all 500 keep `n`/`ts`. One `test_prompts_per_harness` case each for the existing claude, pi, codex and opencode fixtures in this file asserting the same human prompt list.
- [ ] **Step 2:** Run `python3 -m pytest tests/test_session_history.py -q -k "prompt or normalize"`. Expected: FAIL (names undefined).
- [ ] **Step 3: Implement** the functions in `session_history.py`. Wrapper prefixes: `<task-notification`, `<system-reminder`, `<environment_context`, `<command-`, `<local-command`, `<pasted_content`. Epoch values above `1e11` are milliseconds. Number prompts from 1 over events of kind `user` accepted by `is_human_prompt`, computed on the full event list before the trim in `read_history`. Text is `_clip`ped, then cut to 120.
- [ ] **Step 4:** Rerun Step 2 plus the whole file. Expected: PASS.
- [ ] **Step 5:** Commit `feat(history): expose every real user prompt, untrimmed`.

### Task 2: Block extraction and validation

**Files:**
- Create: `herdr_engineering/drift_graph.py`
- Test: `tests/test_drift_graph.py`

**Interfaces:**
- Consumes: Task 1 prompt dicts and `dispatch_ts`.
- Produces: `extract_block(report: str) -> tuple[str, str | None]` (report with every `` ```drift-graph `` fence removed and surplus blank lines collapsed; raw JSON of the first block or `None`); `build_graph(raw: str | None, prompts: list[dict], total: int, dispatch_ts: list[str | None] | None = None) -> dict | None` returning `{"total_prompts": int, "segments": [{"label": str, "from_prompt": int, "to_prompt": int, "count": int, "start": str | None, "agents": int | None}], "drift": {"at": int, "note": str} | None}`. Constants `MIN_PROMPTS=5`, `MIN_SEGMENTS=2`, `MAX_SEGMENTS=8`, `MAX_LABEL=40`, `MAX_NOTE=120`.

- [ ] **Step 1: Failing tests.** `test_extract_block_strips_all_blocks_and_returns_first`; `test_report_without_block_is_unchanged`. For `build_graph`, one test each returning `None`: invalid JSON, non-list segments, 1 segment, 9 segments, gap between segments, not starting at 1, `to_prompt` above `total`, `total` of 4. `test_valid_graph_computes_counts_and_start_times_from_prompts` (model supplies no times; `count == to - from + 1`; `start` is the `ts` of `from_prompt`; `None` when that prompt has no `ts`). `test_drift_on_first_segment_is_dropped_but_segments_kept`; `test_drift_must_be_a_segment_start`. `test_labels_and_note_are_redacted_and_clipped` (a token-shaped string is redacted by `redact_text`; label cut to 40 with an ellipsis). `test_agents_counts_dispatches_inside_each_segment_and_is_none_without_dispatches`.
- [ ] **Step 2:** Run `python3 -m pytest tests/test_drift_graph.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement.** Regex for the fence is line-anchored. `agents` per segment is the number of `dispatch_ts` at or after the segment's `start` and before the next segment's `start`; every segment's `agents` is `None` when `dispatch_ts` is empty or `None`. Never raise; return `None` on any invalid input.
- [ ] **Step 4:** Rerun. Expected: PASS.
- [ ] **Step 5:** Commit `feat(summary): validate the drift-graph block against the real prompts`.

### Task 3: Graph renderer

**Files:**
- Create: `herdr_engineering/graph_render.py`
- Test: `tests/test_graph_render.py`

**Interfaces:**
- Consumes: the graph dict from Task 2.
- Produces: `render_graph(graph: dict, width: int, *, unicode: bool = True, color: bool = True) -> list[str]`; every returned line has `visible_width <= width` (use `md_render.visible_width`). `width < 30` returns a numbered list, one segment per line, plus the drift note.

- [ ] **Step 1: Failing tests.** `test_every_line_fits_every_width` over widths `0, 1, 12, 29, 30, 31, 40, 60, 80, 110, 200` and graphs of 2, 5 and 8 segments (with a CJK label and a 40-character label); assert no line exceeds `max(width, 0)` and no exception. `test_wide_width_is_one_row_and_narrow_width_is_a_column` (arrow glyph `▶` joins boxes in the row; `▼` in the column). `test_box_shows_label_start_time_and_prompt_count` (`10:55 · 14 prompts`; time omitted when `start` is `None`). `test_drift_marker_sits_under_its_segment_and_is_worded_as_a_judgement` (the marker line says it "looks like a new task" and shows the note; it never states drift as fact). `test_no_drift_says_no_drift_found`. `test_ascii_fallback_has_only_ascii` (`unicode=False`). `test_color_off_has_no_escape_codes`. `test_agent_badge_only_when_known` (`agents=2` shows `2 agents`; `None` shows nothing).
- [ ] **Step 2:** Run `python3 -m pytest tests/test_graph_render.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Box inner width is the longest wrapped label line (wrap at 18 columns in row mode, up to `width - 6` in column mode); pick row mode only when the sum of box widths plus 3 columns per arrow fits `width`. Truncate with an ellipsis rather than overflow. Local time is shown as `HH:MM` of the `start` converted to the local zone; a date line is added when segments span days.
- [ ] **Step 4:** Rerun. Expected: PASS.
- [ ] **Step 5:** Commit `feat(summary): draw the drift graph with line characters`.

### Task 4: One resize-aware terminal loop

**Files:**
- Create: `herdr_engineering/pane_viewer.py`
- Test: `tests/test_pane_viewer.py`

**Interfaces:**
- Produces: `run_viewer(render: Callable[[int, int], list[str]], *, scroll: bool, title: str = "", fd_in: int = 0, out=None, size: Callable[[], tuple[int, int]] | None = None) -> int`. `render(width, height)` returns the document lines for that size. The loop switches to the alternate screen, hides the cursor, reads keys with `termios` cbreak and `select`, redraws on SIGWINCH (a self-pipe wakes `select`), and restores the terminal in `finally`. Keys: `q`/`Q`/Ctrl-C quit; `j`, Down, Enter scroll one line; `k`, Up one back; Space, PgDn, `f` one page; `b`, PgUp one page back; `g`/Home top; `G`/End bottom. Scroll position is kept as a fraction of the document so a reflow keeps the reader's place. Returns 0 on `q`, 1 on error. A footer line shows the key help (`q closes this pane`) and the percentage.

- [ ] **Step 1: Failing tests (pty).** Using `pty.openpty()` and `fcntl` `TIOCSWINSZ`: `test_draws_to_the_current_size_and_quits_on_q`; `test_resize_redraws_at_the_new_width` (start 80x24, resize to 40x10, send `os.kill(pid, SIGWINCH)`, assert the next frame's rendered lines were requested with `(40, 10)` and no output line exceeds 40 columns); `test_resize_keeps_reading_place` (scroll to 50%, resize, assert the new top line is within one page of 50% of the new document); `test_terminal_is_restored_on_exit_and_on_error` (alternate screen and cursor-show sequences present, termios attributes equal the originals); `test_resize_storm_of_20_signals_ends_with_one_correct_frame`; `test_non_scrolling_document_ignores_scroll_keys`.
- [ ] **Step 2:** Run `python3 -m pytest tests/test_pane_viewer.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `run_viewer`. Query size with `os.get_terminal_size(fd_out)` unless `size` is given; lines longer than the width are never written (clip by `visible_width`).
- [ ] **Step 4:** Rerun. Expected: PASS.
- [ ] **Step 5:** Commit `feat(summary): a small pager that reflows on resize`.

### Task 5: Viewers, peer closing, CLI

**Files:**
- Modify: `herdr_engineering/summary_report.py` (`view_report`, new `view_graph`), `herdr_engineering/cli.py` (`summary view --peer`, new `summary graph`)
- Test: `tests/test_summary_report.py`

**Interfaces:**
- Consumes: `run_viewer`, `render_markdown`, `render_graph`.
- Produces: `view_report(file, *, close_pane=None, peer_pane=None, ...)` now renders through `run_viewer(lambda w, h: render_markdown(text, width=w).split("\n"), scroll=True)`; `less` is dropped. `view_graph(file, *, close_pane=None, peer_pane=None, run=subprocess.run, out=None, herdr="herdr") -> int` reads the sidecar JSON and renders it through `run_viewer(..., scroll=False)`. In `finally` both close `close_pane` then `peer_pane`, ignoring a missing pane. CLI: `herdr-eng summary graph FILE [--close-pane ID] [--peer ID]`; `summary view` gains `--peer ID`. Without a terminal both print the rendered output and exit 0. Both viewers pass `color=False` when `NO_COLOR` is set and `unicode=False` when the output encoding is not UTF-8 (`sys.stdout.encoding`); the pager loop strips escape codes when `color` is false.

- [ ] **Step 1: Failing tests.** Update the existing `view` tests that assumed `less` (they inject `which` and `run`) to assert the new loop is used and the close calls happen. Add `test_graph_viewer_closes_itself_then_its_peer`, `test_peer_already_gone_is_not_an_error`, `test_graph_viewer_without_sidecar_closes_and_returns_1`, `test_view_without_terminal_prints_rendered_report`, `test_cli_graph_and_view_accept_peer`, `test_viewers_honour_no_color_and_non_utf8_output`.
- [ ] **Step 2:** Run `python3 -m pytest tests/test_summary_report.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Remove the `less` and `input()` fallback paths added in #597; a dumb or absent terminal prints and returns.
- [ ] **Step 4:** Rerun with `tests/test_cli.py`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(summary): graph viewer, reflowing summary viewer, peer closing`.

### Task 6: Publish builds, saves and opens the graph

**Files:**
- Modify: `herdr_engineering/summary_report.py` (`publish_report`, `_open_side_pane`, new `_open_graph_pane`)
- Test: `tests/test_summary_report.py`

**Interfaces:**
- Consumes: `extract_block`, `build_graph`, `sh.resolve`, `sh.read_history`.
- Produces: `publish_report` result gains `"graph": {"ok": bool, "pane_id"?: str, "error"?: str, "skipped"?: True}`. Order (the graph is built after the summary pane is open, so it can never delay it): validate the report, extract and strip the block, write the stripped report, post it to the dashboard, open the summary pane; only then load history for `(agent, session_id)`, build the graph, write the sidecar `<file>.graph.json` (mode 0600, removed when there is no valid graph) and split the graph pane down from the summary pane. The two panes are started with each other's id as `--peer`; their ids are recorded in sidecars so a re-publish closes any live pair first. `--no-pane` skips both. The result `message` is unchanged.

- [ ] **Step 1: Failing tests** (use `_fake_herdr` and injected history). `test_publish_strips_the_block_before_storing_posting_and_rendering`; `test_publish_writes_sidecar_and_opens_graph_pane_below` (the split call has `--direction down`, `--pane <summary pane>`, `--no-focus`, and the run commands carry each other's ids); `test_invalid_block_still_publishes_without_graph_pane`; `test_history_failure_is_soft` (history raises: publish succeeds, `graph.ok` false); `test_republish_closes_both_old_panes_first`; `test_no_pane_flag_skips_the_graph_pane`; `test_fewer_than_five_prompts_opens_no_graph_pane`; `test_sidecar_removed_when_a_later_publish_has_no_graph`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3: Implement.** History access must be injectable (`history=None` parameter defaulting to a function that calls `sh.resolve(agent=agent, session=session_id)` then `sh.read_history`).
- [ ] **Step 4:** Rerun. Expected: PASS.
- [ ] **Step 5:** Commit `feat(summary): publish builds the drift graph and opens its pane`.

### Task 7: Skill text, docs, changelog

**Files:**
- Modify: `skills/summary/SKILL.md`, `docs/operations/session-summary.md`, `CHANGELOG.md`
- Test: `tests/test_session_summary.py`

- [ ] **Step 1: Failing test** `test_skill_asks_for_the_optional_drift_graph_block`: the installed skill text mentions `drift-graph`, says the block is optional, says to use the `prompts` from the history helper, and says labels only (no times or counts).
- [ ] **Step 2:** Run `python3 -m pytest tests/test_session_summary.py -q`. Expected: FAIL.
- [ ] **Step 3: Edit** `SKILL.md`: under **Required output**, add an optional final fenced block `drift-graph` with JSON `{"segments":[{"label","from_prompt","to_prompt"}],"drift_at":N,"drift_note":"..."}`, built from the history's `prompts` (group consecutive prompts by topic, 2 to 8 segments, labels at most 40 characters, `drift_at` only where the topic really changed to something that deserved its own session, omit the block when the session never drifted or has fewer than 5 prompts). Document the panes, sidecar, keys and resize behaviour in `docs/operations/session-summary.md`; add a CHANGELOG entry.
- [ ] **Step 4:** Rerun plus `tests/test_summary_model_deploy.py`. Expected: PASS.
- [ ] **Step 5:** Commit `docs(summary): ask for the drift-graph block; document the panes`.

### Task 8: Real-pane verification and install

**Files:**
- Create: `scripts/check_summary_panes.sh`

- [ ] **Step 1:** Write the script: publish a fixture report with a `drift-graph` block from inside a Herdr pane, `herdr pane resize` the panes through three sizes, `herdr pane read --source visible` after each and fail if any line exceeds the pane width, send `q` to the summary pane and assert both panes are gone.
- [ ] **Step 2:** Run it in a real Herdr pane. Expected: exit 0 and the final message `both panes closed`.
- [ ] **Step 3:** Run the full gates the repo uses (`python3 -m pytest tests -q -x`, `ruff check` on touched files) and open the PR to `main`; do not merge on a pending check.
- [ ] **Step 4:** After merge, patch the installed `herdr-eng` the way the viewer work was installed (back up first) and re-run Step 2 against it.
