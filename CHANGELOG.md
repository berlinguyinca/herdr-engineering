# Changelog

All notable changes to HerdR Engineering are documented here. Dates are
YYYY-MM-DD. The format follows Keep a Changelog.

## [Unreleased]

### Unified workspace P02: persistent Work Graph and reconciliation (flag off by default)
- Migrations `0031_event_trace_id.sql` (nullable `fabric_events.trace_id`, `Event.trace_id`, envelope `trace_id`) and `0032_work_graph.sql` (`wg_*` tables: event ledger, assertions, entities + aliases, typed edges with intervals and unique `(type, from, to)`, historical facts, layouts, cursor, gaps). Additive, idempotent, `-- DOWN:`.
- `work_graph_core` (pure fold: idempotent, order-tolerant, per-field merge, tombstones that cannot be resurrected, immutable artifact versions), `work_graph_store` (Postgres, ACL inside every statement, cursor paging, search, snapshot by root, `as_of`, facts, layouts with ETag/If-Match), `work_graph_sync` (sources, reconciler with orphan check in both directions, replay-from-clean with `snapshot_required`, worker), `work_graph_service` (sync facade + request handler).
- New `GET/POST /api/v1/graph/work/*` and `/api/v1/collab/graph/work/*` behind `control_plane.unified_workspace.enabled`; 404 when off, 503 without Postgres. The lineage graph vocabulary gains the owned kinds `project team artifact_version external_action` and edges `contains derived_from discussed_in approved_by notified_via`.
- Contract: `docs/contracts/work-graph-v1.md`; Pi session-ingest ids: `docs/specs/herdr-unified-workspace-20261028/contracts/PI_INGEST_FIELDS.md`.

### Unified workspace P01: contracts, ids and the capability/action broker (library only, flag off)
- New `schemas/workspace-v1.schema.json` (JSON Schema 2020-12) for project, team, room, mission, issue, task, agent, agent_session, host, artifact, artifact_version, review, decision, external_action, event and the 14 typed edges, with canonical ids `urn:herdr:<kind>:<uuid>` (uuid5 over the native id, so every host agrees), aliases, scope, version/ETag, trace/correlation/causation ids and reference URIs. Contract table and graph.py mapping: `docs/contracts/unified-workspace-v1.md`.
- New `herdr_engineering/control_plane/workspace_contracts.py`: id/alias canonicalisation, validators, ETag compare-and-swap, idempotency tokens, a discover/authorize/approve/invoke broker (user role, agent delegation, room grant, artifact grant, account grant and risk policy are intersected; room membership never grants shell or external-account access), untrusted-content tainting, ACL re-check hooks for traversal, search, export and subscriptions, and the replay-cursor contract.
- Capability `unified_workspace_v1` in `/api/v1/health`, only when `control_plane.unified_workspace.enabled: true` (YAML, default off). No existing route, table or graph vocabulary changes; nothing calls the broker yet. `jsonschema` joins the `dev` extra for the contract tests.

### Skill registry on registry artifacts (flag off)
- `control_plane.skill_registry.enabled` (YAML, default false) adds `/api/v1/skills` publish, search, get, 60-second transfer capability, download and install receipt. Skills are `artifact_type='skill'` registry artifacts (immutable versions, signed package in RustFS, sharing = artifact ACL grant); no new tables, no CSP change. Extracted from #366. See `docs/contracts/skill-registry.md`.

### Control plane: inspector replay snapshots (opt-in)
- `control_plane.inspector_snapshots.enabled` lets the worker keep a replay snapshot of the inspector in the new `inspector_snapshots` table (migration 0030) so a boot replays only newer events instead of ~780k (about nine minutes). Off by default; any doubt makes the boot replay everything. See `deploy/control-plane/README.md`.
- Deploy health window 30 minutes (#373) and boot phase timings in the log; host-metrics rollups replay in linear time (#379).

### Dashboard: on-demand full `/summary` report
- **Generate full report** on the agent page runs the `summary` skill on the session's recorded conversation and shows the result in the summary card; in the fullscreen workspace it gets its own column so the conversation and composer fit the viewport without page scrolling (the fullscreen panes were previously sized to the whole viewport and cut off the composer). Off by default: `summary_report.enabled` in the central YAML.
- Runs on the host that owns the session, not in the control-plane container (which has no session records and no `claude`): the control plane queues the request (`POST .../summary/report`, durable event), the per-host session service claims it (`POST /api/v1/host/summary-reports/claim`), runs `herdr-eng summary report` locally and posts the result back, which the server validates and redacts again before storing. Unclaimed requests fail as `host_unreachable`, stuck ones as `timeout`. Roll the plugin to each host.
- Read-only by construction: the host collects the session history and read-only git facts itself and gives a fresh, non-persisted `claude -p` run no tools; the target session is never resumed or prompted. The report is validated (`# Session summary` ... Evidence coverage) and served as `full_report` on `GET /api/v1/sessions/{id}/summary`.
- `herdr-eng summary report` prints the same result as JSON. `herdr-eng summary history` now returns the session's recorded `cwd` and finds a session by id without `--agent`. The skill gains a target mode (`/summary <agent> <session-id>`, optionally with prepared `SUMMARY_EVIDENCE_JSON`). Codex as summarizer is implemented but not enabled until it has run live. See `docs/operations/session-summary.md`.

### Dashboard and terminal: `/summary` publishes, the report is easier to read
- Terminal `/summary` and `$summary` publish the report to the dashboard (`herdr-eng summary publish`, `POST /api/v1/host/summary-reports/publish`) and open a read-only side pane, then print a single line instead of scrolling the whole report. If publishing fails the full report is printed.
- Dashboard: **Quick summary** / **Full report** buttons; a status strip with overall status, evidence-coverage badge and resume point; collapsible sections; a stale badge ("N newer messages since this report") with one-click regenerate; elapsed time while generating; **Copy as markdown** and **Send to agent**; timestamps that jump to the matching turn; a failed quick summary shrinks to one line when a full report exists. Fullscreen hides usage rows by default and the Activity pane can be collapsed.
- Evidence: the history helper records every distinct `cwd`, and the report inspects the latest worktree that is a repository and lists earlier ones as `other_worktrees` (it previously inspected the first directory, which was wrong once a session moved into a worktree). Jobs and completions carry `basis_seq` for the stale check.

### Fix: idle Codex agents showed an empty dashboard and could not be summarized
- Codex does not keep its rollout file open between writes, and Herdr reports no `agent_session` for it, so a Codex agent's dashboard page said "Waiting for this agent to identify its transcript" with no conversation, no activity and `recording_unavailable` in the summary card. The feed plugin now also reads Codex's own state database (`logs_*.sqlite`: owning `pid:<pid>:...` and `thread_id`) as process evidence. It is the weakest evidence (only used when no fd, descendant or native identity names a conversation), rows must be newer than the process (reused pids are rejected), only a primary conversation counts, and a tie is never guessed.
- `herdr-eng summary history` uses the same rule, so `/summary` and the dashboard agree on which Codex conversation a pane holds. Deploy the plugin (`deploy/herdr-cp-feed-plugin`) on each host to pick this up.

### Fix: macOS release staging
- `docs/specs/herdr-codeboarding-native-port-2026-10-23/AUDIT.md` collided with `audit.md` on case-insensitive filesystems, so every fresh checkout of `main` since #194 looked dirty on macOS and `fleet-sync` refused to stage it. The requirements stub is now `AUDIT-REQUIREMENTS.md`.

### `/summary` session-recovery skill (default install and fleet converge)
- New canonical read-only `summary` skill (`skills/summary`) installed by `scripts/install.sh` and converged on every fleet host (`ansible/tasks/summary-skill.yml`). `/summary` in Pi, Claude Code and OpenCode; `$summary` in Codex (no Codex command file is written). Evidence-based: separates verified from reported work and states an explicit coverage verdict.
- `herdr-eng summary install|uninstall|verify|history`. Install/upgrade is manifest-tracked and never overwrites a customized skill, prompt or command (`preserved`); uninstall removes only unmodified files and our own links. `history` is read-only: it resolves the session from an explicit id, then Herdr pane metadata, and only then a labelled directory heuristic; it reads Claude/Pi/Codex JSONL and OpenCode SQLite (`mode=ro`), marks compaction and child-agent gaps, and redacts output. See `docs/operations/session-summary.md`.
- `herdr-eng fleet status` now shows `summary=current|differs|MISSING|unknown` per host (informational; never fails parity, since a customized skill is deliberately preserved).

### Collab addressing (`@owner.harness`, sigil completion, mention chips)
- Release note: a room task's text now starts with the target's `@address` (e.g. `@gert.pi fix the build`). The worker in this repo strips its own leading address; worker scripts installed from an earlier release pass it through to the harness unchanged, so update them.
- An external (sponsored) agent never gets an `@owner.harness` address; it is reached by its `ext-...` name and its chip is marked external. A person handle outranks an agent name, an agent cannot be renamed to (or granted under) a member's handle, and an agent keeps its last 5 old names as aliases.

### T17 / ADR-0008 P2: peer registry, room shares and signed read-only export (default off, YAML only)
- New: `control_plane.federation` gains the peer registry (`public_key` pinned per peer, per-peer `enabled`, `export_rooms`, `requests_per_minute`, `max_bytes_per_hour`, `key_expires_at`, `rotate_after`, `next_public_key` for rotation overlap) plus `server_id`, `signing_key_file` and the validity knobs. Still default off, hot-reread, no environment variable; an unreadable file, an invalid section or a non-boolean `enabled` is off.
- New collab events `federation_share_granted` (revocable creation), `federation_share_revoked` (in `SAFETY_EVENTS`, pre-paid), and `federation_peer_enabled` / `federation_peer_disabled` (the YAML change record and its pre-paid revocation). A room owner accepts a share (`POST /api/v1/collab/rooms/<room>/federation/shares`); the operator YAML must also list `{peer, room}`; any owner revokes, and the kill switch never blocks a revocation. Members can see that their room is shared.
- New `GET /api/v1/federation/shares` and `/shares/<id>/events`: read-only, authenticated by an Ed25519 request signature (pinned FULL key, audience, timestamp window, single-use nonce), answered with exports signed over the exact bytes, a bounded validity window, a per-type FIELD ALLOWLIST (`message` only; a pseudonymous sender, no ids, emails, mentions or context) run through `redact.py`. `org`/`private`/`mission` scope and history from before the grant never leave; every unshared, revoked, unlisted or unauthenticated case is one byte-identical 404.
- Fixed (P1 gap): `PeerPolicy.matches` compared only 12 hex characters of the key fingerprint. It now compares all 64 (`hmac.compare_digest`); the peer principal carries the full fingerprint and a peer ingest event is admitted only for the pinned key. `mirror_room` is now optional (an export-only peer needs none; a peer without one can never write). A P1-shaped YAML (a 12-64 hex `key_fingerprint` and no `public_key`) still parses but can no longer authenticate or ingest: add `public_key`.
- No puller, importer, mirror room or typed write (P3/P4). Signing uses `cryptography` (already installed with the `collab` and `review-gate` extras; new `federation` extra names it), imported lazily: without it federation is off. Contract: `docs/contracts/federation-export.md`. Rollback: revert the PR (kill switch `control_plane.federation.enabled: false`).

### T17 / ADR-0008 P1: `peer` principal and visibility clamp (flagged dark, no network code)
- New per-principal visibility clamp in envelope normalisation (`federation.Clamp`, `normalize_envelope(..., clamp=)`): an event from a `peer` principal is rewritten to `visibility_scope: room` in its one local mirror room, never wider, with the sender's origin, requested scope and local ids kept only as `claimed_*` provenance. It cannot be skipped: a peer without a clamp, and any v1 or non-`lineage` peer event, is refused.
- New `peer` principal kind, never `operator`, `collab` or `external`, and never mintable from an API key. `control_plane.federation` (YAML only, default off, hot-reread, any error means off; `"*"` rejected for a peer) is parsed and enforced; startup refuses a bad section. `_check_lineage_scope` and `_check_reserved_nodes` treat a peer as non-operator.
- New reserved lineage namespace `peer:<peer>.` (node type `peer`): writable by nobody locally (operator included); a peer may write only inside its own namespace on every edge endpoint. Mirrored edges take no part in local gate staleness (fork promotion, herdr#264).
- No endpoint, puller, network code, environment variable or migration. Rollback: revert the PR (the kill switch is `control_plane.federation.enabled`, default false). Contract: `docs/contracts/event-envelope-v2.md`.

### T17 Independent-server federation ADR (docs only)
- ADR-0008 is now Accepted with the owner's answers to D1–D10 recorded; implementation starts with P1 (visibility clamp and `peer` principal kind), flagged off.
- `docs/adr/ADR-0008-independent-server-federation.md`, status Proposed, awaiting approval: options, trust model, data flow, failure modes, migration, non-goals and ten owner decisions. No code, configuration, endpoint or migration; nothing is implemented until the ADR is approved. Rollback: revert the PR.

### T16 Herdr-side gaps from the Pi and AutoSpec wiring
- Steering: an ingest key can read the CURRENT fence and gate digest at apply time: `GET /api/v1/steering/targets/{node_id}` (owner of the node, live
  request, room-scoped nodes included) and `fence`/`digest`/`digests` on `GET /api/v1/graph/nodes/{id}` for nodes the key may read. A `fork` may carry
  a bounded, redacted `instruction` (the changed requirement), delivered in the pending item like a redirect's.
- Review quorum: `POST /api/v1/graph/nodes/{pr}/review-quorum/rereview` records a durable, idempotent-per-head, scoped (trusted head writer, reviewer or
  operator), rate-limited, audited re-review request; the quorum read shows it as `rereview_request` for that head.
  A head may be asked about again once its latest ask is older than `review_quorum.rereview_retry_seconds` (YAML, default 3600); inside that window
  AutoSpec owns retries. Review-gate worker keys are refused on the route (403); the gate's own re-reviews are a separate, independent mechanism.
- External agents: workspace-link authorize no longer tells a non-owner whether a link id exists; MCP `tools/list` is per agent (an external agent is never offered the task or helper tools); delegation budgets accept
  and enforce `max_turns` (an answer or proposal past it closes the task; a `complete` answer is always accepted, and a task stored without `max_turns` has no ceiling; and echo `max_hops`, the chain-depth ceiling); a consultation also records `asker -> target` as a graph edge (reference nodes).
- Acceptance: four Herdr strict xfails became real tests; the manifest minimums rise (s2 20, s5 15); no minimum is lowered.
- No migration, no environment variable. Rollback: revert the PR. Checked by loading rows of this stream into the previous build: it starts and serves normally and the rows do no harm, but it does NOT keep them out of its event feed (it lists them as ordinary events, each carrying only the head and request id); the stored rows stay in the log and are ignored again once this code is back.

### T16 security fixes, Herdr side (stacked on the T16 release-gate branch)
- Ingest is an allow-list of entity types; `collab`, `ci_*` and other adapter-owned streams and the server's session receipts can no longer be forged
  by an ingest key (H1, M1, M5). Foreign `collab` rows already in a database are quarantined at startup, and a corrupt collab log disables collaboration
  alone with an ERROR instead of stopping the control plane.
- A full collab log still revokes: creating a revocable thing pre-pays its revocation (cannot be drained by create/revoke cycles or presence flips); chat stops at 90 %, other writes at 95 %, revocations may use 100 % (H2).
- `collab-repair` (dry run by default) sets aside foreign `collab` rows without deleting; a corrupt log disables collaboration only and shows in `/api/v1/health` (H1).
- `replay_history: since_join` now also covers node/edge/listing reads and the insights/progress snapshots (M2, L2).
- All collab feature switches (`steering`, `contextual`, `insights`, `delegation_enabled`, `replay_history`) are hot and fail closed the same way,
  including under `--collab-only` (M3, M15). One consistency test enumerates them.
- `/api/v1/ingest/sources` is operator-only for other principals' sources (M4); gap state is partitioned per key (M6); trusted evidence writers are
  selected before the contribution cap (M7); the quorum route hides a head the reader cannot read (M9); the generic rate limiter keys clients behind a
  proxy by their forwarded address (M12); an open listener is no operator in a reserved namespace (L1).
- Test: the graph neighbourhood budget test counts rendered nodes instead of timing a wall clock.
- Ingest sequence-conflict 409 now says `reason: "sequence_conflict"` and `last_sequence` (the caller's own source only) so a restarted emitter can resume (Pi H6).
- Round 4: the revocation reserve counts every unrevoked join token and every member including expired guests (revoking those emits an event); repair/server
  locking works on filesystems without `flock` (lock-file fallback, never a startup crash), re-checks the inode after locking, and refuses when no lock can be proven;
  repair on a pre-0003 database says to migrate first; degraded operator reads answer 503.
- Round 3: safety events are idempotent (a repeated `revoke_agent`/`external_leave`/user decision commits nothing, so they cannot fill the log); the
  revocation reserve is recomputed from state; `collab-repair --apply` works on a real PostgreSQL (tid binding fixed, tested against a throwaway
  server), refuses to rewrite a log a live server holds, reports a gap tail it cannot fix by default, and also quarantines rows whose
  timestamps disagree; unauthenticated callers cannot see that collaboration is degraded; a shared-mission event written from another room is shown
  to the reader after their join.
- Round 5 (after merging main): main's panic switch, helper cancel and host Remove work on a nearly full log (`helpers_paused`, `helper_cancelled`,
  `egress_removed` are safety events; helper requests, host approvals/promotions and owner-listed hosts pre-pay their revocation); a degraded start
  writes nothing into the fabric graph and keeps unknown agent namespaces reserved; ingest still accepts the system-graph adapter's
  `repository`/`request`/`span` and refuses the review-gate prefixes; the `.lock` fallback records `host pid` (another host's lock is live, our own
  pid is stale, so a container restarting as PID 1 starts; caveat: a recreated container gets a new hostname, so its predecessor's lock must be
  removed by hand); `collab-repair` keeps the log's owner and mode. The since_join id map is capped at 1,000,000 ids (oldest evicted first, an
  evicted id is pre-join: fail closed), see docs/contracts/lineage-events.md.
- No migration, no environment variable. Rollback: revert the PR.

### T16 end-to-end acceptance harness
- New `tests/e2e` suite (scenarios S1-S7) driven by `scripts/run_acceptance.py` against real servers, a real Postgres and a fake `gh`.
  It is opt-in: plain `pytest` and `scripts/validate.sh` deselect everything marked `e2e`; the runner passes `-m e2e`.
  Local overrides (autospec binary, Pi checkout, Postgres bin dir) go in the git-ignored `tests/e2e/acceptance.local.yaml`; no environment variables.
- The runner's verdict guards reject non-strict or bare `xfail`, and report "nothing selected on the herdr side" (pytest exit 5)
  instead of a failure when `--only` picks Pi-only scenarios.
- Redaction: `author`, `authors`, `author_login` and `author_name` keys are no longer redacted by name (review quorum compares the
  author login, and a redacted value let an author approve their own PR). The `sk-` pattern now also matches after an ANSI colour
  code or `%3D` and keeps ids such as `task-sk-ab` intact.

### Attention inbox, evidence-based progress and optimization analytics (T15, stacked on #174, #173, #168, #169)
- New read routes `GET /rooms/{id}/attention|progress|analytics` and `POST /rooms/{id}/attention/ack|snooze|restore`, plus Attention and Progress
  tabs. Everything is derived from what the reader can see in the room graph, review quorum (T06), steering/promotion (T12/T13), proposals (T11),
  consultations (T14) and the external-agent roster; nothing is copied into a second store. Ack and snooze are per reader, bounded and bound to the
  state of the item, so a changed condition comes back.
- Completion counts only evidence a configured trusted writer attests (merged pull request, validated edge, accepted proposal); an agent saying "done"
  is shown but never counted. Estimates state their method, confidence and assumptions, or say "insufficient evidence".
- Analytics (cycle time, rework, re-review, approach win rate, blocked/idle time, self-reported cost) cover only visible items, hide groups smaller than
  `min_group_size`, and have no per-person or per-agent breakdown.
- New YAML `control_plane.collab.insights` (off by default, `kill_switch`). No environment variable, no migration, no Pi change.
- Rollback: `insights.enabled: false`, or revert the PR.

### Contextual chat, consultation and cross-workspace links (T14, stacked on T11 #172 and T12 #171)
- Chat messages can carry one validated reference to a graph node, edge, comment or proposal. It is checked when posted and again for every
  reader of every read; a reader who cannot see the target gets one fixed "hidden reference" and nothing else (agents, remote agents and guests always do).
- Consultation: a member asks another member or a remote agent one bounded, expiring question about a graph node and gets one text answer
  (never a task, proposal or action). The graph records who asked whom about what, never the text. New MCP tools `consult_inbox`, `consult_answer`.
- Cross-workspace links: new edge kind `cross_link`, authorized on both ends and written only by the HCN service for one room; revocable and
  self-expiring when the room loses a workspace grant. See `docs/contracts/lineage-events.md` ("T14").
- New YAML `control_plane.collab.contextual` (everything off by default, `kill_switch`). No environment variable, no migration.
- Rollback: `contextual.enabled: false`, or revert the PR.

### Alternative implementation forks, comparison and promotion (T13, stacked on T12)
- New steering actions `fork` and `promote` (opt-in via `steering.actions` and `steering.forks.enabled`), a comparison read model
  `GET /api/v1/collab/rooms/{id}/forks`, and a Forks panel in the room workbench. Fork preserves the original; nothing is deleted.
- Promotion is two-step and human-confirmed, promotes exactly one fork at an exact head, and never merges. See `docs/contracts/lineage-events.md` ("T13").
- Rollback: revert the PR (code only; no migration).

### Bounded task delegation and artifact proposals (T11, stacked on #170, #167, #165, #164)
- A room owner can delegate one bounded task (scope, budget, expiry, allowed output kinds) to a coworker's external agent invited with
  `proposals: true`. The agent answers only with an artifact proposal (patch, doc, analysis or link) that waits in quarantine; a room owner
  accepts or rejects it with the digest of what was reviewed. Accepting records `task:hcn-delegation.*` and `artifact:hcn-proposal.*` nodes and a
  `produced` edge in the lineage graph; nothing is ever applied. Same checks over REST, MCP (4 tools) and A2A (`tasks/get|list|cancel`, `message/send` with a `taskId`).
- New YAML kill switch `control_plane.collab.delegation_enabled` (default `false`; works for the combined and `--collab-only` servers). No environment variable.
- Extends the existing task store, event log and external credentials; new log events are additive; no migration. Defence in depth (untested): an API key whose configured
  role is spelled `external` or `collab` is given principal kind `unknown`, never those service-only kinds. See `docs/operations/bounded-delegation.md` and the T11 addendum of the HCN threat model.

- Hardening after independent review: bounded validator regexes, cheap checks before parsing and parsing outside the lock, strict `---`/`+++` names, `/dev/null` vs hunk
  agreement, wider hard-coded deny-list, CRLF refused, task-list `bounds` limited to owners, open ingest listener refused for the reserved node ids.
- Rollback: `delegation_enabled: false`, or revert the PR.

### Lineage graph, Gantt timeline and replay (T09, stacked on #167, #165, #164)
- Room workbench: new read-only "Lineage" panel (`packages/collab-lineage.js`): layered lineage graph, Gantt timeline with
  head-change and stale markers, replay scrubber, filters and node inspector, each with a table alternative; keyboard
  operable; nothing encoded by colour alone; all ids and labels escaped.
- New read routes `GET /api/v1/collab/graph/lineage[/timeline|/events]` (room_id required, same membership 404, budgets, 240/min per person).
  Replay at a position equals the live projection at that point and never shows what the reader cannot see now.
  No migration, no new table (derived from the stored lineage events). See `docs/contracts/lineage-events.md` ("T09").
- Replay is per room (a busy room cannot break another's), visibility is scanned in 128-event chunks so viewers do not stall
  ingest, member responses no longer carry the global `admission` counter, the ledger window is counted in the reader's own visible events (exact steps, opaque page tokens), and the new
  operator YAML `control_plane.collab.replay_history` (`since_join` default | `full`) sets how far back a member may replay.
- Rollback: revert the PR (code only).


### Head-bound review quorum (T06)
- Review round 4: head handling is conservative. A lower-sequence write after the applied one is ignored only if it repeats
  the applied head or a head that writer already reported earlier; any other head makes the PR `head_unverified`
  (`head_unverified`/`alert` fields, one log line, operator `resync_event_id`). It clears when the writer writes past the
  applied sequence or an operator acknowledges the write with `attrs.head_resync`; a run of writes is never auto-adopted
  (`head_rebased` is gone, `head_resynced` replaces it). Evidence is kept per distinct head so re-emits cannot evict it.
- Review round 3: `rereview_needed` counts only what the caller can see. Matching is ASCII-only (same as autospec;
  `invalid_target`). Shared vectors grew to 32 cases.
- `GET /api/v1/graph/nodes/{pr_id}/review-quorum`: derived, head-bound quorum over `reviewed` edges (stale verdicts never
  counted, distinct configured reviewers, author/replay/forged-head defences, `rereview_required`). Config:
  `control_plane.review_quorum` in the fleet config. One policy shared with `autospec review-quorum` via
  `tests/control_plane/fixtures/review-quorum-vectors.json`; statuses `unknown_head`, `head_unverified` (emitter sequence
  reset), `unknown_author`; digests are 64-hex and bound; `reviewer_groups`. Contract: `docs/contracts/lineage-events.md`. Rollback: remove the
  block (endpoint answers 404); no schema or event change.

### Tabs/panes and membership-scoped lineage (T08, stacked on #165 and #164)
- Lineage `room`/`mission` scopes now resolve to HCN membership: an event that names its `room_id` / `mission_id`
  (and optionally `workspace_id`) is readable by that room's non-guest members / that mission's listed members (T07),
  through the new `GET /api/v1/collab/graph/...` routes (node, neighborhood, edge and a per-room type listing);
  everyone else still gets 404, and a signed-in person never reads `org` lineage. Bare `room`/`mission` stay
  operator-only and the keyed `/api/v1/graph` API is unchanged. No migration; reads only.
- Room workbench: new read-only "Tabs" panel (`packages/collab-tabs.js`): primary agents as tabs, spawned subagents as
  panes, from recorded `spawn` lineage only. Closing a pane never changes the agent.
- Breaking: `GET /api/v1/collab/graph/nodes` (listing) requires `room_id` (400 otherwise); a key with no
  `control_plane.lineage_key_scopes` entry can no longer write `room`/`mission` scope (403 `lineage_forbidden`; `"*"` opts in);
  mission scope needs `room_id`, so stored mission events without it become operator-only after rebuild.
- Rollback: see `docs/contracts/lineage-events.md` ("T08 rollout and rollback").

### Plannotator bridge: digest echo
- `herdr-plannotator plan` now prints the `plan_digest` (and `review_id`) the receipt was bound to alongside the upstream decision, computed by the bridge and authoritative over upstream output, so a caller can verify a decision belongs to the exact plan bytes it submitted. Code reviews are unchanged.

### YAML-only fabric agent configuration
- The fabric agent, its install scripts, the enroll-sync/service-metadata units and the Ansible fabric tasks no longer
  write or read `fabric.env` (`HERDR_ENGINEERING_FABRIC_URL`). The gateway URL is `fabric.url` in the central
  `fleet-config.yaml` (fallback: `fabric.gateway_url` in `config.yaml`). `--gateway`/`--control-plane` on the install
  scripts are ignored with a warning. Existing `fabric.env` files are left in place and are harmless.

### Lineage graph projection (T02, Herdr side)
- Migration `0004_sequence_domain_per_principal.sql`: the envelope sequence domain is `(actor id, source)`.
- Migration `0005_event_id_per_actor.sql`: `event_id` is unique per principal (global primary key dropped).
- Lineage review round 6: commons fallback primary is the writer who wrote the field first (stable under updates) and
  readers apply the earliest VISIBLE owner; neighborhood stops rendering neighbours once its budget is spent;
  commons admission-limit errors are generic; known-limitations list in the lineage contract.
- Lineage review round 5: commons field visibility is per writer (a widened node no longer exposes another writer's
  private fields); an empty ensure never makes the ensurer the commons owner; commons endpoints confer no tombstone
  authority; tombstoned edge instances free their slot; contribution caps keep operator/owner/own entries; migration 0005
  rollout order and rollback documented (`docs/operations/event-id-per-actor-rollout.md`).
- Lineage review round 4: admission mode/sequence are server-only (reserved `_` payload keys rejected); commons nodes keep
  an owner/operator primary value with attributed `fields`/`contributions`, widest-scope visibility, per-writer attr budgets
  and per-writer `from`-commons edge instances; `node.state` on a missing commons node creates it.
- Lineage refusals are HTTP 403 `{"reason": "lineage_forbidden"}`; shared COMMONS nodes (commit/pr/issue/ci/artifact) and
  idempotent "ensure" upserts are accepted from any ingest key.
- `lineage` envelope events (v2 only) project into a durable graph derived from `fabric_events`
  (no new table; rebuilt on hydrate). See `docs/contracts/lineage-events.md`.
- `GET /api/v1/graph/nodes/{id}`, `.../neighborhood`, `/api/v1/graph/edge`: viewer-level reads,
  visibility-filtered at every hop, depth/node/edge capped, fail closed.

### Event envelope v2 (T01, Herdr side)
- `POST /api/v1/ingest` accepts `schema_version: 2` bodies per
  `docs/contracts/event-envelope-v2.md`: server-set `actor`/`received_at`,
  idempotent `event_id`, `(source, source_sequence)` collision and gap tracking,
  409 on conflicts, fail-closed `visibility_scope`. v1 bodies are unchanged.
  `GET /api/v1/ingest/sources` reports per-source sequence, gaps and freshness.
- Migration `0003_event_envelope_v2.sql` adds nullable envelope columns, a
  partial unique index and `fabric_event_sources`.
- `EventStore.append` no longer reports every error as a duplicate: only an
  existing `event_id` returns False; other failures propagate.

### Terminal button on agent pages
- An agent page has a **Terminal** button (operator only). It shows the exact command
  that attaches a terminal on the viewer's own machine to the agent's HerdR pane:
  `ssh -t <host> "bash -lc 'herdr --session <s> agent attach <pane>'"` from another machine,
  or `herdr --session <s> agent attach <pane>` on the agent's host, each with a Copy button.
  The server builds it from the recorded session only (strict charsets, never client input)
  and never runs it. `POST /api/v1/sessions/<id>/terminal`; see
  `docs/operations/terminal-attach.md`. The old "Copy attach command" line
  (`ssh host && herdr workspace focus ...`, which ran the second half locally) is gone
  from the page.

### Host telemetry and fleet provisioning
- Collector (`host_metrics.py`, run by the fabric agent): CPU, memory, swap, disk and
  mounts, load, uptime, network, GPU (NVIDIA, ROCm, Apple), top consumers and agent
  count every 30 s, plus an hourly report of package/security updates, reboot-required,
  failed units and tool versions. Intervals: `fabric.host_report_interval` and
  `fabric.host_slow_interval` (YAML only). The agent count is the number of agent
  processes named in `fabric.agent_binaries` (YAML, default `pi`, `claude`, `codex`).
- Control plane: validated host metrics with 48 h raw history and 7 d of durable
  5-minute rollups (max 300 points); `GET /api/v1/hosts/<id>/telemetry?range=1h|24h|7d`;
  server-side `problems`/`problem_count` on host reads (stale, never_reported, disk,
  memory, GPU, failed units, security updates, reboot, version drift, update age).
- Pages: host detail page and richer list cards with range picker, gauges,
  sparklines, GPU, mounts, top consumers, updates, versions with pin drift, a
  problems strip and a never-reported state.
- Ansible: `tailscale` role (joins only from `NeedsLogin`, auth key from Vault) and
  `fleet-update.yml` with the `herdr_fleet_update` role, run daily by cron on the main
  host: security-only OS updates, busy-host deferral, pinned herdr-eng revision, one
  host at a time, agent restart once, `maintenance` flag and a forced single-host run.
  See `ansible/README.md`.
- Behaviour changes: `GET /api/v1/hosts/<id>/telemetry?limit=` is now honoured
  (1..500); host events from non-agent sources (e.g. the fleet update) no longer
  refresh a host's liveness or create hosts; `versions.herdr` is now the herdr binary
  version and `herdr_eng` is the herdr-engineering package version.

### Central fleet config: deploy and servers
- `deploy/control-plane/deploy.sh` renders the compose variables from the central
  fleet file into a transient 0600 env file (removed on exit, even on failure),
  mounts the file's own directory read-only at `/etc/herdr/fleet/` and refuses to
  run when the file is missing or invalid or its directory holds anything else. `generate-env.sh`, `.env.example` and the
  Woodpecker/GitHub token-file compose overrides are removed.
- The control-plane web, worker and llm-proxy read configuration only from the
  fleet file (`--fleet-config`): Postgres DSN, host aliases, RustFS/MinIO,
  `control_plane.ci_actions`, `control_plane.inspector_limits`,
  `control_plane.llm_proxy`, `llm.upstream_key`. The `HERDR_CP_*`,
  `HERDR_INSPECTOR_*`, `HERDR_LLM_PROXY_*`, `HERDR_ENGINEERING_CI_ACTIONS` and
  `FABRIC_LLM_UPSTREAM_KEY(_FILE)` variables no longer have any effect.
- `deploy-fleet-agents.sh` copies no env or secrets and skips hosts without `host.json`.
- The central file lives alone in `~/.config/herdr-engineering/fleet/` (init leaves a
  symlink at the old `~/.config/herdr-engineering/fleet-config.yaml`). Writers replace
  it atomically (rename), dated backups go to `fleet-backups/` (newest 10 kept), and
  `herdr-eng fleet config revoke <host>` removes a host. Rewriting drops YAML comments.

### Collaboration network foundation (HCN P1, flag off by default)
- Added `control_plane/collab.py` and `/api/v1/collab/*`: per-user bearer auth,
  two-user rooms with owner/contributor/viewer/guest roles, device-bound agent
  enrollment with revocable, rotating credentials, owner-attenuated agent grants,
  idempotent auditable messages, cursor replay and a per-room SSE that
  re-authorizes every tick. State is event-sourced into the existing
  `fabric_events` sink and kept out of the shared event streams.
- Enable with `control-plane web --collab-users-file`; see
  `docs/operations/collaboration-network.md` and ADR-0007.

### Fleet mission recovery
- Added pinned Pi runtime staging and `fleet rollout plan/apply/run/status` with
  durable operation IDs, offline retries, bounded online stall detection, loaded
  build checks and preservation of operator pauses.
- Native discovery and control share the frozen target workstation. Missing
  conversation references can be verified against the foreground PID and live
  private Pi control instance without reading transcripts.
- Fleet parity now requires complete inventory coverage and installed provenance;
  macOS companion installation preserves the selected release.
- Session messages use real native delivery with durable receipts. Unsupported
  lifecycle actions and delivery without durable storage fail explicitly.

### Added
- `herdr-eng session run --project P -- <cmd>` injects `OPENAI_BASE_URL` / `OPENAI_API_KEY` (a
  per-session or reused per-project key) plus service-URL hints into a session's environment,
  revokes per-session keys at exit, and has opt-outs (flag, `llm.inject`, env). `llm-env` now writes
  its file atomically at 0600 and never follows redirects.
- The fabric agent warns once per port that listens like a dev service but has no lease, and
  `scan_once` returns them as `unleased`.

### Security
- **Fixed an authorization bypass in the control-plane API.** `_require` sent the 401/403
  response but returned `None`, so the handler then executed the request anyway: with API keys
  configured, a viewer key could set preferences / post session messages / run session actions,
  and an unauthenticated caller could register hosts via `/api/v1/ingest`. Refusals now stop the
  request. Deployments that rely on keys should upgrade; `--allow-open` (tailnet-trust) deployments
  were never affected because they have no keys to enforce. Regression test:
  `test_refused_requests_do_not_execute`. Refusals also close the connection so an unread
  request body can no longer be parsed as the next request.
- **Behaviour change (keyed mode only):** because refused GETs used to run anyway, the static UI,
  `/api/v1/health` and the SSE streams worked with no key. They now correctly return 401, and the
  bundled UI (which sends no `X-API-Key`) cannot load against a keyed server. Tailnet-trust
  (`--allow-open`) deployments are unaffected.

### Docs
- Inspector authentication: tokenless (tailnet-trust) vs keyed operation, and the privacy
  implications of open mode (spec 0200).

### Added
- Agent inspector (spec 0200, ADR-0006): per-session conversation / reasoning /
  tools / files / tests / usage views, streaming tool status, searchable
  history, diffs, subagent attribution, mission/handoff/worktree/PR
  associations, per-agent & global preferences, bounded retention, role-gated
  and redacted bodies. Mounted inside the existing session view.
- `agent-inspector-event` contract; `entity_type=inspector` on `POST /api/v1/ingest`.

### Fixed
- Session detail page requested the wrong session id (`parts[1]`) and 404'd.
- Control-plane listen backlog raised from 5 to 256 (connection resets under bursts of agents).

### Added
- **Operability of the live control plane**:
  - `/api/v1/health` now reports accurate deployment context — `source`
    (`postgres` when hydrated from durable state, else `memory`), image
    `version`, and a build-time `commit` stamp (Dockerfile `GIT_COMMIT` build
    arg / `HERDR_ENGINEERING_COMMIT` env; `dev` for local builds).
  - Lightweight per-client **rate limiting** on `/api/v1` (in-memory
    sliding window, generous default, SSE streams excluded) with `429` +
    `Retry-After`.
  - **Redaction** extended: `client_secret`/`authorization`/`x-api-key`/`auth`
    keys, `dsn`/`connection_string` key hints, OpenSSH private-key blocks, and
    more connection-string schemes (`postgresql`, `rediss`, `mongodb+srv`,
    `amqp`, `minio`, ...).
  - README documents the TLS path (`tailscale serve --https` on bender) for
    exposure beyond the Tailnet.

### Added
- **Live deploy of the control-plane compose stack on bender** — the HerdR UI
  is now served at `http://dev.lan` (port 80) on the Tailnet IP, backed by
  durable Postgres + MinIO. Compose now reads `HERDR_CP_PG_DSN` (generated into
  `.env`) so the web/worker reach Postgres by service name over the bridge
  network; MinIO is pinned to a known-good RELEASE tag (minio:latest is not
  always pullable); the worker takes `--migrations /app/migrations` because the
  bundled migrations path doesn't exist inside the installed image.

### Fixed
- **Web/worker durable hydration** (found during live deploy):
  - `_maybe_pg_source` imported `from .bridge` (wrong module) — now
    `from .control_plane.bridge`, and it reads `HERDR_CP_PG_DSN` from the env so
    the DB password never appears in process args.
  - asyncpg connections cannot be used across separate `asyncio.run` calls —
    `PostgresSource` now owns a single persistent event loop (`_LoopRunner`,
    daemon thread) and lazily connects + fetches on it.
  - Migration `0001_control_plane.sql` had a `UNIQUE (...)` constraint
    containing `COALESCE(...)` expressions (Postgres forbids expressions in
    constraints) — replaced with a `CREATE UNIQUE INDEX`.

### Added
- **Dev Fabric — port-matching (localhost:<port> -> dev.lan:<port>)** — a
  locally started dev server now appears at the SAME port on `dev.lan`, not a
  random range port:
  - Agent `AgentForwarder` tries binding the same port on the tailnet IP first
    (falling back to an OS-allocated ephemeral port if that exact tailnet port
    is taken), so loopback-only services register `(tailnet_ip, <same port>)`.
  - Gateway `register()`/`_allocate_port()` prefer `preferred_port` (defaults to
    `target_port`) whenever it is bindable and collision-free — falling back to
    the `[18000, 28999]` window otherwise. Preferred ports outside the window
    (4040, 5173, 3000, ...) are honored. Idempotent re-registration still
    preserves the existing external port across heartbeats.
  - The gateway front-door feedback-loop guard is narrowed: a port is skipped
    only when the local listener is already bound to the tailnet IP / all
    interfaces (i.e. it is the router front-door or a 0.0.0.0 service already
    reachable on the tailnet). A loopback-only dev server on a port that
    another host also uses is still adopted (with its own forwarder + fallback
    external port), so two developers can both run port 4040 on different
    machines without one being silently dropped.

### Added
- **Control Plane — Phase 2 Shared Design System (spec 0190 §6-16, §92)** — new
  build-free `packages/` workspace shared by every HerdR web surface:
  - `packages/herdr-design-system/` — canonical **light** theme (ChatGPT-inspired,
    not dark-NOC) as plain CSS custom properties: surfaces, text, brand accent,
    semantic status colors, Inter/JetBrains Mono font roles, radius/shadow/
    motion tokens, plus `base.css` (reset, visible focus ring, reduced-motion).
    Status is never color-alone — every token pairs with a label/icon.
  - `packages/herdr-web-components/` — framework-agnostic Web Components
    (`<herdr-status>`, `<herdr-spinner>`, `<herdr-empty>`, `<herdr-error>`,
    `<herdr-ident>`, `<herdr-metric>`) with DOM-free pure modules
    (`status.js`, `format.js`, `util.js`) unit-tested via `node --test`.
  - `packages/herdr-web-client/` — structured `/api/v1` `HerdrClient` (missions,
    sessions, services, hosts, activity, analytics, message send, actions,
    health) + SSE `connectEvents()` live-update helper; injectable fetch/
    EventSource for node tests.
- **Control Plane — web UI (Phases 3-7, spec 0190 §5, §20-34, §42-47)** —
  `packages/index.html` + `packages/app.js`: a hash-routed SPA that is a pure
  **client** of the structured API. Implements the full route inventory
  (`/overview /missions /missions/:id /herd /herd/session/:id /services
  /services/:id /hosts /hosts/:id /activity /analytics[/*]`), rendering
  loading/empty/error/data states via the design-system Web Components
  (`<herdr-app-shell>` sidebar shell, status badges, metric tiles, idents).
  Live updates are SSE-driven (not polling).
- **Control Plane — Artifacts API (Phases 5, spec 0190 §56-74, §102)** —
  `POST /api/v1/artifacts` (raw-body upload → stable `artifact_id`, SHA-256,
  mime, session/mission binding) and `GET /api/v1/artifacts/{id}/download`
  (streams exact bytes with `Content-Type` + `X-Content-Type-Options: nosniff`).
  Bytes held in the in-memory repo as the RustFS stand-in; the client is
  content-addressed and swap-to-RustFS is config-only (Phase 1 ruling).
- **Control Plane — Host telemetry (Phase 6, spec 0190 §23-25, §89, §105)** —
  `POST/GET /api/v1/hosts/{id}/telemetry` recording CPU/mem/disk/network/GPU
  samples per host, plus existing `/api/v1/leases` + `/api/v1/health`
  (control-plane self-observability).
- **Control Plane — Hardening (Phase 8, spec 0190 §87-88, §96-104, §106)** —
  - `control_plane/redact.py` — boundary secret redaction (OpenAI/AWS keys,
    JWTs/Bearer tokens, passwords/secrets/API keys, private keys, DB
    connection strings); applied to event payloads in API/SSE responses and to
    session messages before persistence (secrets never stored/rendered).
  - `control_plane/retention.py` — TTL-based pruning of high-volume telemetry
    + old raw event frames + artifact bytes; structured entity records are
    durable and never auto-deleted (Spec §96, §99).
  - Backup/restore — `repo.export_state()` / `import_state()` JSON round-trip
    (Spec §102) with a restore test.
  - Role-based auth — `api_keys={key: role}` with `viewer` (read-only) /
    `operator` (read + mutate); `X-API-Key`; unknown key → 401, viewer POST
    → 403 (Spec §104).
- **Control Plane — Tailnet trust boundary + .env secrets** —
  - **Auth-free within dev.lan**: the compose web service no longer needs API
    keys. The Tailnet is the trust boundary (all machines belong to each other,
    WireGuard-encrypted); `--allow-open` is the explicit acknowledgment. The
    web host port binds to the Tailnet interface via `HERDR_CP_WEB_BIND`
    (default `127.0.0.1`), so it is reachable on dev.lan but not from `0.0.0.0`.
  - **`.env` with random secrets**: `./generate-env.sh` writes a git-ignored
    `deploy/control-plane/.env` (0600) with randomly generated strong Postgres
    + MinIO passwords; compose refuses to start without them and DB/object-store
    ports stay loopback-only. `.env.example` documents every variable.
  - **Fail-closed auth kept for standalone**: an unauthenticated standalone
    server still refuses to bind a non-loopback address unless `--allow-open`
    is passed; `X-API-Key` compare stays constant-time (`secrets.compare_digest`).
- **Control Plane — security hardening (fail-closed auth, constant-time, sealed compose)** —
  - **Fail-closed auth (Spec §104)**: an unauthenticated server refuses to bind
    any non-loopback address (raises `ValueError`); loopback stays open for
    local dev only.
  - **Constant-time key compare**: `X-API-Key` verification uses
    `secrets.compare_digest` instead of `==` (removes a timing side channel).
  - **Sealed compose defaults**: Postgres and MinIO credentials are no longer
    hardcoded `herdr:herdr`; compose refuses to start without secrets, and
    Postgres (5432) / MinIO (9000/9001) publish to `127.0.0.1` only.
- **Control Plane — live SSE wiring + per-entity event stream + demo seed** —
  - The SPA now subscribes to `/api/v1/events/stream` and refreshes the current
    view on incoming events (debounced, no polling) — live interaction (Spec
    §52, §85) is wired in the browser, not just available client-side.
  - New `GET /api/v1/events?entity_type=&entity_id=` returns a single entity's
    full event stream (previously the UI filtered the global 20-event window).
  - Fixed host-telemetry route shadowing: `/api/v1/hosts/{id}/telemetry` was
    being swallowed by the host-detail branch (`len(parts) > 4`); the detail
    branch is now exact (`len(parts) == 5`). The host view now shows the latest
    CPU/mem/disk sample instead of always-empty fields.
  - Removed the duplicated `esc()` in `app.js` (now imports `escapeHtml`).
  - `herdr-eng control-plane web --seed-demo` seeds a fresh in-memory repo so
    the UI is populated for local evaluation when no Postgres is present.
- **Control Plane — Durable bridge (spec 0190 §106, §114-115)** —
  `control_plane/bridge.py`: `load_into(repo, source)` hydrates the in-memory
  repo from a read-only durable view (Phase-1 Postgres tables: missions,
  sessions, services, hosts, fabric_events), so the API/UI can serve real
  persisted state. `PostgresSource` is asyncpg-backed (lazy, graceful
  degradation if asyncpg is absent); the mapping is pure and unit-tested with
  fakes (no live DB in CI). `herdr-eng control-plane web --pg-dsn <dsn>`
  hydrates from Postgres on startup.
- **Control Plane — `control-plane-web` compose service + `--api-keys` RBAC** —
  compose project now ships a web service (UI + `/api/v1` + SSE, private by
  default, `127.0.0.1:8080:8080`), and `herdr-eng control-plane web` accepts
  `--api-keys 'k1=viewer,k2=operator'` (or `HERDR_CP_API_KEYS` env) for
  role-based `X-API-Key` auth.
- **Control Plane — `herdr-eng control-plane web`** subcommand — serves the UI +
  `/api/v1` + SSE private-by-default (`--host 127.0.0.1`), optional `--api-key`,
  from the bundled `packages/` web root.
- **Control Plane — structured repository + API/SSE server (spec 0190 §20-34,
  §42-47, §52, §85, §91-92)** — the UI-is-a-client backbone:
  - `control_plane/repo.py` — in-memory, event-sourced `ControlPlaneRepo`
    (missions/sessions/services/hosts/artifacts/leases) with stable identity,
    dynamic location, 0-indexed per-stream event sequencing, replayable
    `recent_events`, pub/sub `subscribe()`, and mission/model/host analytics.
  - `control_plane/api.py` — stdlib `ControlPlaneServer` exposing structured
    `GET/POST /api/v1/*` JSON + **SSE** live stream (with initial frame + history
    replay + auto-reconnect), optional `X-API-Key` auth, and static serving of
    the design-system/web packages. Private-by-default (loopback/tailnet),
    daemon handler threads (a stuck SSE client can never block shutdown).

- **HerdR Dev Fabric Control Plane — Phase 1 Foundation (spec 0190)** — new
  `herdr_engineering/control_plane/` subpackage laying the structured-state
  backbone for the unified `http://dev.lan` control plane:
  - **Stable entity IDs** (`ids.py`) — time-ordered, lexicographically sortable
    Crockford-base32 ULID-style ids (`new_id(kind)` for mission/session/
    service/artifact/host/agent/plan/worktree) with a per-process monotonic
    counter so creation order matches string order even within the same
    millisecond. Identity is stable; location is dynamic.
  - **Versioned SQL migration runner** (`db.py`) — homegrown idempotent
    `Migrator` (`ensure_schema`/`applied`/`apply`, `schema_migrations` table,
    per-file transactions, filename order).
  - **Phase-1 schema** — `deploy/control-plane/migrations/0001_control_plane.sql`
    with all Spec §53 tables (hosts, host_samples, missions, mission_events,
    mission_stage_runs, plans, plan_revisions, sessions, session_events, agents,
    agent_runs, services, service_events, artifacts, artifact_bindings,
    artifact_materializations, token_usage, resource_usage, test_runs,
    test_results, reviews, review_findings, pull_requests, audit_events,
    fabric_events, state_snapshots) + indexes.
  - **Event envelope / sequencing / idempotent ingestion** (`events.py`) —
    `EventStore` with per-stream 0-indexed sequences, exclusive resumable
    `read_stream(after)`, and `INSERT ... ON CONFLICT DO NOTHING RETURNING`
    so a duplicate `event_id` is ignored (idempotency enforced by the PK).
  - **Mission/Session/Service stores** (`models.py`) — stable-id CRUD with
    provenance fields.
  - **State snapshots + replay-after** (`snapshots.py`) — `SnapshotStore.save`/
    `load_latest` and `recover()` folding later event payloads over state, so
    no full event replay is needed after restart.
  - **Content-addressed RustFS client** (`rustfs.py`) — `content_key` =
    `sha256/ab/cd/<hex>`, `put`/`get`/`head`/`delete`, `IntegrityError` on
    SHA-256 mismatch. boto3/S3-agnostic (MinIO drop-in for Phase 1).
  - **Artifact model** (`artifacts.py`) — `ArtifactStore.register`/`download`/
    `attach`/`materialize`/`mark_available`; artifacts are content-addressed,
    never stored in Postgres, integrity-verified on download.
  - **Bounded background worker** (`worker.py`) — `run_one_pass`
    (stale-lease detection + host health reconciliation + snapshots + artifact
    GC hook; hosts/services marked unavailable, never deleted) and `run_worker`
    (infinite loop with `stop` event that never raises).
  - **Compose project** — `deploy/control-plane/` with `postgres:16-alpine`,
    `rustfs` (MinIO drop-in), and `control-plane-worker` (runs migrations then
    the worker; separate from the web frontend).
  - **CLI** — `herdr-eng control-plane migrate|worker` subcommands.
- **Multi-operator fleet lifecycle** — idempotent scripts + guide so any
  operator can stand up a private dev net, join a machine, and leave one:
  `scripts/fabric-agent-macos.sh` (join a macOS host via `uv` + Python 3.12
  venv + launchd LaunchAgent; subcommands `install`/`status`/`uninstall
  [--purge]`), `scripts/fabric-offboard.sh` (cross-platform leave: stop the
  local agent — systemd on Linux / launchd on macOS — close the host's leases
  on the gateway via its tailnet IPv4, and drop it from the fleet inventory;
  flags `--host`/`--gateway`/`--leave-tailnet`/`--purge`), and
  `docs/operations/fleet-lifecycle.md` (set up → join → verify → leave,
  including the one external Tailscale split-DNS step). README and
  `deploy/fabric-stack/README.md` now point at this flow.
- **`dev.lan` shared dev domain (Option B) + master-host compose stack** —
  `deploy/fabric-stack/` runs the whole fleet front door as one Docker Compose
  project on the gateway host: CoreDNS (authoritative for the `dev.lan`
  namespace, split-DNS from Tailscale), the fabric gateway (`url_base:
  dev.lan`, so every lease is `http://dev.lan:<port>`), and the private web
  UI. All services use `network_mode: host`; CoreDNS binds only loopback + the
  tailnet IP (avoids systemd-resolved/libvirt on port 53). The per-host
  auto-registration agent stays a host systemd unit. Deploy via
  `scripts/fabric-stack.sh` (generates `.env` + CoreDNS config from the live
  tailnet IP, builds the image, replaces the old host-run gateway/web).
  `.local` is mDNS-reserved, so Tailscale MagicDNS will not serve it — the
  admin-console split-DNS nameserver (restrict to `dev.lan`) is the one
  external step. See `deploy/fabric-stack/README.md`.
- **Gateway container-safe lock handling** — `devfabric gateway` now handles
  SIGTERM (graceful stop releases `gateway.lock`, so `docker compose stop`/
  restart no longer leaves a stale lock) and gains `--reset-lock` to take
  over a stale lock after a hard crash/SIGKILL. This is required in a
  container, where the entrypoint is always PID 1 and a stale lock from a
  previous container (`pid=1`) was otherwise mistaken for a live instance and
  permanently blocked restart. The compose stack passes `--reset-lock`.
- **Dev fabric gateway** — the unified `dev:<port>` front door (spec 0110,
  component #3). `herdr-eng devfabric gateway` runs the fleet gateway:
  control API (register/heartbeat/close/list/resolve/healthz) on
  tailnet+loopback with optional bearer token, a dynamic router that binds
  each leased external port on loopback + tailnet and byte-proxies to the
  owning machine, and a probe loop that renews healthy leases, expires dead
  ones (TTL), and releases their ports. Every lease gets a stable fleet-wide
  address `http://<gateway-magicdns>:<port>` (e.g.
  `http://bender.tail0c50da.ts.net:18000`) reachable from any tailnet device.
- `HERDR_ENGINEERING_FABRIC_URL` / `fabric.gateway_url` config: registrar
  commands route through the gateway automatically and degrade to local mode
  with an explicit warning when it is unreachable.
- **Dev-fabric auto-registration agent** (spec 0110, zero manual steps) —
  `herdr-eng devfabric agent` runs a per-host loop that detects web services
  starting locally and registers them with the gateway automatically: it
  scans listeners (`ss`/`lsof`), probes each for HTTP, puts a tailnet-bound
  forwarder in front of loopback-only apps, heartbeats tracked leases and
  transparently re-registers any the gateway drops (restart/TTL), and closes
  leases when a service disappears. A noise filter registers only *dev* web
  services (skips privileged/ephemeral ports and well-known infrastructure
  daemons; thresholds configurable via `fabric.agent_*`). Deployed as a
  systemd user service by `scripts/fabric-agent.sh` and the fleet Ansible
  playbook on every host, so a web service started on any fleet member
  appears at a stable `dev:<port>` and in the gateway host's web UI with no
  manual registration.
- The web UI's dev tab now sources `url_base` from the live gateway
  (`/healthz`) and renders a clickable front-door link per lease.
- `--host auto` for `devfabric register`: resolves the local tailnet IPv4 and
  verifies the target is listening before publishing the lease.
- `gateway.lock` (pid:nonce): refuses a second live gateway over one store;
  stale locks from dead PIDs are taken over.
- Fabric gateway tests (register/route, close/unbind, dead-target expiry,
  healthy renewal, token auth, list/resolve, restart re-bind, port-taken
  refusal, URL resolution, single-gateway lock, API shape).
- `docs/architecture/dev-fabric-gateway.md` (design + live evidence).
- **Live Woodpecker CI view** — validated against the real instance
  (Woodpecker 3.18.1). `herdr-eng ci repos|agents|pipelines|pipeline|logs|
  debug`: repos, runner agents (auth token stripped), pipelines with status,
  pipeline→task drill-down (3.x `workflows[].children[]` flattened to tasks
  with state + exit code), honest step-log handling, and a bounded
  Debug-with-Pi handoff for failing pipelines.
- **Real Ansible convergence of three of four fleet hosts** — `mac` (macOS,
  over the tailnet), `beast` (Ubuntu 24.04, over Tailscale SSH) and `bender`
  (local): each converged then immediately idempotent (`changed=0`).
  Inventory renamed `macbook-m4` → `mac` to match the real tailnet name;
  per-host connection details moved to gitignored `inventory/host_vars/`.
  `fry` is not on the tailnet (blocked for that host alone).
- Ansible fixes found during the live runs: brew check gated on a
  `brew_present` fact (the old check always exited 0, so brew tasks would
  fail on a Mac), `become` overridable (`-e herdr_engineering_become=false`)
  for user-scoped hosts without passwordless sudo, play PATH now includes
  the Homebrew prefixes and the fleet user's `~/.local/bin` (non-interactive
  SSH on macOS does not source the login PATH — and the setup-task
  interaction made herdr checks fail on every run), the tailscale report
  no longer matches a word that `tailscale status` never prints, and the
  config `lineinfile` now sets `owner` — with `become: true` it created the
  fleet user's config root-owned and unreadable by that user (caught by the
  idempotency re-run on beast, which also repaired the live file).

### Fixed
- **Fabric agent could hang forever on a wedged DNS resolver** (root cause of
  a long-run live stall): `urllib`'s `timeout=` covers the TCP connect/read
  but NOT `getaddrinfo`, so a control-plane call to the hostname `dev.lan`
  could block in the libc resolver indefinitely — no logs, low CPU, single
  thread, no new services adopted, fixed only by a restart. The `FabricClient`
  now resolves the gateway host to an IPv4 with a bounded lookup (daemon
  worker + `join(timeout)`, cached and refreshed), so a stalled resolver
  surfaces as a normal bounded `UnreachableError` the agent retries next
  cycle. As a safety net, `FabricAgent.run()` now runs a watchdog that, if a
  scan cycle fails to complete within `cycle_timeout` (default 300s), dumps
  every thread's stack to the log and exits so systemd/launchd respawn a
  healthy agent.
- **CI provider corrected to the real Woodpecker 3.x API** (found during live
  validation): base is `/api/`, not `/api/v0/` (the earlier `/v0` was a
  mis-diagnosis caused by trailing-slash 301s returning the SPA); repos are
  addressed by numeric id (resolved from `owner/name`); pipeline detail uses
  `workflows[].children[]` (tasks), not a `steps` array; offline `pipelines`/
  `pipeline`/`step_log` return empty/None instead of raising.
- `ci logs` no longer implies logs are missing due to a bug: it reports
  explicitly that this Woodpecker 3.x build streams logs over WebSocket (no
  REST endpoint) and the OAuth2 proxy does not forward a bearer token on the
  upgrade, then links the web UI.
- Fabric router `unbind` leaked one connection through an in-flight
  `accept()`; unbind now joins the accept threads so a port is only reported
  released when it truly is.
- The probe loop never released ports for expired leases (expiry was
  consumed by `active_leases()`' internal reconcile); the router now
  self-syncs to the active set each cycle (self-healing after restarts and
  closes).
- `gateway_url_from_config` ignored the `HERDR_ENGINEERING_FABRIC_URL`
  environment variable, silently degrading registrar commands to local mode.
- Registry store writes from an overlapping process could be lost on the
  next persist; the gateway now reloads the store when its mtime changes.
- Test port ranges were host-dependent; allocation tests now pick
  OS-free ports so they are hermetic.

## [0.1.0] - 2026-09-24

First public-quality release of the HerdR Engineering integration layer.
Implements all 18 dependency-gated specs (0010–0180) plus the five typed
contracts, as a thin curated layer over native Herdr 0.9.1 (verified).

### Added
- `herdr-eng` CLI + `herdr_engineering` Python package implementing all 18
  numbered specs: naming, Herdr adapter, powerpack lifecycle, web/mobile
  surface, browser preview + Plannotator, shared artifacts, session journal,
  Pi/AutoSpec correlation, dev fabric, test explorer, CI adapter, candidate
  comparison, attention, observability/security, doctor.
- Automatic semantic workspace naming (`deriveWorkspaceIdentity`) with
  `name_source` provenance, collision safety, user-rename preservation.
- One-shot installer `scripts/install.sh` (assumes Herdr exists; idempotent;
  enrolls the local machine into the private fleet inventory and, when a key
  is provided, onto the Tailscale tailnet; never modifies Herdr or its state).
- Config auto-discovery with deep-merge precedence:
  machine-local < repository < `$HERDR_ENGINEERING_CONFIG`.
- Ansible fleet playbooks (Linux + macOS) and GitHub Actions CI.
- 91 unit/contract/integration tests (ruff-clean, no collection warnings).
- Five typed contracts (ActivityEvent, ArtifactRef, DevServiceLease, TestEvent,
  CIEvent) as validated dataclasses.

### Fixed (found by live validation drills)
- `herdr-eng browser` now wires the artifact workspace so screenshots are
  actually persisted as artifacts (spec 0070).
- Dev-fabric lease persistence now round-trips across processes (the loader
  reconstructed nested `target`/`owner`/`health` fields).
- Dev-fabric port allocation now skips OS-bound ports (not just other leases).
- `test_build_tree` no longer recurses pytest over the whole repo (was 300s).
- pytest adapter captures passing tests (`-rA`) and strips trailing messages.

### Security
- Private by default: web binds `127.0.0.1`; dev-fabric refuses public bind
  addresses; no secrets in repo/config/artifacts/logs; hidden chain-of-thought
  never persisted.

### Validation
- `91 passed`, `ruff` clean.
- Live: cross-machine Herdr snapshot via `herdr --machine beast`; dev-fabric
  HTTP drill (leased port 18000 → HTTP 200); browser capture published as an
  artifact. See `docs/evidence/0180/live-validation.txt`.
- Remaining `BLOCKED_EXTERNAL` items (live Woodpecker CI, iPhone/iPad tailnet,
  full Ansible convergence) are documented in `docs/acceptance-matrix.md`.

[0.1.0]: https://github.com/berlinguyinca/herdr-engineering/releases/tag/v0.1.0
