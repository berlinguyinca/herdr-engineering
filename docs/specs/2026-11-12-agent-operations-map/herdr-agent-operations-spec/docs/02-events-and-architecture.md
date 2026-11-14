# Architecture and event contract

Use the **existing Herdr backend and plugin mechanisms**. No duplicate room bus, orchestration engine, task database, model router or monitoring stack. Inspect code first.

Event envelope (proposed, versioned): event_id, schema_version, source_adapter, provenance(observed|derived|inferred|unknown), occurred_at, received_at, host_id, workspace_id, room_id, task_id, agent_id, session_id, model_id, parent_session_id, handoff_id, correlation_id, event_type, status, sanitized_label, sanitized_metadata, sequence_or_cursor. Optional fields must stay optional; do not fabricate missing metrics.

Events: session lifecycle, model selected/switched, tool start/end/failure, context metric/compaction, message sent/delivered/acknowledged, delegation proposed/accepted/rejected, owner transfer, review requested/completed, artifact created/verified, tests started/completed, agent joined/left room, disconnect/reconnect, drift warning. Preserve distinction between communication, delegation, task ownership, contribution and model switching.

Transport: use existing authenticated SSE/WebSocket/stream if present. One collection pipeline for terminal+web. Deduplicate, order where guaranteed, resume by cursor, bounded queues, retention, graceful offline/degraded state. Rendering failures never block agents.

Privacy: metadata-only by default; no raw prompts, responses, credentials, environment, file contents or shell arguments in telemetry. Link to original room messages through existing permissions. Redact error strings and file paths. Enforce room/workspace authorization server-side. Cross-room metadata must not leak.

Context pressure: show measured numerator/denominator and source; unknown is unknown. Drift is derived/inferred and cannot be treated as proven policy violation. Replay events should reconstruct historical UI state deterministically.

Integration contracts: inspect herdr-handoff namespaced commands and metadata; herdr-artifacts provenance; herdr-powerpack lifecycle; Herdr Engineering orchestration; Pi hooks; Claude hooks; Codex structured session events. Record actual APIs and compatibility in discovery report.
