# Artifact sharing GUI and git-style collaboration — design

Status: draft for operator review, 2026-10-30. Builds on the merged share-link feature (PR #486,
`docs/superpowers/specs/2026-10-30-artifact-share-links-design.md`).

## Why

The operator (one person, owner of this control plane) wants to manage sharing from the artifact page, not the
CLI, and wants outside parties to be able to *work on* an artifact, not just view it:

1. On an artifact: pick who may see it (private / public / group), generate a link, and keep track of it.
2. Give other bots and external harnesses (agents, other tools) a way to check out, edit and commit changes to the
   same artifact. Every change is a version. Non-overlapping changes merge automatically; overlapping ones are
   rejected and the contributor must fix them and push again — the way git behaves.

## Goals

* One **Share** panel on the artifact detail page covering viewer links, collaborators and activity.
* A per-artifact **grant** to an existing collab agent (internal or external), read-only or read+write, revocable at
  once, used through new tools on the collaboration MCP server.
* **Git-like commit semantics**: every commit names the version it started from; a stale base is merged
  automatically when the changes do not overlap and rejected with a conflict report when they do.
* **History is never lost**: merges become real versions with two parents; rejected pushes are logged, not stored as
  versions; revert is a new version.
* Signed-in group members (lab viewers) can **annotate** shared content; an agent integrates open annotations into a
  new revision.
* Group members can **create and share their own artifacts** from the artifact skill in Claude, Codex, Pi and
  OpenCode, through the collaboration MCP server. Members can share to the group only — never publicly.

## Non-goals / anti-goals

* Not real-time co-editing, not branches (one head per artifact), not a general permissions redesign.
* Not writing binary files (PDF, images, parquet), `report`-profile or renderer-built artifacts via collaboration in
  v1 (read-only grants are possible for them).
* A grant must never allow more than the granted artifact's collaboration tools — no sharing, no ACL changes, no other
  artifact, no registry access from the MCP process.
* Anti-goal: silently overwriting either side. A merge that is not clean is never applied.
* Existing share links stay frozen copies; a collaborator commit never changes a live share link.
* Anonymous visitors (public links) have **no edit rights at all**: no annotations, no writes.
* **Members can never create a public link** (server-enforced, not just hidden in the UI). Public links remain an
  operator-only capability.

## Current state (what exists)

* `ArtifactStore.commit_version(... base_version_id ...)` is a compare-and-swap on the artifact head; a stale base
  raises `ArtifactConflict` (HTTP 409, `code: artifact_version_conflict`). Versions have a single
  `parent_version_id` (= base), so history is linear.
* A version = `output_manifest` (list of named files with sha256 / storage key) + source manifest blob + provenance.
* ACL permissions: read, edit, review, share, admin. Auth is `X-API-Key` with a role; no per-artifact credential.
* Share links (owner API under `/api/v1/artifacts/{id}/shares`, needs `share` permission): create → pending with
  dry-run summary → approve (token URL shown once) → revoke; events log. Web client has no share methods yet.
* Web UI: `packages/artifacts-view.js` `renderArtifactDetail`; API client `packages/herdr-web-client/client.js`.

## Design

### A. Share panel (GUI)

A "Share" card on the artifact detail page with three sections.

**Viewer links.** Form: version (default: current head), audience **Private** (nothing — the default; no link),
**Group** (a lab share: pick Cognito group(s) and/or emails), **Public** (expiry required, default 30 days, max 90),
optional title and entry file. "Review" calls create and shows the server's summary (exact files exposed, size,
warnings). "Generate link" calls approve with `confirm: true` and shows the URL **once** with a copy button, plus a
notice that it cannot be shown again (the token is stored hashed; to re-share, generate a new link). Below the form:
the list of links with state, audience, recipients, expiry, open/denied counts (from events), a per-link event
drawer, and a Revoke button. All state-changing calls are same-origin POSTs as the CLI already uses.

**Collaborators** (section B) and **Activity** (section C) are tabs of the same card.

New web-client methods mirror the existing routes (`listShares`, `createShare`, `approveShare`, `revokeShare`,
`shareEvents`) and the new collaborator routes. Rendering follows the existing conventions (`esc()`, `hd-*` classes,
`Promise.allSettled` per section, retry button, data attributes as test hooks). No secrets or URLs are written to
storage or logs; the URL is held only in the DOM until the panel is closed.

### B. Collaborators over the collaboration MCP server

Bots and external harnesses already reach the platform through the **collaboration MCP server**
(`https://localdev.mcp.metabolomics.us/mcp`, JSON-RPC over HTTP, `Authorization: Agent hca_…` + `X-Herdr-Device`).
That server runs in the isolated `collab-public` container with **no database or registry access**, and its tools are
a static list in `collab_mcp.py`. Artifact collaboration is therefore added as MCP tools, not as new public HTTP
routes, and no new token type is introduced.

**Who is a collaborator.** An existing collab *agent* (internal agent, or an external agent redeemed through an
invite) that the owner grants access to one artifact. New table grants stored as `artifact_acl` rows for principal `collab:<agent_id>` (see second builder check). The GUI's Collaborators section
lists the agents the owner can see, grants read or write per artifact, can create an external-agent invite and grant it
in one step, and revokes. Revocation takes effect on the next request.

**MCP tools (new list `_ARTIFACT_TOOLS`, offered by `tools_for` only to a principal holding at least one grant,
always refused to agents without one).** All validated by hand with `additionalProperties: False`, results wrapped
`untrusted=True`:

| Tool | Right | Purpose |
|---|---|---|
| `artifacts_list` | read | artifacts the agent is granted, with head version and permission |
| `artifact_head` | read | head version id/number and file manifest (names, sizes, sha256, media types) |
| `artifact_read` | read | contents of one file (text, size-capped) of a version; `version_id` optional (default head) |
| `artifact_history` | read | versions with parents, author, message, merge flag |
| `artifact_commit` | write | submit a commit (below); returns a ticket id immediately |
| `artifact_commit_result` | write | result of a ticket: `created`, `merged`, `conflict` (with markers), `rejected`, or `pending` |

**How it reaches the registry without breaking the boundary.** The same pattern as share-links, in both directions:

* *Reads:* a worker pass (the control-plane side, which has the registry) publishes a read-only **mirror** of each
  collaboration-enabled artifact — `head.json` and the head version's text files — into a directory that is mounted
  read-only into `collab-public`. The mirror is refreshed on every commit and on a short timer. Mirrors carry only
  what a granted agent may read and are capped per artifact.
* *Writes:* `artifact_commit` appends an **intent** (agent id, artifact, base version, changes with inline text,
  message, idempotency key) to an append-only intents directory that is mounted read-write into `collab-public` and
  read by the worker. A dedicated fast loop in the worker (about 1 s) validates the intent against
  the artifact ACL grants (the control-plane side is the authority, not the MCP process), applies the merge/commit
  as principal `collab:<agent_id>`, and writes a **result** file the MCP tool reads. `artifact_commit_result` waits
  briefly (long-poll like `tasks_wait`, bounded) so a simple client sees the outcome in one or two calls.

Trust note: a compromised `collab-public` could forge intents for any agent that holds a grant, but only against
artifacts that were explicitly opened for collaboration, with every effect versioned, attributed and revertible by the
owner. It still cannot touch the registry otherwise. The same trust level already applies to `share-public`.

**Commit semantics (unchanged in spirit).** Intent carries `base_version_id`, `message`, `idempotency_key` and
`changes`: list of `{name, op: put|delete, text}` against the base. v1 collaborator writes are **text files only**,
≤ 100 changed files, ≤ 2 MiB total per commit.

1. `base == head` → fast-forward: new version, parent = [head] → result `created`.
2. `base != head` → three-way merge of base / head / mine per file. Clean → version with
   `parent_version_ids = [head, base]` → result `merged`.
3. Conflict → nothing is stored except a rejected-attempt log row; result `conflict` with `head_version_id`,
   per-file conflict kind, and `merged_text` with `<<<<<<<`/`=======`/`>>>>>>>` markers. The contributor resolves and
   commits again with `base_version_id = head_version_id`. Resolution is always done by the side that pushed late,
   like a rejected `git push`.

**Merge rules (per file name):** changed on one side only → take it; identical on both → take it; delete vs unchanged
→ delete; added on both with different content, or delete vs modify → conflict; both modified → line-based diff3
(own implementation on `difflib`, no `git` dependency) for text within a size cap, non-overlapping hunks merge,
overlapping produce a conflict; non-UTF-8 or oversize → conflict. A merged result must pass the artifact's existing
manifest checks, else it is treated as a conflict (`kind: validation`).

**Scope.** Write grants are available only for **plain file artifacts** (no `report_descriptor`, no renderer source).
Report and renderer-built artifacts can be granted read-only. Binary files are not writable by collaborators in v1.

**History model.** A migration adds `parent_version_ids text[]` (backfilled from `parent_version_id`, which stays the
first parent so readers and lineage emission keep working), `created_via` (`owner`|`collaborator`) and
`collaborator_agent_id`. Merge versions emit lineage edges for each parent. Rejected attempts are logged in
`artifact_collab_attempts`.

**Limits, audit, safety.** Per-agent commit rate (reuse `_throttle`, default 30/min) and daily byte quota. Every
tool call and every denial is audited through the collab event log; every applied commit is attributed to the agent
in the version. Collaborator-authored content is untrusted: the GUI previews it only inside the existing sandboxed
preview, and `artifact_read` results carry the `UNTRUSTED` notice. A retracted artifact refuses commits.

**Owner revert.** The owner (normal API key, `edit` permission) can restore any earlier version with
`POST …/versions` (new version restoring that manifest); the GUI gets a "Restore this version" button. No history
rewriting.

**Docs and CLI.** Tools are documented in `packages/collab-guide.md` (and its mirror in `docs/operations`) and a short
skill reference. A thin owner CLI `herdr-eng artifacts collab grant|list|revoke` mirrors the GUI. Harnesses use the
MCP tools directly (a pull/push CLI is not needed in v1).

### C. Activity

The third tab shows the version list as a simple history: version number, author (owner / collaborator label),
message, parents (merge marked), and a toggle for rejected attempts. Selecting a version shows its file list and a
two-way diff against its first parent (reusing the existing text diff). Collaborator list shows last used, commit
count, and a Revoke button.

### D. Annotations by signed-in group members

Only a signed-in lab viewer who passes the share's lab check (verified Cognito token, on the allow-list) and the
artifact owner can annotate. Public/anonymous visitors cannot (the endpoint refuses them).

* **Anchor.** HTML: W3C-style text-quote selector (exact text + short prefix/suffix) plus the file path within the
  bundle; PDF: page number and optional quoted text; other types: whole-document comment. Parquet data views are not
  annotatable in v1.
* **Capture.** The lab viewer shell already hosts the content; for HTML the share server injects a small
  selection script into served HTML (the sandboxed content cannot be touched from outside) that reports selections to
  the viewer shell by `postMessage`. The shell shows an "Annotate" button and a comment box and posts to
  `POST /s/<token>/k/<session>/annotations` (session-authenticated, body size-capped, rate-limited per user and per
  share). The text is stored as plain text, never rendered as HTML.
* **Write path.** `share-public` stays database-free: it appends the annotation (share id, version id, author email
  and sub from the verified session, anchor, comment, time) to an append-only annotations file; the worker ingests it
  into `artifact_annotations` (status `open` | `addressed` | `dismissed`, `addressed_in_version_id`). The usual
  size/rotation rules of the audit events file apply.
* **Owner view.** A **Feedback** tab in the Share panel lists annotations per artifact (author, version, anchor, comment,
  status), with a Dismiss action and a link to the version that addressed it. Annotators see their own annotations
  and the "addressed in version N" status when they reopen the link.
* **Agent integration.** New MCP tools for granted agents: `artifact_annotations` (open annotations, wrapped
  `untrusted=True`) and `artifact_annotation_resolve` (marks annotations addressed by a given version id the agent
  just committed). The agent integrates them with the normal `artifact_commit` (section B). Annotation text is
  untrusted input to the agent (prompt-injection risk): it is delivered as data with the `UNTRUSTED` notice, the
  resulting revision is an ordinary attributed, revertible version, and nothing goes live automatically — a live
  share link stays frozen on the version people saw until the owner shares the new one ("share latest" convenience is
  a follow-up).

### E. Members share their own artifacts

Today the control plane is single-operator. This section makes it multi-user for artifacts, narrowly.

* **Ownership.** `artifacts` gets an `owner_principal` (existing artifacts: the operator). A member sees and manages
  only artifacts they own, plus artifacts explicitly granted to them (section B), plus artifacts shared with them via a
  lab link (read-only viewing through the share page). The operator sees everything.
* **Who is a member.** A collab agent whose owner is a user in the configured group (`collab.cognito_group`, as
  already used by the collab service). Disabled users lose access immediately.
* **MCP tools for members** (offered only to such agents, acting on the caller's own artifacts only):
  `artifact_create`, `artifact_publish` (new version of an own artifact; chunked upload), `artifact_share_create`,
  `artifact_share_approve`, `artifact_share_list`, `artifact_share_revoke`, `artifact_share_events`. Share creation
  from a member accepts **only the lab audience** (group and/or listed emails of pool users); `audience: public` is
  refused with a clear error and an audit row, regardless of caller. Expiry and the summary/confirm flow are the same
  as for the operator; the member's agent shows the exact file list to its human before approving.
* **Upload.** MCP requests are small, so file content goes in chunks (`artifact_upload_chunk`, ~512 KiB each,
  hash-checked, assembled in a quarantine area by the worker); caps per member: 25 MiB per artifact, 200 MiB total,
  20 artifacts live. Bundles larger than that are an operator action. Content types follow the share rules (PDF, HTML,
  HTML + parquet); the same bundle scanner and sandbox CSP apply, and members' bundles get an extra warning banner
  in the lab viewer ("shared by <name>").
* **Writes go through the intents queue** (section B): intent → worker validates ownership/quotas against the
  control-plane tables → applies as the member's principal → result file. Every action is audited; a revoked or
  disabled member's pending intents are rejected.
* **Skill.** The existing artifact skill (`skills/herdr-artifact`, installed for Claude, Codex, Pi and OpenCode) gets
  an **MCP mode**: the same commands (`create`, `publish`, `share`, `list`, `revoke`) call the MCP tools when the
  collaboration server is configured and the control-plane CLI is unreachable or not configured. The skill reference
  and `packages/collab-guide.md` document the tool set; harness install/update paths stay as they are.
* **Operator controls.** A Members panel (operator only): list members' artifacts and shares, revoke any share, set
  per-member quotas, disable a member's sharing.

## Decisions made by me (operator may overrule)

* Viewer-link URL is shown once (token stored hashed) — re-sharing means generating a new link.
* Collaborators are existing collab agents granted per artifact (operator's direction: use the collaboration MCP
  server); no new credential type and no new public route.
* Writes are applied asynchronously by the control-plane worker from an intents queue; the MCP tool waits briefly for
  the result. This keeps `collab-public` free of registry/database access.
* Line-based text merging only; binary, report and renderer-built artifacts are not writable by collaborators in v1.
* Conflicts are returned to the pushing contributor; the owner is not asked to resolve them in the GUI in v1.
* "Group" in the GUI maps to the existing `lab` audience (Cognito groups and/or emails).
* Default agent grant lifetime 30 days; commit rate 30/min.
* Annotations: lab members and the owner only, text-quote anchored (HTML) or page anchored (PDF), shown in a Feedback
  tab; the agent integrates them via MCP and a new revision never changes a live link by itself.
* Members: group-only sharing, no public audience ever, 25 MiB/artifact and 200 MiB/member default caps, 20 live
  artifacts, chunked upload through MCP. Operator public links are unchanged.

## Builder-check resolutions (my calls unless marked)

* **v1 scope of collaboration = plain file artifacts** (no `report_descriptor`, no renderer source): the outputs are
  the content. Renderer-built and `report` artifacts are *readable* by collaborators (`read` permission) but a write
  token cannot be issued for them in v1; the GUI explains why. Editing sources with server-side re-render is a
  follow-up. This replaces the earlier "stale push to a report is a conflict" wording.
* **Identity:** a collaborator commits as principal `collab:<agent_id>` (own `created_by`, own idempotency
  namespace). The artifact owner/admin may retract or restore collaborator-authored versions; the lineage permission
  check is extended to "artifact admin", nothing else.
* **Merge engine lives in the store** (`ArtifactStore` merge helper) and is used by the intent applier in the worker.
  Owner commits through the generic `POST …/versions` keep today's strict 409 in v1 (plain file artifacts have no
  render jobs to collide); adopting merge there is a follow-up.
* **Network reach (resolved by the operator):** harnesses use the existing collaboration MCP server and its
  agent credentials; nothing new is exposed at the edge.

## Second builder check: resolutions (my calls; operator may overrule)

* **Who is the authority (agents, grants).** `collab-public` is authoritative for *who an agent is* (it authenticates
  `hca_` credentials, knows revoked/expired/disabled). The control plane is authoritative for *what that agent may do*
  (grants, ownership, quotas). Grants and the member list are mirrored *into* `collab-public` (files under `/sync`-style
  read-only mount) so `tools_for` and reads work; the worker re-checks grants on every intent, and every intent carries
  a timestamp the worker rejects after 60 s. Honest promise: revocation/disabling takes effect within seconds, not
  instantly. `collab-public` also writes a read-only `agents.json` feed (agent id → owner user, status, external flag,
  group membership) that the worker uses to confirm an agent is still active when applying an intent.
* **Members have no web UI.** Members use the artifact skill / MCP tools only (as the operator described). Members'
  own Feedback and history are read through MCP tools (`artifact_annotations`, `artifact_history`). Members do not
  grant collaborators in v1; the operator does.
* **Share URL travels back once.** The approve result for a member is written to a 0600 result file on the volume
  shared only by the worker and `collab-public`, deleted on first read by the MCP tool and swept after 60 s. The token is
  never stored in plain text elsewhere (the share row still holds only its hash).
* **Ownership and permissions reuse what exists.** No `owner_principal` column: an artifact's owner is its existing
  `created_by`. Member principal = `member:<cognito sub>`; collaborator grants are rows in the existing
  `artifact_acl` for principal `collab:<agent_id>` (with `expires_at`); `artifact_collaborators` is dropped. Quotas and
  last-used tracking live in a small `artifact_member_usage` table.
* **Owner annotations.** The owner annotates through the lab link like any lab member (sign in with Cognito, owner's
  email is added to the share's allow-list automatically). A capture path in the dashboard preview is a follow-up.
* **Mirror contents.** Head version files plus the previous 5 versions' text files; manifests for all versions.
  `artifact_read` of an older, unmirrored version returns a clear "not mirrored" error.

## Delivery order (five plans, each shippable on its own)

1. **Share panel GUI** (section A) + history view + "Restore this version". Front end on existing routes.
2. **History model and merge engine**: migration (`parent_version_ids`, `created_via`, `artifact_collab_attempts`),
   diff3 merge in `ArtifactStore`, per-parent lineage edges. Backend only.
3. **Bridge and collaborators** (section B): mirror, intents queue + fast loop, grants via ACL, read/commit MCP
   tools, owner CLI, Collaborators and Activity tabs.
4. **Annotations** (section D).
5. **Member-owned artifacts** (section E), last: it is the riskiest and depends on the bridge.

## Open for the builder

Exact file/column names and migration number (next free after main's latest), choice of rate-limit mechanism, and
the visual layout of the panel within the existing design system.
