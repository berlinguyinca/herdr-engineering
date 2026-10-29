# Rakazo fit-gap

Source: `elie222/rakazo` at `3a0155f`, Apache-2.0 (no NOTICE file; copied code must keep headers). Code was read, not executed.

**Decision: Herdr's own `assistant.py` stays the single assistant backend. No Rakazo runtime is deployed.**
The active assistant today is Herdr-native only; nothing in `herdr_engineering/` or `packages/` references Rakazo at runtime.

| Rakazo capability | Evidence in Rakazo | Verdict | Reason |
|---|---|---|---|
| Auth (Better Auth, OTP, passkey, OIDC, orgs) | `packages/auth/src/index.ts` | Reject | second identity system; Herdr API-key principals are canonical |
| Postgres/Prisma, 102 models | `packages/db/prisma/schema.prisma` | Reject | second DB |
| Pi inside API/worker process, Pi tools | `docs/computer-runtime.md:3-9` | Reject | different tool loop and trust model than Herdr grants |
| Sandboxes (E2B, Daytona, Boat, Docker) | `docs/self-host-sandbox-providers.md` | Reject | overlaps Herdr host policy |
| Graphile worker routines | `apps/worker` | Reject | second scheduler |
| HMAC GitHub webhook | `apps/api/src/github-webhook.ts:11-20` | Reject (pattern only) | Herdr has its own ingest and replay handling |
| Envelope encryption per space | `docs/content-encryption.md` | Reuse as reference | model for connector secret storage, which Herdr lacks (connectors hold credentials in plain objects) |
| Secret store, MCP OAuth refresh | `packages/adapters/src/secret-persistence.ts`, `mcp-oauth.ts` | Reuse as reference | needed before any Gmail connector |
| Approval semantics | `apps/api/src/mcp-approval.ts` | Reuse as reference | compare with Herdr approver-must-differ rule |
| Expo mobile app | `apps/mobile/lib/server-config.ts`, `api.ts:14` | Reject for now | app can target an external server only if it speaks Rakazo's oRPC contract (`packages/contracts`) and auth; emulating it would fork the API |

**Mobile and voice:** use the Herdr PWA and `packages/voice.js`. Revisit Rakazo mobile only through a restricted bridge after an auth/session security audit.

**Threats if Rakazo were wired in anyway:** separate token store holding Gmail/GitHub OAuth outside Herdr audit; remote-shell sandboxes outside host policy; a second background scheduler that could act without Herdr approvals; identity mapping errors that break tenant isolation.
