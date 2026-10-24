# Implementation phases and gates

P0 Inventory/audit: locate actual Herdr, Pi Engineering, Artifact Studio and InferWeave repos, read architecture, APIs, stores, event schemas, UI components and deployment; map reuse/replace/extend; run tests and establish baseline. STOP if key repos unavailable; request access instead of inventing integrations.

P1 Contract/prototype: schema registry, provenance/ACL rules, sample event ingestion, graph projection using existing storage, fixture-backed API and test harness. Gate: schema compatibility, duplicate/out-of-order replay and permissions.

P2 Mission slice: real mission/task/handoff events to live Herdr graph, node detail, blockers, progress and honest ETA. Gate: end-to-end test from event to UI and offline reconnect.

P3 Repository slice: discovery, dependency parsing, cross-repo map, Git webhook refresh and scheduled reconciliation. Gate: known fixture graph accuracy, rename handling, ACL tests.

P4 Request slice: distributed trace correlation across Herdr/Pi/InferWeave, sampled flow and waterfalls. Gate: real trace evidence, sampling/unknown-span indicators, queue latency verification.

P5 Artifacts: evaluate and pin Archify, add CLI slash commands, render validated snapshots in Libre WebUI with source links, sharing and annotation. Gate: deterministic snapshot and malicious HTML/security tests.

P6 History/scale/rollout: replay, diff, impact analysis, performance optimization, canary, documentation, operational runbook. Gate: SLO/load tests, failure recovery, rollback exercise.

For every phase publish status: features accepted/total, tests pass/fail, blockers, measured velocity, remaining work with confidence interval. Do not claim progress by counting generated files alone.
