# ADR-0002 — Authorization rides the control plane; assistant data is user-scoped

Status: **accepted** (P1/P2 slice) · Date: 2026-10-27

## Context

The spec (D1, 05-security) requires: no separate login/auth stack; deny-by-default
tools; tenant/user isolation (A3, A4); sensitive actions need approvals (A2);
fail closed when approval state is unknown.

## Decision

1. **Auth stays on the control plane.** Requests reach `/api/v1/*` only after
   `_Handler._require("viewer"/"operator")` (existing API-key role model). The
   assistant adds no login.
2. **User scope.** Every assistant route reads `X-Herdr-User` / `X-Herdr-Tenant`
   headers (never the body). The dashboard operator defaults to `operator` /
   `default`. All store lookups are filtered by `(tenant_id, user_id)`.
3. **Deny-by-default tools.** `TOOL_CATALOG`; `request_tool_action` returns
   `denied` unless the assistant holds an unexpired, unrevoked grant for the
   tool+scope.
4. **Sensitive actions.** `TOOL_CATALOG[tool].sensitive` requires a `pending`
   approval unless the grant is `preauthorized`. A sensitive request raises
   `ApprovalRequired`; `decide_approval` completes/denies the linked action.
5. **Fail closed.** Expired or unknown approval state is treated as denied; a
   revoked grant blocks queued and future actions.
6. **Idempotency.** `request_tool_action` dedupes on `(idempotency_key,
   request_hash, run_id)`.

## Consequences

- Positive: strong, testable isolation (A2–A4) without a parallel auth stack.
- Negative: the control plane's key-role model does not yet map to per-user
  login; `X-Herdr-User` is a trusted internal header on the tailnet listener.
  A real user-identity mapping (OIDC) is deferred (risk R3).
