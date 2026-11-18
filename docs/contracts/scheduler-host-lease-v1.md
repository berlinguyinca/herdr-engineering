# Host-bound scheduler lease renewal

`POST /api/v1/fleet/runtime/leases/heartbeat` is a narrowly scoped callback
for a trusted host supervisor maintaining an already-claimed execution.
It reuses central fleet `X-Host-Token` authentication and scheduler lease
serialization. An operator API key does not substitute for the host credential.

## Request and binding

The JSON object contains `lease_id`, a positive integer `generation`, and the
private lease `token`. Keep both credentials outside the agent sandbox,
transcripts, task input, and terminal output. Do not copy host credentials
between devices.

The server derives host UID from that authenticated host's persisted resource
observation. Caller-supplied host, owner, or TTL fields confer no authority.
Within admission serialization it checks the lease, committed reservation,
request identity/context, execution claim, host UID, generation, and token
binding. Inventory-only host metadata is insufficient. Released, unclaimed,
expired, fenced, foreign, or mismatched executions cannot renew.

Renewal uses the server's 90-second TTL, including when new admission is disabled
for rollback. The callback cannot submit an execution, claim work, release
capacity, confirm a process stopped, or authorize migration.

## Response

Successful responses contain only `lease_id`, `state`, `generation`, and
`expires_at`. Lease state `running` identifies an active lease, not proof that
a native process is ready or still executing. No lease token, request content,
or owner details are returned.

Malformed JSON/proof types return 400; missing or invalid host credentials
return 401; invalid lease/host authority returns a generic 403. Unusable host
configuration or required persistence returns 503 without private diagnostics.
An absent fleet configuration returns 404. The host must distinguish temporary
unavailability from revoked authority and must not invent a successful renewal.

## Integration status

Service and real HTTP tests cover binding, credentials, malformed generations,
stale/fenced leases, rollback maintenance, response minimization, and configuration
failure. This callback is a prerequisite for the existing helper supervisor's
lifecycle integration. Physical launch, readiness, process-tree stop evidence,
and family-specific checkpoint/fencing certification remain separate gates.
Keep migration disabled and retain uncertain execution capacity until verified
runtime reconciliation is available.
