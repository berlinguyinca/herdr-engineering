# 0100 — Pi Engineering and AutoSpec Semantic Integration

## Goal

Make HerdR Engineering understand the engineering workflow semantically without taking control of it.

## Ownership

Pi Engineering remains mission/worker/DAG/policy/review/repair authority. AutoSpec remains spec/issue workflow authority. HerdR Engineering consumes typed lifecycle/state and offers navigation/actions back through documented interfaces.

## Pi Engineering integration requirements

Honor the staged Herdr runtime migration design already specified in Pi Engineering:

- Herdr is an external runtime dependency;
- Pi Engineering accesses runtime concerns behind its `AgentRuntime` abstraction;
- normalized worker states/events and structured `WorkerResult` are emitted;
- worktree ownership is explicit;
- artifact-first bounded communication prevents oversized 413-style request bodies;
- dynamic model/context metadata comes from Tern/provider capabilities rather than fixed context constants;
- legacy runtime stays available until parity/recovery/canary/rollback gates pass.

HerdR Engineering should consume these semantic events and expose deep links to the owning Pi session/worktree/artifacts.

## AutoSpec correlation

Correlate spec → issue → mission → worker/session → worktree → commit/PR/test/CI/review through stable identifiers. Do not infer issue ownership from branch names when an explicit identifier exists.

## Cross-repository changes

If Pi Engineering/AutoSpec lacks a needed event/endpoint, add the smallest compatible contract in that owning repository and pin the required version. Do not reproduce their orchestration logic inside HerdR Engineering.

## Exit gate

A real AutoSpec/Pi Engineering mission with multiple workers is represented correctly in HerdR: workers map to their native sessions/worktrees; mission/issue status comes from the owner; restarts/recovery do not create duplicate workers; and runtime migration/canary state remains visible but controlled by Pi Engineering.
