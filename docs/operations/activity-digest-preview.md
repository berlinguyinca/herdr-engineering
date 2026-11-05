# Recorded activity digest preview

This foundation provides an internal pure Python preview, not automatic session
summaries or retention. It registers no endpoint, hook, scheduler or command.
`preview_activity()` defaults to `not_enrolled`; every result has
`preview_only=true` and `delivery_enabled=false`. It performs no SDK, model,
filesystem, network or persistence operations.

Callers supply independently trusted `DigestEnrollment`, `ProducerBinding`,
`SessionBinding`, `EventBinding` and optional `RevisionObservation` objects.
These are construction contracts, not payload attestation. Production binding
and enrollment wiring is deferred. Agent labels, harness names, cwd and payload
claims never establish repository, session, producer or completion authority.

`InspectorStore.activity_metadata(session_id, role="operator")` copies only
file path/operation and test run/status/count fields, stable sequence, revision
and explicit confidentiality gates under the existing Inspector lock. It never
renders full events or queries. The private operator role does not require a new
application login. Aggregate redacted/truncated flags from excluded prose do not
invalidate independently approved metadata; explicit sensitive/withheld does.
The bounded accessor cannot recover metadata evicted upstream.

The extractor accepts exact manifest-approved canonical repository-relative
paths, completed reported-test receipts with matching trusted run bindings, and
trusted observed full revision references. Manifest approval is authoritative;
lexical validation does not establish filesystem or symlink containment. Test
counts must be actual nonnegative bounded integers, excluding booleans and
floats. Missing counts remain absent. Reported terminal status and counts are
not independent proof of success. An observed revision is not session authorship.

The canonical digest includes at most eight paths, four completed receipts and
three observed revisions. Source and run references are hashed. No commands,
output, diffs, names, messages, reasoning or arbitrary payload URLs are copied.
The exact canonical JSON is at most 3,072 UTF-8 bytes. Overflow drops whole items,
removes unreferenced source hashes and records fixed numeric omission reasons.
A link alone cannot create activity. An explicitly operator-approved HTTP(S)
origin generates `/#/herd/session/<encoded-id>`; no hostname suffix implies
privacy, and no DNS lookup, request or sign-on occurs.

Raw Inspector revision is available for future cursoring but excluded from
semantic identity. Deduplication uses only kind-specific approved fields,
stable sequence and confidentiality gates. Conflicting metadata for one sequence
is rejected deterministically, without input-order or raw-revision arbitration.
Changes to excluded prose, MCP/personal events and unused fields do not affect
selection, omission counters, bytes or fingerprint when bindings and approved
metadata remain fixed. Selected metadata and trusted observed SHA updates do.

No delivery, automatic enrollment, raw transcript parsing, historical migration,
retention or forgetting is implemented. Manual engineering summary workflows
remain separate. Full conversation auto-sync remains off.
