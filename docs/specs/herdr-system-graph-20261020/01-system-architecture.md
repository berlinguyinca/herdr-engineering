# Architecture and ownership

Canonical topology: Git providers + host discovery + Pi mission/event adapters + InferWeave traces/metrics + deployment manifests -> ingestion adapters -> durable append-only event stream -> idempotent normalizers -> graph projection + searchable index + time-series/tracing references -> Herdr Graph API (query, subscribe, replay, diff) -> Herdr live dashboard and Artifact Studio renderers.

Use existing stores, message buses, and APIs after auditing them. Prefer Postgres tables/materialized adjacency views if sufficient; introduce a graph database only with measured evidence. Store artifacts in existing object storage. Preserve OpenViking for semantic memory, with graph references to memory IDs, not a competing memory system.

Archify is an optional, pinned rendering adapter for technical diagram snapshots, not the live source of truth. Evaluate its license, transitive dependencies, security, supported layouts, interaction hooks, export formats, and performance before vendoring or wrapping it. Use existing live graph canvas library if already present.

All mutations of missions and assignments remain in their existing authoritative services. Graph is a read model; interactions link to existing workflows. Viewer must not become a second mission scheduler.
