# WC-001 — Herdr Unified Work Navigation & Progress

Status: **implementation starter, not integrated or deployed**. Source review: berlinguyinca/herdr-engineering, main, 2026-11-04. The executable pure projection model is a starting reference and must be adapted to the actual API and reused helpers; never replace the existing mission progress implementation.

## Goal
In the existing Herdr web dashboard, display a *This Week* overview of started, finished, open and paused work, show evidence-based progress, and navigate into already existing mission/session views and their relevant related panes. Do not build a competing task manager, session router, terminal or personal assistant.

## Actual code anchors
- `packages/app.js`: hash routes `/overview`, `/missions/:id`, `/herd/session/:id`, `/github/projects`, `/handoffs`, `/llm/session/:id`.
- `packages/overview-view.js`: existing aggregate overview; extend in place.
- `packages/mission-catalog-view.js`: `measuredProgress(activity)` with acceptanceCoverage then weighted_dag; reuse *exactly*.
- `packages/sessions-view.js`: mission association via mission_key / session membership; avoid double-counting.
- `packages/session-workspace.js`: existing conversation/activity panes, session related links.
- `packages/entity-links.js`: canonical route builder; never build links from untrusted arbitrary strings.
- `packages/related-panel.js`: mission/session/PR/CI cross links.
- `packages/github-projects-view.js`: read-only GraphQL project board, external item links; only add Herdr links for verified mapping.
- `packages/attention-view.js`: existing acknowledge/snooze and internal navigation.
- `packages/terminal-attach.js`: copyable server-built attach command only; browser cannot focus arbitrary native terminal pane.
- `herdr_engineering/github.py`, `control_plane/github_store.py`: read-only Projects v2 catalog and durable store.
- `control_plane/intel_bridge.py`: existing durable event store, suitable for later history.
- `control_plane/rakazo_tickets.py`: per-space ticket board, not a replacement canonical ledger.

## Strict scope
First slice is read-only: no GitHub writes, auto agent restart, pane spawning, new task DB or scheduling. Work item identity: mission when session belongs unambiguously to one mission; otherwise session, plus GitHub project link only with verified mapping. Explicit unconfirmed and partial-data states. Existing navigation remains authoritative.

## Package contents
- `specs/`: six tiny work specs in order
- `reference/`: data contract, UI/UX, audit, deployment checks
- `implementation/work-center-model.mjs`: tested reference projection, **not drop-in production integration**
- `tests/work-center-model.test.mjs`: node tests (run `node --test tests/*.test.mjs`)
- `ONE_SHOT_PROMPT.md`: copy/paste prompt for Pi/Claude agent running on actual Herdr checkout

Do not copy reference projection blindly into the dashboard; first identify existing shared functions and data shapes and minimize duplication.
