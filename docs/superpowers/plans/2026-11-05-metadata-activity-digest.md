# Metadata activity digest foundation and deferred integration

## Implemented preview slice

Reuse the Inspector journal with an operator-only allowlist accessor and a typed,
pure extractor in `control_plane/activity_digest.py`. Independent caller-owned
bindings authorize finite metadata; payload labels grant nothing. Return bounded
canonical JSON and its SHA-256 without side effects. Keep enrollment default off
and delivery disabled. Tests cover privacy invariance, binding/path rejection,
reported counts, revision observations, deterministic conflicts, exact UTF-8
budget and Inspector projection. See the operational preview contract in
`docs/operations/activity-digest-preview.md`.

## Required review before production integration

1. Independently review the frozen foundation against the eight-section Herschel
   audit and property tests. No push or data enabling in this slice.
2. Establish producer/session/context bindings from authenticated existing journal
   ingestion, not source labels or cwd. Specify unsupported harnesses explicitly;
   metadata-poor or standalone sessions skip. No transcript plugin installation.
3. Design revision-aware incremental reads, explicit enrollment watermarks and
   closed activity windows. Cursor freshness is separate from semantic digest
   identity. Do not import history or repeatedly summarize whole sessions.
4. Reuse existing private projection storage for cursor/delivery bookkeeping;
   introduce no second payload outbox, gateway, installer or store. Preserve
   native harness provenance separately from the current web service writer's
   `codex` profile slot.
5. Only after review, deliver candidate project summaries through the canonical
   CLI summary bank. No personal/global/infra writes, fact promotion or claims
   of independent evidence. Explicit opt-out and failure tests precede activation.
6. Resolve SDK replay revocation first: current accepted queued writes can replay
   after opt-out. Cancellation of pending POSTs is not deletion of accepted data.
   Track logical delivery IDs/source windows independently of renderer versions
   and normalized server addresses in the same writer/profile/cache. Failed
   resolution or deletion remains unresolved, never reported forgotten.

No cursor, delivery, replay revocation or forget implementation is claimed here.
No dashboard authentication activation, deployment or production policy change
is part of this work.
