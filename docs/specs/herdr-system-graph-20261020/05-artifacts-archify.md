# Artifact Studio + Archify

Adapter contract: input = graph snapshot + diagram intent + scope + filters + provenance; output = typed validated diagram specification, deterministic layout metadata, HTML/SVG/PNG when supported, manifest, version and source references. Pin upstream revision, license notices and dependencies; sandbox generated HTML, enforce CSP, sanitize links, and block untrusted scripts.

Diagram families: architecture, dependency, sequence, dataflow, class, ERD, timeline, waterfall. Confirm actual upstream support before promising each format. Preserve editable source spec and reproducible rendering; artifact versions link to graph snapshot IDs.

CLI and Pi slash commands only for authoring: `/artifact architecture`, `/artifact repo-map`, `/artifact mission`, `/artifact request-flow`, `/artifact impact`. Web viewer is read-only with permitted comments/annotations/sharing, including restricted authenticated guests and optionally public read-only links under policy.

> **DEVIATION recorded (2026-10-24, see 09-decisions-and-open-questions.md):** the Archify/WebUI/artifact-surface deliverables above are **re-scoped to OPEN**. Upstream Archify was verified and formally dropped (not vendored); the deterministic safe renderer (`graph_artifact.py`) closes only the P5 deterministic/security gate. WebUI rendering, click-through source links, sharing/annotation and the per-family `/artifact` commands remain unimplemented.

Do not render entire graph as static HTML on every event. Live canvas consumes graph diffs; Archify creates on-demand or scheduled durable snapshots for reviews, PRs, reports and sharing.
