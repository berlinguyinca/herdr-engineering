# Herdr Project Intelligence & Autonomous Operations v1

Status: implementation-ready design proposal; **repository audit required before code changes**.

This bundle specifies one native Herdr project-intelligence service, connected to existing Pi Engineering, InferWeave, RustFS-backed artifact registry, OpenViking, CI/GitHub, Rakazo assistant and dashboard. The bundle makes no claim that unverified APIs or schemas already exist.

## Reading order
1. `00-AUDIT-AND-IMPLEMENTATION-RULES.md`
2. `01-ARCHITECTURE-AND-DATA-CONTRACTS.md`
3. `02-INGESTION-AND-INTELLIGENCE.md`
4. `03-AUTONOMY-AND-CI-REPAIR.md`
5. `04-UX-UI-MOBILE.md`
6. `05-SECURITY-OPERATIONS.md`
7. `06-TEST-PLAN-AND-ACCEPTANCE.md`
8. `07-PHASED-DELIVERY.md`
9. `START-PROMPT.md`

## Product invariants
- Project and investigation are first-class, distinct records; sources are evidence, not authority by default.
- No duplicate scheduler, runner, artifact registry, dashboard, auth, event bus, or memory platform.
- The intelligence layer owns links, normalized events and projections; source systems remain systems of record.
- Ingestion cannot autonomously grant permissions or authorize actions.
- Every material claim is attributable to immutable source spans or object identifiers.
- All writes are scoped, audited, idempotent; merge gates are deterministic and branch protections remain enforced.
- Artifact originals are preserved separately from regenerated summaries and derived analyses.
- Mobile/desktop/browser share one backend, with restricted device authority.
