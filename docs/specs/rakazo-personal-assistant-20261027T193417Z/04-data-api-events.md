# 04 — Data model / API / events (proposed contracts; reconcile before implementation)

## Canonical entities
Assistant {assistant_id,user_id,tenant_id,name,status,persona,model_policy_id,memory_policy_id,created_at,updated_at,version}
AssistantThread {thread_id,assistant_id,user_id,title,created_at,last_activity_at,retention_policy}
AssistantMessage {message_id,thread_id,role,content_ref,created_at,provenance,visibility}
AssistantRun {run_id,thread_id,task_id,step_cursor,state,initiator,placement_id,model_route,created_at,updated_at,version}
AssistantAction {action_id,run_id,tool_id,scope,request_hash,approval_id,idempotency_key,status,evidence_ref,timestamps}
AssistantGrant {grant_id,subject_id,tool_id,scope,constraints,expires_at,revoked_at}
AssistantApproval {approval_id,action_id,actor_id,decision,expires_at,policy_snapshot,decision_at}
AssistantRoutine {routine_id,assistant_id,trigger_type,trigger_spec,timezone,quiet_hours,scope_grants,enabled,next_at}
AssistantMemoryMetadata {memory_id,namespace,user_id,source_ref,classification,created_at,updated_at,retention_at,revision}
AssistantNotification {notification_id,task_id,channel,delivery_state,dedupe_key}

All canonical tables/collections must use existing DB patterns, migrations, tenant isolation and stable IDs; no new DB server. Prefer linking canonical events/entities rather than copying them.

## Proposed routes under existing API prefix
GET/POST `/api/v1/assistants`; GET/PATCH/DELETE `/api/v1/assistants/{assistant_id}` (DELETE archival by default)
GET/POST `/api/v1/assistants/{id}/threads`; GET `/api/v1/assistant-threads/{thread_id}`; POST `/api/v1/assistant-threads/{thread_id}/messages` -> 202 with run_id; POST `/api/v1/assistant-runs/{run_id}/cancel`.
GET `/api/v1/assistant-runs/{run_id}`; GET `/api/v1/assistant-runs/{run_id}/events` (existing SSE bus where possible).
GET/PUT `/api/v1/assistants/{id}/permissions`; GET/POST `/api/v1/assistant-approvals`; POST `/api/v1/assistant-approvals/{id}/decision`.
GET/POST/PATCH `/api/v1/assistants/{id}/routines`; POST `/api/v1/assistant-routines/{routine_id}/trigger`.
GET/POST `/api/v1/assistants/{id}/memory/search`; PATCH/DELETE `/api/v1/assistant-memory/{memory_id}`; POST `/api/v1/assistants/{id}/memory/export`.
GET `/api/v1/assistants/{id}/tools`; GET `/api/v1/assistants/{id}/diagnostics`.

Require user-scoped RBAC and tenant checks on every route, optimistic concurrency via version/ETag, bounded pagination, filters, request IDs, structured errors (unauthorized, approval_needed, quota_exceeded, unavailable, retryable, conflict). API documentation + contract tests and client types.

## Proposed events
assistant.thread.created; assistant.run.{queued,started,checkpointed,completed,failed,cancelled}; assistant.tool.{requested,denied,completed,failed}; assistant.approval.{requested,approved,denied,expired}; assistant.routine.{triggered,skipped,completed}; assistant.memory.{stored,updated,deleted}; assistant.notification.{queued,delivered,failed}.
Events include event_id, timestamp, actor, assistant_id, run_id, tenant_id, correlation_id, schema_version, sanitized payload; no credentials, raw private memory or hidden prompt content in generic events.
