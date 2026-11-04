# Verifiable acceptance suite

## Cross-host artifact
A on beast publishes an editable web artifact. B on nibbler checks out and changes it. C on donbot reviews output; history shows source/output SHA, actor, host, exact session and mission; dashboard previews both versions. Creator process and origin host can disappear; artifact remains accessible from RustFS. All new commands are tested in authenticated and denied cases.

## Concurrency/recovery
Concurrent base-version publish: one succeeds and other gets conflict, zero lost updates. Crash before metadata commit leaves no visible incomplete version; orphan reaper safe with pinned references. Restart control-plane worker and render host; idempotent retry. GC preserves published snapshots, pinned collections, and currently referenced blobs. File size streaming tests prove bounded memory.

## Format fidelity
PDF preview and page count, images, SVG sanitization, MD rendered/raw, HTML isolated, JSON tree, DOCX converted preview, unknown binary as download. Source edit and regenerate works at least web+Markdown in first vertical slice; later adapters add documents/presentations/charts with profile-specific golden fixtures. Snapshot screenshot and accessibility regression tests.

## Sharing
Private installation works with zero exposure providers. Human approval required for public exposure, including agent-origin requests. Pinned version public link never changes on new version. Revocation and expiry deny via gateway even if provider route remains. Public edge cannot reach control plane, RustFS origin, arbitrary RFC1918 IP or other private artifacts. No cookie sharing, no public iframe breakout, no access to unauthorized attachments in collab.

## Providers
Fake adapter contract tests: caps/validate/expose/status/revoke/reconcile, retries, drift, failed setup cleanup, host disconnect, service ID resolution, provider identity, portal race conditions. Tailscale adapter real integration only in isolated test tailnet with explicit operator authorization; no production changes in automated tests. No false claim of active route without observed provider+gateway health. Other adapters conform to same suite.

## Observability
Metrics: publish count/duration/failures, bytes stored/transferred, render failures, preview load, search latency, active/expiring publications, edge denies/rate limits, provider drift, revoked token attempts. Tracing joins publication ID, artifact version, agent session, build job ID; strip secrets and private content from logs. Alert on orphan provider routes and stale gateway policy.

## Release guardrails
Migrations forward/back test, fixture backfill from existing artifacts, compatibility of current control plane API, no disruption to live sessions. Feature flag for authoring, separately gated public exposure. Rehearse global kill switch, rollback preserving data and release notes.
