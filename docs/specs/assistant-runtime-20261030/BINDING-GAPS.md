# Pending scoped Pi bindings

This is requirements coordination, not an enabled tool registry or additional runtime code.
The event lane owns secretary business behavior and private task context. Core owns the
authenticated, sandboxed launch boundary, immutable role binding, and future tool
registration/authorization.

The secretary must execute through actual Pi. Codex maintenance/coordination must not stand
in for the secretary or read personal sources to produce its answers. The tested core
runner is fixed to no tools and now digest-binds the reviewed Secretary role. Native Herdr
pane creation alone still does not provide this sandbox, tool registration or source
authorization.

Future bindings must provide narrowly authorized, read-only calendar availability and
email-obligation data. Existing location-history access is optional and must be discovered
and authorized if actually available; this requirement does not enable tracking or create
a history source. Availability should be derived from those sources and real provider
openings rather than requiring users to enumerate every possible time.

Provenance, hard versus inferred constraints, travel buffers, and protection for recurring
obligations belong to the event/secretary planning contract. User identity, data-source
grants, model selection and tool authority are supplied by the authenticated platform,
never by extracted model data. Business details and personal-source contents stay in the
event lane and authorized Pi context.

Browser observation/proposal support is not authenticated booking authority. Account access,
provider openings and booking must each have real capability implementations with the
necessary grants before the agent can claim it can act. No source or booking binding is
invented by a package definition, successful no-tools runtime proof, or prompt/skill load.

Until those capability boundaries are supplied, source-backed secretary work is accurately
blocked on tool binding. The clarification-only Pi launch has an explicit role binding,
approved model, empty tool allowlist, strict sandbox and private task context; it must not
bypass the guard by launching an unboxed host Pi wrapper.

## Delegation wiring status

Implemented behind the existing Personal Assistant, with no new authority:

- **Startup binding.** `personal_assistant.delegation.pi_bindings` lists operator-owned bindings, one per
  `(tenant_id, user_id)`, each with exactly `room_id`, `agent_id`, `api_url` and a `credential_file`
  (owner-only; never an inline token). `ControlPlaneServer` builds the resolver from it. Only an exact
  scope plus the `pi-secretary` target resolves; anything else, or an unreadable credential, resolves to
  nothing, so delegation stays unavailable rather than borrowing another user's binding. Startup refuses
  bindings without acknowledged assistant persistence and refuses combining them with a fixed adapter.
- **Task-stop receipt.** Cancelling a task only withdraws the logical task. `execution_stopped` is
  reported only once the host has reported every runtime of the bound agent failed or cancelled; a
  missing or still-live runtime is never read as stopped. The runtime is asked to stop only when no
  other open task of that agent would be failed by it.
- **Consumer boundary.** `DelegationConsumer` ignores records that carry a `role_ref` or `external_id`
  in dispatch, sweep, reconcile and cancel, so engineering handoffs cannot overwrite a live specialist
  delegation.

Still open: which existing assistant is the delegating parent (a trusted current-context decision, not
inferred here), the live root-assistant to Pi proof, and the production rollout/rollback.
