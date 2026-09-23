# Decision History and Spec Genealogy

This file explains how the current master bundle was assembled and which later decisions supersede earlier assumptions.

## 2026-09-20 — Pi Engineering ↔ Herdr runtime integration

Key decisions carried forward:

- staged integration, not a rewrite;
- Pi Engineering owns missions/DAG/policy/review/repair;
- Herdr owns persistent runtime/session/process concerns;
- all Herdr access from Pi Engineering goes through an abstraction boundary;
- worktrees must be isolated;
- lifecycle/events/results should be structured;
- recovery, idempotency, canary and rollback are gates;
- large communication uses artifact-first bounded messages rather than oversized prompts;
- Pi-Web remains external;
- Pi Forge is excluded.

Current master interpretation: HerdR Engineering does not absorb Pi Engineering runtime logic. It consumes/extends the contract and owns the operator-facing integration around it.

## 2026-09-21 — distributed thin-client/fleet model

Key decisions carried forward:

- cockpit may be small; work stays on remote machines;
- Tailscale provides private network reachability;
- remote machines keep independent repos, sessions and processes;
- HerdR must not become a model/GPU scheduler;
- fleet configuration should be Ansible-managed;
- validation hosts are `fry`, `beast`, `bender`, `macbook-m4`;
- Pi on those machines uses the lab inference endpoint.

### Superseded detail

An earlier design considered implementing more remote/fleet aggregation ourselves. Current Herdr now has native saved SSH machines and machine-targeted API forwarding. The custom aggregation layer is deleted from the plan; native Herdr is the foundation.

## 2026-09-22 — Powerpack/plugin bundle

The original planned repository was `berlinguyinca/herdr-powerpack`.

Requirements carried forward:

- one curated installation;
- audit and reuse existing plugins first;
- pinned dependencies;
- browser and mobile access;
- Plannotator;
- swarm/worktree helpers;
- file/code annotation;
- GitHub/PR visibility;
- notifications;
- safe update/rollback;
- Linux/macOS/Ansible;
- private-by-default networking.

The v2 Powerpack spec then added:

- shared contributor/agent artifact workspace;
- per-session client panel;
- newest-first timeline;
- Started / Completed / Current / Next summaries;
- artifact linking;
- explicit ban on hidden chain-of-thought capture.

### Supersession

Because nothing had been implemented yet, `berlinguyinca/herdr-powerpack` is folded into the broader canonical repository `berlinguyinca/herdr-engineering`. Powerpack becomes a distribution/package concept inside the repo rather than a second project.

The full predecessor v2 files are archived under `docs/archive/2026-09-22-powerpack-v2/`.

## 2026-09-22/23 — transparent dev fabric

New requirement:

- dozens of concurrent agents/worktrees may each start dev servers;
- an existing skill already chooses a random free local port;
- humans must not manage dozens of host-specific names;
- one logical tailnet name such as `dev` plus a leased port should locate any dev service regardless of host;
- phone/iPad/laptop should open the same address;
- HTTP, WebSocket, SSE/HMR, and required TCP behavior should act normally;
- leases must recover from crashes/reconnects;
- preview comparison should link to worktree/session metadata.

The current spec uses Tailscale Services/MagicDNS as the preferred stable network primitive and adds a thin port lease/routing layer rather than changing how dev processes are launched.

## 2026-09-23 — IDE-style Tests and CI

New requirement:

- Herdr should feel increasingly like a cross-system IDE;
- show a traditional test tree with pass/fail/running/skipped states;
- click a test to inspect logs and stack/trace details;
- support pytest, Go, Rust, Java, Scala and extensible others;
- preserve native runners instead of replacing them;
- unify local/agent/CI evidence;
- show Woodpecker/Pileated pipelines, steps, queue and runners;
- support CI → “debug with Pi” style handoff through correlation, not by moving CI authority into Herdr.

These requirements are now specs 0120 and 0130.

## 2026-09-23 — master HerdR Engineering program

This bundle introduces the final implementation order and repository structure:

`berlinguyinca/herdr-engineering`

It consolidates all previous HerdR-specific work, adds explicit dependency gates, records current upstream Herdr capabilities, and defines one completion path from empty repository to validated fleet release.
