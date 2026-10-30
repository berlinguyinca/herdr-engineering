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

### B. Collaborators (bots and external harnesses)

**Credential.** New table `artifact_collaborators`: `collaborator_id`, `artifact_id`, `label`, `kind`
(`bot`|`harness`), `permission` (`read`|`write`), `token_hash` (sha256, token never stored), `created_by`,
`created_at`, `expires_at` (default 30 days, max 365), `revoked_at`, `last_used_at`. The token is `hc_` + 43 URL-safe
characters, shown once when created. Revoke is immediate (checked on every request, no caching).

**Surface.** A new route module `artifact_collab_api.py` under `/api/v1/artifacts/{id}/collab/…` with its own
authentication: the `Authorization: Bearer hc_…` header is hashed and looked up; the token's artifact must equal
`{id}`; otherwise a uniform 404. These routes do not use `X-API-Key`, and the token is not accepted anywhere else
(the generic auth path rejects `hc_` tokens). Owner-side management routes (create / list / revoke collaborators,
list rejected attempts) live under the same prefix but require the owner's normal `share` permission.

| Route | Auth | Purpose |
|---|---|---|
| `GET collab/head` | collaborator read | head version id, version number, manifest (names, sizes, sha256, media types) |
| `GET collab/versions/{vid}` | read | manifest of any version of this artifact |
| `GET collab/versions/{vid}/files/{name}` | read | one file's bytes (hash-verified) |
| `POST collab/blobs` | write | upload a file's bytes; returns sha256 / storage key (size-capped) |
| `POST collab/commit` | write | commit (below) |
| `GET collab/versions` | read | history incl. parents, author collaborator, merge flag |

**Commit.** Body: `base_version_id` (required except for an empty artifact), `message`, `idempotency_key`,
`changes`: list of `{name, op: put|delete, blob}` against the base. The server computes the full new manifest as
*base + changes*, then:

1. `base == head` → fast-forward: new version, parent = [head]. 201 `{"result": "created"}`.
2. `base != head` → three-way merge of base / head / mine per file (below). Clean → new version with
   `parent_version_ids = [head, base]`, 201 `{"result": "merged", "merged_files": [...]}`. The committer is told what
   it now has so it can pull.
3. Conflict → nothing is stored except a rejected-attempt log row; 409 `{"code": "collab_conflict",
   "head_version_id", "conflicts": [{name, kind, base/theirs/mine hashes, "merged_text": "<<<<<<< … >>>>>>>"}],
   "clean_merged": [names]}`. The contributor resolves (using the markers), then commits again with
   `base_version_id = head_version_id` (a fast-forward), optionally listing `merge_of` in the message for the record.
   Resolution is therefore always done by the side that pushed late — exactly like a rejected `git push`.

**Merge rules (per file name; manifests compared by sha256):**

* changed on one side only → take that side; identical change on both → take it; delete vs unchanged → delete.
* added on both with different content, or delete vs modify → conflict.
* both modified: if the media type is text (`text/*`, JSON, YAML, XML, HTML, markdown, source code) and both sides
  are valid UTF-8 within a size cap (2 MiB per file), do a line-based diff3 (own implementation on
  `difflib.SequenceMatcher`; no external `git` dependency in the container). Non-overlapping hunks merge;
  overlapping hunks produce a conflict with markers. Binary, oversize, or non-UTF-8 → conflict.
* `report`-profile artifacts (those with a `report_descriptor`): fast-forward only; a stale push is a conflict with
  kind `report_not_mergeable`.
* After a clean merge the result must still pass the artifact's existing validation (renderer / manifest checks);
  a merged result that fails validation is treated as a conflict (`kind: validation`) and not stored.

**History model.** New migration adds `parent_version_ids text[]` (backfilled from `parent_version_id`) and
`created_via` (`owner`|`collaborator`), `collaborator_id`. `parent_version_id` stays the first parent so existing
readers and lineage emission keep working; merge versions emit lineage edges for each parent. A rejected attempt is
logged in `artifact_collab_attempts` (collaborator, base, head, conflict names, time) — never a version.

**Limits and safety.** Per request: ≤ 100 changed files, ≤ 64 MiB of new blobs; per collaborator: commit rate limit
(default 30/min) and a daily byte quota; whole artifact: the existing size policy. Collaborator content is
untrusted: the GUI previews it only inside the existing sandboxed preview, never inline. A retracted artifact refuses
commits (409, as today). Tokens in logs are redacted; events record collaborator id, never the token.

**Owner revert.** The owner (normal API key, `edit` permission) can create a version that restores any earlier
version's manifest (`POST …/versions` already exists; the GUI gets a "Restore this version" button that does exactly
that). No history rewriting.

**CLI for bots/humans.** `herdr-eng artifacts collab create|list|revoke` (owner) and
`herdr-eng artifacts collab pull <artifact> <dir>` / `push <dir>` (contributor; token from an env var *name*, never
the token on the command line). `pull` writes the head files plus a `.herdr-collab` state file with the base version;
`push` computes changes against the base, commits, and on a conflict writes the markers into the files, prints the
conflict list and exits 2; after fixing, `push` again. A short skill reference documents this for harnesses.

### C. Activity

The third tab shows the version list as a simple history: version number, author (owner / collaborator label),
message, parents (merge marked), and a toggle for rejected attempts. Selecting a version shows its file list and a
two-way diff against its first parent (reusing the existing text diff). Collaborator list shows last used, commit
count, and a Revoke button.

## Decisions made by me (operator may overrule)

* Viewer-link URL is shown once (token stored hashed) — re-sharing means generating a new link.
* Collaborator credentials are a separate token type with its own narrow route surface, not new API-key roles.
* Line-based text merging only; binary and report artifacts are fast-forward-only.
* Conflicts are returned to the pushing contributor; the owner is not asked to resolve them in the GUI in v1.
* "Group" in the GUI maps to the existing `lab` audience (Cognito groups and/or emails).
* Default collaborator lifetime 30 days; commit rate 30/min.

## Builder-check resolutions (my calls unless marked)

* **v1 scope of collaboration = plain file artifacts** (no `report_descriptor`, no renderer source): the outputs are
  the content. Renderer-built and `report` artifacts are *readable* by collaborators (`read` permission) but a write
  token cannot be issued for them in v1; the GUI explains why. Editing sources with server-side re-render is a
  follow-up. This replaces the earlier "stale push to a report is a conflict" wording.
* **Identity:** a collaborator commits as principal `collab:<collaborator_id>` (own `created_by`, own idempotency
  namespace). The artifact owner/admin may retract or restore collaborator-authored versions; the lineage permission
  check is extended to "artifact admin", nothing else.
* **Merge engine lives in the store** (`ArtifactStore` merge helper) and is used by the collaborator commit route.
  Owner commits through the generic `POST …/versions` keep today's strict 409 in v1 (plain file artifacts have no
  render jobs to collide); adopting merge there is a follow-up.
* **Network reach (operator's call, open):** see below.

## Open question for the operator

Where do the external harnesses run? **Tailnet/VPN only** (default, no edge change: they reach the control plane the
way your hosts do today) or **the public internet** (needs a new, deliberately narrow edge route to the artifact
registry, which the share-links design treated as an anti-goal; the `hc_` token would be the only barrier). v1 is
written for the tailnet; internet reach would be a separate, explicitly approved step with its own review.

## Open for the builder

Exact file/column names and migration number (next free after main's latest), choice of rate-limit mechanism, and
the visual layout of the panel within the existing design system.
