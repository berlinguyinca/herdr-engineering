# 0030 — Native Herdr Contracts and Adapter Layer

## Goal

Create a small HerdR Engineering adapter boundary over the actual Herdr API so higher phases do not depend on brittle terminal parsing or scattered CLI calls.

## Required capabilities

Normalize, without duplicating Herdr state:

- list/resolve machines and connectivity state;
- list/get workspaces, tabs, panes and sessions;
- agent integration state and observable lifecycle fields;
- send permitted terminal/session input;
- create/list/remove worktrees using native capability where appropriate;
- plugin capability discovery;
- event subscription/streaming when available;
- version/schema/capability negotiation;
- machine-targeted invocation;
- structured errors (`unreachable`, `permission`, `unsupported`, `timeout`, `conflict`, `invalid`).

## Design

- Treat Herdr session/worktree IDs as foreign/native IDs; do not mint competing canonical IDs unnecessarily.
- Expose capabilities explicitly so UI/features can degrade rather than assume all versions/plugins support all actions.
- Prefer direct socket/plugin APIs. CLI wrappers are acceptable only behind the adapter and must use structured output when supported.
- Correlate remote errors to the target machine; one failed host never changes another host to unknown.
- Add contract tests using representative local and saved-SSH machine fixtures/mocks plus at least one real smoke test.

## Exit gate

A thin test application can enumerate the fleet, sessions/worktrees and Pi integration state through one typed interface, target a specific saved machine, survive one unreachable machine, and contain no terminal-screen scraping for these capabilities.
