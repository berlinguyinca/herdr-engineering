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
  OpenCode, through the collaboration MCP server, and from a small web UI ("My artifacts") at the share host. Members
  may create lab or public links on artifacts where they hold the `share` permission, under the operator's rules
  (see section E; amended 2026-10-30 by operator decision, reverses the earlier lab-audience-only rule).

## Non-goals / anti-goals

* Not real-time co-editing, not branches (one head per artifact), not a general permissions redesign.
* Not writing binary files (PDF, images, parquet), `report`-profile or renderer-built artifacts via collaboration in
  v1 (read-only grants are possible for them).
* A grant must never allow more than the granted artifact's collaboration tools — no sharing, no ACL changes, no other
  artifact, no registry access from the MCP process.
* Anti-goal: silently overwriting either side. A merge that is not clean is never applied.
* Existing share links stay frozen copies; a collaborator commit never changes a live share link.
* Anonymous visitors (public links) have **no edit rights at all**: no annotations, no writes.
* Public visitors have **no edit or annotation rights**, whoever created the link. (Amended 2026-10-30 by operator
  decision: members may now create public links where they hold `share`; the earlier ban on member public
  links is reversed. Every member link is audited, carries a "Shared by" banner, and the operator sees and
  revokes all of them.)

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

**Constraint: no frames anywhere, the dashboard included (operator 2026-10-30).** No page, script or style sheet this
project ships creates or contains a frame, frame set, `object`, `embed`, `portal`, fenced frame or `srcdoc`, and nothing
uses `postMessage` between documents (the vendored pdf.js posts to its own worker, which is allowed). A repo-wide test
(`tests/control_plane/test_no_frames.py`) scans the sources, checks every rendered response and runs the sanitiser on
frame markup. The raw `f/` route and the owner `live` route serve an artifact's own bytes and are the only exceptions.

**Amendment 2026-11-05 (report Rich view).** One deliberate exception in the owner dashboard: a report's own HTML page may be shown in a single `<iframe sandbox="">` (empty sandbox, no `allow-*` token, no `srcdoc`) whose `src` is the owner route `/api/v1/artifacts/{id}/versions/{vid}/rich`. That route answers with `Content-Security-Policy: sandbox; default-src 'none'; …; frame-ancestors 'self'`, so the page is inert even without the attribute. `/live` stays unframeable (`frame-ancestors 'none'`, `X-Frame-Options: DENY`). The share host's frame is covered by the next amendment. `tests/control_plane/test_no_frames.py` allow-lists only `packages/artifact-rich-view.js` and pins that exact shape.

**Amendment 2026-11-05 (report share Rich view, opt-in).** A report share may include the author's single `text/html` asset, only when the creator sets `include_author_html` (CLI `--include-author-html`; default off; refused for member-created links). The asset is chosen by the dashboard's `pick_html_asset`, capped at 2 MiB and hash-verified when the bundle is built; the approval plan lists `author-page.html` with its size and a warning, and `state.json` carries `rich: author-page.html` only after approval. The share host serves it at `/s/<token>/f/author-page.html` with the dashboard Rich view headers minus the two framing controls (bare `sandbox` with no `allow-*`, `default-src 'none'`, nosniff, no-referrer, no-store, noindex; no `frame-ancestors`, no `X-Frame-Options`, because the share preview is sandboxed with an opaque origin that can never match `'self'`/SAMEORIGIN; the dashboard route keeps both), 404 without the opt-in, and with the same access, revocation and expiry checks as the preview. The preview of an opted-in share gains a "Rich view" toggle that mounts `<iframe sandbox="">`; the preview keeps its opaque origin exactly as for any share (its header sandbox never gains `allow-same-origin`) and only its meta CSP gains one hash-pinned script and `frame-src 'self'`. `test_no_frames.py` also allow-lists `share_static/rich-toggle.js` with a pinned shape. Report evidence files stay unshared.

* **Share host.** Share links live on their own host, `control_plane.share.public_url` (for example
  `https://artifacts.metabolomics.us`), served by `share-public`. The collaboration (MCP) host serves no `/s/`.
  Sessions, the CSRF token and the Cognito callback (`/s/callback`) belong to the share host.
* **Two views of a lab share.** A lab link lands directly on the **live** view (`f/` for HTML, `v/` for the other
  types), and a mode switch moves between **Live** and **Annotate**: on HTML an injected, script-free link on `f/`; on
  `v/` and `a/` a mode bar. The annotation view `a/<path>` is a **static render**, never the running page: HTML goes
  through `nh3` (an allow-list sanitiser run in a killable child process with a deadline), text becomes an escaped
  `pre`, images are an `img`, PDFs are drawn to canvases by the vendored pdf.js. Scripts, forms, frames and every
  external URL are removed or rewritten to the bundle's own files. The live page stays sandboxed as before.
* **Capture.** The annotation view has a per-response nonce CSP (`default-src 'none'`; script, style, image, font and
  connect sources limited to the share host and the nonce; no `frame-src`). The sanitised content is mounted in an
  open shadow root on an inner element of a wrapper that has `contain: paint` and `overflow: clip`, below the mode bar
  and the annotation panel, so content styles cannot reach the page and cannot cover the panel. The selection is read
  in the page itself (`anchor.js`), with no script injected into the content and no `postMessage`. The panel posts to
  `POST /s/<token>/k/<session>/annotations` with a CSRF token that is delivered in our markup only
  (session-authenticated, body size-capped, rate-limited per user and per share). The text is stored as plain text,
  never rendered as HTML.
* **Anchor.** HTML and text: W3C-style text-quote selector (exact text + short prefix/suffix) plus the file path within
  the bundle; PDF (`pdf_page`): page number, optional quoted text and an optional normalised region; images: a
  normalised region; other types: whole-document comment. Parquet data views are not annotatable in v1.
* **Edit and delete.** The author may edit or delete their own annotation while it is **open**; an edit records
  `edited_at`, a delete sets the status `withdrawn`. A resolved (addressed or dismissed) annotation is final for the
  author. Withdrawn annotations are kept for the owner's audit under the status filter and are not offered to agents.
* **Limits.** `enabled`, `max_comment_chars` (4000), `per_user_per_minute` (60), `per_share_per_minute` (600),
  `global_per_minute` (3000), `per_user_per_day` (2000) and `max_per_share` (20000) are YAML settings under
  `control_plane.share.annotations`; the defaults are higher than in the first draft (`ANNOTATION_DEFAULTS`).
* **Write path.** `share-public` stays database-free: it appends the annotation (share id, version id, author email
  and sub from the verified session, anchor, comment, time) to an append-only annotations file (`op` `create` | `edit`
  | `delete`); the worker ingests it into `artifact_annotations` (status `open` | `addressed` | `dismissed` |
  `withdrawn`, `addressed_in_version_id`) and writes each author's own projection back for their read-back. The usual
  size/rotation rules of the audit events file apply.
* **Owner view.** A **Feedback** tab in the Share panel lists annotations per artifact (author, version, anchor, comment,
  status, "edited"), with a status filter, a Dismiss action and a link to the version that addressed it. A failed read
  shows a fixed message and Retry, never the server's text. Annotators see their own annotations and the "addressed in
  version N" status when they reopen the link. The dashboard previews of HTML, text, PDF and images use the same static
  render (shadow root, no frame).
* **Agent integration.** MCP tools for granted agents, registered through the intent registry: `artifact_annotations`
  (open annotations, wrapped `untrusted=True`) and `artifact_annotation_resolve` (marks annotations addressed by a
  given version id the agent just committed; a read-only grant is refused). The agent integrates them with the normal
  `artifact_commit` (section B). Annotation text is
  untrusted input to the agent (prompt-injection risk): it is delivered as data with the `UNTRUSTED` notice, the
  resulting revision is an ordinary attributed, revertible version, and nothing goes live automatically — a live
  share link stays frozen on the version people saw until the owner shares the new one ("share latest" convenience is
  a follow-up).

### E. Members share their own artifacts

> **Amended 2026-10-30 by operator decision; reverses the earlier group-only rule.** This section describes the
> as-built behaviour (plan 5, `docs/superpowers/plans/2026-10-30-member-owned-artifacts.md`, decisions D1-D10). The
> earlier text (group-only sharing, no public audience, 25 MiB per artifact / 200 MiB per member / 20 artifacts, no
> member web UI) no longer applies.

Today's control plane was single-operator. This section makes it multi-user for artifacts, narrowly.

* **Ownership (no new column).** An artifact's owner is its existing `created_by`; a member's principal is
  `member:<cognito username, else sub>`, derived in one place. A member sees and manages the artifacts they own, plus
  artifacts explicitly granted to them (section B), plus artifacts shared with them via a lab link (read-only viewing
  through the share page). `collab:` principals (grant collaborators) never create shares.
* **Operator access (D4).** The operator has **read** and **share** (not edit or commit) on member-owned artifacts and
  may create lab or public links on them (active artifacts only; not publications). One rule, `operator_member_access`,
  feeds every permission check.
* **Who is a member.** An *actor* is `Actor(kind, id)`: a collab agent (`kind: agent`) or a signed-in Cognito user
  (`kind: user`, the web UI). The user must be approved, not disabled and in the configured group
  (`collab.cognito_group`); an agent must also be active and **not external** (external agents are never members).
  Membership is read from a directory feed with a tri-state status (`approved`, `disabled`, `removed`, `unknown`).
  Only positive evidence is destructive: `disabled` acts at once, `removed` only after a grace period (default 3600 s,
  `collab.members.removed_grace`), and an absent or stale directory (restart, expired claims) revokes and deletes
  nothing and fails closed for writes. Revocation therefore takes effect within seconds for disabling and up to the
  grace period (plus the claim lifetime) for group removal.
* **Two front ends, one service layer.** The MCP tools and the web API call the same transport-neutral member service,
  so they behave identically. MCP tools (offered only to members): `artifact_create`, `artifact_publish`,
  `artifact_upload_chunk`, `artifact_share_create`, `artifact_share_approve`, `artifact_share_list`,
  `artifact_share_revoke`, `artifact_share_events`, `artifact_delete`, plus listing, history and usage reads. Member
  annotation tools are out of scope in v1. Members still do not grant collaborators; the operator does.
* **Web UI (D2).** "My artifacts" is served by `collab-public` at `share.public_host` (the artifacts host, replacing
  the old host's `/s/`: share links live on the new host only, no redirects). Sign-in reuses the lab Cognito login; the
  browser exchanges it for a short member session (own signing domain, `Authorization: Member` header, never a cookie,
  kept in memory/sessionStorage). The page is plain DOM, no iframe/frame/object/embed, and is served with a strict
  **exact-path CSP** (the edge passes the application's CSP through unchanged). Features: list, chunked upload with
  progress, version history, share (lab or public), list and revoke own links, delete, usage. The member web API is
  mounted only for the configured host; on any other host it is a plain 404.
* **Sharing (D3, D8).** A member may create a lab or public link on any artifact where their principal holds the `share`
  permission (their own, or granted with `share` or `admin`; `read`/`edit`/`review` alone do not include it). A `share` holder can create and open a link, so `share` effectively
  lets them read the bytes even though the member API stays gated on `read`. The rules
  are the operator's: a pinned immutable version, an expiry, revocable, and a two-step create then approve with
  `confirm: true` by a `share` holder (the creator). Public links default to 30 days and cap at 90; lab links likewise
  default to 30 days and cap at 90. A member's **lab** audience is exactly the lab group (`collab.cognito_group`); the
  per-member allow-list of emails must belong to known pool users (`member_audience_refused` otherwise). Member
  shares have their own caps (`collab.members.share_limits`, `null` = unlimited) and rate limits; the global
  `control_plane.share.max_share_bytes` / `max_total_bytes` keep applying to operator-created shares only.
  Denial is a non-disclosing `not_found`.
* **Share URL travels back once.** The approve result never carries the token. The URL is written to a read-once
  secret file on the volume shared by the worker and `collab-public`, deleted on first read and swept after 60 s; the
  share row holds only the hash. Because the worker and `collab-public` run as different uids, the secrets directory
  is group-shared (`members.secrets_gid`, falling back to the worker's `HERDR_COLLAB_CERT_UID`); with no resolvable
  gid, members **fail closed** at start-up rather than approving links nobody can read.
* **"Shared by" banner.** Every link created by a member (lab or public) is labelled "Shared by <name> — content was
  not reviewed by the operator". The name is server-authoritative (display name, else user id, stored in the share's
  `state.json`); operator links carry no label and stay byte-identical. The bar is injected server-side into HTML on
  the `v/`, `a/` and raw `f/` pages, after a strict tokenizer gate; member links refuse types that cannot be labelled
  (SVG, XHTML/XML, PDF) and HTML over 2 MiB, with odd charsets or with markup the gate cannot prove renders the bar
  (fail closed). The banner is a best-effort provenance hint, not a security boundary (page script or CSS can hide
  it); containment is the sandbox CSP, the opaque origin, the group/expiry and revocation. Public visitors get no
  annotation or edit UI.
* **Upload and storage (D1, D10).** There are **no default storage caps**. Usage is always accounted (bytes, artifact
  and version counts per member, soft-deleted artifacts reported separately until purged) and shown to the member and
  the operator. Limits exist only as optional YAML values (`collab.members.limits`, default `null` = unlimited) with
  per-member overrides set by the operator. Anti-abuse rate limits and per-request sanity limits stay, all
  configurable (default per-file limit 1 TiB, `request_limits.max_upload_bytes`, `null` = only the structural bound; the effective limit is the smaller of it and the collab_artifacts bridge limits). File bytes use plan 3's chunk protocol
  unchanged (fixed 512 KiB chunks, base64 on MCP, sha256 and size verified at assembly, idempotent resumable), staged
  under a per-member owner key so an upload resumes across MCP and web, bounded by a staging cap and swept by TTL;
  the worker assembles at publish, so multi-GB uploads do not ride the 60 s intent clock. Content types follow the
  share rules (HTML, PDF, HTML + parquet) with the same bundle scanner and sandbox CSP (members' lab/public links add
  the type restrictions above).
* **Writes go through the intents queue** (section B): an intent carries the actor and the worker re-checks
  membership, the `share` permission and any configured limit against Postgres, then applies it as the member's
  principal and writes a result file. Intents expire after 60 s; a disabled or removed member's pending intents are
  rejected. Every member share create/approve/revoke (lab and public) writes an `artifact_member_events` audit row
  with principal, actor, artifact, share id, audience and outcome, besides the existing share events.
* **Delete and purge (D5).** A member can delete their own artifact: it is retracted, its live links are revoked, open
  uploads aborted, and it is **tombstoned** (hidden from readers and the member's lists). Only the operator can
  **purge** (remove the bytes), with an explicit confirmation that repeats the artifact id; purge composes the existing
  tombstone-and-purge lifecycle, refuses artifacts under legal hold or without a purge date, revokes any open links
  first, and **anonymises** the member's third-party data (annotation text and author identity, share-event viewers,
  staged names, display name and original filename become `[removed]`; the feedback table is redacted through an
  append-only-safe migration path).
* **Disabling (D6).** Disabling a member (directory `disabled`, removal from the group after the grace period, or
  sharing disabled by the operator) revokes the live and pending links they created; the existing sweep runs it and
  is idempotent.
* **Operator controls.** A Members area (operator API `/api/v1/artifact-members` and `herdr-eng artifacts members`
  list|show|links|limits|disable-sharing|enable-sharing|revoke-share|purge, plus the dashboard view): list members,
  usage and every member link (filterable), revoke any link, set per-member limits, disable or re-enable a member's
  sharing, purge. All operator routes need the operator role, check same-origin, and answer 404 for unknown targets.
* **Skill.** The existing artifact skill (`skills/herdr-artifact`, installed for Claude, Codex, Pi and OpenCode) has an
  **MCP mode**: the same commands (`create`, `publish`, `share`, `list`, `revoke`) call the MCP tools when the
  collaboration server is configured and the control-plane CLI is unreachable or not configured. Very large files
  should go through the web UI (over MCP a 1 TiB file is ~2 M chunks). `packages/collab-guide.md` documents the set.
* **Schema.** Migration `0048` adds the member usage/limits and audit tables and indexes; `0049` adds the purge
  redaction path for feedback. (`0047` belongs to the artifact-delete work.)

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
* Members (amended 2026-10-30 by operator decision; reverses the earlier lab-only rule): lab or public links on
  artifacts where they hold `share`, under the operator's rules, audited, with a "Shared by" banner; no default storage
  caps (accounting plus optional YAML limits); a member web UI at `share.public_host`; delete (member) and purge
  (operator); operator read + share on member content; member share caps separate from the operator's. Public visitors
  keep no edit or annotation rights.

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
* **Members use the skill/MCP tools and a web UI** (amended 2026-10-30; the earlier "no web UI" call is superseded, see
  section E). Members do not grant collaborators in v1; the operator does. Member annotation MCP tools are out of
  scope in v1.
* **Share URL travels back once.** The approve result for a member is written to a read-once secret file (group-shared
  between the worker and `collab-public`, see section E), deleted on first read and swept after 60 s. The token is
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
