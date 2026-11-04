# Personal Assistant → Pi Secretary adapter

The existing `AssistantService` remains the only root assistant. It owns user/tenant authorization,
connector access, persistence, audit, idempotency and the delegation state machine. The injected
`PiSecretaryDelegationAdapter` receives only the sanitized `DelegationDispatch` and maps it to one
operator-configured Pi task target.

The root service resolves a binding by scoped `(tenant, user, target)` before producing the sanitized
dispatch. Each binding fixes `room_id`, `agent_id`, target `pi-secretary`, and the reviewed Secretary role
reference. The adapter sends the task, bounded authorized context and source references as a closed JSON
data envelope plus a deterministic delegation-derived idempotency key.
It never receives root credentials, connectors, grants, OAuth tokens, browser access, personal
sources or arbitrary model/runtime configuration. A role changes instructions, not authority.

Adapter methods are `submit(delegation)`, `poll(external_id)` and `cancel(external_id)`. Results map
the existing Pi states to queued/running/done/failed/cancelled and carry a bounded text result plus
the non-authoritative `pi-task://` trace reference. `AssistantService` persists before delivery,
retries queued submissions, maps terminal states to its existing delegation record and keeps all
cross-user checks on its side of the boundary. A logical task cancellation is reported as
`cancellation_requested`; the root record remains running until the transport supplies explicit
`execution_stopped` evidence. Restart without a scoped resolver leaves external delegations queued
rather than fabricating completion or cancellation.

Deployment must inject a transport already bound to the current owner's authenticated internal Pi
task service. The adapter does not create tokens or infer an owner from task text. The isolated
Secretary validation proved the immutable role/runtime mechanics, but was not the deployed root
assistant and received no private event or business context.
