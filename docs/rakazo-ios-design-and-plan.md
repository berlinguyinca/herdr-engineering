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

### Update 4 (decisions taken autonomously)

Decisions made rather than left open, each implemented and contract-tested:
- #469 **Agent skills** are stored per space and injected into the bot's system prompt. This also fixed a gap found on the way:
  the model runner never sent a bot's instructions or its chosen model; it now does (bots/update instructions and modelId are real).
- #472 **Vault, bot secrets, agent secrets**: Fernet at rest under an operator key file (`vault_key_file`; off and says so without
  it). Stores and reveals; injecting credentials into agent requests is not part of the bridge.
- #473 **Remote MCP servers** are assistant connectors (https only, per-origin allowlist, credentials never returned). `stdio`
  is refused because it runs commands on this machine.

Still answered with "nothing configured" or a reasoned refusal: MCP assignments/OAuth, other connectors, messaging, tickets,
boards, onboarding, approval rules, voice and computer hosts, marketplace. Not started: auto-sync of running agents into rooms,
steering cards. Impossible without other infrastructure: push (APNs), voice/desktop hosts.

### Update 5

#475: a per-space ticket board (boards, tickets, comments; `me.ticketBoardEnabled` is now true) and onboarding acknowledgements.

What remains, and why it is left alone rather than guessed at. These are the ones where acting autonomously would mean a
security decision on the owner's behalf:
- **Approval rules / auto review**: built in Update 6 below, in the one direction that cannot widen authority.
- **Auto-sync of running agents into rooms**: grants room membership; left to an explicit `groups/create`/`update`. (Steering cards: Update 7.)
- **Messaging channels, marketplace, computer/voice hosts, push**: need external accounts or infrastructure (Slack/Telegram
  apps, a registry, donbot/nibbler hosts, APNs) that do not exist yet.

### Update 6

Approval rules and auto review (`rakazo_rules.py`). The person's own rules (account-wide or per space, set from the app) are
consulted by `AssistantService.rule_policy` inside `request_tool_action`:
- `deny` makes the request fail as if no grant existed (stricter than default; wins over `always_allow`, and a space rule wins
  over an account rule).
- `always_allow` skips the approval prompt **only** for a tool that already holds an active grant and is not in
  `NEVER_PREAUTHORIZED` (`send_message`, `run_shell`). It never creates authority.
- A failing rule lookup means no rule. Removing a rule tombstones it. `approvalRules/preview` reports before/after for a sample tool.
- `autoReview/get,set` store mode and enabled; `checkerAvailable` is false (no checker model runs here) and `skipApprovals` is
  refused with a pointer to per-tool rules.

### Update 7

Steering cards in groups. Pending steering requests of a room appear in the group thread as `steer:<id>` ask cards next to the
proposal cards. Confirm calls the existing `confirm_steering` with the request's own digest and fence (single use, fenced, a
different owner than the proposer unless the operator allows self-confirm); Decline calls `reject_steering`. A person who may
not read or confirm steering sees no cards or gets the collaboration service's refusal. The instruction is untrusted text and is
only shown as a snippet. Auto-sync of running agents into rooms stays an explicit action by design: your own agents already
appear live as bots, but adding one to a room is a permission grant.

### Update 8

Auto-join, as an operator opt-in. `control_plane.rakazo_bridge.auto_join_rooms: [room_id, ...]` (YAML only, empty by
default, at most 50) makes `groups/list` add the signed-in person's own active agents to those rooms they already belong to,
with chat rights only (`read`, `post`), through the normal `grant_agent` path (so room roles, the agent ceiling and approvals
still apply). It is best effort and never breaks the listing. The grant stays a deliberate operator decision rather than a default.

### Update 9

Sign-in with the lab Cognito pool (`rakazo_cognito.py`), optional and YAML-only:

    control_plane.rakazo_bridge.cognito:
      pool_id: us-west-2_GjtcM0PCp            # the pool BinView / lc-binbase-scheduler and the LLM gateway use
      client_ids: [<app client id>]           # app client allowlist for the access token
      required_group: <group>                 # REQUIRED, no default: only members of this group may sign in
      group_check: true                       # default; re-reads membership while the session lives (needs boto3 + AWS read access)

An email that is not a local account is checked by Cognito (`InitiateAuth`, `USER_PASSWORD_AUTH`); the access token must verify
against the pool's JWKS and carry the group, and the ID token supplies a verified email. The person then acts as the collaboration
user named by their Cognito username (the same identity HCN gives them), so they see the rooms and agents they already have there.
Local accounts never go to Cognito; a wrong pool password or a non-member fails like any wrong password; an outage is a 503.
Removing someone from the group ends their session within about a minute (`group_check`). Pool sessions are held in memory, so a
web restart asks them to sign in again.

Two things this does NOT do, because they change the shared pool: it does not create an app client or a group. The BinView web
client has no `USER_PASSWORD_AUTH` flow listed (its flows are legacy defaults), so a dedicated app client with
`ALLOW_USER_PASSWORD_AUTH`, and a group such as `rakazo-users`, should be created in the pool by you before enabling this.
Members get the chat-only `family` role by default (`cognito.role: operator` is an explicit opt-in), so keep the group small either way.

### Update 10

Outside systems join through the existing external-agent path instead of provider code in the control plane (see
`docs/operations/external-bridges.md`): a reference client and a relay loop (`herdr_engineering/external_bridge/`,
`herdr-eng relay join|status`); no provider adapter ships yet. In the app, the sponsor's external agents already appear as agent bots; they now report online while
their heartbeat is fresh (`list_my_agents.online`), and `messaging/status`, `messaging/installations/list` and
`messaging/installations/remove` list and revoke them. `messaging/installations/start` stays refused: the invite code is a
one-time secret created in the dashboard, not a URL. Any provider, voice or desktop host would be an adapter on the same client.

### Update 11: one owner for the phone and the dashboard

In single-operator mode a phone `operator` account (YAML, not Cognito) now owns its assistants, threads, runs, grants and approvals
as the dashboard operator (`open-listener`), in the default space only, so `bots/list` on the phone and the dashboard page show the
same assistants. `_Ctx.user` is the owner key; `_Ctx.principal` stays the phone account's own principal. Family accounts, Cognito
accounts, non-default spaces and servers outside single-operator mode are unchanged. Approvals and executions record the phone account's own principal as the actor, so the audit trail still
names the phone; the owner key says whose approval it is. In single-operator mode self-approval is already allowed by configuration.

Not migrated: assistants the phone created before this change stay owned by `rakazo:<account>` and are shown on neither surface.
`RakazoBridge.legacy_phone_assistants()` lists them (it moves nothing); moving them means rewriting the owner on every record type
that carries `user_id`, which needs its own reviewed migration.

### Trying the app locally

`scripts/local-phone-server.py` starts a throwaway control plane (in-memory, bridge on, one operator account with a random
password printed once, loopback only). Point the iOS simulator's Server field at `http://127.0.0.1:8787` and sign in with the
printed email and password. Checked: the server starts and `/api/auth/sign-in/email` returns a token. The simulator itself needs Xcode.
