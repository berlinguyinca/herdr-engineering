# Herdr Unified Hindsight Memory — One-Shot Implementation Package
Date: 2026-11-02
Status: source-audited design and implementation contract; **not deployed**.

## Goal
One shared, securely scoped, evidence-backed Hindsight memory across Pi Engineering, Claude Code, Codex, OpenCode, and future Herdr-managed harnesses on multiple hosts, accessible from the existing Herdr Dashboard. Reuse and extend existing code rather than duplicate it.

## Audit confidence / constraints
Read-only GitHub connector review of default-branch sources and checked-in historical deployment evidence. `berlinguyinca/herdr-memory` was referenced by Herdr code and prior audit but could not be fetched using the available GitHub identity (404); its current implementation, history, endpoints, and deployment are **unverified**. A live 2026-11-02 host or container check was not possible. Historical October 28 smoke results are not proof of present deployment health. GitHub search covers default branches and can miss files; implementation agent MUST rerun a complete working-tree/code audit, inspect PRs and deployed versions before editing.

## Files
- `audit/REUSE_MATRIX.md`: evidence and reuse ownership
- `audit/SOURCES.md`: inspected sources, gaps and verification commands
- `spec/ARCHITECTURE.md`: modules, contracts, scope boundaries and data flows
- `spec/DASHBOARD_UX.md`: detailed user experience
- `spec/IMPLEMENTATION_PLAN.md`: sequential independently testable phases
- `tests/ACCEPTANCE.md`: test matrix and release gates
- `operations/MIGRATION_ROLLOUT.md`: migration, canary, rollback and safety
- `ONE_SHOT_PROMPT.md`: full prompt to execute from a coding harness

## Non-negotiables
Do not implement a new memory engine, vector DB, event bus, scheduler, agent runtime, graph source of truth, or separate dashboard. No personal assistant memory cross-contamination. No removal of existing memory until migration parity and backups verified. Never present mocks as live evidence. No production deployment or credentials changes without explicit permission. Preserve all pre-existing user modifications, active sessions and host constraints.
