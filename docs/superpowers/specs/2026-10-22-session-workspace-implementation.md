# Session workspace implementation and operating limits

The session detail page now defaults to a resizable conversation/activity workspace. Advanced inspection remains available in a disclosure and subscribes only while open. Collaboration shares the composer and safe Markdown renderer.

## Implemented

- Chronological public conversation; commands, outputs and changes appear separately with agent, search, type and error filters.
- Collapsed activity, explicit long-body expansion, full retained output expansion and stable open details during updates.
- Mobile pane tabs, keyboard/pointer resize, reader-position preservation, new-message jump and accessible completion.
- Shared multiline composer with session-scoped drafts, explicit delivery feedback and operation identities retained across ambiguous failures/navigation. Non-localhost HTTP does not require `crypto.randomUUID`.
- File references from observed file activity, helper references, and explicit GitHub issue/PR/commit references. A reference inserts text for the receiving agent; it does not imply the file was uploaded or another agent received a task. Collaboration retains its existing leading `@agent` task semantics.
- Helpers use recorded parent relationships or the operator's explicit Collaboration instance links. Collaboration cards and flow details link back to known sessions; session links open the corresponding Collaboration agent card.
- Native Codex/Pi/Claude transcript normalization excludes hidden reasoning and system/developer instructions, preserves analysis-channel tool calls, and publishes bounded, idempotent batches with acknowledged file offsets.
- Canonical encoded pane IDs ingest correctly. Redacted public messages survive configured durable-sink restarts; failed durable writes are not acknowledged. Existing retention limits apply after restoration.
- Reconnect resets the loaded history window explicitly so older pagination cannot jump over an interior gap.

## Runtime limits

The native prompt API accepts text only. Both active session and Collaboration connections therefore disable uploads and explain the missing capability. The shared composer has tested picker/drop/paste, previews, retry and removal through an upload adapter, but there is no enabled attachment delivery adapter. Attachments remain in the mounted composer through refresh; only draft text survives full navigation/reload.

Text completion currently means reference completion. Available runtime slash commands and predictive prose completion are not inferred or advertised.

The feed never selects a transcript by cwd or newest modification time. It needs a native agent-session ID/path or exact foreground-process evidence. The inspected `herdr:bender:default:w1Z%3ApF` pane had neither, so its recording status is explicitly unavailable until native identity is reported. Its prior history has not been recovered.

On 2026-10-22 the supported `herdr integration install codex` command installed the missing native Codex SessionStart hook on bender. Existing hooks were verified preserved; configuration was privately backed up. The hook's exact-identity reporting was verified against an isolated Unix test socket. No prompts were sent and no active session or service was restarted. SessionStart reports apply when a session starts/resumes; this installation does not retroactively relabel a running pane.

Public messages, tool activity/output, file diffs and usage are retained through the acknowledged durable sink when configured. Redaction, per-event bounds, age retention and per-session limits apply before and after restart. Physical retention of the append-only sink remains its existing deployment policy.

## Local operation

Worktree: `/home/wohlgemuth/IdeaProjects/herdr-session-workspace`, branch `feat/session-workspace`.

Deploy the control plane and host feed from the same merged revision so recording status, durability acknowledgements and runtime imports agree. The fleet runtime bundle includes both transcript modules and installs native Codex/Claude/Pi reporting hooks without restarting those agents. No database schema migration or added project dependency is required.

Validation uses the existing `npm test`, the feed's `npm test --prefix deploy/herdr-cp-feed-plugin`, Python control-plane tests, and Ruff. New browser tests exercise the real shared components and retain existing advanced-inspector timing coverage in an isolated harness with the real backend/client.

## Release hardening

Exact-ID lookup uses Codex UUIDv7 date buckets and resumable bounded traversal instead of abandoning established hosts with more than 2,048 log files. Native headers are bounded separately from message bodies. Linux Claude descriptors require matching queried PID, process start time, boot and PID namespace; Codex descendants require exact runtime/pane identity. Neither path joins by cwd or newest file.

Explicit Codex parent headers populate helper conversations, nested attribution and activity. Known transcript bindings survive pane exit, pane reuse and collector restart, allowing the reader to drain final output. Native event hooks hand off exact bindings even for short sessions that disappear before a snapshot poll. A legacy session that was never observed with exact identity cannot be backfilled safely. Claude/Pi helper associations are shown only when explicitly reported.

File references come from confirmed file-edit evidence; tool exit codes and usage deltas are normalized where the native format provides them. Missing evidence remains missing. Replay checkpoints and pending file-edit evidence are bounded private local state.
