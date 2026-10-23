# Agent Summaries and Editor Implementation Plan

> **For agentic workers:** Use the existing test-first execution workflow on each owned slice; do not revert work by other agents.

**Goal:** Persistent readable Agent names, automatic grounded continuation summaries and shared fullscreen editing.

**Architecture:** Existing event persistence plus a bounded background model client; shared dependency-free editor/renderers and compatible session routes.

**Tech Stack:** Python control plane, ES modules/Web Components, OpenAI-compatible self-hosted inference, pytest, node:test and Playwright.

**Spec:** ../specs/2026-10-22-agent-summaries-editor.md

## Global constraints

- Preserve native sessions and source IDs; no unsolicited agent restarts or prompts.
- No hidden reasoning/system/developer content enters summary inputs.
- Inference does not run in request handlers or store locks.
- No new frontend dependencies; model infrastructure is isolated and bounded.
- No remote workspace-file writes without resolving the pending scope question.

## Tasks

- [x] Implement/test persistent unique naming registry; integrate session reads, name collisions and rename errors.
- [x] Implement/test validated summary model client, coverage/source fingerprints, persistence, cache/debounce and bounded worker scheduling.
- [x] Integrate summary/name API and configuration/lifecycle without breaking session endpoints or existing deployments.
- [x] Expand shared editor and safe Markdown/JSON rendering with browser regressions.
- [x] Rename user-facing Sessions labels, integrate agent names/summary card and workspace fullscreen.
- [x] Provision an internal small-model CPU candidate, benchmark synthetic summary latency and quality, and configure only the verified endpoint.
- [x] Run targeted tests then full relevant checks, independent review, PR/merge/deployment and live evidence using the previously authorized delivery workflow.

## Interfaces and regression contracts

- Identity: `AgentIdentityRegistry(persist=callback)` exposes `allocate(identity, preferred_name=None)`, `rename(identity,name,expected_version)` and `hydrate(events)`. Profiles contain display_name, handle, identity and version. Global Unicode-normalized name reservations are locked; failed persistence publishes nothing. Test late provider updates, collisions/concurrent rename, restore and stale versions.
- Summary: `AgentSummaryService(repo, config, persist=callback, model_client=...)` exposes `get(session_id)`, `request(session_id, force=False)`, `start()`, `close()`, `hydrate(events)`, and a bounded `run_once()` for tests. `summary_source` uses viewer-safe full message bodies, stable content-based IDs and explicit coverage. Test tool-only updates do not change cache keys, source/conversation replacement invalidates results, source expiry preserves prior coverage, invalid citations/JSON are rejected and no private source enters prompts.
- Persistence: root integration owns acknowledged `agent_identity`/`agent_summary` records and replay, kept out of generic public event feeds. Pure worker modules must not independently edit repo.py/api.py.
- Editor: reuse the same textarea, draft and attachments across fullscreen/preview. Test undo after JSON formatting and replace, IME, completion Escape priority, selection/focus restoration, bounded JSON tree, malicious keys/HTML and clipboard failures.
- Model endpoint must be explicit, not a fallback alias. CPU artifact and image checksums are pinned as in the spec; warm latency evidence precedes production enablement.

Release evidence: PR #148 merged and deployed as `a203cb77781c`; 295 unique Agent names restored unchanged after restart, and real continuation summaries persisted. CPU timeout was raised to 90 seconds after full-window measurements. Retained-conversation paging received the separately tested follow-up described in the spec.
