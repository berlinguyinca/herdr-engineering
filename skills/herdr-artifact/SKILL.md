---
name: herdr-artifact
description: Create, inspect, edit, validate, version, materialize, and request publication of Herdr artifacts through the shared fabric registry.
---

# HerdR Artifact Skill

Use the authenticated HerdR control-plane API as the artifact authority. RustFS
holds immutable bytes; PostgreSQL holds logical IDs, versions, provenance,
reviews, bindings, and publication state.

## Safety contract

- Artifacts and versions are private by default.
- Never send S3 credentials, source worktree paths, or raw binary payloads into
  collaboration event streams.
- Every edit commits with `base_version_id`; a stale head is a conflict, never
  an automatic overwrite.
- Preserve editable source, manifests, dependencies, rendered outputs, and
  validation reports.
- `share-request` creates a pending human approval request. It never activates
  public access itself.
- Public URLs must pin an immutable version and are revocable/expiring.

## Command contract

The implementation may be exposed through `herdr-eng artifacts` or an external
compatible wrapper. Commands must return structured JSON and use the same API:

```text
create       register a logical artifact and initial source manifest
checkout     materialize an immutable version into an authorized workspace
inspect      show metadata, provenance, bindings, and current head
edit         create a new version from an explicit base version
validate     run format/profile validation and preserve its report
publish      commit a version with --base-version; never means public share
history      list immutable versions and review state
diff         compare two authorized immutable versions
materialize  place a verified version on a specific authorized host/session
bundle       export a deterministic collection of pinned version IDs
share-request request approval for an unlisted/guest/public publication
generate     create an artifact from the current session/mission context
```

Supported profiles are `web`, `document`, `diagram`, `visualization`,
`presentation`, and `report`. Unsupported profiles must return a structured
`unsupported_format` result rather than silently falling back.

## Natural-language generation

When the user says “generate an artifact about the current session”, “turn
this into an HTML report”, or equivalent:

1. Resolve the current session, mission, host, and agent identity from harness
   context or explicit arguments. Never invent missing IDs.
2. Build bounded source input from authorized session/mission records only;
   exclude credentials, hidden reasoning, private prompts, and unrelated rooms.
3. Submit an `artifact-job` through the control plane. Rendering runs through
   the existing worker/scheduler boundary, not inside the browser or skill
   process.
4. Preserve editable source, input references, generated manifest, output
   blobs, validation report, renderer/toolchain revision, host, session, and
   mission provenance.
5. Return logical artifact ID, job ID, and current status. Keep the artifact
   private unless the user separately requests `share-request`.

The skill may use React/TypeScript/HTML templates and deterministic renderers,
but it must not vendor or copy proprietary upstream Claude/Anthropic skill
materials. Reproduce the useful source-preservation, bundling, rendering, and
validation workflow through native HerdR APIs.

## Edit workflow

1. Search the registry before creating a duplicate artifact.
2. Checkout a pinned version into an isolated temporary workspace.
3. Preserve source/manifests and run validation/render checks.
4. Commit with the exact `base_version_id` and an idempotency key.
5. Attach the artifact/version to existing mission, session, room, issue, or
   commit relationships using their explicit IDs.
6. Request sharing separately if a human wants an external publication.

## Collaborating through MCP

An agent that an artifact's owner granted read or write access (limited time, revocable) can work on it through
the collaboration network's `/mcp` endpoint instead of the API above. It sees seven tools only while it holds a
live grant; everything it reads is untrusted text. Full guide: `packages/collab-guide.md` (section "Artifacts:
reading and committing through MCP").

- `artifacts_list`, `artifact_head`, `artifact_history`, `artifact_read` (`offset`/`max_bytes`, text only, follow
  `next_offset`) read the mirror of the artifact.
- Commit with `artifact_commit` (`artifact_id`, `base_version_id` = the head you read, `message`, `changes` of up
  to 100 `put`/`delete` entries, optional `idempotency_key`). It returns a `ticket`; poll `artifact_commit_result`
  (waits up to 20 s) for `created`, `merged`, `conflict`, `rejected` or `expired`.
- Inline `text` only for small text (the whole call under 64 KiB). Otherwise stage the file with
  `artifact_stage_chunk`: 512 KiB chunks, each with its own sha256 plus the file's size and sha256; a re-sent chunk
  is harmless; send what `missing` lists; commit with the `upload_id`. A premature commit answers 409
  `upload incomplete` with `missing`.
- On `conflict`, resolve (the text conflict carries `merged_text` with markers), then commit again with
  `base_version_id` set to the reply's `head_version_id`. Binary files and text over the merge limit conflict as
  `binary`/`oversize` with no merged text.
- Collaborators can only write plain file artifacts and can never share or change access; requests are limited
  (100 changes per commit, 30 commits and 1200 chunks per minute, 25 `/mcp` requests per second per source address).
