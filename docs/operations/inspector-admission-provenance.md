# Private Inspector admission provenance

This change preserves attribution already available at server admission. It
adds no authentication, enrollment, producer grant, SDK integration, capture
switch, delivery, endpoint, database column or service. Existing private
open-operator operation remains supported without sign-in or forced keys.

## Admission path

`api.py:_principal()` supplies the server principal to `IngestService.ingest()`.
`normalize_envelope()` separates that principal from submitted `claimed_actor`.
After the existing acknowledged `record_event()` call succeeds, the v2 Inspector
path constructs an immutable `AdmissionContext` and passes it through the
keyword-only `admission` argument of `_dispatch()` and `InspectorStore.ingest()`.
No acknowledged sink means unknown attribution, even if a request principal is
available. Legacy v1 and direct/CI-local calls remain unknown. No historical
association is inferred from labels, timestamps, cwd or matching event IDs.

The private allowlist retains only actor kind, existing opaque credential
identifier and authentication method, admitted envelope event ID, session entity
ID, server receive time, source/source sequence and batch child index. No raw
API key, host token, request headers, claimed actor or arbitrary principal fields
are copied. Source and source sequence remain admitted client claims, not proof
of harness identity, repository ownership or completion.

- `open_listener` / `operator` / `open-listener` means attribution to the trusted
  private operator listener. It is not authenticated human or host identity.
- Existing `api_key` principals preserve only supported role and `key:<digest>`
  attribution. Credential identity does not prove a human, host, repository,
  native session or harness.
- Unsupported principal forms preserve unknown actor attribution. Route-specific
  fleet/deployment host tokens are not promoted to generic Inspector authority.

Each batch child receives its original index, including positions around rejected
or duplicate children. Duplicate children do not overwrite prior attribution.
Every live tool/test mutation replaces its admission context; an unattributed
later mutation writes unknown instead of inheriting a previous writer. Lifecycle
interruptions also replace attribution on affected tools. Context describes the
latest mutation, not authorship of every inherited field in an accumulated record.

## Persistence, replay and compatibility

The existing event serializer retains a fixed column set. Therefore the server
writes `{version: 2, admission: ...}` under the reserved `_inspector_context`
field in the existing private detail payload. Sanitization never copies that
field from a client event. Replay consumes it only from persisted private detail,
validates the exact allowlisted schema and session match, and does not expose it
through event rendering, queries, detail or SSE. Old rows, invalid/unsupported
wrappers and mismatched contexts replay with unknown attribution. Activity itself
is not discarded merely because attribution is missing or invalid.

Private Inspector snapshots advance to version 2; version-1 snapshots are refused
so the existing replay fallback can read old detail as unknown. The shared
checkpoint container remains version 1 with an Inspector-private schema marker
of 2. Old unmarked Inspector checkpoints are accepted with all attribution reset
to unknown. New marked checkpoints validate context strictly before replacing
state. Corrupt contexts refuse the checkpoint, allowing existing recovery to fall
back; no compatibility alias or historical backfill is introduced.

`admission_durable=true` asserts only that the original envelope passed the
existing acknowledged persistence path. It never asserts projection application,
projection durability, test success or completion. Existing required/best-effort
projection persistence semantics remain unchanged. If required detail persistence
fails, no activity mutation occurs; the already durable envelope remains consumed
and its duplicate retry does not reconstruct missing detail. If best-effort detail
persistence fails, live activity may exist while restart has only the envelope and
no activity to restore. This patch does not repair that pre-existing transaction
boundary or claim full restoration.

## Verification and remaining authority gaps

Synthetic tests cover payload actor/reserved-field forgery, typed context,
open-listener and key HTTP requests, mixed writers, batches/duplicates, admission
and projection write failures, actual event serialization, legacy replay,
checkpoint/snapshot validation and delta replay. Existing public Inspector and
ingestion response shapes remain unchanged; raw credentials are not retained in
the new private context.

No production enrollment, canonical repository/project authority, native context
epoch, completed-test producer binding or SCM observation authority is established
here. Those require separately reviewed mappings before metadata preview can
use real production records. No automatic summaries, delivery or retention is
enabled by preserving this admission context.
