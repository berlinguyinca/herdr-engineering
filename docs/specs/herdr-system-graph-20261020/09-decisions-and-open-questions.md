# Decisions and unresolved dependencies

Decisions: Herdr owns dashboard; authoritative mission services own mission mutations; existing telemetry/event stores remain authoritative; graph is derived projection; Artifact Studio owns persisted visual artifacts; Archify is a candidate renderer; source provenance and ACL are mandatory; offline reconciliation and historical replay are first-class.

Must verify in P0: actual Herdr/Pi repo locations and permissions; current Herdr dashboard implementation; event transport/store and mission schema; Libre WebUI artifact code location; trace propagation between Pi and InferWeave; cross-host identity; GitHub webhook availability; existing graph libraries; Archify license and API. Do not assert these integrations already exist.

Potential future extensions: service ownership inference, anomaly detection for cyclic handoffs, bottleneck analysis, impact forecasting, code-aware review visualization, dependency drift warnings. Keep speculative ML inference labeled, gated and separate from observed topology.
