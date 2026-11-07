# Fleet provisioning, one path: base project design (draft)

Status: draft for the owner's approval. Written from the 2026-11-03 brainstorm; the decisions the owner made are listed as such, everything else is marked "my call". Final home: `herdr-fleet` (`docs/specs/`), next to its existing spec 0001; this draft lives in `herdr-engineering` until the owner says to move it. Companion file: `2026-11-03-fleet-provisioning-backlog.md` (items to settle, later projects, open checks).

## Intent

The herdr system grew around one person and has no setup process: bender runs about a dozen containers, six user services, a dozen timers, two herdr plugins and many secrets, all set up by hand. The owner needs to install it on a Slurm cluster head node and wants other people to be able to deploy it. The goal is that anyone can stand the whole system up from nothing, grow it, update it and roll it back, with one tool and one path: `herdr-fleet apply` over Ansible.

## Goals

- One path changes software on any fleet host: `herdr-fleet apply`. Timers, CI and people may trigger it; nothing else installs or updates.
- A one-machine fleet works from day one. Machines are added one at a time. An optional Slurm level adds a cluster whose head node is the entry point.
- Other people deploy it with their own private operator repo, using our public repos at pinned commits, and fork a repo to change it.
- The owner's own fleet (bender, beast, the Mac, fry) is the first operator repo and is adopted without downtime.

## Non-goals and anti-goals

- The Slurm fabric itself (how agents are scheduled, long-lived LLM and database services on the cluster, how many run at once) is the second project and has its own brainstorm. This design only leaves the room for it.
- The terminal decision (Roamgate versus our relay) is a separate small decision, listed in the backlog.
- Not a multi-user platform: each fleet has one owner. Not a replacement for Ansible or for Docker.
- Anti-goal: anything that makes a public repo contain a host name, address, key or account. A design that needs a second update path "just for X" has failed.

## Decisions made by the owner

1. Provision the whole system over Ansible; one update path; deployable by other people.
2. Each operator has a private repo (hosts, config, secrets, pins) that consumes our public repos by pinned commit. To change something, they fork the repo and name the fork for that repo in their pins.
3. Levels: (1) one machine, (2) several machines, (3) optional Slurm fabric. Agents on level 3 are placed on non-busy nodes as containers that can reach only their own data, their own network and what is explicitly allowed.
4. Every service is a pinned container, timers included. herdr, the host agents and the Pi toolchain stay native. The Mac uses native installs wherever possible.
5. Everything is reached through herdr: every agent, containerized or not, appears as a herdr pane.
6. Every node added to the fabric enrolls in Tailscale and in herdr as a fleet host.
7. One agent image holds Pi, Claude Code and Codex. Pi's JSON config ships with a custom image. Claude and Codex logins (several subscriptions each) are synchronised across nodes.
8. Each agent works in its own isolated work area and cleans up after itself, so two agents never edit the same files.
9. Optional roles: the campus network allow rule, the bench agent, Google and AWS credentials.
10. Retire `herdr-web`. Retire the old `ansible/` folder and fleet scripts in `herdr-engineering` once the new roles cover them.
11. The deployed system learns over time what resources each kind of agent needs (project X always needs lots of memory, Y many CPUs, A much disk) and uses that for placement and container limits.

## The model

**Operator repo.** Created by `herdr-fleet init`. It holds `fleet.lock` (exact pins: for each component the repository URL, the commit, versions and image digests), `fleet-config.yaml` (hosts, roles per host, service URLs), an inventory, and vault-encrypted secrets. A pin's repository defaults to our public repo; pointing it at a fork is a one-line change. No role contains a repository URL or host name of its own.

**Roles** (each one Ansible role, idempotent, with `--check`, pins in `fleet.lock`). Those of spec 0001 are kept (`baseline`, `toolchain`, `herdr`, `herdr-engineering`, `pi-runtime`, `secret-scan`, `controller`, `gateway`, `ci-server`, `ci-agent`); my calls for the additions this design needs:

| Role | Provides | Where |
|---|---|---|
| `host-agent` | the herdr plugins (session feed, Pi recovery), the service feed, the fabric agent, mission feed, build cleanup, `host.json` | every host, native |
| `control-plane` | control plane web and worker, Postgres, rustfs, LLM proxy, collab, share, fabric gateway and DNS | the control host, containers |
| `jobs` | the scheduler container that runs the former timers (collab sync, certificate renewal, CI relay, pipelines sync, gateway monitor, backups) from the same image or a pinned jobs variant of it; the fabric certificates' renewal stays a host timer of `host-agent` | the control host, container |
| `memory` | Hindsight and the memory API with their databases and backups | containers |
| `powerpack` | the herdr plugin bundle (Roamgate and others) at its pinned commit | every host, native |
| `agent-image` | build and pin the single agent image (Pi, Claude Code, Codex) from our Dockerfile plus the operator's config overlay | build, containers |
| `enroll-node` | Tailscale, herdr, host token and host agents for a node added to the fabric (level 3 nodes included) | per new node |
| `git-mirror` | a git mirror and cache that hosts clone from | control host (level 2) |
| `storage` | repositories, caches and model files on the shared filesystem, read-only to agents | level 3 |
| `slurm-fabric` (optional, project 2) | head node entry point, agent placement | level 3 |
| `campus-network`, `bench-agent` (optional) | the campus allow rule and the bench agent | where wanted |

Timers that must touch the host (fleet update, the capacity watchdog) stay native timers on that host.

**Levels.** Level 1 puts `host-agent`, `control-plane`, `jobs`, `memory` and `powerpack` on one machine. Level 2 adds hosts with `host-agent`, `powerpack` and `enroll-node`, plus `git-mirror` on the control host. Level 3 adds the Slurm nodes (each enrolled as a host), `storage` and `slurm-fabric`. Nothing in levels 1 and 2 assumes the absence of a scheduler.

**Agents.** Each agent runs from the single agent image; the environment selects the harness (Pi, Claude Code or Codex). It gets one private work area made as a worktree or reference clone from the read-only mirror, only the one login it is assigned, and only what it is allowed to reach. When it ends it removes its work area; a janitor removes work areas of killed agents after a delay. Results return through git, one branch per agent. Placement prefers a node that is not busy and an account that is not near its subscription limit.

**Resource profiles (learned).** Capture belongs to the base project, learning and use to the Slurm project. Every agent run records peak memory, CPU time, disk growth and whether it hit a limit or failed on resources; the host agents and the container runtime report this to the control plane, which stores it in Postgres keyed by project (and agent kind). My calls: a conservative estimate per project, for example a high percentile of recent runs plus headroom, with a floor and a ceiling; new projects start from defaults; an out-of-memory or disk-full failure raises the estimate and retries once; the dashboard shows each project's profile and the operator can override or pin it. The repo already has a host metrics module and a capacity watchdog on bender, to be read before this is designed.

**Logins.** Provisioning never creates or handles logins. It installs the CLIs and checks login state in `doctor`. Pi uses the LLM gateway with per-project keys, injected at start as a secret, never baked into the image. Claude Code and Codex logins are kept in the operator's private store as named accounts; each account has one primary node that refreshes it, and a sync step gives followers a read-only copy over the tailnet, authenticated by each host's token. A stale follower raises an alert in `doctor`. This is subject to the experiment in the backlog and to the vendors' terms.

## What exists today and where it goes

| Repo or piece | Disposition |
|---|---|
| `herdr-fleet` | home of the engine and roles; grows from slice 2 (only `toolchain` exists) |
| `herdr-engineering` | stays the product (control plane, host agents); its `ansible/` and fleet scripts retire at parity |
| `herdr-powerpack` | stays; applied by the `powerpack` role |
| `herdr-handoff`, `herdr-artifacts` | stay; installed into the harnesses by a role |
| `herdr-memory`, `inference-hosts`, `autospec-node` | private; to read, decide and wrap as optional roles |
| `autospec-orchestrator`, `autospec-dispatcher` | to read; likely building blocks for the Slurm project |
| `pi-engineering`, `pi-rescue`, `autospec` and companions | the Pi and AutoSpec stack, `pi-runtime` role |
| `herdr-web`, Agent Mine playbook and cache | retire (Agent Mine to be verified) |
| test and demo repos (`herdr-ci-fix-sandbox`, `autospec-e2e-*`, `weaveforge-provision-demo`, `pi-engineering-stack`, `minion`, `multi-agent-pipeline`) | retire candidates, verified one by one before anything is deleted |

## Constraints

- Debian or Ubuntu and macOS for hosts; GitHub; Tailscale. Python 3.12 or newer on hosts that run our Python tools.
- Secrets never in a repository. The operator's private store is Ansible Vault for the first setup; runtime rotation needs more (backlog).
- The Mac has no containers for agents or services that need the host; Docker Desktop cannot pass sockets reliably.
- No root assumed on level 3 nodes: roles need a no-root profile, and Tailscale on compute nodes may need its no-root mode (backlog).

## Definition of done

1. From nothing: on a fresh machine and a new operator repo made by `init`, one `apply` brings up the control plane, herdr and a host agent; the dashboard answers on the tailnet; no manual steps.
2. A second `apply` changes nothing and `plan --check` reports no drift.
3. Adding a machine takes a few lines and one `apply`; it appears in the dashboard and in Shepherd.
4. Upgrade and rollback: bump one pin, apply, healthy; apply the old lock and it returns.
5. Nothing personal in the public repos: a scan finds no host names, addresses or secrets; a stranger can follow the README without asking.
6. The owner's own fleet converges with no unexpected drift, and then the old scripts and `ansible/` are deleted.
7. Restore: from a backup taken by the system, a clean machine restores the control plane's data (Postgres, rustfs, Hindsight, collab) and comes up with it, tested in CI on a throwaway fleet.

## Scope of this project and settled questions

Scope (my call, from the brainstorm's split into projects): this project is levels 1 and 2, the operator-repo and fork model, adopting the owner's hosts, backup and tested restore, and capturing per-run resource data. The agent runtime (isolated work areas, janitor, branch per agent, containerized agents appearing as herdr panes, the single agent image, login sync, no-root profile), `storage`, `enroll-node` for Slurm nodes and the Slurm fabric itself are project 2. This project reserves their role names and leaves schema room (`slurm` inventory group, per-account and per-agent fields), and builds nothing of them.

Settled as my calls, the owner to correct if wrong:
- **HTTPS and DNS without AWS:** the default is Tailscale-issued HTTPS (`tailscale serve` and MagicDNS names). AWS ACME renewal for the public gateway name is an optional role. A stranger needs no cloud account.
- **Where `apply` runs:** from the `controller` host, started by a person by default. An unattended update timer on the controller host (the owner's fleet already has a daily fleet update) is opt-in. The vault password lives in the operator's keystore on the controller host (mode 0600) or as a CI secret; hosts hold no repo access or vault key.
- **Adoption:** short restarts per service are acceptable, with a backup first. Existing volumes and data paths are adopted in place. Running agent sessions survive, because herdr and the agents are native and only the containers and timers move.
- **Backup and restore are in this project:** definition of done gets a restore test. The backup target is operator configuration (shared filesystem, S3, or another host); a backup that stays on the same host does not count.
- **The Mac** runs only `host-agent` and `powerpack` (native). A control host is Linux.
- **Relation to spec 0001:** this design builds on it and amends it. Its `gateway` role splits into `control-plane` (control plane stack) and a gateway role (coredns and `fabric-gateway`); its `herdr-engineering` role installs the `herdr-eng` environment, CLI and build gate, and `host-agent` is the set of services that use it. `ci-server` and `ci-agent` (Woodpecker) stay later optional roles: needed to adopt the owner's fleet, not for a stranger's level 1.

Answered by the owner ("go for it, we can do all of this now"): the design and the calls above are approved, including the adoption downtime (short restarts per service, backup first). My reading, to be corrected if wrong: container images are published by our CI to a public registry by digest (an operator who forks builds their own); the `memory` role is optional until the owner decides that `herdr-memory` becomes public; no repository is made public without the owner naming it.

## Left to the builder

The exact schema of `fleet.lock` and `fleet-config.yaml` (extending version 1 compatibly), how `init` scaffolds the operator repo, the Ansible mechanics of per-component repository overrides, the container image for `jobs`, the scheduler chosen inside it, how adoption of existing hosts is staged, and the testing approach for the roles.

## My calls (not confirmed by the owner)

The role list above beyond spec 0001; the `jobs` scheduler container; the one-writer-per-account login design; the git mirror for level 2; the janitor for killed agents; the no-root profile for level 3; the retire candidates other than `herdr-web`.
