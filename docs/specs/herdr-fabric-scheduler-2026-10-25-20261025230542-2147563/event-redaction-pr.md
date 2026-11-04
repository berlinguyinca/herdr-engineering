Per-entity event responses bypassed the redactor used by activity and SSE, allowing
viewer credentials to retrieve secret fields written directly to the event store.
Pagination also returned raw events, and repeated Python object references could
bypass the shared redaction traversal.

Redact both paginated and unpaginated event responses and normalize JSON-encoded
durable payloads before field redaction. Memoize sanitized containers, mask cycles,
and scan repeated strings. Preserve stored records, response metadata, role checks,
and the existing plain-key allowlist.

Promote the strict expected-failure leak test into a normal regression, covering
dict/JSON payloads, both pagination paths, shared containers, repeated bearer
strings, cycles, and independent non-mutation baselines.

Validation: 35 upstream-based security/shared-reference tests passed, plus scoped
independent review with no remaining findings. Targeted Ruff and diff checks
passed. The shared integration checkout also passed 68 focused security/native/
recovery checks. Related scheduler acceptance remains tracked in #290.
