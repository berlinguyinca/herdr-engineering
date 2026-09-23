# Dependency Lock, Lifecycle and Security

Maintain machine-readable lock: repo, resolved version/tag/commit, license, platforms, enablement, compatibility, provenance, verification date.

Install/reconcile must preserve user configuration and be idempotent.

Updates require explicit lock change, compatibility/security preflight, Powerpack-owned state snapshot, smoke tests and rollback on critical failure.

Treat plugins as third-party local code:
- inspect install hooks
- inventory processes/ports/permissions
- no arbitrary marketplace auto-install
- redact secrets
- no public debug/CDP endpoints
- detect unsafe listeners
- shared FS excludes credentials by default

Optional-plugin failure must not break HerdR.
