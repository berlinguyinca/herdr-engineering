# Existing Personal Assistant → Pi Secretary delegation

This slice completes the existing `AssistantService` delegation lifecycle. It does not
create another root assistant, auth stack, database, connector, OAuth flow or tool grant.
Tenant/user/assistant/run scope stays in the existing store and API policy.

## Dispatch boundary

`AssistantService` accepts an optional `DelegationRuntimeAdapter` with three methods:

- `submit(DelegationDispatch)` → external ID, queued/running state, artifact refs;
- `poll(external_id)` → queued/running/done/needs-context/failed/cancelled state,
  bounded result text, checkpoint and artifact refs;
- `cancel(external_id)` → terminal cancellation receipt.

The sanitized dispatch contains delegation/assistant/run IDs, task text, bounded context,
source references, configured target and immutable role reference. It omits tenant/user
credentials, connector records, grants, OAuth data and root API credentials. The injected
`PiSecretaryDelegationAdapter` uses only delegation ID, target, task, bounded authorized
context and source references to create one scoped task for a preconfigured registered Pi
Secretary agent. Those fields are encoded as a closed data envelope, not authority.

A fixed adapter requires an explicit `(tenant_id, user_id)` binding scope. Multi-user services
use a server-side resolver that selects an adapter from authenticated scope; tenant/user IDs are
not passed onward to the child. A binding for one owner can never receive another owner's task.

## Target and authority

Targets come from `personal_assistant.delegation.targets`. The default remains legacy `pi`.
`pi-secretary` is enabled only by configuration and is mapped to the reviewed immutable
Secretary role. Neither API callers nor model output may provide a role, agent ID, room,
command, path, model override or capability grant.

The existing `delegate` tool remains sensitive and passes through the current grant and
approval gate. Its run ID and idempotency key are derived from the active server-side run.
Direct user API requests have no run ID; a submitted `run_id` field is ignored.

## Persistence and lifecycle

The root delegation is persisted before adapter submission. A deterministic delegation
idempotency key prevents duplicate external work; transport failure leaves the request queued
with a bounded `runtime_unavailable` marker for retry. Store snapshots include external ID,
context/source provenance, configured role, checkpoint, result/error, cancellation request,
terminal timestamp and revision.

External adapters require an acknowledged existing store backend (atomic file snapshot or
required Postgres write). An in-memory store or failed durable write refuses dispatch. The
idempotency reservation occurs under the existing store lock and is durably acknowledged before
`submit`, so concurrent/restarted callers converge on one delegation and one external task.

The existing server sweep and scoped delegation reads call adapter maintenance. Status maps to
`queued`, `running`, `succeeded`, `needs_context`, `failed` or `cancelled`. Completion is never
fabricated: only an adapter receipt advances or terminates externally submitted work. Artifacts
remain references in the existing delegation record. Cancellation is task-scoped; when an
external task exists, the root records a request, recovers a possibly lost submit receipt, and
waits for verified process-stop evidence before accepting `cancelled`. Adapter-bound artifacts
come only from runtime receipts; the owner artifact route cannot forge them.

## Current boundary

The Secretary runs with the already verified strict no-tools Pi profile. It receives only task
text plus bounded root-authorized context/provenance and can return `needs_context` when facts are missing. Calendar,
email, location, browser and booking access remain with the root assistant/connectors and are not
enabled for the child. Email or page content remains untrusted data if later passed as bounded
context.
