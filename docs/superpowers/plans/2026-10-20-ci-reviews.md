# Native CI and Reviews Implementation Plan

**Goal:** Connect actual Woodpecker execution and Plannotator reviews to HerdR's existing web workspace.
**Architecture:** A bounded background CI projection and event-backed review service feed existing authenticated APIs and native frontend pages. Provider credentials and local review subprocesses stay out of browser code.
**Spec:** ../specs/2026-10-20-ci-reviews-design.md
**Tech Stack:** Python stdlib, existing PostgreSQL event sink, JavaScript modules, existing Playwright.

## Global constraints
No new dependencies. No fixed service ports or port-derived identity. No secrets in logs/browser output. No fake actions, approvals, percent progress or inferred completion. Existing upstreams retain authority.

- [x] Provider: first add failing contract tests for user repository discovery, denied auth, bounded logs and correct POST action routes; implement and run focused tests.
- [x] CI projection/API: test first-request nonblocking sync, single-flight concurrency, retained failures, progress, detail validation and action role/opt-in; implement bounded snapshots and wire server lifecycle.
- [x] Reviews: verify official CLI ready/result contract, then test durable registration/result replay and revision binding; implement event-backed service and host launcher without arbitrary web subprocess execution.
- [x] Frontend: add CI/Reviews using existing navigation/styles/tabs; test empty/error and grouped outcome states, detail/log navigation, contextual links and polling cleanup.
- [x] Integrate: run focused and full Python/lint/client/browser checks; review security and native presentation; correct acceptance docs and document secret setup. Validate live read-only CI and actual review launcher where credentials/upstream permit.
