# Collaboration Rooms, Data, and Agent Tooling Design

**Status:** Design approved in brainstorming; implementation not started  
**Date:** 2026-10-24  
**Scope:** Collaboration rooms, user lifecycle, agent delegation, room files, archival, previews, direct S3 uploads, and reusable analysis skills.

## 1. Product intent

Herdr collaboration must let approved people create and manage chat rooms, attach agents that already exist in their external systems, share room files, and iteratively analyze those files through chat. Public API registrants must receive a clear waiting-room experience until approved. Administrators must be able to distinguish room-level removal from global account deletion.

The system is a self-developing analysis platform: agents and harnesses should be able to generate reusable visualization and data-processing tooling, refine it with users in chat, publish it at an appropriate scope, and download it locally for later use.

## 2. Design principles

- Keep user approval, room membership, agent ownership, agent delegation, and file upload permission as separate capabilities.
- Use the existing control-plane Postgres for structured collaboration state and an S3-compatible API for file bytes and archives.
- Keep the public edge stateless and without database/object-store credentials; the collaboration service is the shared source of truth for dashboard, web, MCP, and A2A clients.
- Transfer large files directly between the registered harness and S3; Herdr coordinates grants, manifests, validation, and status but does not proxy terabytes of data.
- Treat room deletion as archive-then-delete, never as an unverified destructive operation.
- Preserve only minimal audit metadata for deleted users; remove user-visible identity, authored messages, credentials, and access.
- Treat remote data, generated code, previews, and skills as untrusted until validated and sandboxed.

## 3. Authorization model

### 3.1 User lifecycle

Users have these effective states:

- `pending`: registered through the public API but awaiting platform approval.
- `approved`: allowed to create rooms, accept/invite room members, register existing external agents, and use approved rooms.
- `deleted`: no active account record; only a minimal audit tombstone remains.

A later registration by the same person creates a new identity and starts at `pending`. Old credentials, invitations, join tokens, and sessions never revive access. Deletion also scrubs the deleted identity and authored content from live records and retained room archives; only minimal security/audit metadata survives.

Administrators have all normal capabilities and may override room/member/agent permissions, but destructive operations remain audited and room deletion still requires a verified archive.

### 3.2 Rooms and members

An approved user may create, rename, and delete rooms; invite approved platform members; approve invitees; remove members from a room; and configure per-member file-upload permission.

Room membership is independent of platform approval:

- `pending`: invited or requesting access to one room.
- `approved`: may use the room according to room and agent permissions.
- `removed`: no access to that room; can be invited again.

“Remove from room” is reversible room-level revocation. It must not delete the user or affect membership in other rooms.

### 3.3 Agents

Users may register agents that already run in their own systems through MCP/A2A. They may rename or delete only their own registered agents. They may not create Herdr-native agents.

An agent can be attached to multiple rooms. An attachment does not automatically grant every room member task access. Agent task permission is represented by a separate `(room, agent, user)` grant:

- a member may request access when they first try to task the agent;
- the agent owner receives an approval prompt in chat and in their task list;
- approval grants ongoing access in that room;
- the owner can revoke it;
- agent access disappears when the agent is detached, the member is removed, or the user is deleted.

The agent should respond conversationally to an unapproved request: “Would you like to request access to me?”

### 3.4 Files

Files belong to rooms, not users. Room owners can upload and can grant upload permission to individual room members. A user deletion does not delete room file bytes, but it removes or anonymizes uploader attribution and any user-owned metadata. A room deletion removes live room files only after they have been archived and verified.

## 4. Public waiting room

After public registration, an unapproved user is routed to a dedicated waiting-room page, not a blank page or generic forbidden response.

The page displays:

- “Waiting for approval” status;
- a clear explanation that an administrator must approve access before rooms or agent management are available;
- the signed-in identity and registration time;
- automatic status updates through SSE or short polling;
- sign-out;
- explicit approved, denied, deleted/revoked, and temporary-service-failure states.

Before approval, the user must not learn room names, member names, agent names, or other private operational details.

Once approved, the page transitions to room list, invitations, agent management, MCP/A2A setup, and pending agent-access requests.

## 5. Persistence architecture

Rooms live in the central collaboration service, not in the browser, public edge, or users’ systems.

```text
Public web / MCP / A2A
          |
      public edge
          | authenticated internal API
          v
Collaboration service
   |- Postgres: rooms, memberships, permissions, messages, requests, manifests
   |- S3-compatible store: files, previews, archives
   `- audit events: security and lifecycle history
          ^
          |
Dashboard / operator interface
```

The public edge remains isolated and receives no database or object-store credentials. Dashboard, public web, MCP, and A2A all use the same collaboration source of truth.

### 5.1 Relational entities

The structured schema must include at least:

- `users`
- `rooms`
- `room_memberships`
- `room_invitations`
- `room_files`
- `agents`
- `agent_room_attachments`
- `agent_permissions`
- `agent_access_requests`
- `messages`
- `session_logs`
- `room_uploads`
- `room_upload_parts` or equivalent upload manifest state
- `room_archives`
- `skills`
- `skill_versions`
- `skill_installations` or local-install audit records where needed
- `audit_events`

All mutating operations must be idempotent and auditable. Foreign-key relationships must prevent access records from surviving deletion of their parent room, user, or agent.

## 6. S3-compatible storage schema

The implementation must use standard S3-compatible APIs so RustFS can run locally, on a different server, or be replaced by another compatible provider.

Use separate buckets for live data and archives, with environment and room prefixes:

```text
herdr-collab-live
herdr-collab-archive
```

Live objects:

```text
env/{environment}/rooms/{room_id}/files/{file_id}/blob
env/{environment}/rooms/{room_id}/files/{file_id}/preview/{variant}
env/{environment}/rooms/{room_id}/uploads/{upload_id}/...
```

Archive objects:

```text
env/{environment}/archives/{archive_id}/manifest.json
env/{environment}/archives/{archive_id}/room.json
env/{environment}/archives/{archive_id}/members.ndjson
env/{environment}/archives/{archive_id}/messages.ndjson
env/{environment}/archives/{archive_id}/tasks.ndjson
env/{environment}/archives/{archive_id}/sessions/{session_id}.ndjson
env/{environment}/archives/{archive_id}/files/{file_id}/metadata.json
env/{environment}/archives/{archive_id}/files/{file_id}/blob
```

Object keys use opaque IDs, never room names or email addresses. The archive manifest includes original room ID, archive ID, schema version, timestamps, object list, sizes, hashes, and encryption/integrity metadata.

One bucket per room is explicitly out of scope: prefixes provide organization and policy boundaries without bucket-management overhead.

## 7. Room deletion and restoration

Room deletion is a two-phase operation:

1. Transition the room to `archiving` and revoke normal access.
2. Export room metadata, membership and permission history, messages, tasks, agent attachments and grants, session logs, file metadata, file objects, and preview metadata/assets. The archive must contain only identities and content that still exist at archive time.
3. Write the archive to the configured S3-compatible archive bucket.
4. Verify the manifest and every object checksum.
5. Mark the archive `verified`.
6. Delete live Postgres room records and live room objects.
7. Retain the archive catalog and audit event.

A failed archive leaves the live room intact. The dashboard shows progress and prevents duplicate deletion attempts. Room deletion is not complete before verification.

Restore always creates a new room ID. The archived original room ID is retained only as provenance. Restored data is copied into new live prefixes and registered with new relational IDs as required.

If a user is deleted after a room archive exists, the deletion workflow must locate all affected archive manifests and remove or redact that user's authored messages, session-log content, identity attribution, agent records, and user-owned metadata from the archive before considering deletion complete. Room-owned file bytes remain, but uploader attribution is removed or anonymized. Archive manifests and checksums are rewritten and reverified after this scrub.

## 8. High-volume direct uploads

MCP through a registered harness is the preferred high-volume interface. The harness, not Herdr, sees the local filesystem.

Example intent:

```text
Upload /data/test/**/*.cdf to room abc123 and show a dry run first.
```

The agent:

1. Expands the pattern locally and reports matched files, count, total size, and estimated transfer.
2. Checks room upload permission, or offers to request it from the room owner.
3. Creates a short-lived upload grant scoped to the room and upload ID.
4. Uses a standard S3 client/SDK with multipart, parallelism, retries, checksums, and resume.
5. Reports aggregated progress rather than one chat event per file.
6. Submits a manifest and finalizes the upload.
7. Herdr verifies objects and performs batched metadata registration.

The grant may use temporary access-key/secret/session-token credentials or presigned multipart URLs as a provider-compatible fallback. Credentials never appear in model-visible output. Incomplete multipart uploads are cleaned up automatically.

Upload states are `created`, `uploading`, `verifying`, `available`, and `failed`. Unfinalized objects are not visible as room files.

MCP operations may include:

```text
room_upload_preview(room_id, pattern)
room_upload_start(room_id, manifest, options)
room_upload_status(upload_id)
room_upload_cancel(upload_id)
room_upload_finalize(upload_id)
```

A convenience operation may combine these into `room_upload_files(room_id, pattern, confirm=true)`.

## 9. File browser and previews

Each room has a file browser with path navigation, search/filter, metadata, upload status, preview, and download. Preview access uses short-lived authorized URLs or range requests.

The preview registry supports:

- MSP mass-spectra libraries with metadata, spectrum selection, and interactive m/z-intensity plots;
- chromatograms through a room-authorized Flasheic-server adapter;
- PDF page rendering and text search;
- sanitized Markdown rendering with source toggle;
- formatted and collapsible JSON/XML/YAML views with raw download;
- metadata/download fallback for unsupported types.

Large or expensive previews are asynchronous, cached as room-owned derived assets, and included in room archives. Renderers run in a sandbox with parser size/time limits. Untrusted content must not execute scripts or active XML content.

## 10. Agent-assisted analysis and reusable skills

Users can refine analysis in chat, for example: “Plot the TIC for these files and compare the detected peaks.” The agent resolves authorized files, selects or generates tooling in the harness, runs it, and returns a chart, table, or report.

Generated analysis outputs retain provenance:

- source file IDs;
- code/tool and skill version;
- parameters;
- runtime environment;
- generated outputs;
- timestamps.

Generated tools follow this progression:

```text
Chat request -> generated analysis -> user refinement -> reusable skill
            -> scoped publication -> local agent download/cache
```

Skill packages contain `SKILL.md`, code/scripts, input/output schemas, supported file types, capabilities, tests or fixtures, dependency lock, provenance, version, and checksum/signature.

Publication scopes:

- private: creator’s agent only;
- room: members and agents authorized in that room;
- shared: organization-wide library.

Default publication authority:

- any approved member may create private skills;
- room owners may publish room skills;
- administrators or designated maintainers may publish shared skills.

Agents can search, download, verify, install, update, and run authorized skills locally. The library uses immutable versions and content-addressed packages. Skill execution cannot expand room or file access and must be isolated from the control plane.

## 11. Dashboard behavior

The dashboard separates global People management from room management.

### 11.1 Public/dashboard capability parity

Every capability available in the public collaboration view must also be
available in the Herdr dashboard through the shared collaboration service.
This includes room browsing and membership, chat, invitations and approvals,
agent management, agent attachment and access requests, file upload and
preview, direct-upload batch status, MCP/A2A connection setup, analysis
artifacts, skill discovery/download, and waiting/degraded states where they
apply. The dashboard may add operator-only controls, but it must not expose a
less capable or divergent collaboration model.

The dashboard and public view must use the same room IDs, message history,
file records, upload statuses, preview artifacts, skill versions, and access
decisions. Capability parity is an acceptance requirement, not a later UI
enhancement.

People actions:

- approve/deny pending users;
- inspect memberships, agents, sessions, and pending requests;
- delete person permanently.

Deletion confirmation states that all memberships, sessions, tokens, invitations, owned-agent records, and authored messages will be removed; room-owned files will remain. The person disappears from the active People list and only minimal audit metadata remains.

Room actions:

- invite and approve members;
- remove a member from this room;
- grant/revoke file-upload permission;
- attach/detach agents;
- approve/revoke agent access;
- rename or delete the room;
- inspect upload batches and archive/deletion status.

The UI must use “Remove from room” and “Delete person permanently” as separate labels and confirmation flows.

## 12. Acceptance criteria

- A public registrant sees a nonblank waiting room and cannot inspect rooms before approval.
- Every public collaboration capability is also available in the Herdr dashboard against the same source of truth.
- A room member can be removed without losing global approval or membership elsewhere.
- Global deletion invalidates sessions, tokens, invitations, and agent credentials, removes authored messages, and prevents resurrection through old credentials.
- Global deletion removes or anonymizes the user's authored content and identity attribution from live rooms and existing room archives while preserving room-owned file bytes.
- Room-owned files survive user deletion.
- Room deletion archives all required data and files, verifies checksums, then removes live data.
- Restore creates a new room ID with recoverable messages, sessions, permissions, agents, and files.
- RustFS or another S3-compatible service can be configured at a different endpoint without application-level storage changes.
- A registered harness can upload a large pattern-selected dataset directly with standard S3 tooling, resume after interruption, and finalize through MCP.
- Unverified uploads are hidden from the room file browser.
- Room owners can grant ongoing agent-task access and file-upload permission per member.
- Agents can attach to multiple rooms, and their owners can revoke individual room/user grants.
- File previews are room-authorized, sandboxed, and available for MSP, chromatogram, PDF, Markdown, JSON, XML, and YAML inputs.
- Users can refine an agent-generated analysis in chat and promote the result into a versioned private, room, or shared skill according to publication authority.

