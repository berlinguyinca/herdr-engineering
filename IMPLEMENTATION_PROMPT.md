# Paste-ready implementation prompt

You are in the parent directory where the canonical repository `herdr-engineering` should exist.

Implement the complete Super HerdR project from this bundle as the GitHub repository:

`berlinguyinca/herdr-engineering`

If the repository already exists, reconcile and extend it. Do not delete working behavior or reset history. If it does not exist, create a normal Git repository named `herdr-engineering` and copy this bundle into it.

## Read first

Read, in order:

1. `README.md`
2. `START_HERE.md`
3. `docs/specs/0000-master-program.md`
4. `docs/decision-history.md`
5. `docs/architecture.md`
6. `docs/upstream-baseline-2026-09-23.md`
7. `SPEC_MANIFEST.yaml`
8. every numbered spec `0010` through `0180`
9. archived predecessor specs under `docs/archive/` for provenance.

Treat the numbered current specs as normative when an archived document conflicts with them.

## First actions — do not guess

Before production implementation:

- inspect the actual current Herdr binary, docs, plugin API, CLI, socket schema and installed integrations;
- run/record `herdr --version`, `herdr api schema --json`, `herdr machine list --json`, `herdr plugin list --json`, and `herdr integration status` where available;
- inspect current upstream repositories and licenses for every dependency candidate;
- inspect the current Pi Engineering, AutoSpec and fleet/Ansible repositories used by this environment;
- create the reconciliation and compatibility artifacts required by specs 0010 and 0020;
- explicitly mark prior assumptions that are now superseded by native Herdr capabilities.

Do not build a custom remote-machine aggregation layer: current Herdr already has saved SSH machines and `--machine` API forwarding. Do not build a custom generic worktree engine when native Herdr/worktree APIs or an adopted plugin already supply the needed primitive.

## Hard boundaries

- **Herdr is external.** Integrate; do not fork/reimplement it.
- **HerdR Engineering** owns integration/distribution, activity UX, transparent dev services, tests/CI views, doctor/security/update/rollback and fleet packaging.
- **Pi Engineering** owns missions, workers/subagents, orchestration, engineering lifecycle and repair policy.
- **AutoSpec** owns specs/issues workflow.
- **Tern** (formerly InferWeave) owns model/runtime/GPU routing and queueing. All Pi LLM traffic in this environment goes through `https://llm.metabolomics.us`.
- **Woodpecker/Pileated** owns CI execution and runner queues; HerdR only observes/controls through supported APIs.
- **Plannotator** owns review semantics.
- **Pi-Web** is an external existing Pi UI. Link/embed/proxy it when useful; never rebuild it.
- **Pi Forge is excluded.**
- Never persist or expose hidden chain-of-thought.
- Never synchronize active source worktrees through the shared artifact filesystem.
- Never expose browser CDP, dev servers, web UI, test endpoints, or debug interfaces to the public Internet by default.

## Current fleet expectations

Validation targets are `fry`, `beast`, `bender`, and `macbook-m4`. Do not hard-code them into application logic.

Use Ansible to ensure the supported fleet has:

- Herdr
- Pi configured for `https://llm.metabolomics.us`
- `berlinguyinca/pi-engineering`
- `berlinguyinca/autospec`
- `vim`, `mc`, `btop`
- required audited HerdR Engineering dependencies/plugins.

Preserve existing configuration and credentials. Secrets never belong in Git.

## Implementation method

Follow `SPEC_MANIFEST.yaml` dependency order. For each phase:

1. inspect/reconcile existing implementation;
2. implement the smallest complete vertical slice;
3. add unit/contract/integration/failure tests;
4. run formatting/lint/typecheck/build/tests as appropriate;
5. run the spec's acceptance gate;
6. update docs and `STATUS.md` with evidence;
7. commit a coherent milestone;
8. continue to the next dependency-ready phase.

Parallelize only independent phases/worktrees. Never let multiple agents unknowingly edit the same checkout.

When a requirement needs a coordinated change to another repository (for example Pi Engineering semantic events), make that boundary explicit and implement the smallest compatible change in the owning repository rather than duplicating its subsystem here.

## Reuse-first plugin policy

Freshly verify and prefer compatible upstream projects such as:

- Herdr Browser
- herdr-web
- Herdr Swarm
- worktree include helpers
- Plannotator Herdr integration / annotation tools
- GitHub checks / PR board
- notifications
- file viewers

Pin only reviewed versions/commits. A plugin name in an old spec is a research lead, not permission to install it blindly.

## Transparent dev fabric

The end state must support dozens of concurrent worktree dev servers without unique human-chosen hostnames. A service registered by any participating host receives a cluster-wide leased port and is reachable from tailnet devices through one stable logical name, conceptually:

`http://dev:<leased-port>`

Use current Tailscale Services/MagicDNS capability where available and preserve HTTP, WebSocket, SSE/HMR and generic TCP behavior where the target protocol requires it. The registry must survive reconnects, recover stale leases, and never expose a target outside the tailnet by default.

## Test and CI experience

Implement an IDE-like test tree with source links and per-test logs/traces for at least Python/pytest, Go, Rust, Java and Scala using structured native protocols where possible. Integrate local/agent test runs with Woodpecker/Pileated pipeline/step/queue/runner visibility. Do not create another test runner or CI scheduler.

## Completion

Do not stop after scaffolding, TODOs, or design notes. Continue through spec 0180 unless blocked by an external action that cannot safely be performed automatically.

Before declaring completion:

- audit every numbered spec and every row of `docs/acceptance-matrix.md`;
- perform fresh-install and upgrade/rollback drills;
- perform disconnect/reconnect and stale-lease recovery drills;
- validate Linux + macOS + phone/iPad use over Tailscale;
- run multi-agent/worktree/dev-preview/test/CI comparison scenarios;
- prove no hidden reasoning is persisted;
- prove shared-storage failure cannot corrupt local source worktrees;
- prove no public listeners are required;
- update the README with real installation/use commands from the implemented system.

At the end print a concise report: phases completed, files/components changed, upstreams adopted, cross-repo changes, tests and fleet evidence, known limitations, and any genuine external blockers.

Begin now with repository reconciliation and spec 0010.
