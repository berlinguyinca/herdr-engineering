# Optional session-summary retention

This is an implementation brief, not a claim that automatic retention is enabled.
The operator requested automatic summaries, with evidence or explicit consent for
shared durable facts, and personal memory kept separate.

## Reuse

Use `control_plane/agent_summaries.py` for bounded, redacted generation, source
citations, conversation identity, source freshness and private projection storage.
Use the canonical `herdr-memory summary` command for delivery into the existing
project summary bank. Do not introduce another model client, vector store, outbox
or transcript collector. The existing `/summary` recovery-report skill remains a
separate, explicitly requested read-only report; installing it does not enable
memory retention.

## Required boundary before enabling

Automatic delivery stays disabled until an authenticated human can enroll an
engineering session and its destination project. The current production shared
`open-listener` identity cannot establish that consent. A repository path, harness
name, agent-supplied source label, or default project is not personal-data consent.
Unknown, mixed, personal or changed conversation identities suppress delivery.
Enrollment alone is insufficient: a personal MCP lookup can contaminate an
otherwise engineering conversation without changing its identity. Require verified
domain provenance for every input, or sticky suppression after any personal or
unknown-context access. Unobserved tool activity is unknown, not engineering;
redaction and tool/source labels supplied by an agent cannot establish provenance.
Do not enable old archived capture hooks to bypass this requirement.

## Delivery slice

1. Persist opt-in against exact conversation identity, project and policy revision
   using existing authenticated configuration/approval facilities. Enrollment must
   not grant global or infrastructure scope. Disabling it prevents new enqueue
   operations. Already accepted durable queue entries retain their original
   authorization and can replay: the current CLI has no policy-revocation check.
   Do not claim revocation cancels those entries. If immediate delivery revocation
   is required, add a trusted replay-time policy check before enabling retention.
2. Consume only an acknowledged, ready summary projection with current source
   revision and matching enrolled identity. Do not pass transcript messages, tool
   payloads, hidden reasoning or a recovery report to the memory client.
3. Render a bounded summary from explicitly allowed summary fields. Preserve
   session ID, harness, source revision and citations as provenance. Reject secret
   patterns through the canonical client. Keep generated material labelled as
   generated and candidate; do not turn citations into verified-fact promotion.
4. Derive an idempotent document ID from project, conversation identity and source
   revision. Invoke the existing CLI with an argument array and bounded timeout.
   Retain its persisted/queued/rejected distinction and reuse its durable outbox.
5. Add completion adapters only where actual native events are supported. Pi has
   `agent_end`; Claude has lifecycle hooks. Verify Codex and OpenCode separately;
   unsupported completion must remain an explicit fallback in capabilities.
6. Preserve unrelated harness hooks, MCP permissions, model settings and user
   configuration. Installation alone must not enroll sessions or enable retention.

## Acceptance

Test disabled behavior, missing enrollment, personal/unknown identity, conversation
switch during generation and delivery, stale source, duplicate completion,
secrets, backend outage/replay and permission rejection. Verify actual payloads in
real sessions of each claimed harness: only the bounded summary may cross the
memory boundary. A REST loop with four harness labels is insufficient.

## Evidence on 2026-11-05

Existing session-summary installer/history and agent-summary tests: 140 passed.
This establishes the reusable baseline, not completion of retention adapters.
Production authentication and per-session enrollment remain prerequisites.
