# Security threat model and mandatory controls

## Trust boundaries
1. Human browser ↔ collaboration server (session, SSO, CSRF, XSS, permission checks)
2. Local agent bridge ↔ central server (mutual agent identity, replay protection, TLS, revocation)
3. User A ↔ user B (cross-tenant and project isolation)
4. Agent-generated text ↔ agent tools/execution (prompt injection and privilege escalation)
5. Artifact/media ↔ previews and model prompts (malicious payloads, hidden instructions and data leakage)
6. Server ↔ git/CI/object storage/memory (scoped service identities)

## Threats and mitigations
- Agent prompt injection in room chat: treat as untrusted content; explicit tool authorization + human approval for sensitive effects; limit available context and tools.
- Cross-user file exfiltration: deny local filesystem access; artifact allowlists, classification tags, project ACLs and content scanning; egress control for sandboxed runners.
- Credential exfiltration: never mount SSH/Git/API credentials into peer-controlled execution; redact logs and exceptions, short-lived tokens, scoped delegation.
- Tenant ID spoofing: identity from auth token; server joins on authorized org/project; database policy checks; negative tests on every resource and artifact reference.
- Unbounded inter-agent recursion: hop/turn/token budgets, cycle detection, cancellation and kill switches.
- Malicious guest invite or link: invited named principals, short expiry, MFA where required, read/comment default, audit download/export.
- Device compromise / stolen agent token: binding to device if supported, rotation, revoke, audit anomalous activity, no privilege amplification.
- Insecure WebSocket: authenticated upgrade + origin checks, authorization per room subscription/event, immediate revocation.
- Artifact conflicts: immutable versions, compare-and-swap, explicit review; never silent overwrite.
- SSRF/metadata leakage: no free-form URLs fetched by collaboration server, allowlist outbound integrations and block metadata networks.
- Supply-chain contamination: pin vetted dependencies, SCA, SAST, SBOM and reproducible container image process.
- University/restricted data: data classification and legal/institutional approvals before enabling external sharing or nonapproved model contexts.

## Non-negotiable pre-release tests
- User A cannot enumerate user B's rooms/artifacts/agents, even via IDs, search, event streams, notifications or cached previews.
- A guest cannot trigger tool execution, read terminal buffers, retrieve internal paths or follow private artifact links.
- A forged agent event cannot impersonate an owner or project admin.
- Revocation blocks subsequent REST, stream and agent-bridge actions.
- Prompt injection saying 'ignore your instructions' cannot override task ACL, permitted tools, or approval rules.
- Parallel artifact proposal does not overwrite either author's work.
- Client retries don't duplicate task execution or published artifact versions.
- Existing local single-user CLI/socket operations still work after feature is enabled/disabled.

## Release gate
Threat modeling review by a security-qualified engineer, authorization integration tests, external guest penetration testing, secrets handling review and documented deployment hardening. Do not market as 'no security exposure'; aim for least privilege and verifiable isolation with residual risk documented.

## T10 addendum: external coworker / remote agents
Scope: an agent run by another person on another machine is enrolled into ONE room by that room's owner (invite code,
redeemed at `/agents/join`). It is an HCN agent record with an `external` block, shown everywhere as its own principal kind.
Code: `herdr_engineering/control_plane/collab.py` (`create_external_invite`, `_redeem_external`, `_effective`,
`external_heartbeat`, `external_lineage`, `list_room_externals`, `revoke_external`), `collab_external.py`; tests
`tests/control_plane/test_collab_external.py`. Operator notes: `docs/operations/external-agents.md`.

| Threat | Control | Test |
|---|---|---|
| Agent impersonation (claims to be a person, another agent, the operator, or the owner) | Identity comes only from the credential; every extra identity/scope field on a lineage body is a 400 and audited `forged_identity` (on messages too, via the existing check); audit rows carry `actor_kind` (`external`, `user`, `agent`), and refused lineage writes outside its namespace are audited (`external.lineage`, denied, at most once a minute per reason); actor kind `external`, sender type `external`; lineage actor is never `operator`; node ids must be in the agent's own namespace and are reserved against every other writer | `test_lineage_cannot_forge_identity...`, `test_the_namespace_of_an_external_agent_is_reserved...` |
| Token replay / theft | Single-use invite stored only as a digest; credential bound to a per-enrollment device id; hard expiry (default 24 h, max 7 d) that rotation cannot extend; rotation kills the old secret at once; revocation is a 401 on the very next request; streams and long-polls end | `test_credential_is_bound_to_its_device...`, `test_revocation_cuts_access_at_once...` |
| Privilege escalation to operator / terminal / tasks | Actions are capped at `read`, `post` in `_effective` whatever any later grant says; no `instruct`/`execute`; excluded from `terminal_principals`, `authorize_workspace` terminal, pane links, task routes, roster, flow, room bundle, link and mission routes, fleet routes; the allow-list sweep drives every HCN route (including the workspace authorize/unlink/primary/grants, member, invite, request, task and mission routes with real ids) and asserts only room chat read/post, `events/stream`, the roster-free room fetch, its own `agents/external/*` routes, rotate and leave answer, and that nothing the refused routes name changed | `test_only_an_allow_list_of_routes_answers...`, `test_no_terminal_pane_link...` |
| Confused deputy via room_id claims | The room is bound at invite time and never read from a request; `_effective` returns nothing for any other room, so a grant elsewhere is inert; lineage envelope room comes from the enrollment | `test_the_room_binding_makes_a_grant_elsewhere_inert` |
| Enrollment-link guessing | 192-bit random code, single use, TTL 1 min - 24 h, generic 401 for every failure, `auth_failures` counted, per-token throttle, per-IP limiter at the HTTP layer | `test_redeem_is_single_use...` |
| Stale presence shown as live | Heartbeats live in memory only; online means a heartbeat within 90 s of the server clock, else "stale" with the status withheld; a restart shows "no heartbeat since this service started", never the last status | `test_presence_is_online_then_stale...` |
| DoS by mass enrollment / heartbeats / lineage | 10 invites per owner per hour; 25 live agents + open invites per room; sponsor's agent limit; 12 heartbeats per minute that never touch the log; at most 10 durable status events per 10 minutes; messages at a third of an agent's rate and still inside the sponsor's budget; lineage 60 per minute and 2000 per credential | `test_invites_are_rate_limited...`, `test_heartbeat_validation_cleaning_and_budget`, `test_lineage_has_a_rate_limit...` |
| Prompt-injection-like content in agent-supplied text (labels, activity, lineage labels) | Tab label is written by the owner, not the agent; agent text is stripped of control/bidi/invisible characters, secrets redacted, length capped, tagged untrusted in the API (`untrusted_text`), escaped by every renderer, never added to MCP/A2A output or any prompt, never executable | `test_label_is_cleaned...`, `test_everything_an_external_agent_supplies_is_escaped...`, Playwright smoke |
| Revocation hides history or leaves a stale tab | Revocation changes the agent's state only: messages and lineage already stored stay readable; its tab remains, marked revoked | `test_revocation_cuts_access...hides_nothing...` |
| Data exposure to an external reader | Read means chat only: no roster (owner ids and emails), flow, tasks, membership/grant events, links or missions; nothing before its enrollment | `test_external_sees_chat_only...` |

Residual risk (accepted, documented): the invite code is a bearer secret until redeemed; the credential is a bearer secret
(device binding is a client-supplied header, as for every HCN agent); presence is reported by the agent and unverified;
a coworker's agent can post untrusted text into the room chat when the owner ticks "post"; message sender ids reveal
Cognito user ids of people who wrote messages the agent can read.
