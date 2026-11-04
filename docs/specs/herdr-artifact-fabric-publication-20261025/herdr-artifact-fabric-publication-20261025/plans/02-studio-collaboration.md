# Work package 2 — native UI and agent links

- Reuse dashboard components to add artifact list, preview, details, metadata, version history, diff, source links and processing status. Do not create unrelated frontend architecture.
- Wire conversation composer upload and reference selection to same artifact registry; room and session views share adapter, permissions, event semantics.
- Bind existing graph nodes, mission state, GitHub links, agent handoffs and collaboration to artifact IDs/versions. Track comments and review requests with immutable version context.
- Implement PDF/image/MD/HTML/JSON/SVG safe previews; convert DOCX, presentation as supported adapters; show unsupported download fallback.
- Add security tests for malicious content in preview and ACL-filtered search; UI golden screenshots only after inspecting actual styles.

Done when: user can locate an artifact from global and contextual views, preview, comment, select versions, inspect provenance and open associated session/issues.
