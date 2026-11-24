# Segment Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Show time worked and fresh/cached tokens per drift-graph segment, plus a total and the cost of the drift.
**Architecture:** Normalizers emit per-harness usage rows into `meta`; `read_history` exposes `activity_ts` and `usage`;
`drift_graph.build_graph` computes metrics per segment; `graph_render` formats them; `summary_report` passes the data through.
**Tech Stack:** Python stdlib, pytest. **Spec:** `docs/superpowers/specs/2026-11-04-segment-metrics-design.md`

## Global Constraints
Spec decisions verbatim: idle gap 1800 s; fresh = new input + output, cached separate; no estimates; None when unknowable;
no line wider than the pane; soft failure (a metrics error must never lose the graph).

## Review Focus
- Codex running totals must not be summed (use `last_token_usage`); repeated identical `token_count` events counted once.
- Claude repeated usage across content-block records counted once (message id).
- Session with no usage rows: no token lines, no `0`.
- Records with missing/naive timestamps: not attributed, no crash; midnight/overnight gap dropped as idle.
- Very narrow pane and ASCII mode keep metrics inside the width.

### Task 1: Usage rows from each harness
**Files:** Modify `herdr_engineering/session_history.py`; Test `tests/test_session_history.py`
**Produces:** `meta["usage"]: list[{"ts": str|None, "fresh": int, "cached": int}]` from `_claude/_pi/_codex/_opencode`;
`read_history` returns `usage` and `activity_ts` (all events' `normalize_ts(ts)`, before trimming).
- [ ] Failing tests with one fixture per harness (use the real field names in the spec; Codex fixture has two `token_count`
  events with growing totals plus one exact repeat; Claude fixture repeats one message id on two records).
- [ ] Run: expect FAIL (no `usage` key). Implement. Run: PASS. Commit.

### Task 2: Metrics in `build_graph`
**Files:** Modify `herdr_engineering/drift_graph.py`; Test `tests/test_drift_graph.py`
**Produces:** `build_graph(raw, prompts, total, dispatch_ts=None, activity_ts=None, usage=None)`; segments gain
`worked_s, span_s, fresh, cached`; graph gains `totals`; `drift` gains `worked_s, fresh, cached` (from drift point to end).
Helper `worked_seconds(timestamps, lo, hi, idle=IDLE_GAP_SECONDS)`.
- [ ] Failing tests: 40 min of events then a 10 h gap then 20 min → worked 60 min, span 10 h 20 m; usage split at the
  boundary; no usage → `fresh is None`; unsorted/None timestamps ignored; boundary without time → `None`.
- [ ] Fail, implement, pass, commit.

### Task 3: Rendering
**Files:** Modify `herdr_engineering/graph_render.py`; Test `tests/test_graph_render.py`
**Produces:** `format_duration(seconds)->str`, `format_tokens(n)->str`; `_meta` adds `42m worked · 3h span` and
`180k fresh · 3.1M cached` lines only when known; total line after the boxes; drift line gains cost text.
- [ ] Failing tests: formatting table (`<1m`, `42m`, `1h05m`, `999`, `180k`, `3.1M`); every width 4–200 in unicode and
  ASCII keeps all lines ≤ width; no-usage graph has no token text and no `0 fresh`.
- [ ] Fail, implement, pass, commit.

### Task 4: Wire-through, docs, check
**Files:** Modify `herdr_engineering/summary_report.py` (`_build_graph` passes `activity_ts`, `usage`; the sidecar stays
0600); `skills/summary/SKILL.md` (model still gives labels only); `docs/operations/session-summary.md`; `CHANGELOG.md`;
Test `tests/test_summary_report.py`.
- [ ] Failing test: `publish_report` with a history carrying usage writes a sidecar with metrics; a history without them
  still publishes the graph. Fail, implement, pass.
- [ ] Run affected suites, then the full suite once (log to file); commit; open PR stacked on #619.
