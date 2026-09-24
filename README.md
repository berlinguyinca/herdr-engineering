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
├── herdr_engineering/            # Python integration layer (the implementation)
├── ansible/                      # fleet bootstrap playbooks (Linux + macOS)
├── config/                       # safe defaults/examples; never secrets
├── lock/                         # pinned upstream/plugin dependency lock
├── scripts/                      # dev-env / validate / naming dogfood scripts
├── tests/                        # 79 unit/contract/integration tests
├── fixtures/                     # test fixtures
├── docs/evidence/                # real command snapshots per phase
└── .github/                      # issue/PR templates and CI workflow
```

## Install and use (real commands)

The implementation is a small Python package exposing one CLI, `herdr-eng`,
plus the `herdr_engineering.*` library. It layers on top of an existing Herdr
installation (verified against `herdr 0.9.1`).

### One-shot install (assumes Herdr already exists)

`scripts/install.sh` sets everything up on the machine it runs on and does
**not** install, upgrade, or restart Herdr — your running agent sessions are
unaffected. It is idempotent.

```bash
git clone https://github.com/berlinguyinca/herdr-engineering
cd herdr-engineering
scripts/install.sh
```

The installer, in order: verifies Herdr is present; creates a project `.venv`
and installs the package; creates the machine-local state + artifact
directories; writes a private-by-default machine-local config at
`~/.config/herdr-engineering/config.yaml` (preserved if present); enrolls this
machine (detected host + OS) into the private fleet inventory
(`ansible/inventory/hosts.yml`); puts `herdr-eng` on PATH via
`~/.local/bin`; and runs `herdr-eng doctor` to confirm.

### Manual setup (equivalent)

```bash
# 1) create the venv and install the package (dev extras for tests/lint)
scripts/dev-env.sh
source .venv/bin/activate

# 2) run the full validation (lint + schema + config + lock + tests + doctor)
scripts/validate.sh

# 3) health / capability / security checks
herdr-eng doctor --json
herdr-eng capabilities --json

# 4) automatic semantic workspace naming (dogfooded)
herdr-eng name --prompt "Implement transparent dev routing so agents can preview from any device"
# => transparent-dev-routing-agents  (name_source=auto_prompt)
herdr-eng name --issue-number 421 --issue-title "Add test tree with pytest and JVM support" --repo owner/repo
# => 421-test-tree-pytest-jvm       (name_source=auto_issue)
herdr-eng name --spec-id 0110 --spec-title "transparent dev fabric"
# => 0110-transparent-dev-fabric    (name_source=auto_spec)

# 5) Powerpack distribution / lifecycle
herdr-eng powerpack status
herdr-eng powerpack snapshot pre-release-1
herdr-eng powerpack preflight
herdr-eng powerpack rollback

# 6) private web/mobile control surface (binds 127.0.0.1 by default)
herdr-eng web --port 8787

# 7) other integration surfaces
herdr-eng machines --json
herdr-eng workspaces --json
herdr-eng devfabric list
# lease a port for a running app, then forward it (protocol-transparent TCP):
herdr-eng devfabric register --machine bender --host 127.0.0.1 --port 5173 --label my-app
herdr-eng devfabric serve lease_xxx            # or --bind <tailscale-ip> for other hosts
herdr-eng tests adapters
herdr-eng tests run pytest --repo owner/repo --worktree 0110-dev-fabric --machine localhost
herdr-eng ci pipelines --repo owner/repo
herdr-eng candidates id --repo owner/repo --worktree wt-18 --branch checkout-redesign-a
herdr-eng journal timeline <session_id>
herdr-eng attention list
herdr-eng artifacts list
herdr-eng browser http://localhost:5173 --json
```

Run `herdr-eng --help` for the full command set. Every command that emits
structured data supports `--json`. The web UI is private by default
(`private_only: true`, host `127.0.0.1`); expose it only via the tailnet.

## Implementation order

`SPEC_MANIFEST.yaml` is the machine-readable source of order and dependencies. `docs/implementation-roadmap.md` visualizes the dependency graph, while `docs/spec-genealogy.md` maps every predecessor HerdR specification into the current program. Humans should start with:

1. `START_HERE.md`
2. `docs/specs/0000-master-program.md`
3. specs `0010` through `0180` in order, respecting dependency gates.

Parallel work is allowed only when the manifest marks specs independent and their dependencies are complete.

## Definition of done

The project is complete when a fresh supported Linux or macOS machine can be enrolled declaratively, the four-machine validation fleet passes the acceptance matrix, mobile/tablet access works privately, concurrent agent worktrees can publish and compare dev previews, local and CI test evidence is navigable, session activity survives reconnect/restart, and all security/update/rollback drills pass.

The final release must not depend on undocumented manual SSH edits or one-off local configuration.
