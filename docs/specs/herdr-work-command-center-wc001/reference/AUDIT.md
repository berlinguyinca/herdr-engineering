# Repository evidence / gaps (reviewed 2026-11-04)

VERIFIED IN SOURCE:
- `app.js` hash-routed web SPA; detail views and subsection navigation.
- `entity-links.js` standardized internal links; `related-panel.js` links mission/agent/CI/review/PR/LLM.
- `mission-catalog-view.js` measured progress and animation.
- `overview-view.js` existing mission/agent/CI/review/service/host/attention overview.
- `session-workspace.js` conversation/activity UI with related agent links.
- `github-projects-view.js` existing board/list and read-only project catalog.
- `terminal-attach.js` / operations doc: command output for native pane, no browser terminal focus.
- `assistant.py` + Rakazo UI / hub; future coaching should reuse.

NOT VERIFIED:
- Live production deployment and feature flags.
- That all panes report stable, current IDs; source doc states IDs can change on pane moves.
- Complete historical weekly work accounting; current overview uses present observations.
- GitHub Projects write authorization (not needed in WC-001).
- Live repo test suite (remote clone unavailable in artifact runtime).

BUILD RULE: inspect current main AND open PRs before touching files; prefer existing functions to package's reference code. Do not overwrite concurrent modifications. Run actual repo tests, commit one spec per branch/PR, report failed gates clearly.
