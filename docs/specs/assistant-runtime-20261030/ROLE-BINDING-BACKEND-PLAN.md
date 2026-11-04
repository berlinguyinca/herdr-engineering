# Pi role-binding backend plan

Goal: require every immutable package procedure reference to be verified by the
attested host and the ready receipt, without accepting role content, paths or tools.

1. Add failing tests for host role summaries, required claim bindings, exact ready
   receipts, missing/wrong/extra roles, empty-reference compatibility and privacy.
2. Validate a bounded list of exact `{id, version, digest}` records in operator host
   attestations. Package role requirements remain server-derived from the immutable
   package manifest and never come from the activation request.
3. Require every package procedure reference to appear in the host registry before
   activation. Return only the package's required bindings in the private runner claim.
4. Accept `ready` only when its verified `role_bindings` exactly equal the required
   claim set. Expose that immutable metadata in the owner's launch receipt.
5. Run role, runtime, runner and collaboration regressions. Preserve the fixed no-tools,
   broker-only sandbox and existing `read`/`execute` room authority.

No API accepts prompt text, role bytes, host paths, tool grants, credentials, private
business context or model-control instructions. Packages with no procedure references
bind an empty role list and remain compatible.
