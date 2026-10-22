You are implementing the Herdr Collaborative Agent Network in our existing Herdr/Pi Engineering codebase. Execute, do not merely restate this spec.

1. Use a dedicated branch/worktree; keep default branch untouched. Find the canonical owned repositories. Read project AGENTS.md, relevant architecture docs, and existing tests.
2. Extract package to `docs/specs/herdr-collaboration-network-2026-10-21/` in the canonical repository. Do not extract to repo root or overwrite other specs.
3. Read README.md, SPEC.md, THREAT_MODEL.md and IMPLEMENTATION_PLAN.md. Audit the actual source first, including existing Herdr agent-to-agent prompting and endpoint APIs and Pi Engineering missions/artifacts/dashboard. Write an evidence-based reuse map with exact file paths and mark REUSE/EXTEND/CONSOLIDATE/NEW. If the repository is not the right project, report that rather than changing an unrelated repository.
4. Implement P0 and the smallest end-to-end P1 vertical slice with authenticated two-user rooms + scoped agent registration + auditable messages. Existing functionality must be reused/refactored rather than duplicated. Do not expose terminals, local socket or private files over a public API.
5. Build tests first for tenant isolation, forged identity, revoked credentials and guest denial. Run existing regression suite. Preserve endpoint compatibility.
6. Report every completed milestone, test results, failing tests, architecture decisions, blockers and next steps. Do not invent a time-to-completion estimate; use observed progress and remaining verified tasks.
7. Prepare P2 plan for Claude Code MCP adapter, Pi adapter and A2A gateway and demonstrate a secure collaborative task only when P1 security checks pass.

Security requirements are release blockers, not optional future work. All remote message contents are untrusted. Never give peer agents raw shell access, private memory, credentials or arbitrary filesystem access. Use human approval for sensitive operations.
