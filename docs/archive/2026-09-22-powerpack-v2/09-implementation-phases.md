# Implementation Phases

## Phase 0 — Mandatory research/brainstorming
NO production code first.
- inspect current HerdR plugin/event/session/socket/persistence/UI APIs
- audit all candidate plugins and search for better alternatives
- compare mature shared-filesystem technologies for Linux/macOS/Tailscale
- decide journal persistence architecture
- identify overlaps/conflicts/stale assumptions
- produce:
  - docs/upstream-audit.md
  - docs/shared-filesystem-decision.md
  - docs/session-journal-design.md

## Phase 1 — Foundation
Current-format HerdR plugin/meta-package, lock, safe config ownership/reconcile, doctor skeleton.

## Phase 2 — Shared workspace
Integrate selected filesystem technology and existing file viewer; namespace, permissions, health, artifact paths.

## Phase 3 — Session journal
Existing event/timeline integration, semantic event schema, persistence, newest-first UI, CURRENT state, artifact refs.

## Phase 4 — Incremental summary
Started/Completed/Current/Next with event-driven token-efficient updates.

## Phase 5 — Curated upstream bundle
Verified browser/mobile/Plannotator/swarm/worktree/file-review/GitHub/PR/notification capabilities.

## Phase 6 — Cross-linking
Timeline to shared files/browser/Git/reviews.

## Phase 7 — Security/update/rollback
Supply chain, listeners, lifecycle.

## Phase 8 — Pi Engineering/AutoSpec
Semantic events/capability exposure without duplicate orchestration.

## Phase 9 — Ansible/multi-host validation
Linux/macOS/headless and failure/recovery.

## Phase 10 — Optional ecosystem
Only after core works.
