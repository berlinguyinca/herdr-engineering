# AssistantService Pi delegation validation

Validated 2026-10-31 in isolated worktree `herdr-assistant-delegation`, branch
`feat/assistant-delegation-pi`, based on deployed source commit
`daca260689a6633519d14e6344886f08150cb7d6`.

## Evidence

- Assistant service/API/intelligence/config/delegation/adapter regression selection:
  **195 passed**, one integration test deselected.
- A pre-existing unregistered `integration` pytest mark emits one warning; this slice
  does not add or change that marker.
- Ruff passed on changed service/API/adapter and new tests. Existing legacy assistant
  API import-order findings were checked with undefined-name selection and remain outside
  this diff.
- Python compilation and `git diff --check` passed.
- Independent code/security review approved with no substantive finding.

## Covered boundaries

- existing AssistantService/store/API only; no second root, auth, OAuth or database;
- fixed owner binding or server-side adapter resolver prevents cross-tenant dispatch;
- acknowledged durable reservation before external submit, including resolver changes;
- concurrent identical calls converge on one delegation/external idempotency key;
- sanitized dispatch excludes tenant/user identity, connectors, grants and credentials;
- exact reviewed Secretary role and bounded context/source provenance;
- serialized monotonic receipt processing and malformed-receipt terminal failure;
- cancellation stays nonterminal until Pi reports execution stopped;
- adapter-bound artifacts cannot be forged through the owner artifact route;
- direct API bodies cannot provide a run ID; model-tool run provenance is server-derived.
- fixed adapters are tenant/user bound; multi-user services resolve adapters from authenticated
  scope without sending that identity to the child;
- atomic acknowledged persistence precedes every external submit, including resolver changes;
- concurrent receipt processing is serialized and terminal state cannot regress;
- cancellation remains nonterminal until the Pi transport reports execution stopped;
- malformed adapter receipts fail terminally instead of retrying forever.

## Remaining live proof

The parent lane owns the real configured root-assistant → registered no-tools Pi Secretary
run. It must record the scoped delegation, actual Pi result/artifact, terminal status and
cancellation/scope teardown without reading private user sources in the build session.
