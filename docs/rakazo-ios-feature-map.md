# Rakazo iPhone app: feature surface and Herdr mapping

Upstream: `elie222/rakazo` @ `6e31c3fe` (2026-10-30), `apps/mobile` (Expo/React Native) talking oRPC to `POST /rpc/<proc>` plus
`/api/auth/*`. Source of truth for the surface: `packages/contracts/src/rpc.ts` (`appContract`, ~60 namespaces) and the
procedures `apps/mobile` actually calls (~165, extracted from `rpc("…")` call sites outside tests).

Bridge today (`control_plane/rakazo_bridge.py`): **19 procedures** + `health` + sign-in. Everything else returns
404 "This server does not support that feature." That message is the sad one the app shows.

Legend: DONE = bridge answers it. MAP = Herdr already has the capability, only the translation layer is missing.
BUILD = Herdr has no equivalent; needs new work. STUB = answer with an honest empty/disabled shape so the screen works.
SKIP = out of scope for a Herdr-backed server (answer "not offered" cleanly, hide the entry via capabilities).

## 1. What the app exposes (by screen) and how we cover it

| App area (screens) | Upstream procedures the app calls | Status | Herdr side to map onto |
|---|---|---|---|
| Sign in / server (`sign-in`, `server`) | `/api/auth/*`, `health` | DONE | bridge accounts + scrypt + tokens |
| Account (`account`, `change-password`) | `me`, `/api/auth/*` password change | DONE (me) / STUB (password) | operator accounts live in fleet YAML; password change = refuse with clear text |
| Bots list / profile / settings (`index`, `bot-profile`, `bot-settings`, `bot-model`) | `bots/list,get,create,update,archive,restore,remove,listArchived,restoreInstructions,setComputer`; `botSections/*`; `groups/*` | list/get/create DONE; rest MAP | `create/update/archive_assistant`, `list_assistants` (archive = `archive_assistant`). Sections/groups: STUB (empty) or BUILD |
| Chat thread (`thread`) | `threads/head,get,messages,send,answer,stop,markRead,subscribe` | DONE | assistant thread/message/run/approval |
| Chat extras | `threads/clear,markUnread,react,endCall`, attachments on `send` | MAP (clear, markUnread) / BUILD (react, attachments) | attachments need a blob path into Herdr artifacts |
| Runs / activity | `runs/list` | DONE | `list_runs` |
| AI consent | `aiConsent/status,allow,revoke` | DONE (static) | n/a |
| Push | `notifications/registerPush,unregisterPush` | STUB (accepted, dropped) | BUILD APNs sender, or poll `list_notifications`/`digest` |
| **Artifacts** (`artifacts`, `artifact`) | `artifacts/listSpace,list,listVersions,get,getById,create,remove` | **MAP** | Herdr artifact registry (`artifacts.py` ArtifactStore, versioned families, ACL via `artifact_acl.py`, `artifact_writer` hook already used by the email-draft tool). See section 3 |
| Routines (`routines`, `routine`) | `routines/list,listForMentions,update,remove,create,history,testRun` | **MAP** | `create/list/get/update/archive_routine`, `due_routines`, `trigger`, `routine_fired` |
| Memory | `memory/list,update,remove,exportMarkdown`, `memory/provider*` | **MAP** | `store/search/correct/forget/export_memory` (`export_memory` = `exportMarkdown`); provider* = STUB null |
| Search (`search`) | `search/query` | MAP | `search_memory` over threads/messages/artifacts, merged server side |
| Usage | `usage/summary,list` | MAP | `usage_spend.py`, `llm_stats.py` |
| Approvals settings (`approvals`) | `approvalRules/list,set,remove,preview`, `autoReview/get,set`, `trustedLinkSites/*` | MAP (rules) / STUB (autoReview, trusted sites) | grants (`grant_tool`, `revoke_grant`, `list_grants`), `approval_view`, `evaluate_computer_policy` |
| Skills (`skills`, `skill`, `plugin`) | `agentSkills/*`, `skills/*` (taught), `capabilities/*`, `marketplace/catalog` | MAP | `skill_registry.py` + `skill_routes.py` + `skill_transfer.py`; taught-skill recorder = SKIP |
| Integrations (`integrations`, `integration-setup`) | `connections/*`, `integrationSetup/get`, `mcp/servers/*`, `mcp/assignments/*`, `mcp/oauth/begin` | MAP (connectors, MCP) / SKIP (Composio/Pipedream catalog) | `register/list/get/archive_connector`, `connector_request`, `collab_mcp.py`; catalog = Herdr connectors only |
| Messaging (`messaging`) | `messaging/*` (Slack, Telegram etc.) | SKIP (return empty lists) | `collab_external.py` is the nearest, not 1:1 |
| Computer (`computer`) | `computer/boot,status,stop,reset,screenUrl,takeover,heartbeat,release,updates,dismissUpdate` | PARTIAL MAP | `run_shell_tool`, `browse_tool`, `evaluate_computer_policy`, `terminal_attach.py` (terminal). No graphical desktop: report `screenUrl` unavailable |
| Models (`models`, `model-connection`, `model-picker`) | `models/list,credentials,connect,disconnect,forRole,role,setRole,rename,*OAuth,probeOpenAiCompatible` | MAP (list/role) / SKIP (BYO credentials) | Herdr picks models via llm proxy and fleet config; expose the gateway catalog read-only |
| Voice (`voice`) | `voice/catalog,status,connect,credentials,voices,prepare,setVoice,setSpeechModel,disconnect`, `threads/endCall` | SKIP | answer `voice/status` as not configured |
| Billing / wallet (`billing`, `paywall`, `wallet`) | `billing/*`, `paymentWallet/*` | SKIP | `/api/auth/capabilities` already says billing:false; make these return disabled shapes |
| Vault (`vault`) | `vault/list,put,remove,reveal`, `botSecrets/*`, `agentSecrets/*` | SKIP by default | never put secrets in the bridge; at most read-only names from the Herdr secret store |
| Onboarding / survey / spaces | `onboarding/*`, `spaces/list,create,remove`, `spaceSettings/*`, `availability/set` | `spaces/list` DONE; rest STUB | single space `ev.SPACE_ID` |
| Export / delete | `export/bot`, `export/accountToken` | MAP (bot) | `export_memory` + thread dump |
| Misc | `links/favicon`, `analytics/screen`, `scratchpad/*`, `boards/tickets` | STUB / BUILD | favicon: STUB null; scratchpad: could ride artifacts |

## 2. What is missing on our side (honest list)

1. **No generic fallback shape.** An unknown procedure is a hard 404. Many screens only need an empty list or a
   "disabled" object to render. Cheap, high payoff: a STUB table.
2. **Attachments in chat** (`threads/send` rejects them). Needs blob storage into the artifact registry, plus image/audio blocks in `threads/get`.
3. **Push (APNs).** Notifications exist in Herdr (`list_notifications`, `digest`, `ack_notification`); nothing delivers to a phone.
4. **Groups / peer bots / delegation UI.** Herdr has `delegate`/`list_delegations`; the app's group model has no counterpart.
5. **Graphical computer.** No VNC-style `screenUrl`. Terminal and browse tools only.
6. **Artifact blob/version mapping.** The registry is versioned but the app's `ArtifactSchema` (mime, name, versionCount, contentBase64) needs an adapter.
7. **Multi-user.** Bridge has only `role: operator` accounts from YAML; no per-user signup.
8. **No contract conformance test.** We hand-write response shapes. Nothing checks them against `rpc.ts`, so a field the app requires can silently be missing.

## 3. Artifacts: the example you gave

Upstream: artifacts belong to a bot/thread, are versioned in a *family*, and carry `name, description, mimeType, content`.
Herdr: the artifact registry is already versioned, ACL'd and has renderers (`artifact_renderers.py`, `artifact_preview.py`,
`artifact_report_*`). Mapping:

| App call | Herdr |
|---|---|
| `artifacts/listSpace {botId?, cursor, limit}` | list families visible to the account (ACL), newest first, cursor = opaque offset |
| `artifacts/listVersions {familyId}` | versions of one family |
| `artifacts/get / getById` | latest (or given) version + content base64 |
| `artifacts/create {name, mimeType, contentBase64, clientNonce}` | `artifact_writer` with idempotency key = `clientNonce` |
| `artifacts/remove` | retraction (`artifact_retraction.py`), not a hard delete (we confirm before deleting) |
| assistant-produced artifacts (e.g. email drafts, delegation `artifact_refs`) | surface as message blocks and in `listSpace` so they appear in the phone's Artifacts tab automatically |

Open question for you: should phone-created artifacts be private to the account, or land in the shared workspace?

## 4. Recommended order

1. **Contract harness first.** Vendor `rpc.ts` shapes (or generate JSON schemas) and test every bridge response against them. Prevents regressions on all later steps.
2. **STUB table** for every call the app makes at start-up or opens from a menu (billing, voice, vault, messaging, onboarding, models credentials, autoReview, trustedLinkSites, links/favicon). Result: no more "server doesn't support" for SKIP items; they show as off.
3. **Artifacts** (section 3). Biggest visible win and the feature you asked for.
4. **Routines, memory, search, usage.** Pure MAP, no new Herdr work.
5. **Skills, approval rules/grants, connectors/MCP.** MAP, needs ACL review.
6. **Bot update/archive/restore/clear/markUnread**, then attachments.
7. **Push (APNs)** and **computer/terminal** last; both need new infrastructure and a security review.

Then hide SKIP features properly by returning the right flags from `/api/auth/capabilities` and `integrationSetup/get`, so the app does not
offer buttons that lead nowhere.

## 5. Caveats

- The procedure list comes from static extraction of call sites; a few calls built from variables could be missed. Step 1's harness would catch these.
- I read the bridge dispatch table and Herdr service method names, not every Herdr implementation. "MAP" means a matching capability exists by name; the exact adapter effort is unverified.
- Upstream is beta and pushes daily; pin the SHA above and re-diff `rpc.ts` before each release.
