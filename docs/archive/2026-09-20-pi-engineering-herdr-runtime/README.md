# Source lineage — Pi Engineering ↔ Herdr runtime integration (2026-09-20)

This predecessor specification established the integration boundary now captured primarily by normative spec 0100.

Key decisions preserved:

- staged migration, not a rewrite;
- Herdr is an external dependency owning persistent process/session runtime concerns;
- Pi Engineering owns mission/DAG/policy/review/repair;
- all Herdr runtime access in Pi Engineering goes behind `AgentRuntime`;
- normalized worker state/events and structured `WorkerResult`;
- strict worktree isolation;
- artifact-first bounded worker communication and dynamic token + serialized-byte budgeting to prevent request-body/413 failures;
- Tern/InferWeave owns model/runtime/GPU routing; no static GPU assignments;
- OpenViking is semantic shared memory and PostgreSQL operational history;
- Pi-Web remains external; Pi Forge excluded;
- keep the legacy runtime until parity, recovery, canary and rollback pass.

Current normative authority: `../../specs/0100-pi-engineering-autospec-integration.md` plus this repository's master boundaries.
