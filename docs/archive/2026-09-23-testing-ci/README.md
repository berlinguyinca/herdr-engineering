# Source lineage — Super HerdR Tests + CI Workspace (2026-09-23)

Requirements folded into normative specs 0120 and 0130:

- make HerdR increasingly IDE-like across systems;
- display a clickable hierarchical test tree with running/passed/failed/skipped states;
- selecting a test reveals logs, stack/failure trace and exact source link;
- support pytest, Go, Rust, Java, Scala and extensible future adapters;
- native test runners remain authoritative;
- unify local/agent/CI evidence without conflating run identities;
- show Woodpecker/Pileated pipeline state, logs, queue and runners;
- correlate CI to session/worktree/commit;
- support a CI failure → Pi debugging handoff;
- keep execution in the native runner/CI provider, with HerdR as integration/observability surface.
