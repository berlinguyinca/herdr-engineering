# Shepherd Agents Tab (read side) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An Agents tab in the Shepherd app that lists every agent session by what needs the owner, and opens any one as a live, readable conversation.

**Architecture:** Server: a new `rakazo_agents.py` module adds `agents/*` procedures to the Rakazo bridge, reading the control plane's session projection and inspector store (already redacted) and streaming over the bridge's existing SSE style. App: a bottom tab bar on the two home routes, a new `/agents` list and `/agent` conversation, reusing the existing wide-window `SplitFrame` for iPad.

**Tech Stack:** Python 3.12 (`herdr_engineering/control_plane`, pytest); TypeScript, Expo / React Native, expo-router, zod/oRPC contract (`packages/contracts/src/rpc.ts`), vitest.

**Spec:** `docs/superpowers/specs/2026-11-01-shepherd-agents-tab-design.md` (in `herdr-engineering`). Server tasks (1-5) run in `herdr-engineering`; app tasks (6-10) in the fork `berlinguyinca/herdr-shepherd`. Two PRs, server first.

## Global Constraints

- Owner-only, tailnet, owner session already opens without a password: add no new gates. Piece 1 is read-only; nothing in it writes to an agent.
- The server returns only data that already passed the inspector's per-field redaction and `redact_text` / `redact_payload` (`herdr_engineering/control_plane/redact.py`). No new redaction logic. Inspector reads use `role="viewer"` (sensitive output stays withheld on the phone; my call).
- Status vocabulary: herdr `idle`, `done`, `working`, `blocked`, `unknown`. "Waiting for input" = `done`. Closed = the closed set in `terminal_attach.py` (`closed, complete, completed, finished, failed, error, stopped, cancelled, canceled, exited, dead, terminated`).
- List order: `blocked`, `done`, `working`, `idle`, `unknown`, closed; newest first within a group. Default filter hides closed sessions and sessions idle for more than 7 days; "Show older" reveals them.
- One list row = one session record, named through `repo._named_session`. A session with no inspector detail still appears; its conversation shows "No conversation was captured for this agent."
- The list answers from in-memory projections, never the database; first screen target under one second on 550 sessions.
- Live updates: SSE as `threads/subscribe` does (`accept: text/event-stream`, integer cursor >= -1, `ev.event_stream`, stream cap via `self._streams`, 429 `Retry-After: 10`). The list stream falls back to polling every 5 seconds when refused or disconnected.
- App strings go through `t()`; every new string needs an entry in `apps/mobile/lib/locales/{de,es,ja,nl,ru,zh,zh-TW}.ts` (an existing test fails otherwise); translations are supplied by the builder.
- Assistant tab behaviour, its connections and authorization are unchanged. Existing stack routes and deep links keep working.
- Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

- A session with thousands of events: `agents/messages` is capped (limit <= 200) and pages by `before_seq`; the app never asks for everything.
- A tool call with huge output: rows carry at most 2,000 characters (the inspector already truncates and sets `*_more`); the full text comes only from `agents/event`.
- A session that closes or vanishes between list and open: `agents/get` / `agents/messages` answer 404 `NOT_FOUND`; the app shows "This agent is no longer available" and returns to the list, not a crash.
- Sensitive or redacted content in the one-line summary: the summary never contains withheld text; a withheld last message falls back to the status line.
- Stream cap reached (429) or the connection drops: the list and the conversation fall back to polling and recover; no error toast loop.
- An unknown or missing status string sorts as `unknown`, not first and not last by accident.

---

## Server (herdr-engineering)

### Task 1: Row model, status mapping and ordering

**Files:**
- Create: `herdr_engineering/control_plane/rakazo_agents.py`
- Test: `tests/control_plane/test_rakazo_agents_rows.py`

**Interfaces:**
- Produces: `STATUS_ORDER: tuple[str, ...]` (`("blocked","done","working","idle","unknown","closed")`); `def agent_status(session: dict) -> str` (returns one of `STATUS_ORDER`); `def agent_row(session: dict, summary: str | None) -> dict` returning `{"id": session_id, "name": str, "machine": str | None, "status": str, "summary": str, "updated_at": str, "has_conversation": bool}`; `def sort_rows(rows: list[dict]) -> list[dict]`; `def visible(rows: list[dict], *, show_older: bool, now: float) -> list[dict]`.

- [ ] **Step 1: Write failing tests** `test_agent_status_maps_herdr_values_and_closed_set`, `test_unknown_or_missing_status_is_unknown`, `test_sort_blocked_first_then_done_working_idle_unknown_closed_newest_first_within_group`, `test_visible_hides_closed_and_week_old_idle_unless_show_older`, `test_summary_falls_back_to_status_line_when_none`. Use the spec's exact order and the 7-day cutoff (`7 * 86400` seconds).
- [ ] **Step 2: Run** `pytest tests/control_plane/test_rakazo_agents_rows.py -q`; expect FAIL (module missing).
- [ ] **Step 3: Implement** the four functions above in `rakazo_agents.py`; import the closed set from `terminal_attach._CLOSED`; pure functions, no I/O.
- [ ] **Step 4: Run tests; expect PASS.**
- [ ] **Step 5: Commit** `Agents: row model, status mapping and ordering`.

### Task 2: Inspector read helpers for the phone

**Files:**
- Modify: `herdr_engineering/control_plane/inspector.py` (add two methods beside `query`, near line 880)
- Test: `tests/control_plane/test_inspector.py`

**Interfaces:**
- Produces: `InspectorStore.events_after(session_id: str, after_seq: int, role: str = "viewer", limit: int = 200) -> list[dict]` (ascending by `seq`, rendered with `_render(..., full=False)`; `KeyError` for an unknown session); `InspectorStore.last_message(session_id: str, role: str = "viewer") -> str | None` (the text of the newest `message` event with role `assistant` that is allowed for `role`, else None; truncated to 160 chars after `redact_text`).

- [ ] **Step 1: Failing tests** `test_events_after_returns_only_newer_events_ascending_and_respects_limit`, `test_events_after_withholds_sensitive_text_for_viewer`, `test_events_after_unknown_session_raises_keyerror`, `test_last_message_skips_withheld_and_non_assistant_messages`.
- [ ] **Step 2: Run them; expect FAIL.**
- [ ] **Step 3: Implement** both under `self._lock`, reusing `_allowed` and `_render`; clamp `limit` to 1..200 like `query`.
- [ ] **Step 4: Run `pytest tests/control_plane/test_inspector.py -q`; expect PASS (existing tests untouched).**
- [ ] **Step 5: Commit** `Inspector: events_after and last_message for the phone`.

### Task 3: The `agents/*` bridge procedures (unary)

**Files:**
- Modify: `herdr_engineering/control_plane/rakazo_agents.py` (add class), `herdr_engineering/control_plane/rakazo_bridge.py` (register after `self._procs.update(self.rooms.procs())`), `herdr_engineering/control_plane/api.py` (set the repo on the bridge where `self.rakazo` is built, next to the existing `self.rakazo.desk.routes` wiring)
- Test: `tests/control_plane/test_rakazo_agents_rpc.py`

**Interfaces:**
- Consumes: Task 1 functions; Task 2 methods.
- Produces: `class Agents` with `__init__(self, bridge, error)`, attribute `repo` (set by the API glue; `None` until then) and `def procs(self) -> dict[str, Callable[[_Ctx, dict], Any]]` returning `agents/list`, `agents/get`, `agents/messages`, `agents/event`.
  - `agents/list` input `{"showOlder": bool = false}` -> `{"agents": [row...], "hiddenCount": int, "cursor": int}` (`cursor` is the list stream's current change counter: subscribe from it).
  - `agents/get` input `{"id": str}` -> `{"agent": row, "hasConversation": bool}`; 404 `NOT_FOUND` when the session is unknown.
  - `agents/messages` input `{"id": str, "beforeSeq": int | None, "limit": int = 50 (max 200)}` -> `{"events": [rendered...], "hasMore": bool, "newestSeq": int | None, "rev": int}` newest-first like `query` (`rev` is the session's current revision: the cursor for `agents/subscribe`), 404 for an unknown session.
  - `agents/event` input `{"id": str, "seq": int}` -> the full rendered event (`full=True`), 404 when absent.
  - Without a repo (`repo is None`) every procedure answers 503 `UNAVAILABLE` "The Agents view is not ready."

- [ ] **Step 1: Failing tests** `test_list_sorts_and_hides_older_by_default`, `test_list_show_older_reveals_hidden_and_reports_hidden_count`, `test_get_unknown_agent_is_404`, `test_messages_pages_by_before_seq_and_caps_limit_at_200`, `test_event_returns_full_text_beyond_2000_chars`, `test_summary_never_contains_withheld_text`, `test_procedures_503_without_repo`, `test_requires_owner_token` (call through `bridge.handle` with no token expects 401 like other procedures). Build fixtures with a real `ControlPlaneRepo` and its `inspector` store.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement** `Agents` using `repo.list_sessions()` and `repo.inspector`; summary = the session's own summary (`session["summary"]`, redacted via `redact_text`) when present, else `repo.inspector.last_message(id)`, else the status line (the spec's order); `has_conversation` from `inspector.newest_seq(id) is not None`.
- [ ] **Step 4: Run `pytest tests/control_plane/test_rakazo_agents_rpc.py tests/control_plane/test_rakazo_bridge.py -q`; expect PASS.**
- [ ] **Step 5: Commit** `Bridge: agents/list, agents/get, agents/messages, agents/event`.

### Task 4: Live streams

**Files:**
- Modify: `herdr_engineering/control_plane/rakazo_agents.py`
- Test: `tests/control_plane/test_rakazo_agents_stream.py`

**Interfaces:**
- Consumes: Task 3 `Agents`; `ev.event_stream(fetch, cursor, should_stop=..., **bridge.stream_options)`; `bridge._streams`; `BridgeResponse`.
- Produces: procedures `agents/subscribe` (input `{"id": str, "cursor": int >= -1}`; the cursor is the session's `rev`, because the inspector updates tool events in place and bumps `rev` while `seq` stays; frames are `{"seq": <event rev>, "event": <rendered event>}` for every event whose `rev` > cursor, ascending, so a finished tool call arrives again with its output) and `agents/subscribeList` (input `{"cursor": int >= -1}`; frames are `{"seq": <change counter>, "row": row}` for each row that changed since the cursor; `Agents` keeps a per-row fingerprint map and a monotonically increasing counter under a lock). Errors: cursor not an integer >= -1 -> 400; request without `accept: text/event-stream` -> 400; stream cap exhausted -> 429 with `Retry-After: 10`; unknown agent on `agents/subscribe` -> 404.

- [ ] **Step 1: Failing tests** `test_subscribe_streams_new_events_after_cursor`, `test_subscribe_list_emits_only_changed_rows_with_increasing_seq`, `test_subscribe_rejects_bad_cursor_and_missing_accept_header`, `test_subscribe_429_when_stream_cap_exhausted`, `test_subscribe_stops_when_token_revoked`. Shorten `bridge.stream_options` in tests (as `test_rakazo_bridge.py` does).
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement** mirroring `_threads_subscribe` (acquire `self._streams` non-blocking, release when the generator closes, `should_stop` checks `bridge._closed` and `bridge.tokens.valid(ctx.token)`).
- [ ] **Step 4: Run the new tests and `tests/control_plane/test_rakazo_bridge.py`; expect PASS.**
- [ ] **Step 5: Commit** `Bridge: agents/subscribe and agents/subscribeList`.

### Task 5: Server PR, review and deploy

- [ ] **Step 1:** Run `pytest tests/control_plane -q -k "rakazo or inspector"` and the repo's lint; expect green.
- [ ] **Step 2:** Open a PR to `main`; wait for CI (artifact-handoff, ui-gates, unit-gates); run a code review on the diff and fix findings.
- [ ] **Step 3:** After merge, deploy to bender with `deploy/control-plane/deploy.sh --ref <sha> --no-fetch` from the clean clone (as in earlier deploys), then check `agents/list` with `curl -X POST -H 'Origin: rakazo://' https://local.metabolomics.us/api/auth/open` to get a token and `POST /rpc/agents/list`; expect a non-empty `agents` array sorted blocked-first.

---

## App (herdr-shepherd)

### Task 6: Contract and client helpers

**Files:**
- Modify: `packages/contracts/src/rpc.ts` (add an `agents` group beside `threads`), create `packages/contracts/src/agents.ts` for the schemas
- Create: `apps/mobile/lib/agents.ts`
- Test: `packages/contracts/src/agents.test.ts`, `apps/mobile/lib/agents.test.ts`

**Interfaces:**
- Produces: zod `AgentRowSchema` (`id, name, machine (nullable), status (enum of the six), summary, updatedAt, hasConversation`), `AgentEventSchema`, and procedures `agents.list/get/messages/event` matching Task 3; in `apps/mobile/lib/agents.ts`: `type AgentRow`; `function agentListQuery(showOlder: boolean)`; `function subscribeAgentList(onRow: (row: AgentRow) => void, onFallback: () => void): () => void` (SSE with cursor, calls `onFallback` on 429 / drop so the caller polls); `function subscribeAgent(id: string, rev: number, onEvent: (e: AgentEvent) => void, onFallback: () => void): () => void`; `function isAgentsUnsupported(error: unknown): boolean` (true for the bridge's unknown-procedure 404).

- [ ] **Step 1: Failing tests** `schema accepts the six statuses and rejects others`, `isAgentsUnsupported is true for an unknown-procedure error and false for network errors`, `subscribeAgentList calls onFallback on a 429`.
- [ ] **Step 2: Run `pnpm exec vitest run packages/contracts/src/agents.test.ts apps/mobile/lib/agents.test.ts`; expect FAIL.**
- [ ] **Step 3: Implement** the schemas and helpers; model the SSE code on `subscribeThread` in `apps/mobile/lib/api.ts` (reuse its reader and reconnect handling rather than duplicating it).
- [ ] **Step 4: Run tests; expect PASS; run `pnpm exec tsc --noEmit -p apps/mobile`.**
- [ ] **Step 5: Commit** `Agents: contract and client helpers`.

### Task 7: Tab bar and the Agents list

**Files:**
- Create: `apps/mobile/components/home-tab-bar.tsx`, `apps/mobile/components/agent-row.tsx`, `apps/mobile/app/agents.tsx`
- Modify: `apps/mobile/app/index.tsx` (render `HomeTabBar` with `active="assistant"`), `apps/mobile/app/_layout.tsx` (register `agents` screen, hide the bar on other routes), the seven locale files
- Test: `apps/mobile/lib/agents-screen.test.tsx`

**Interfaces:**
- Consumes: Task 6 helpers.
- Produces: `HomeTabBar({ active }: { active: "assistant" | "agents" })`; `AgentRow({ row, onPress })`; route `/agents`. Strings: "Assistant", "Agents", "Show older", "No agents yet", "This server does not offer the Agents view yet", "Needs you", status labels.

- [ ] **Step 1: Failing tests** `renders rows in the order the server sends and a Needs you badge for blocked and done`, `shows the unsupported-server state when agents.list is an unknown procedure`, `Show older refetches with showOlder true`, `falls back to polling every 5 seconds when the stream is refused`, `tapping the Agents tab from home navigates to /agents and back`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement** the screen with a windowed list (`FlatList`, `windowSize` bounded), pull-to-refresh not required; the tab bar is a bottom bar rendered only on `/` and `/agents`.
- [ ] **Step 4: Run `pnpm test` in `apps/mobile` (the i18n test must pass with the new strings translated); expect all green.**
- [ ] **Step 5: Commit** `Agents tab: bar, list, states`.

### Task 8: The agent conversation screen

**Files:**
- Create: `apps/mobile/app/agent.tsx`, `apps/mobile/components/agent-event.tsx`
- Modify: `apps/mobile/app/_layout.tsx` (register `agent`), the seven locale files
- Test: `apps/mobile/lib/agent-screen.test.tsx`

**Interfaces:**
- Consumes: Task 6 `subscribeAgent`, `agents.messages`, `agents.event`.
- Produces: route `/agent?id=<id>`; `AgentEvent({ event, onExpand })` renders a message as a bubble (reusing the chat markdown component used by `thread.tsx`) and a tool event as a one-line collapsed row that expands to its output (fetching `agents.event` when `*_more` is set).

- [ ] **Step 1: Failing tests** `shows messages oldest at top and loads older on scroll up using beforeSeq`, `collapses tool calls to one line and expands on tap fetching the full event when output_more`, `appends live events from the stream without a refetch`, `shows the empty state for a session with no conversation`, `shows This agent is no longer available on 404 and returns to the list`, `withheld events render a withheld chip, never blank text`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement** so that on EVERY (re)connect the screen first calls `agents.messages` and subscribes from the returned `rev`, never from a remembered cursor (after a server restart per-session revs can be lower than a remembered one); a frame replaces the loaded event with the same `seq` (tool calls update in place); on stream fallback poll `agents.messages` and merge by `seq` every 5 seconds.
- [ ] **Step 4: Run `pnpm test` in `apps/mobile`; expect all green.**
- [ ] **Step 5: Commit** `Agent screen: live conversation`.

### Task 9: iPad split

**Files:**
- Modify: `apps/mobile/app/_layout.tsx` (`SplitFrame`: when the pathname is `/agents` or `/agent`, the left pane renders the agents list instead of the chat inbox), `apps/mobile/app/agents.tsx` (export the list as a reusable component)
- Test: `apps/mobile/lib/split-agents.test.tsx`

**Interfaces:**
- Consumes: the existing `useSplitView()` (`split`, `inboxShown`, `inboxWidth`) and `FULL_WINDOW_ROUTES`.
- Produces: on a wide window the list stays left and `/agent` opens beside it; a narrow window is unchanged.

- [ ] **Step 1: Failing tests** `wide window shows list and open agent side by side`, `narrow window pushes the agent over the list`, `Assistant tab on a wide window still shows the chat inbox`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement** by parameterising the inbox pane content on the route; do not fork `SplitFrame`.
- [ ] **Step 4: Run `pnpm test` in `apps/mobile`; expect all green.**
- [ ] **Step 5: Commit** `Agents: iPad split view`.

### Task 10: App PR and simulator check

- [ ] **Step 1:** `pnpm test` in `apps/mobile`, `pnpm exec tsc --noEmit -p apps/mobile`, `pnpm exec biome check apps/mobile`; expect green (the one existing lint error in `computer-screen.test.tsx` predates this work and is left alone).
- [ ] **Step 2:** Push `herdr/agents-tab`, open a PR on `berlinguyinca/herdr-shepherd`; review the diff.
- [ ] **Step 3:** With the server deployed (Task 5), run the app in the simulator pinned to `https://local.metabolomics.us` (as before, using `simctl` screenshots only, no UI automation) and confirm: the Agents tab shows blocked/done agents first, a tap opens a live conversation, and the iPad simulator shows the split view. Record the screenshots in the PR.
