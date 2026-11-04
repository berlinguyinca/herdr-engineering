# Work package 0 — repository audit and contract freeze (blocking)

- Read HEAD of herdr-engineering and pi-engineering; identify active branches/PRs touching artifact code, collab, graph UI and gateway; do not overwrite in-progress work.
- Trace existing API upload, RustFSClient, DB migrations, service discovery, auth/RBAC, credential management, scheduler, gateway/proxy, websocket channels, frontend routing/design system, tests, deploy config.
- Identify exact reusable functions and migration direction in `docs/specs/<bundle>/audit.md` with source links and tested assumptions. Determine real object backend (RustFS vs MinIO S3-compatible replacement) and deployment topology.
- Compare upstream Anthropic web-artifacts-builder and any local artifact skills. Record license and permissible reuse; design Herdr-native wrapper regardless.
- Confirm whether current architecture allows public data plane separate from web/control-plane process. If not, design separate restricted process/role without duplicating business logic.
- Freeze API, ACL precedence, publication state machine, Tailscale adapter capabilities, public domain strategy, and render sandbox contract. Add ADRs before implementation.

Done when: exact code touchpoints, dependencies, existing-feature compatibility and migration/test plan are documented; implementation can proceed without guesswork.
