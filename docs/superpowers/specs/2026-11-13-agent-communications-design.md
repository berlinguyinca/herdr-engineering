# Agent communication audit

Approved by the operator in the disk investigation conversation on 2026-11-13.

## Intent
Operators must see how agents communicate through HerdR, including direct native CLI prompts that currently bypass control-plane receipts. Capture sender/recipient, time, redacted text and truthful delivery status; link the conversation to both sessions. Delivery does not mean acknowledgement, completion or mission progress.

## Approaches
Requiring every caller to use the dashboard API leaves direct CLI calls invisible. Native delivery events would cover all callers but protocol 22 exposes no prompt event and HerdR is an external binary we must not fork. Use an installed integration shim around the native CLI, with a bounded local SQLite audit outbox and the existing authenticated ingestion/event persistence path. Other native commands pass through unchanged. Raw socket clients and harness-private subagent channels are explicitly not covered.

## Behavior
Capture queued before invoking native agent prompt and settle only when the native JSON response confirms delivery or refusal. Record stable native session identity from the native receipt where possible, plus host/pane/server routing and explicitly reported sender context; never infer the sender from the focused pane. Preserve native stdout, stderr and exit codes. Unknown outcomes stay unknown and are never automatically resent.

Redact before any audit persistence. Keep messages at most 16 KiB, with explicit truncation. Bound local storage to 10,000 records/64 MiB and 14 days; warn on audit admission failure while preserving native command behavior. Outbox replay posts observations only, never repeats delivery. Existing ingest credentials and viewer permissions remain unchanged. Host observations cannot mint authoritative server delivery receipts or change session lifecycle.

Direct observations and server session commands share operation IDs where control-plane transport invokes the shim. Dashboard presentation consolidates those IDs, preferring server receipts. Display messages with session links, reported sender/recipient identity, time and status; do not claim acknowledgement or completion without evidence. Preserve current retention for durable events.

## Verification
Use real SQLite, native CLI, HTTP server and browser interactions. Test literal argument handling, failure/unknown delivery, offline replay, replay idempotency, secret redaction, storage bounds, routing identity and dashboard escaping/links. GitHub Actions must run lint, typing, tests with 80% coverage and build. Ship branch/PR/review/checks/merge; deployment state must be reported honestly.
