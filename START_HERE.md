# START HERE

This bundle is the single implementation program for **HerdR Engineering / Super HerdR**.

## Canonical repository

Use:

`berlinguyinca/herdr-engineering`

The older planned `berlinguyinca/herdr-powerpack` repository is superseded before implementation. “Powerpack” remains the name of the curated plugin/distribution concept inside this repository.

## Required reading order

1. `README.md`
2. `docs/specs/0000-master-program.md`
3. `docs/decision-history.md`
4. `docs/architecture.md`
5. `docs/upstream-baseline-2026-09-23.md`
6. `SPEC_MANIFEST.yaml`
7. specs `0010` → `0180`, following dependencies and exit gates.

## Non-negotiable boundaries

- Herdr is external. Do not fork or reimplement it.
- Use current native Herdr multi-machine and Pi integration primitives first.
- HerdR Engineering is not an agent scheduler.
- Pi Engineering remains mission/orchestration authority.
- AutoSpec remains spec/issue workflow authority.
- Tern remains inference/GPU authority.
- Woodpecker/Pileated remains CI execution authority.
- Plannotator remains review authority.
- Pi-Web remains an external Pi-specific UI.
- Pi Forge is excluded.
- Never persist hidden chain-of-thought.
- Shared storage is not active source-code truth.
- Never expose dev/browser/debug services publicly by default.

## Implementation behavior

This is not a “write a plan and stop” bundle. The implementing agent should:

1. inspect the current repositories and installed Herdr version;
2. create reconciliation/audit documents required by specs 0010/0020;
3. update stale assumptions in ADRs;
4. implement one dependency-complete phase at a time;
5. run that phase's tests and acceptance gates;
6. commit a coherent checkpoint;
7. update `STATUS.md` and the manifest evidence fields;
8. continue until the entire acceptance matrix passes or a genuine external blocker is recorded.

Do not silently omit a difficult phase.
