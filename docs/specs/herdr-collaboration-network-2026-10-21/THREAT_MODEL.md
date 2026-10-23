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
| DoS by mass enrollment / heartbeats / lineage | 10 invites per owner per hour; 25 live agents + open invites per room; sponsor's agent limit; 12 heartbeats per minute that never touch the log; at most 10 durable status events per 10 minutes; messages at a third of an agent's rate and still inside the sponsor's budget; lineage 60 per minute and lifetime write counts of 2000 per credential, 5000 per sponsor and 5000 per room (separate principals do not share the graph's per-principal quota, so these cap them as a group; durable via reserve-ahead rows, typed 429 `external_lineage_quota:*`) | `test_invites_are_rate_limited...`, `test_heartbeat_validation_cleaning_and_budget`, `test_lineage_has_a_rate_limit...`, `test_one_sponsor_cannot_exhaust_the_pool...`, `test_the_room_allowance_is_per_room...`, `test_lineage_counters_survive_a_restart...` |
| Revoked credential's in-flight lineage event lands afterwards | Validity is re-evaluated inside the ingest ledger lock immediately before admission; refused 401, audited `revoked_in_flight`, uses no budget | `test_an_event_in_flight_when_the_credential_is_revoked_does_not_land`, `test_ingest_evaluates_the_validity_callback...` |
| Hidden or look-alike text in labels, attrs, lineage labels, external chat | One cleaner: control, format (zero-width, ZWJ/ZWNJ, bidi, tag, musical), CGJ, variation selectors, object-replacement, fillers, private-use, surrogate and unassigned code points removed; applied to external senders' chat (line breaks kept); attr names limited to `A-Za-z0-9_.-` (64) | `test_clean_text_removes_invisible_characters`, `test_an_external_senders_chat_text_is_cleaned...`, `test_lineage_attr_names_outside_the_id_alphabet_are_refused` |
| Enrollment-link guessing source unknown | Failed redemptions counted per client address (existing proxy convention, bounded 256, aggregate only); successful enrollments record the address in the audit row | `test_failed_redemptions_are_counted_per_client...`, `test_a_successful_enrollment_records_the_client_address...` |
| Prompt-injection-like content in agent-supplied text (labels, activity, lineage labels) | Tab label is written by the owner, not the agent; agent text is stripped of control/bidi/invisible characters, secrets redacted, length capped, tagged untrusted in the API (`untrusted_text`), escaped by every renderer, never added to MCP/A2A output or any prompt, never executable | `test_label_is_cleaned...`, `test_everything_an_external_agent_supplies_is_escaped...`, Playwright smoke |
| Revocation hides history or leaves a stale tab | Revocation changes the agent's state only: messages and lineage already stored stay readable; its tab remains, marked revoked | `test_revocation_cuts_access...hides_nothing...` |
| Data exposure to an external reader | Read means chat only: no roster (owner ids and emails), flow, tasks, membership/grant events, links or missions; nothing before its enrollment | `test_external_sees_chat_only...` |

Residual risk (accepted, documented): the invite code is a bearer secret until redeemed; the credential is a bearer secret
(device binding is a client-supplied header, as for every HCN agent); presence is reported by the agent and unverified;
a coworker's agent can post untrusted text into the room chat when the owner ticks "post"; message sender ids reveal
Cognito user ids of people who wrote messages the agent can read.

## T11 addendum: bounded task delegation and artifact proposals
Scope: a room owner delegates a bounded task to an external agent invited with `proposals`; the agent answers only with an artifact
proposal that sits in quarantine until a room owner accepts it, and acceptance records it in the lineage graph without applying
anything. Same service, task store, event log and credentials as T10; MCP, A2A and REST call the same `CollabService` methods.
Code: `collab.py` (`create_delegation`, `delegation_respond`, `submit_proposal`, `withdraw_proposal`, `decide_proposal`, `_promote`,
`is_hcn_node`), `collab_delegation.py` (all validators), `collab_mcp.py`, `collab_a2a.py`, `ingest.py` (`service_node`), `packages/collab-proposals.js`.
Operator notes: `docs/operations/bounded-delegation.md`. Tests: `tests/control_plane/test_collab_bounded.py` (T = test), `tests/test_central_config_servers.py`,
`packages/herdr-web-client/collab-proposals.test.js`, `tests/playwright/proposal-quarantine.test.mjs`.

| Threat | Control | Test |
|---|---|---|
| Delegate exceeds scope (writes or refers outside the delegated paths, nodes, questions; wrong kind) | Every patch path and every `refs` entry is checked against the owner-written scope in one place (`collab_delegation`), refused 403 `out_of_scope` / `kind_not_allowed`, audited; the scope grants no read access to anything | `test_every_refusal_is_the_same_on_service_rest_mcp_and_a2a[outside_scope_*, kind...]`, `test_well_formed_patches_are_parsed_exactly` |
| Delegate exceeds budget (proposals, steps, bytes, tokens, time) | `max_proposals`, `max_steps`, `max_artifact_bytes` counted by the server; overrun of steps/reported tokens closes the task (`failed`, 409 `budget_exhausted`, audited); expiry is absolute and capped by the credential; tokens are declarative (documented) | `test_proposal_step_and_token_budgets_close_the_task`, `test_expiry_stops_the_delegate_but_not_the_review...`, `[oversize]`, `[tokens_over_budget]` |
| Proposal carries prompt injection | Content is stored and rendered as inert data: `trust: untrusted`, `executable: false`, escaped text only in the UI, never added to any model prompt by this service, link shown as text; instruction-like phrases only raise a reviewer warning (a hint, not the control); nothing is ever applied | `test_the_injection_hint_is_advice_for_the_reviewer_not_a_control`, `test_hostile_proposal_text_is_escaped_everywhere...`, Playwright `hostile proposal text is inert` |
| Malicious patch (mode bits, symlink, submodule, rename, git internals, CI files, hook paths) | Strict unified-diff parser: hunk counts must add up; refuses modes other than `100644`, mode changes, renames/copies, `.git*`, `.husky`, `.github/workflows`; scope-bound paths; hidden characters refused so the reviewed text is the applied text | `[symlink_mode]`, `[exec_mode]`, `[mode_change]`, `[rename]`, `[git_internals]`, `test_malformed_patches_are_refused` |
| Oversized or binary payload | 32 KiB hard cap (task cap lower), UTF-8 text only, NUL/control/invisible refused, `GIT binary patch`/`Binary files` refused, 2000 lines / 20 files | `[oversize]`, `[nul_byte]`, `[binary]`, `[zero_width]`, `[bidi]` |
| Path traversal / absolute paths / symlink escape | `norm_path` on scope and patch paths (no `..`, `.`, empty, absolute, drive, `:`, backslash, trailing dot/space); symlink creation refused; the server never opens a path (it validates text) so a symlink already on a workspace is the applier's duty (documented) | `[traversal]`, `[absolute]`, `test_bad_paths_are_refused`, `test_creating_a_delegation_is_strictly_validated` |
| Secret exfiltration through a proposal or link | Secret-like strings refused (not stored); links https-only, no userinfo, public DNS name, port 443, never fetched | `[secret_like]`, `[http_link]`, `[js_link]`, `[private_link]`, `[userinfo_link]` |
| Delegate impersonation / spoofed provenance | Identity, room, sponsor and delegate id come only from the credential and the stored task; extra identity/room/mission fields are a 400 (audited `forged_identity`); the graph node attrs are server-built, delegate text only appears as a cleaned title; the `task:hcn-delegation.*` and `artifact:hcn-proposal.*` ids are reserved on every endpoint of an edge so no ingest key or delegate can write, squat or attach to them; an API key whose role is spelled `external`/`collab` is unprivileged | `[forged_room]`, `[forged_agent]`, `[respond_forged]`, `test_nobody_but_the_service_can_write_or_attach_to_accepted_proposal_nodes`, `test_real_server_delegation_end_to_end_and_the_ingest_key_cannot_forge_provenance`, `test_the_graph_node_carries_only_server_derived_provenance` |
| Replayed accept | Accept is terminal and idempotent: the same decision again is `replayed: true` with no new event or graph write; the opposite decision is 409; needs the digest of the reviewed content | `test_accept_is_idempotent_terminal_and_never_applied_twice` |
| Accept of a superseded / expired / withdrawn proposal | State machine: only `quarantined` can be decided; a newer proposal supersedes atomically in the same event; expiry computed from the server clock | `test_a_superseded_proposal_cannot_be_accepted`, `test_an_undecided_proposal_expires...`, `test_the_delegate_can_withdraw...` |
| Stale task state (cancel/expire/answer races) | Every state-changing delegate call carries `expected_version`; the version bumps on every task, progress and proposal event; stale = 409 `stale_task_state`; answers and proposals are refused unless the task is `accepted` and unexpired; replays by idempotency key or content digest return the first result | `[stale_version]`, `[respond_stale]`, `[no_version]`, `test_propose_before_accept_and_after_the_task_is_closed`, `test_replaying_a_proposal_or_an_answer_costs_nothing` |
| MCP vs A2A parity bypass (one door checks less) | MCP tools, A2A methods and REST routes are thin translations onto the same service methods; the 34-case refusal table runs on service, REST, MCP and A2A and must give the same status and code; decide/delegate are not offered on MCP/A2A | `test_every_refusal_is_the_same_on_service_rest_mcp_and_a2a` (x4), `test_deciding_is_not_offered_over_mcp_or_a2a...`, `test_a2a_tasks_get_list_cancel_map_onto_the_same_store` |
| Confused deputy through room / mission claims | The room is the task's stored room, bound to the delegate's enrollment; a body that names a room, mission or agent is refused; a delegate sees only tasks addressed to it in its room (same 404 for every miss); accept/get require ownership of the proposal's own room (path room must match) | `test_a_task_of_one_room_or_one_agent_is_invisible_to_every_other_delegate`, `test_only_a_room_owner_delegates_and_decides_and_never_an_agent` |
| Privilege escalation (execute, terminal, instruct, decide own proposal) | The delegate keeps `{read, post}` only; bounded tasks never appear in `tasks/inbox`; `create_task`/`update_task` stay refused for it; deciding is a person's token on REST only | `test_deciding_is_not_offered_over_mcp_or_a2a_and_the_delegate_holds_no_execute_right` |
| Revoked delegate's work gets accepted | Revocation/removal makes its quarantined proposals unacceptable (409 `delegate_revoked`; reject still works); cancelled/declined/failed task likewise | `test_a_revoked_delegate_is_locked_out...`, `test_a_cancelled_delegation_cannot_have_its_proposals_accepted` |
| DoS by many proposals / delegations | Per-delegate 6 per 10 min, lifetime 30 per credential, 150 per sponsor, 40 in quarantine per room, 3 open delegations per agent, 20 delegations per owner per 10 min, 120 inbox polls and 60 answers per minute, 60 decisions per minute; log-full cap still applies | `test_quarantine_quotas_and_rate_limits_bound_the_damage`, `test_delegations_are_rate_limited_capped_per_agent_and_idempotent` |
| Kill switch ignored / half wired | `delegation_enabled` (YAML, default false) on `CollabService`; checked in every delegate and owner-accept entry point; MCP tools / A2A skill hidden; wired for combined and `--collab-only`, validated as boolean | `test_the_kill_switch_stops_everything_but_the_safe_directions` (x4), `test_the_feature_is_off_by_default...`, `test_delegation_kill_switch_*` |
| Lost or duplicated graph write | Decision is durable first; graph writes use ids derived from the proposal id; a failed write leaves `promoted: false` and a repeated accept finishes it | `test_a_failed_graph_write_leaves_the_decision...`, `test_state_survives_a_restart...` |

Residual risk (accepted, documented): a hostile patch can still be wrong or malicious code that passes every syntactic check, and
only a human reading it stops that; the server cannot see workspace symlinks; token budgets are advisory; a compromised room owner
account can accept anything; proposal content lives in the HCN log until the log is rotated; a security-qualified review (T16) is still required.
