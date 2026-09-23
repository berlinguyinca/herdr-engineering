# HerdR Engineering

**Repository:** `berlinguyinca/herdr-engineering`  
**Product:** Super HerdR — a distributed, mobile-capable engineering cockpit built on top of Herdr rather than a fork of it.  
**Status:** master implementation specification; greenfield repository, integrating existing tools and upstream plugins.

> This repository name intentionally normalizes the earlier draft spellings `berlinguyicna/herdr-engeneering` and the superseded planned repo `berlinguyinca/herdr-powerpack`.

## Why this exists

Running one coding agent in one terminal is easy. Running dozens of agents, worktrees, dev servers, tests, reviews, and CI jobs across several machines is not.

The lab needs one coherent place to answer:

- Which machine is each agent on?
- What is it doing right now?
- Which repository, branch, and worktree does it own?
- Which agents are blocked, idle, done, or still working?
- What did a session start with, what has it completed, and what is next?
- Which local/dev web server belongs to which worktree, and how can I open it from my laptop, phone, or iPad without remembering hostnames?
- Which tests passed or failed, and what is the trace for one test?
- What is happening in Woodpecker/Pileated CI and its queue?
- How do I compare several agents' candidate implementations side-by-side?
- Where are the generated screenshots, reports, plans, reviews, and handoff artifacts?

Herdr already solves the most important runtime problem: persistent real terminal sessions with agent awareness, workspaces, worktrees, and multi-machine SSH connectivity. HerdR Engineering adds the integration layer around it so the whole environment feels like one distributed engineering IDE.

## What HerdR Engineering is

HerdR Engineering is a **thin, curated integration/distribution layer**. It owns configuration, adapters, schemas, UI integration, deployment, health checks, and the few capabilities that do not already exist upstream.

It provides, in implementation order:

1. reproducible Herdr/Pi/engineering setup on the fleet;
2. a pinned, audited Powerpack-style plugin bundle;
3. private web/mobile access through Tailscale;
4. browser/dev-preview and Plannotator integration;
5. a shared artifact/handoff workspace;
6. per-agent session history and newest-first activity timelines;
7. semantic integration with Pi Engineering and AutoSpec;
8. a transparent `dev:<port>` fabric for ephemeral web servers across all machines;
9. an IDE-style cross-language test explorer;
10. Woodpecker/Pileated CI status, logs, queues, and runner visibility;
11. worktree/candidate comparison across many concurrent agents;
12. notifications, approvals, health, security, updates, rollback, and full fleet validation.

## What it is not

HerdR Engineering does **not** become another scheduler, CI engine, model router, source-control system, review engine, or agent runtime.

Ownership is intentionally strict:

| System | Owns |
|---|---|
| **Herdr** | persistent terminals, panes/tabs/workspaces, native worktrees, saved SSH machines, agent state, socket/plugin APIs |
| **HerdR Engineering** | curated distribution, adapters, session/activity UX, dev-service fabric, test/CI views, integration and fleet operations |
| **Pi Engineering** | missions, worker/subagent orchestration, engineering lifecycle, policy, review/repair coordination |
| **AutoSpec** | specs → issues → implementation workflow |
| **Tern** (formerly InferWeave) | model/runtime/GPU routing, capacity, queueing, inference; endpoint `https://llm.metabolomics.us` |
| **Tailscale** | private network, identity-aware reachability, stable service names |
| **Woodpecker / Pileated CI** | pipeline execution and runner queues |
| **Plannotator** | review semantics, comments, approvals |
| **GitHub** | repositories, issues, pull requests, repository history |
| **Pi-Web** | existing Pi-specific operator UI; link/embed where useful, never reimplement |

Pi Forge is explicitly excluded.

## Core design principles

- **Reuse first.** Audit the current Herdr ecosystem before writing replacement code.
- **Native Herdr first.** Use saved machines, `--machine`, worktrees, integrations, events, and plugin APIs instead of recreating them.
- **One source of truth per concern.** Session/activity views observe and correlate work; they do not create a second mission/task engine.
- **Private by default.** Web UIs, dev servers, browser debug endpoints, and internal services stay on Tailscale/private interfaces.
- **Worktrees are local source truth.** Shared storage is for artifacts/handoffs, never a multi-writer replacement for Git worktrees.
- **Observable activity only.** Never record or display hidden chain-of-thought. Store user-visible summaries, tool actions, tests, Git changes, artifacts, and status.
- **Structured signals before terminal scraping.** Prefer native APIs, test protocols, CI APIs, and Pi semantic events.
- **Incremental implementation.** Every numbered spec has entry/exit criteria and leaves the system usable.
- **Recoverable changes.** Pinned dependencies, safe config merging, health checks, backups, canaries, and rollback are mandatory.

## Current validation fleet

The initial fleet is:

- `fry`
- `beast`
- `bender`
- `macbook-m4`

These names are validation inventory, not hard-coded application logic. Future machines must be addable without code changes.

All four should have:

- Herdr
- Pi configured to use `https://llm.metabolomics.us` for all LLM requests
- `berlinguyinca/pi-engineering`
- `berlinguyinca/autospec`
- `vim`
- `mc`
- `btop`

Fleet setup is managed declaratively with Ansible. Tailscale provides private connectivity.

## User experience target

From a laptop, phone, or iPad on the tailnet, the operator should be able to:

- see every connected machine and agent;
- jump directly to a session requiring attention;
- inspect the session timeline and current work;
- open its worktree files and artifacts;
- open its browser/dev server at a stable `dev:<port>` address without knowing the physical host;
- inspect test trees and traces;
- inspect CI pipelines/queues/runners;
- compare candidate worktrees and previews;
- send input, approve/reject review work, and resume a session;
- disconnect without killing remote work.

The physical location of the agent should be visible when useful but should not dictate the normal workflow.

## Repository layout

```text
herdr-engineering/
├── README.md
├── MASTER_SPEC.md
├── START_HERE.md
├── IMPLEMENTATION_PROMPT.md
├── CONTRIBUTING.md
├── SECURITY.md
├── SPEC_MANIFEST.yaml
├── STATUS.md
├── docs/
│   ├── architecture.md
│   ├── decision-history.md
│   ├── upstream-baseline-2026-09-23.md
│   ├── acceptance-matrix.md
│   ├── implementation-roadmap.md
│   ├── spec-genealogy.md
│   ├── github-project-setup.md
│   ├── adr/
│   ├── contracts/
│   ├── specs/                    # normative ordered implementation specs
│   └── archive/                  # prior HerdR specs preserved for lineage
├── ansible/                      # implemented during fleet phases
├── config/                       # safe defaults/examples; never secrets
├── lock/                         # pinned upstream/plugin dependency lock
└── .github/                      # issue/PR templates and CI added during implementation
```

## Implementation order

`SPEC_MANIFEST.yaml` is the machine-readable source of order and dependencies. `docs/implementation-roadmap.md` visualizes the dependency graph, while `docs/spec-genealogy.md` maps every predecessor HerdR specification into the current program. Humans should start with:

1. `START_HERE.md`
2. `docs/specs/0000-master-program.md`
3. specs `0010` through `0180` in order, respecting dependency gates.

Parallel work is allowed only when the manifest marks specs independent and their dependencies are complete.

## Definition of done

The project is complete when a fresh supported Linux or macOS machine can be enrolled declaratively, the four-machine validation fleet passes the acceptance matrix, mobile/tablet access works privately, concurrent agent worktrees can publish and compare dev previews, local and CI test evidence is navigable, session activity survives reconnect/restart, and all security/update/rollback drills pass.

The final release must not depend on undocumented manual SSH edits or one-off local configuration.
