# Security and external-action boundaries

Threats: malicious room text and attachments, tool-result injection, forged agent membership, session escape, cross-project artifact exposure, stale approvals, replayed outgoing email, XSS/unsafe HTML/SVG/PDF previews, malicious remote URLs/SSRF, agent privilege laundering through delegation and broad inferred room intents. Mitigation requirements: explicit authorized task creation; untrusted data marking throughout; narrow expiring capability tokens; action-specific approvals; separate session attach ACL; egress allowlist / URL validation; sanitization/sandbox previews; tamper-evident audit; email send idempotency and retry reconciliation; revocation effective on read, subscribe and write; guest/public artifact opt-in with expiry. Never publish secrets in prompts, graph metadata, URL parameters or artifact previews. Security tests mandatory before P07 and P09 sign-off.

---

# Implementation threat model: what is merged on main (2026-10-30)

Scope and method. This section covers only code that is on `origin/main` today. It was written by reading the code
(paths cited per claim); **no test, scan, load run or exploit attempt was executed for it, so nothing here is "verified"**.
Statements about topology come from `deploy/control-plane/compose.collab.yaml` and `deploy/control-plane/collab-edge/nginx.conf`,
also read, not run. Severity is a judgement of likelihood times impact for this deployment (a small lab, a tailnet operator
plane, one public collaboration edge), not a CVSS score. The spec threats above that need unbuilt features (P07 external
actions, Pi mission admission, tool-result injection into agents, email idempotency) are out of scope here and stay open.

Paths below are relative to `herdr_engineering/control_plane/` unless they start with `deploy/`.

## 1. Assets
| Asset | Where it lives |
|---|---|
| Artifact bytes and versions (may hold unpublished research data) | RustFS objects; registry rows (`artifact_registry*.py`, migrations 0003/0008/0031) |
| Artifact ACL grants (who may read/edit/share/admin) | `artifact_acl.py` `ArtifactAcl` (in-memory index, loaded from registry rows) |
| Work Graph entities, edges, facts, layouts | Postgres `wg_*` (migration 0034), `work_graph_store.py` |
| Room membership and role, task log | collab event log (`collab.py`, replayed on start) |
| Share link tokens (bearer secrets) | only the sha256 is stored (`artifact_shares.py` `new_token`); the token exists once, in the approve response |
| Frozen share bundles | `<shares_root>/<share_id>/files/` and `by-token/<sha256>/state.json` on the share volume |
| Share session signing secret, Cognito pool/client config | `share_session.py` (HMAC), fleet config slice rendered by `deploy.sh` |
| Operator API keys, fleet config, deploy receipts | `deploy/control-plane/deploy.sh`, `deploy-state.py`, central fleet config |
| Audit trail | collab log; share `events` (`artifact_shares.py`); `deploy/control-plane/collab-audit-verify.py` |

## 2. Trust boundaries
1. Internet -> `collab-edge` nginx (TLS, rate limits, route allow-list) -> `collab-public` container started `--collab-only`,
   with no Postgres or object-store credentials (`deploy/control-plane/compose.collab.yaml`, `collab-edge/nginx.conf`).
2. Internet -> `/s/` on the same edge -> `share-public` (`share_public.py`), a database-free process that reads a read-only
   volume (`deploy/control-plane/collab-edge/share-location.conf`).
3. Tailnet operator -> `control-plane-web` (keyed `/api/v1/*`, roles viewer/operator) and the operator listener (`:8082`),
   which refuses the share network's subnet (`nginx.conf`, `deny 172.31.251.0/28`).
4. Signed-in person (Cognito) vs agent (enrolled credential) vs guest (expiring membership) inside collab (`collab.py`
   `Principal`, `_effective`, `_member`).
5. Share-bundle author (artifact owner) vs share viewer: the bundle is untrusted active content from the viewer's side.
6. Operator vs deploy host: `deploy.sh` runs with the operator's rights and changes containers, units and slices.

## 3. Attack surfaces, existing mitigations, residual risk

### 3.1 Work graph routes (`unified_workspace.enabled`)
Surface: keyed `GET/POST /api/v1/graph/work/*` (`api.py` `_work_graph`) and the member door `graph/work/*` in collab
(`collab.py` `work_graph_request`, GET at ~4122, POST at ~5615).

Mitigations (read in code):
- Flag off means `work_graph is None` and every route is the same 404 (`work_graph_service.py` `handle`; `api.py` ~448).
- Visibility is enforced twice: SQL (`work_graph_store.py` `vis_sql`) and Python (`work_graph_core.py` `visible`);
  unauthenticated is `FALSE`; a miss, a hidden row and a forbidden row are all the same 404.
- Artifact nodes defer to the registry's own gate (`artifact_ok`, `api.py` ~452-462); without an ACL they deny.
- `org` rows are visible to fleet keys, never to signed-in members; room/mission rows need operator or a collab access snapshot
  that covers the room (`vis_sql`).
- Guests, agents and people with no non-guest membership get 404 (`collab.py` `work_graph_request`, `graph_access is None`).
- Mutations need `application/json` and a same-origin `Origin` (`api.py` ~2345-2353); `reconcile` and `status` are operator-only
  (`work_graph_service.py`); layouts need `If-Match` (428/412), are size-capped, flat, and filtered to visible node ids
  (`work_graph_store.py` `put_layout`).
- Pagination cursors are tagged with the principal and rooms (`work_graph_store.py` `_tag`), so a cursor is not portable.

Residual risks:
- **R-WG1 (Medium, topology).** In the documented public topology the process that has a collab service is not the one with
  Postgres. `compose.collab.yaml` starts `control-plane-web` with `--collab-operator-url` and no collab flags, and
  `collab-public` with `--collab-only`; `api.py` ~603-614 wires `work_graph_handle` and `_wire_room_artifact_access` only when
  `not collab_only`. Read as written, the member door and the room-artifact listing answer 404 in that topology. This fails
  closed (a feature gap, not a leak), but the member door has had no production exposure and must be re-reviewed before anyone
  runs collab and the control plane in one process.
- **R-WG2 (Low).** Any signed-in member who can see a root can write the shared layout of that root (`put_layout` with
  `shared=true` checks only `kind in (operator, member)`, not room role). A low-role member can reshape a team's default map.
- **R-WG3 (Low).** Display names and attrs of entities will be rendered by future UIs and are untrusted text. No renderer exists
  yet (P06 is logic only), so escaping is a requirement on that work, not a present hole.
- **R-WG4 (Low).** The projection trusts the event sources it reads (`work_graph_sync.py` `PgSources`); poisoned events are
  quarantined (`work_graph_store.py` `_poison`), but a forged fabric event under a valid ingest key can create graph facts within
  that key's scope.

### 3.2 Artifact registry and ACL
Mitigations: one SQL predicate for lists, counts, search and lookups (no count/pagination leak; missing equals forbidden, per
`evidence/P03-RESULT.md`); immutable versions (trigger `artifact_versions_freeze`, migration 0031); lifecycle routes need `admin`
(`artifact_registry_api.py`); resumable uploads cap part size and demand one unambiguous `Content-Length`; mutations are
same-origin only (`api.py` ~2354-2359); report evidence is delivered with a no-script CSP (`artifact_report_api.py` `_text`).

Residual risks:
- **R-ART1 (Medium).** Legacy permissiveness: with `artifact_acl is None` every artifact is allowed (`api.py`
  `_artifact_allowed`), and a pre-ACL upload with no `created_by` and no grants is readable by every viewer key
  (`_artifact_acl_allows`). Production must run with an ACL; legacy rows should be audited and given an owner.
- **R-ART2 (Medium).** The grant model is in memory and rebuilt from rows (`ArtifactAcl.load`); a missed `on_change`
  write-through would make a revoke or a room grant disappear or reappear only at restart. Not tested for this document.
- **R-ART3 (Low; unlink added 2026-10-30, partly open).** `link_context` still needs only `edit`. The missing unlink now
  exists (`herdr-eng artifacts unlink ARTIFACT --room ROOM`, `POST /api/v1/artifacts/<id>/links/remove`, needs `edit`, removes
  that room's read grants at once), and a link to a room that does not exist is refused with the generic 404. Still open: an
  API key has no room membership, so an editor can link to any room that exists (existence is all that can be checked).

### 3.3 Room-derived read access (`unified_workspace.room_artifact_access`)
Mechanism (`artifact_room_access.py`, wired in `api.py` `_wire_room_artifact_access`): ordinary `read` grants for
`collab:user:<id>`, `granted_by: room:<id>`, expiring with the member; re-synced on membership change, on link, and at start.

Mitigations: read only, never edit/share/review/admin; agents are not readers (`collab.py` `room_readers`); the listing route is
members-only and agents and non-members get 404 (`collab.py` `room_artifacts`); guest expiry is enforced at read time by
`ArtifactAcl.can` regardless of sync timing; needs BOTH flags, YAML only.

Residual risks:
- **R-RA1 (Medium).** Revocation of a removed (non-guest) member is asynchronous. Removal triggers `notify`, a worker wakes and
  `drain` revokes; a failing sync re-queues the room only for the next wake (the loop waits up to 30 s) or restart. Between
  removal and the sync the person keeps read. Guest expiry is not affected (checked at read).
- **R-RA2 (Resolved 2026-10-30 for direct grants; one narrower limit remains).** `_apply` no longer overwrites a live grant
  held by another source: a direct grant (any `granted_by` not starting with `room:`) is left as it is and is not revoked
  when the room lets go; between two rooms the first holder keeps the row unless the second room's grant lasts longer.
  `herdr-eng control-plane revoke-room-grants [--apply]` removes every `room:*` row after the flag is turned off
  (`docs/artifacts/room-access-maintenance.md`). Remaining limit: one grant row holds one source, so a direct grant that later
  expires or is revoked is not replaced by the room's until that room's next sync. No migration was needed.
- **R-RA3 (Low; mitigated).** Because linking needs only `edit` (R-ART3), an editor can expose an artifact to an existing
  room they are not in. The exposure can now be undone with `artifacts unlink`; it is not prevented.
- **R-RA4 (Medium, topology).** Same as R-WG1: `collab` and the artifact store are in different processes in the compose
  overlay, so this feature cannot run there as written. Enabling it needs a topology decision first.

### 3.4 Share links, lab vs public (`control_plane.share.enabled`, `/s/<token>`)
Mechanism: owner API with `share` permission (`artifact_share_api.py`): create (dry run) -> approve with `{"confirm": true}`
(copies hash-verified outputs into a frozen bundle, writes `state.json` last, returns the URL once) -> revoke (deletes state
first). Serving: `share_public.py`.

Mitigations (read in code):
- 256-bit token (`secrets.token_urlsafe(32)`), only its sha256 stored; the approve response is the single disclosure.
- Public shares must expire in 1-90 days and cannot carry an allow-list (`artifact_shares.py` `create`, `PUBLIC_MAX_DAYS`);
  lab shares require at least one email or group. The API's 3650-day ceiling is narrowed by that store rule for public.
- Every failure (bad token, revoked, expired, traversal, symlink, size mismatch, odd method) returns one identical 404; state is
  re-read on every request so revoke and expiry take effect on the next request; files are opened by index with `O_NOFOLLOW`
  (`share_public.py` module docstring).
- Lab access needs verified Cognito access and id tokens naming the same user with a verified email on the allow-list or a
  listed group (`share_lab.py`); sessions are stateless HMAC, capped by share expiry and token `exp`, and die with the share
  (`share_session.py`); per-share and global rate limits on session attempts (`share_public.py`).
- Bundle files carry `nosniff`, `Referrer-Policy: no-referrer`, private/no-store caching and a sandbox CSP
  (`sandbox allow-scripts allow-downloads`, fetch directives limited to `'self'`, `form-action 'none'`); the lab viewer page has a
  strict CSP with no inline script (`share_public.py` `_CSP_TEMPLATE`, `VIEWER_CSP`). Tokens live only in `sessionStorage`
  (`share_static/share.js`) and are not logged; nginx has `access_log off` on `/s/` and enforces method, body and rate limits
  (`share-location.conf`).
- Per-share bytes, file counts and total live bytes are capped (`DEFAULTS` in `artifact_share_api.py`).
- `share-public` has no database and no registry access; the operator listener refuses the share network.

Residual risks:
- **R-SH1 (Medium).** Bundle authors are untrusted to the viewer. The sandbox CSP allows `script-src 'unsafe-inline'`; the sandbox
  gives an opaque origin (so no access to the viewer page's `sessionStorage`), but a malicious bundle can still render a phishing
  page or navigate itself elsewhere (CSP has no navigation control). Exposure is limited by who holds `share`, not by the server.
- **R-SH2 (Medium).** The only gate for a public share is the `share` permission plus `{"confirm": true}` from the same principal;
  no second approver, no owner notification. A holder of `share` can publish to the internet for up to 90 days.
- **R-SH3 (Medium).** The token is a bearer secret in the URL and can leak by copy/paste, chat previews or browser history (the
  edge does not log paths). A public link is unauthenticated by design; lab links additionally need Cognito, which bounds the loss.
- **R-SH4 (Low-Medium).** Lab shares may have no expiry (`expires_at` is optional for `lab`). "Share with room" snapshots emails
  (`artifact_share_api.py` `_room_emails`, includes unexpired guests) and never updates: a person who leaves the room keeps the
  link until it is revoked. Documented behaviour, but access that outlives membership.
- **R-SH5 (Low).** `_room_emails` does not require the creator to be in the room, and its error for a missing room differs from
  success for an existing one, so a `share` holder can probe room-id existence.
- **R-SH6 (Low).** `Access-Control-Allow-Origin: *` on bundle responses (`share_public.py` `CORS_HEADERS`); any page that knows a
  token can read the bytes cross-origin, which the token already allows.
- **R-SH7 (Low).** `_APPROVE_LOCK` is per process; two processes approving at once could both read the same `live_total_bytes` and
  slightly exceed the total cap. One process approves in the documented topology.

### 3.5 Crash-takeover task leases (`control_plane.collab.task_lease_seconds`)
Mechanism (`collab.py` `_requeue_stale`, `update_task`; `cli.py` `_task_lease`): an accepted task older than the lease goes back
to `pending`, `attempt` rises, an update carrying a stale `attempt` is a 409. Evaluated only when the target agent polls its
inbox. Replay uses event timestamps, so a restart does not extend a lease (`collab.py` ~880-886). YAML only, 60..604800 s,
absent = off.

Residual risks:
- **R-TL1 (Resolved 2026-10-30).** `update_task` now returns 409 `stale attempt` for a run that omits `attempt` once the task
  has been requeued (`attempt` > 1), the same as a wrong `attempt`. A task never requeued (lease off, or not yet expired) still
  accepts updates without `attempt`. The MCP `task_update` tool takes `attempt` and `worker.sh` sends the attempt `tasks_inbox`
  gave it; a custom runner that ignores `attempt` is refused after a requeue (it must echo `attempt` from `tasks_inbox`).
- **R-TL2 (Medium).** The lease is a hard deadline from accept with no heartbeat (`evidence/P05-RESULT.md` says so): a long
  legitimate task is requeued and run twice once the lease is shorter than the task.
- **R-TL3 (Low).** Requeue happens only on inbox poll; a task whose agent never polls again stays `accepted`.
- **R-TL4 (Info).** `team_task_leases_pg.py` (migration 0040) is a separate durable store that nothing calls yet; it is not
  active protection.

### 3.6 Collab public listener
Mitigations: `collab-public` runs `--collab-only --collab-require-https`, has no DB or object-store credentials, no docker socket,
a read-only root and its own network (`compose.collab.yaml`); the edge allow-lists routes, returns 444 for any source that is
not the SNI edge, rate limits per real client address, and caps bodies at 64 KiB (`nginx.conf`); the app refuses ambiguous
framing, cross-origin and non-JSON POSTs, and returns a plain 404 for everything not on its allow-list (`api.py` `_public`,
`_collab`).

Residual risks:
- **R-CL1 (Low-Medium).** Rate limiting trusts the PROXY-protocol address from one hard-coded edge IP (`set_real_ip_from
  192.168.137.250`). If the edge moves, that address must change or clients share a bucket (availability, not confidentiality).
- **R-CL2 (Low).** Join scripts are served with the site address templated in; the base is matched against a strict regex before
  use (`api.py` `_script`). Piping a script to bash is a user-trust decision.

### 3.7 Control-plane deploy
Mitigations (from `deploy/control-plane/deploy.sh` structure and header): builds from a git ref, health gate on the new commit
and on every recreated service, backup of host files before change, automatic rollback to the previous pinned image when a
receipt exists, a deploy lock, and a stated "no automatic rollback" path with a manual recipe when no receipt exists. Migrations
apply idempotently in filename order at start (`db.py` `apply`); recent migrations carry a `-- DOWN:` that is run by hand.

Residual risks:
- **R-DP1 (Medium).** Migrations are forward-only in practice: an image rollback does not run `DOWN`, and an older image against
  a newer schema was not tested here. Keeping new tables additive is the mitigation.
- **R-DP2 (Low-Medium).** The deploy context holds the fleet config (secrets). The slice rendered for the public container is a
  reduced mapping (`compose.collab.yaml`), which is the right design; its safety depends on `render-collab` never being given
  extra keys.
- **R-DP3 (Info).** Flags are read at start, so each change is a restart, not a hot toggle.

### 3.8 Team rooms, guest levels, Suggest queue (`control_plane.collab.team_rooms.enabled`, slice 1)
Mechanism (`collab_team_rooms.py`, wired in `collab.py`): the owner attaches a team to a room as separate `team_attached` /
`team_detached` events; the team's agents become ordinary room grants marked with the team id (fixed role actions, no expiry);
a per-room guest level (`read_only` | `suggest` | `direct`, default `suggest`); a guest files a request that waits as `pending`;
only the owner approves, which creates a NEW task (`origin: suggestion`) whose command is the owner's and whose request rides as
quoted data tainted `external-source:guest`. Hot YAML flag, off by default, fails closed on an unreadable config. Tested with a
real Postgres, real collab service and real HTTP (`tests/control_plane/test_team_rooms.py`); not deployed, not run live.

Residual risks:
- **R-TR1 (Medium).** `direct` is stored and validated but behaves as `suggest`. It fails closed; the risk is a reader assuming it
  is enforced as a limit. Slices 2 and 3 must define it before it does anything.
- **R-TR2 (Medium).** The quoted request still reaches a language model in the target agent's `tasks_inbox`. Taint labels and the
  fixed "untrusted data" note reduce, but do not remove, prompt injection by an approved guest request; approval authorises work,
  it does not sanitise text. The owner can trim it (`edited_text`) before approving.
- **R-TR3 (Low-Medium).** Before this flag, a guest could read the task list and task events (design contradiction 8 said
  otherwise and was wrong). They are hidden only while `team_rooms` is on; with it off the earlier visibility returns.
- **R-TR4 (Low-Medium).** In collab-only mode (no Work Graph) a team id is checked for shape only (`verified: false`); the owner
  can attach a well-formed id of a team that does not exist. The attached agents are still the owner's own.
- **R-TR5 (Low).** Team grants have no expiry; they end only with `detach`. An older image ignores `team_detached` and keeps the
  grants, so detach before rolling the image back.
- **R-TR6 (Low).** Per-guest and per-room caps and rate limits are counted from the log in one process; a restart replays them,
  but multiple collab processes would each count alone.
- **R-TR7 (Info).** No web UI, no fleet-wide operator inbox: pending requests show only through the API, CLI and the room attention
  category `suggestion_decide`.

## 4. Severity summary (judged from reading; unverified)
| Id | Severity | One line |
|---|---|---|
| R-SH1 | Medium | Sandboxed bundle can phish or self-navigate |
| R-SH2 | Medium | One principal can approve and publish a public share |
| R-SH3 | Medium | Bearer token in URL |
| R-RA1 | Medium | Room-derived revoke is asynchronous |
| R-RA4 / R-WG1 | Medium | Member graph door and room artifact access cannot run in the compose.collab topology |
| R-ART1 | Medium | Legacy artifacts without owner, or without an ACL, are broadly readable |
| R-ART2 | Medium | ACL is an in-memory index; write-through drift shows at restart |
| R-TL2 | Medium | No lease heartbeat: a long task is requeued and can run twice (R-TL1, the opt-in fence, is resolved) |
| R-DP1 | Medium | Image rollback does not roll back the schema |
| R-TR1, R-TR2 | Medium | Team rooms: `direct` acts as suggest; approved guest text still reaches a model as quoted data |
| R-TR3, R-TR4 | Low-Medium | Guest task visibility only hidden with the flag on; collab-only cannot verify a team exists |
| R-TR5-7 | Low | See section 3.8 |
| R-SH4, R-CL1, R-DP2 | Low-Medium | See sections |
| R-WG2-4, R-SH5-7, R-ART3, R-RA3, R-TL3, R-CL2 | Low | See sections |

## 5. Rollout and rollback by flag
All flags are YAML only and read at start; a change needs a restart of the process that owns it. "Dark launch" means deploying
with the flag off (migrations are additive, so the image can be ahead of the flag). None of this was exercised for this document.

| Flag | Default | Turn on | Watch | Turn off / rollback |
|---|---|---|---|---|
| `control_plane.unified_workspace.enabled` | off | Migration 0034 applies at start; set `true`; restart control-plane-web. Operator calls `POST /api/v1/graph/work/reconcile`, then reads `/api/v1/graph/work/status` | `status` worker `last_error`, gaps, 5xx on `/graph/work/*`; read as a non-operator key to confirm room rows stay hidden | `false` and restart: routes return 404 again; data stays. Drop tables per 0034 `-- DOWN:` only if they must go |
| `control_plane.unified_workspace.room_artifact_access` (also needs `enabled`) | off | First settle R-RA4 (collab and artifact store in one process); set `true`; restart. Startup syncs every room | grants with `granted_by` `room:*` vs membership; a removed test member's read must fail within one sync interval | `false` and restart stops new grants but does NOT remove written ones. Revoke grants whose `granted_by` starts with `room:` (`herdr-eng control-plane revoke-room-grants --apply`, then restart; see `docs/artifacts/room-access-maintenance.md`) |
| `control_plane.collab.task_lease_seconds` | absent (off) | Set 60..604800, longer than the longest honest task (R-TL2); restart the process that runs collab | `task.requeue` events, 409 `stale attempt`, duplicate results | Remove the key and restart. Tasks already requeued stay requeued; `attempt` survives replay |
| `control_plane.collab.team_rooms.enabled` | off | Set `true` (hot, read each request); existing rooms' guests become Suggest unless a room is set `read_only` first | `suggestion_submitted`/`decided` events, 403/404 rates on `/rooms/*/suggestions`, `team_attached` grants | `false` or `kill_switch: true`: routes 404, guests see tasks again. `herdr-eng rooms detach` first if attached teams must go (works with the flag off). See `docs/operations/team-rooms.md` |
| Share links `control_plane.share.enabled` (+ `signing_secret`, collab `public_url`) | off | Complete the share block; `deploy.sh` refuses incomplete config, renders the edge with `/s/`, starts `share-public` | share `events` (`opened`, `denied`), 404 rate on `/s/`, live bytes | Revoke individual shares first (`POST .../shares/{sid}/revoke` removes state, then bundle); then `enabled: false` and deploy: the edge stops routing `/s/` and `share-public` is stopped |
| Per-user rollout | not built | The spec asks for it; every flag today is global | | |

Revert order: `room_artifact_access`, then `enabled`, then restart; revoke public shares before disabling the share edge so the
bundle volume is cleaned; leave migrations in place.

## 6. Accessibility checklist for screens that do not exist yet
This is a checklist to apply when P06-P08 screens are built. It is **not an audit**: no screen was tested with a screen reader,
keyboard, contrast tool or zoom, and nothing below has been observed.
- [ ] Team Map has a text equivalent reachable by keyboard and offered by default to screen readers (`packages/team-map-model.js` already produces the sentences; the view must expose them).
- [ ] Every graph node and edge is reachable by Tab/arrow keys, has an accessible name (kind, name, status, meaning of the edge) and a visible focus ring.
- [ ] Opening the inspector moves focus into it and Escape returns focus to the node; no keyboard trap.
- [ ] Status is never only colour; contrast meets WCAG 2.2 AA (4.5:1 text, 3:1 UI); the dashboard contrast ratchet in `ui-gates` is the baseline.
- [ ] Live updates (new message, task state, requeue) use polite live regions; reconnect and "task requeued" notices are announced, not only shown.
- [ ] Approval and share confirmation dialogs state the exact exposure (audience, file count, expiry) in text, are keyboard operable, and the public/destructive action is not the default focus.
- [ ] Reduced motion respected for layout animation; nothing conveyed only by animation.
- [ ] Usable at 200 percent zoom and 320 px width without two-direction scrolling; touch targets at least 24 px.
- [ ] Progress uses readable milestone strings ("2 of 5 tasks done"), never an invented percentage.
- [ ] Share viewer and sign-in redirect keep a meaningful title, language attribute and heading order; the 404 text is readable by assistive tech.
- [ ] Terminal clients (Pi) offer a plain-text mode; reconnect notices are text, not cursor tricks.
