# Decisions and open checks

Confirmed design intentions:
- Private by default; public publication is explicit human-approved.
- RustFS-compatible S3 for blobs, PostgreSQL for indexed metadata, existing Herdr control plane and scheduler reused.
- Tailscale is used in initial fabric, but publication/transport provider-neutral. No public provider required.
- Skills and CLI perform authoring; dashboard handles preview, review, linking, sharing.
- Anthropic skill workflow distilled only under vetted applicable licenses. No upstream Archify dependency.
- The first implementation checkpoint is additive: new registry tables and bounded RustFS streaming are introduced before changing the legacy upload API.
- Anthropic's current `web-artifacts-builder` skill materials are not vendorable under their `LICENSE.txt`; Herdr will implement a native skill from behavioral concepts only.

To resolve from checked-out code and operator config:
- Exact deployment of RustFS vs MinIO and availability of public gateway host/domain.
- Current auth, external guest identity and RBAC architecture.
- Existing schema migrations and artifact APIs in active branch.
- Storage tier public snapshot replication vs isolated read-only gateway for MVP.
- Tailscale Funnel restrictions on actual installation; whether Funnel is allowed on controlled gateway host and test tailnet.
- Existing renderer libraries and resource scheduler capabilities.
- Public endpoint domain/cert policy; no hardcoding of owner's domain or Mac SSH hostname.
- Target PostgreSQL version must be used to validate the deferrable current-version foreign key in migration `0008_artifact_registry.sql` before deployment.
