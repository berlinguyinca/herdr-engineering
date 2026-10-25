# Decisions and unresolved dependencies

Decisions: Herdr owns dashboard; authoritative mission services own mission mutations; existing telemetry/event stores remain authoritative; graph is derived projection; Artifact Studio owns persisted visual artifacts; Archify is a candidate renderer; source provenance and ACL are mandatory; offline reconciliation and historical replay are first-class.

Verified (follow-up, 2026-10-24) against pinned upstream `tt-a1i/archify`: MIT, active (88k+ stars, v3.2.0), a deterministic JSON-IR→HTML renderer driven by an AI agent via `node bin/archify.mjs finalize <type> <candidate.json> <output.html>`, covering architecture/workflow/sequence/dataflow/lifecycle/erd/class/tree/timeline/waterfall. **Decision: NOT vendored.** It is a heavy Node ≥18 agent-skill package (per-family renderers, sidecar/delivery/browser-check machinery) driven by an AI agent, and the spec forbids parallel/duplicate implementations. The deterministic safe renderer (`graph_artifact.py`) closes only the P5 deterministic/security gate (deterministic snapshot + CSP + sanitization).

**DEVIATION from spec P5 (recorded, 2026-10-24):** the P5 WebUI/artifact-surface deliverables (validated snapshots in Libre WebUI, click-through source links, sharing/annotation, per-family `/artifact` slash commands) are **re-scoped to OPEN** and would need Archify (or an equivalent interactive canvas) if implemented. This is an explicit, acknowledged deviation from the original P5 scope; it is not claimed as complete.

Must verify in P0: actual Herdr/Pi repo locations and permissions; current Herdr dashboard implementation; event transport/store and mission schema; Libre WebUI artifact code location; trace propagation between Pi and InferWeave; cross-host identity; GitHub webhook availability; existing graph libraries; Archify license and API. Do not assert these integrations already exist.

Potential future extensions: service ownership inference, anomaly detection for cyclic handoffs, bottleneck analysis, impact forecasting, code-aware review visualization, dependency drift warnings. Keep speculative ML inference labeled, gated and separate from observed topology.
