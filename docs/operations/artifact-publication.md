# HerdR Artifact Registry and Publication Operations

## Deployment order

1. Build and deploy the current `herdr-engineering` image containing the
   artifact registry, migration files, renderer sandbox, and gateway modules.
2. Run the control-plane worker migrations through the existing deployment
   path. Migrations `0003_artifact_registry.sql` through
   `0006_artifact_acl.sql` are additive.
3. Confirm the worker reaches healthy state and the web process reports the
   RustFS and PostgreSQL backends as configured.
4. Keep public exposure disabled until the isolated gateway health check and
   human approval path are verified.

## Private-only baseline

Private deployments require no exposure provider. The artifact origin bucket
must remain private. The dashboard/control plane stays on the existing private
fabric/Tailscale boundary. The public gateway is absent or loopback-only, and
publication requests remain pending without an approved provider.

## Public publication safety

- Agents can request publication but cannot approve it.
- Approval pins an immutable artifact version and records the approver.
- Public delivery is served only through the delivery-only gateway; it never
  receives control-plane cookies, PostgreSQL access, S3 credentials, or an
  arbitrary origin URL.
- Revoke changes gateway policy before provider cleanup. Provider cleanup is
  best effort and must not keep delivery alive.
- HTML/SVG output requires an isolated origin and restrictive CSP. Risky file
  types use download/content-disposition behavior rather than inline execution.

## Tailscale Funnel adapter

The adapter is disabled by default. Enabling it is an operator-controlled
deployment decision and must be tested in an isolated tailnet first. The
adapter accepts only approved loopback targets and exact provider commands; it
does not authorize resources or determine publication state.

## Rollback

- Stop accepting new publication requests.
- Disable the provider adapter or activate the gateway global pause.
- Revoke active publications through the control-plane API.
- Roll back the web/worker image while preserving PostgreSQL and object-store
  data. The additive migrations are forward-compatible; do not drop tables as
  part of application rollback.
- Re-run the private-only health and artifact download checks.

## Required release evidence

- PostgreSQL 16 migration rehearsal through `0006`.
- RustFS/MinIO-compatible temporary bucket write/read/delete probe.
- Authenticated artifact create/version/materialize and stale-CAS conflict.
- Review approval followed by pinned public delivery and revoke denial.
- Host A → Host B checkout/edit → Host C review evidence with source/output
  hashes, actor, host, session, and mission provenance.

## Fast source validation

When a full dependency rebuild is unavailable, use the checked-in
`deploy/fabric-stack/Dockerfile.validation` with an existing HerdR runtime
image. It overlays current Python/frontend/migrations without changing any
production tag and is suitable for import/CLI smoke checks. A full release
still requires the normal deployment image build and dependency installation.
