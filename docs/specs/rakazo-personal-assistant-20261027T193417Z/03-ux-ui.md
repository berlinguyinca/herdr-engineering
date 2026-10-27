# 03 — UX/UI specification

## Information architecture
Sidebar: existing nav remains unchanged except one **Personal Assistant** destination/launcher. Under existing Settings: **Personal Assistant** route; avoid standalone application route styling.

## Settings: desktop layout
Header: 'Personal Assistant' and status chip (Disabled / Ready / Running / Needs attention), top-right onboarding or disable action.
Content width 1100–1280px, two-column where appropriate (configuration 2/3 width, status/help 1/3); collapse to stacked cards under ~900px.
Tabs in this order: General, Models, Permissions, Memory, Routines, Integrations, Voice, Computers, Privacy & Data, Diagnostics.
General: enable toggle, assistant name/avatar, purpose/instructions, default persona, brief explanation of capabilities, last activity.
Models: auto InferWeave option, manual runtime/model select; effective route/fallback; context and cost/usage estimates; failure/no-model state.
Permissions: 4 named profiles (Conversational/Read-only, Supervised, Trusted, Advanced); **actual permissions shown as granular scopes**, not implied by profile. Restricted actions approval gates.
Memory: enable, namespaces, search and source, inspect/edit/forget/export, retention, provenance. Separate private vs shared deliberately.
Routines: list, enable, create/edit, timezone, next run, quiet hours, permissions, last result, retry policy, manual trigger, cancel.
Integrations: connected apps, scopes, revoke, credential health, inspect tool capability inventory, server-side only secrets.
Voice: microphone/output choice, provider and opt-in, test, privacy/retention controls.
Computers: private vs shared hosts, trust classification, affinity and denylist, compute usage.
Diagnostics: runner health, events, logs, test connection, model available, queue and task links.

## Chat surface
Global assistant icon opens a 420–520px desktop drawer, or full-screen mobile panel. Expand-to-page action. Conversation history, new thread, clear input, attach artifact, voice entry, model label, token/usage on request, tool execution timeline, approval panels, retry and cancel. Differentiate thought vs actual tool outcomes without exposing raw reasoning. Make sources/action logs discoverable.

## Activity
Timeline: request, plan summary, tool action, delegated run, checkpoint, approval, evidence/artifact, outcome. Filtering by assistant, task, status, date. Link to canonical Herdr sessions, hosts, artifacts.

## Visual design contract
Read `DESIGN.md`, `packages/herdr-design-system/README.md`, `theme.css` and existing components before writing UI. Preserve light/dark themes, canonical spacing/typography/tokens, reduced motion, keyboard controls, WCAG 2.2 AA contrast/focus, responsive widths 375/768/1280/1600; avoid new component library. No iframe or embedded foreign Rakazo dashboard. Respect SSE disconnected/reconnect state.

## Empty/error/loading states
Disabled → opt-in CTA. No model → configuration CTA. No memory → explain opt-in. No routines → starter templates. Offline → queued or unavailable explicitly. Expired connector → reconnect. Approval expired → deny and show retry. Long work → durable task progress. Never show fake success.
