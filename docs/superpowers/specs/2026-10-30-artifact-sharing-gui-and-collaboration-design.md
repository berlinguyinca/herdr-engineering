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
* A **collaborator** credential scoped to exactly one artifact, read-only or read+write, revocable at once.
* **Git-like commit semantics**: every commit names the version it started from; a stale base is merged
  automatically when the changes do not overlap and rejected with a conflict report when they do.
* **History is never lost**: merges become real versions with two parents; rejected pushes are logged, not stored as
  versions; revert is a new version.
* Everything is usable by a bot with only HTTP + a token, and by a `herdr-eng` CLI wrapper with `pull` / `push`.

## Non-goals / anti-goals

* Not real-time co-editing, not branches (one head per artifact), not a general permissions redesign.
* Not merging binary files (PDF, images, parquet) or `report`-profile artifacts (structured descriptors): a stale
  push to those is always a conflict in v1.
* Collaborator tokens must never be able to reach anything but their one artifact's collaboration routes — no
  sharing, no ACL changes, no other artifact, no other API.
* Anti-goal: silently overwriting either side. A merge that is not clean is never applied.
* Existing share links stay frozen copies; a collaborator commit never changes a live share link.

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
invite) that the owner grants access to one artifact. New table `artifact_collaborators(artifact_id, agent_id,
permission read|write, granted_by, created_at, expires_at, revoked_at, last_used_at)`. The GUI's Collaborators section
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
  `artifact_collaborators` (the control-plane side is the authority, not the MCP process), applies the merge/commit
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

## Open for the builder

Exact file/column names and migration number (next free after main's latest), choice of rate-limit mechanism, and
the visual layout of the panel within the existing design system.
