# /summary Snapshot History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Whole-session coverage in the report and a kept, plotted history of snapshots.

**Architecture:** `session_history` adds an outline of the elided middle, full-list counts and the previous snapshot. `summary_snapshots.py` owns the private store and the point computation. `publish` records a point per run, passes it to the dashboard, and the terminal viewer and the dashboard card draw it.

**Spec:** `docs/superpowers/specs/2026-11-21-summary-snapshot-history-design.md`

## Global Constraints

- Standard library only; numbers computed by code; redaction through `control_plane.redact`; store mode 0600, bounded; `summary history` read-only; failure soft; config is YAML (there is no new config).

## Review Focus

1. A store that is corrupt, oversized, world-readable or from a newer version: ignored or rewritten, never fatal.
2. `summary history` leaves the cache directory byte-identical.
3. Plot at widths 0, 12, 30, 200; one snapshot; 300 snapshots.
4. Dashboard: a hostile `snapshot` body cannot inject keys or non-numbers.

### Task 1: Outline, counts and previous snapshot in the history
- Files: `herdr_engineering/session_history.py`, `tests/test_session_history.py`
- [ ] Failing tests: outline covers the omitted middle, empty when nothing is omitted, bounded to 20 chunks with failed-tool counts; `counts` independent of the trim; `previous_snapshot` is `None` without a store and reflects the newest snapshot with one.
- [ ] Implement; run `pytest tests/test_session_history.py`.

### Task 2: The snapshot store and point
- Files: create `herdr_engineering/summary_snapshots.py`, `tests/test_summary_snapshots.py`
- [ ] Failing tests: `snapshot_point` from a history and graph (nulls stay null, coverage from history); `record` writes 0600, redacts report text, keeps report only on the newest 5, caps at 200 keeping the first, survives a corrupt file; `clean_point` rejects extra keys, strings for numbers, bools.
- [ ] Implement; run the tests.

### Task 3: Publish records a snapshot
- Files: `herdr_engineering/summary_report.py`, `tests/test_summary_report.py`
- [ ] Failing tests: two publishes keep two snapshots; the dashboard post carries `snapshot`; a failing history records none and still publishes; `summary history` does not write.
- [ ] Implement; run `pytest tests/test_summary_report.py tests/test_summary_report_herdr.py`.

### Task 4: Terminal plot
- Files: create `herdr_engineering/snapshot_plot.py`, `tests/test_snapshot_plot.py`; modify `summary_report.view_report`
- [ ] Failing tests: widths 0/12/30/80/200 never overflow, ASCII fallback, one snapshot note, 300 snapshots, monotonic series scale, `view_report` output ends with the plot.
- [ ] Implement.

### Task 5: Dashboard
- Files: `herdr_engineering/control_plane/summary_report.py`, `tests/control_plane/test_summary_report.py`, `packages/summary-snapshot-plot.js` (+ test), `packages/summary-report-status.js`, `packages/session-workspace.js`
- [ ] Failing tests: publish stores a clean snapshot, hostile keys are dropped, `get` returns the series oldest first across publishes; node tests for the SVG model.
- [ ] Implement; run `node --test` on the touched packages.

### Task 6: Skill and docs
- Files: `herdr_engineering/skill_data/summary/SKILL.md`, `docs/operations/session-summary.md`
- [ ] Tell the skill to use `outline` and `previous_snapshot`; replace the two limitation bullets in the operations doc; run `pytest tests/test_session_summary.py`.
