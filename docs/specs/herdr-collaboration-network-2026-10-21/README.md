# Herdr Collaborative Agent Network — implementation package

Status: architecture and implementation specification, not implemented code.
Date: 2026-10-21

## Evidence / limitations
- Inspected public `herdrdev/herdr` master README and AGENTS.md. README says agent-to-agent prompting, agent status, persistent sessions, multiple machines, CLI/socket API and plugins already exist. AGENTS.md says runtime is moving toward server-owned endpoint APIs and backwards-compatible endpoint generation 1.
- The public upstream is not confirmed to be the user's editable Herdr/Pi Engineering fork. GitHub permissions showed read-only to public `herdrdev/herdr`. The user's private Pi Engineering repository could not be identified from connected access. Do not claim that the implementation has been audited or started.
- First task: find canonical editable repository and inventory actual existing components before code changes. No duplicate parallel orchestration, artifact or identity stack.

## Files
- `SPEC.md` — architecture, protocols, authorization, data model, API, UI and security.
- `IMPLEMENTATION_PLAN.md` — milestones, acceptance criteria and no-duplication audit.
- `THREAT_MODEL.md` — cross-boundary threats and gates.
- `START_PROMPT.md` — paste into coding agent in canonical repo.
