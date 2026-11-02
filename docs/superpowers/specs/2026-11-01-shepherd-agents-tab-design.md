# Shepherd: the Agents tab (read side)

Piece 1 of five. The program, in the order the owner chose: (1) Agents tab, read side, (2) Terminal, (3) Assistant tab with inline agents and approvals, (4) Rooms and teams, (5) iPad layout, built into every piece rather than added last. This document covers piece 1 and the shared navigation. Each later piece gets its own design.

## Why

Shepherd is the owner's phone and iPad companion to the herdr dashboard. The owner works in the herdr terminal today and rarely opens the web rooms. Away from the desk they want to see which agents need them, read what an agent is doing, and (in later pieces) answer it and drive its terminal. Piece 1 delivers the first half: seeing.

Success: opening the app on a phone, the owner sees within one second which agents are blocked or finished, reads a one-line summary of each, and opens any agent to read its recent conversation. No sign-in, no setup beyond the server URL (already built).

## Who and where

One person: the owner of the fabric. The server is on their tailnet, reached over HTTPS (`https://local.metabolomics.us`), the phone is locked, and the app already opens an owner session with no password (`open_owner`). The owner decided this is a sufficient security model for the whole program, including the later terminal. Do not add extra gates; do write an audit entry for state-changing actions in later pieces.

## Goals

- A bottom tab bar with two tabs: **Assistant** (the existing chat, unchanged in this piece) and **Agents** (new). The tab bar is the navigation for the program.
- **Agents tab:** every agent session across all machines, in one list sorted by what needs the owner most (blocked, then waiting for input / done, then working, then idle, then closed), most recent first inside a group. Each row shows agent name, machine, status, and a one-line summary.
- **Agent screen:** tapping a row opens the agent's conversation: user and assistant messages, with tool calls collapsed to one line each and expandable on tap. Live: new events appear without refresh.
- Phone first, and an iPad-aware layout from the start: on a wide window the list and the agent screen sit side by side (list left, conversation right). The summary and skills column in the iPad mockup belongs to later pieces.
- Works on the owner's real data: 550 sessions, 199 with inspector detail, 7,000+ services, 64 hosts, so lists must be paged or windowed and filter out closed or old sessions by default.

## Non-goals

- Sending anything to an agent. Prompts, keys and the terminal are piece 2.
- Assistant-spawned agent cards and approvals: piece 3.
- Rooms, teams, assignments: piece 4.
- Any change to the existing Assistant tab behaviour, to its connections or to its authorization.
- Multi-user accounts, sharing, billing, sign-up. The app has none and stays that way.

## Anti-goals

- Making the owner learn terms like harness, pane, host or session id to use it. Show agent names and machine names; keep ids out of the UI.
- A list that is slow or noisy because it shows every closed session. A pull-to-refresh or a laggy list counts as failure even if the data is right.
- Exposing data the phone should not see: the existing inspector redaction (sensitive flags, redacted fields) must apply to everything shown.

## Constraints and what exists

- The app speaks the Rakazo-style RPC protocol to the control plane's bridge (`herdr_engineering/control_plane/rakazo_bridge.py`, `POST /rpc/<procedure>`). It does not call the control plane's `/api/v1` directly. New server capability is therefore new bridge procedures. The bridge is a table of procedure names to handlers (`me`, `spaces/list`, `bots/list`, `threads/*`, `runs/list`, and others), with the owner token already validated per call.
- The control plane already holds what is needed: sessions with status (`repo.list_sessions`), per-session agent names (`_named_session`), inspector detail with messages and tool calls and per-field redaction (`inspector.py`), and agent summaries (`agent_summaries.py`). herdr's own agent status values are `idle`, `done`, `working`, `blocked`, `unknown`.
- The app is the fork `berlinguyinca/herdr-shepherd` (Expo / React Native, expo-router). It already has `thread`, `group-thread` and `peer-chat` screens whose message rendering can be reused for the agent conversation. Strings go through `t()` and need entries in the seven non-English catalogs (an existing test enforces it). The server's tests and CI are in `herdr-engineering`.
- Live updates: the app has `threads/subscribe`; the inspector store already pushes events to subscribers. Reuse that style rather than inventing a new transport.
- Server side runs on bender behind the deploy script; a deploy restarts the control plane (about ten minutes to boot while the journal replays). Build so a restart is not needed to ship app-only changes.

## Decisions made by the owner

- Two bottom tabs, Assistant and Agents.
- Agent mode models the herdr terminal (machines, agents, per-agent conversation), not the web rooms.
- When an agent is opened later (piece 2) the default view is "smart": Terminal when blocked on a prompt, Chat otherwise, remembered per agent. Piece 1 only builds the Chat view and leaves room for the switch.
- Security model as above.

## Answers from the repository

- **Transport for live updates.** `threads/subscribe` is Server-Sent Events: the client posts with `accept: text/event-stream` and an integer cursor, the bridge streams frames from an event journal (`ev.event_stream`), refuses with 429 when its stream cap is reached, and stops when the token is revoked. The agent conversation uses the same shape (cursor = the inspector's per-session revision, which the store already tracks as `rev` and `since_rev`). The list uses one SSE stream of row changes, with a fallback to polling every 5 seconds if the stream cannot connect or is refused.
- **Redaction.** The inspector redacts per field when events are stored and `agent_summaries` passes text through `redact_text` / `redact_payload` (`redact.py`). Every new procedure returns only data that has gone through those helpers; no new redaction logic. A redacted field shows as a "redacted" chip in the UI.
- **Statuses.** "Waiting for input" is herdr's `done` (the agent finished a turn and waits), per `terminal_attach.py`. "Closed" is the existing closed-status set there (`closed`, `completed`, `failed`, `stopped`, and so on). `unknown` sorts after `idle` and before closed.
- **Two repositories.** The server work (bridge procedures, tests, deploy) lands in `herdr-engineering`; the app work in `herdr-shepherd`. Two PRs, the server first. An app talking to a server that lacks `agents/list` shows a plain "This server does not offer the Agents view yet" state instead of failing.
- **Mockups.** The two layouts the owner reviewed are in `2026-11-01-shepherd-mockups/` next to this file (open the HTML in a browser). They are wireframes; visual styling comes from the app's existing tokens and components.
- **Translations.** The seven non-English catalogs are `de, es, ja, nl, ru, zh, zh-TW` under `apps/mobile/lib/locales/`; an existing test fails if a new string lacks an entry. The builder supplies the translations.

## Left to the builder

- Procedure names and payloads for the new bridge methods (suggested: `agents/list`, `agents/get`, `agents/messages`, `agents/subscribe`), and whether messages page by cursor or by sequence number.
- Where status comes from per row (session record versus inspector) and how "needs you" is computed.
- List windowing, default filters (suggested: hide closed and anything idle for more than 7 days behind a "Show older" control), and empty and error states.
- Whether the one-line summary comes from `agent_summaries` or is derived from the last assistant message when no summary exists. Suggested: summary if present, otherwise the last assistant message trimmed.
- iPad breakpoint and how the split view navigates.
- Tests: bridge handlers with fixtures of redacted and unredacted events; the app's list sort and the conversation rendering; the i18n catalog test.

## Calls I made (not the owner's)

- Sort order and the default filter for old sessions.
- Summary fallback rule.
- Reusing the existing thread message components for the agent conversation.
- Piece 1 ships behind nothing: the Agents tab simply appears. It is read-only, so there is no risk to flag it for.
- One list row is one session record, named through `_named_session`. A session with no captured conversation still appears (it has a status and name), and its Chat view shows "No conversation was captured for this agent."
- The list answers from the control plane's in-memory projection, not the database, with a target of under one second for the first screen on the owner's data.
- The Assistant tab is the existing home screen, unchanged. Existing stack screens (`thread`, `group-thread`, `peer-chat`, settings and so on) stay stack screens above the tabs, and deep links and the share target keep working.
- The server deploys before the app ships; the app tolerates an older server as described above.
