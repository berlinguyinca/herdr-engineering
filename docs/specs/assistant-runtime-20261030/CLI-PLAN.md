# Pi assistant activation CLI plan

Goal: expose owner-authenticated host discovery and Pi package activation controls without
accepting execution details or launching a runtime in the CLI process.

1. Coordinate the backend route and response contract for room hosts and package-agent
   create/inspect/status/cancel. Keep operator attestation and runner lifecycle server-owned.
2. Write failing real-server tests for host listing, create/replay, inspect/status/cancel,
   and versioned JSON output. Cover non-owner and agent denial plus backend rejection of
   missing, stale, unattested, digest-mismatched, or model-disallowed requests.
3. Add narrowly typed client methods and nested commands. Creation sends exactly
   `package_id`, `host_id`, `name`, `idempotency_key`, and optional explicit
   `selected_model`; it accepts no command, image, path, environment, grant, credential,
   user, or sandbox override.
4. Preserve existing `agents list/inspect`. Add package-runtime commands without changing
   room collaboration agent semantics. Do not add an operator or runner command surface.
5. Run runtime CLI, backend, existing assistant CLI, and legacy CLI tests plus Ruff,
   compilation, and diff checks. Document that queued status is not evidence of launch.

No host deployment, process launch, runtime-file edit, external task execution, package
mutation, event/browser prototype edit, dependency addition, or commit belongs to this lane.
