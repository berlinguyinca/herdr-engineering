# Herdr System Intelligence — implementation specification v2
Status: design proposal; repository audit required before implementation. Scope: Herdr dashboard, Pi Engineering/Herdr orchestration, Libre WebUI Artifact Studio, InferWeave and all authorized repositories and hosts.

## Outcome
One continuously maintained, evidence-backed system graph covering source, execution, infrastructure, requests, missions, handoffs, and artifacts. No second orchestration service and no duplicate graph stores until an audit proves needed.

## Non-goals
Do not replace Git, OpenTelemetry, the existing event store, OpenViking, or the existing artifact platform. Do not use an LLM as the source of truth for runtime state. Do not assume Archify has features not verified against its pinned upstream revision.

## Delivery
Ship vertical slices with working screenshots, automated tests, performance budgets, and migration/rollback plans. Each feature must expose progress and an evidence-backed ETA, not fictional estimates.
