# Rakazo iPhone app over Herdr: agreed design and build order

Status: design agreed with the operator, nothing below is implemented yet beyond the 19 procedures in `control_plane/rakazo_bridge.py`.
Companion: `rakazo-ios-feature-map.md` (the per-procedure mapping). Upstream pinned at `elie222/rakazo` `6e31c3fe`.

Goal: every feature of the app works against Herdr. Nothing is hidden. Where Herdr has no direct match we build the backing
(voice and desktop run on donbot and nibbler; the vault is allowed because the network is trusted).

## Decisions

1. **Agents are special bots.** Every running Herdr agent session becomes an agent bot (stable id, live `status`, `modelId`, `spawnKey`
   = its session). External collaboration agents appear the same way, marked external.
2. **A workspace is a room, a room is a group.** A workspace with a running agent gets a room (created and linked as primary if missing);
   the agent is granted into it. Rooms map 1:1 to the app's groups. Members are bots, agents and people (people are bot-shaped members,
   because Rakazo's `GroupMember` is always `{botId,...}`).
3. **Assign and remove bots from the app** = `grant_agent` / `remove_agent_from_room`. Removal revokes access, it never stops the session.
   External agents may join rooms or create new ones; those show up as groups.
4. **A Rakazo space is a hard partition of one account's data** (bots, chats, memory, routines, computers, connections, credentials, skills,
   artifacts). Maps to a derived assistant tenant (see build step 1); collab projects attach to it in step 3. Account-level settings (models, voice, approvals, vault)
   stay global with optional per-space override. Cross-space bot talk is refused unless the bot's switch is on.
5. **Authority is never widened by the phone.** Room access stays grant ∩ owner's room role ∩ agent ceiling; non-members get 404. Peer text
   is untrusted data: shown, never executed.
6. **Defaults:** auto-created rooms sit in a project named after the host, in the host owner's personal space, private until members are added.
   Only your own running agents are auto-listed. Stopped agents stay visible as idle for 7 days. The default space cannot be deleted.
   Nothing is deleted without explicit confirmation; deleting a space archives first.

## Build order (each step gets contract tests against `packages/contracts/src/rpc.ts`)

0. **Contract harness.** Check every bridge response against the app's schemas.
1. **Spaces. DONE (branch `feat/rakazo-spaces`).** A space is a derived assistant tenant (`<account tenant>~<space id>`), not a new column:
   the store already isolates every record by `(tenant_id, user_id)`, so the default space is the account's own tenant and nothing
   moved. The app selects a space with the `x-rakazo-space-id` header; an unknown, foreign or deleted id is 404. New: `rakazo_spaces.py`
   (registry), table `rakazo_spaces` (migration `0037`, registry only), `spaces/create|rename|remove`, `spaceSettings/overrides|setOverride`.
   Removing a space archives its bots and tombstones the id; nothing is deleted.
2. **Identity link.** Bridge accounts to collab users (`link_identity` / `authenticate`), so rooms resolve for the signed-in account.
3. **Agent and room sync.** Sessions to agent bots, workspaces to rooms, stable ids; `bots/list`, `groups/list`, `groups/get`.
4. **Group thread.** Room comments as thread messages, `@handle` addressing, `threads/subscribe` from `room_state`.
5. **Group membership.** `groups/create`, `groups/update`, archive, restore, remove.
6. **Approvals.** Delegation proposals and steering confirmations as approval cards; `approvalRules`, grants.
7. **Artifacts.** `artifacts/*` over the registry (create idempotent on `clientNonce`; remove = retraction), room-scoped listings.
8. **Routines, memory, search, usage, skills, connectors and MCP.**
9. **Bot edit/archive/restore, clear, mark unread, reactions, attachments.**
10. **Vault, models, billing (free plan), messaging, onboarding.**
11. **Computer and terminal, then voice (donbot/nibbler), push (APNs, needs an Apple key), graphical desktop.**

## Open items

- Which Herdr surface feeds "running agent" discovery (`repo.list_sessions` vs collab agent records) needs confirming in step 3.
- Voice and desktop: choose between donbot and nibbler per feature once step 11 starts.
- APNs credentials must come from the operator.
- Re-diff upstream `rpc.ts` against the pinned SHA before each release.

## Status (2026-10-30, end of the first autonomous run)

PR stack, each branch based on the one before it, none merged and nothing deployed:
#445 bridge -> #446 spaces -> #452 groups and agent bots -> #453 bot management -> #454 memory/search/usage -> #455 routines
-> #456 every remaining procedure answers -> #458 files.

| Area | State |
|---|---|
| Chat, streaming, approvals, stop | Done (#445) |
| Spaces (hard partitions) | Done (#446) |
| Groups = collaboration rooms, agents as bots, people as members | Done (#452); needs account `collab_user` |
| Bot edit/archive/restore/sections/instruction history | Done (#453); remove = archive |
| Memory (assistant memory service), search, usage | Done (#454); remove hides; token counts unknown |
| Routines | Done (#455); only schedules that map exactly to interval/daily; runner is opt-in (`routine_runner_seconds`) |
| Files | Done (#458) against an in-memory registry; real registry path unverified |
| Scratchpad, trusted sites, preferences, bootstrap, billing status, bot export | Done (#456) |
| Skills, MCP, connectors, models, messaging, voice, computer, vault, tickets, boards, onboarding | Answered with "nothing configured" or a reasoned refusal (#456); NOT implemented |
| Auto-sync of running agents into rooms; delegation/steering cards in groups; group `threads/answer` | Not started |
| Push notifications | Impossible with a custom server (APNs belongs to the official app) |
| Voice / graphical desktop hosts | Need donbot/nibbler infrastructure; not built |

Known gaps in earlier work: review findings 8-10 on #445 (journal gaps after a quick restart, polling cost under the store
lock, a false 500 on sign-out during a concurrent flush); real-Postgres paths and migrations 0037/0038 only exercised with
fake tables; `threads/clear` and `threads/react` are refused because the store has no equivalent.

### Update (later the same night)

Added on top of the stack: #460 models (read-only gateway catalog, per-bot model = assistant model policy), #461 delegate
proposals as approval cards in groups with group `threads/answer` (tested with stubbed collab calls), #462 review findings 8-10.

Still not implemented (answered with "nothing configured" or a reasoned refusal): skills, MCP servers, connectors, messaging,
vault, tickets/boards, voice, computer hosts; auto-sync of running agents into rooms; steering cards. Each needs its own
design (what existing Herdr service backs it) before code; none is blocked by the bridge itself.

### Update 3

#466: optional Hindsight link through the existing `herdr-memory` client (`control_plane.rakazo_bridge.hindsight`). App memory
edits and removals follow into the shared fact bank; `search/query` adds semantic recall. Tested with a fake client only.

Decision needed before more code (each would otherwise be a stored-but-inert feature, which this bridge refuses to fake):
- **Agent skills** (`agentSkills/*`): the assistant does not load skills today; storing them without it reading them would mislead.
- **MCP servers / connectors / messaging**: need an owner for the credentials the app would enter (the operator's vault policy).
- **Vault**: allowed on the trusted network, but needs an encryption-at-rest choice (key source) first.
- **Auto-sync of running agents into rooms**: needs the host-registry proofs reviewed, since it grants room membership.
