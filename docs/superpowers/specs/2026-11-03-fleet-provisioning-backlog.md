# Fleet provisioning: decisions and backlog (brainstorm of 2026-11-03)

Status: brainstorm notes, not yet a design. The base project's design document is the next step. Items marked (guess) are the assistant's proposals that the owner has not confirmed.

## What the owner decided

- Provision the whole system over Ansible with one path (`herdr-fleet apply`); other people must be able to deploy it too.
- Each operator keeps their own private repo (hosts, config, secrets) that consumes our public repos by pinned commit. To customise, they fork a repo and name the fork for that repo in their pins.
- Start on one machine and add machines as needed. Levels: (1) one machine, (2) several machines, (3) optional Slurm fabric whose head node is the entry point; agents can be placed on non-busy nodes as containers with access only to their own data, network and what is explicitly allowed.
- Every service runs as a pinned container, including the timers (one `jobs` scheduler container, guess); herdr, the host agents and the Pi toolchain stay native; the Mac uses native installs wherever possible.
- Everything is reached through herdr: agents, containerized or not, appear as herdr panes.
- Every node added to the fabric enrolls in Tailscale and is added to herdr as a fabric host.
- One agent image holds Pi, Claude Code and Codex; Pi's JSON config comes with a custom image; Claude and Codex logins (several subscriptions each) are synchronised, one writer per account (guess), after an experiment.
- Optional roles: the campus network allow rule, the bench agent, Google and AWS credentials.
- Retire: `herdr-web` (nobody uses it; the tailnet front door already points at the control plane).

## Learned resource profiles (added by the owner)

The deployed system should learn what each kind of agent needs: project X always needs lots of memory, Y many CPUs, A much disk. Base project: capture per-run peak memory, CPU time, disk growth and resource failures, reported to the control plane and stored per project. Slurm project: the estimator (percentile plus headroom, floor and ceiling, defaults for new projects, retry once with more after an out-of-memory or disk-full failure), use in placement and container limits, dashboard view with override or pin. To read first: the host metrics module and the capacity watchdog.

## To settle in the base design

- Adopting the existing hosts (bender, beast, the Mac, fry) without breaking them: import the central fleet config, converge without downtime.
- State, backup and restore (Postgres, rustfs, Hindsight memory, collab data) and a tested restore.
- Secrets beyond first setup: GitHub app, tokens, certificates, rotation; Ansible Vault covers static ones only.
- Certificates and DNS for operators without AWS: gateway certificates use AWS ACME renewal today.
- Network rules as code: Tailscale access rules and per-agent container network allowances.
- Releases and versions: tagged releases, changelog, upgrade notes, herdr compatibility table (pinned 0.9.1).
- Shared storage so repos are not cloned again and again (see below).

## Shared and distributed storage (added by the owner)

Problem: every host and every agent clones the same repositories over and over.

What exists: the lab already has a shared filesystem (Quobyte, under /quobyte/metabolomicsgrp) mounted on bender; rustfs provides S3-style artifact storage.

Proposal by level (guess):
1. One machine: nothing shared; local worktrees.
2. Several machines: a git mirror and cache on the control host that nodes clone from over the tailnet, using reference clones, refreshed on push or on a timer.
3. Slurm fabric: repositories, package caches (node, pip, model files) and shared read-only data on the cluster filesystem; each agent gets its own private work directory plus read-only mounts of what it is allowed to see.
Owner's rule: each agent works completely isolated in its own work area (never shared with another agent, so there are never concurrent edits to the same files) and cleans up after itself. Shared things are read-only (git mirror, caches, model files). An agent's checkout is a worktree or reference clone from the shared mirror (guess); a small janitor removes work areas whose agent is gone, as a backstop for killed agents (guess); results return through git, one branch per agent and pull requests.
Questions: which filesystem do other operators use (NFS, Lustre, none); where work areas live on each level.

## Later projects, to mention in the design

- Agent container limits: CPU, memory and disk limits, image provenance, egress allow lists, per-agent LLM budgets.
- Testing the provisioning itself: CI that builds a clean VM per level, including a macOS check.
- The Shepherd app for other people: iOS signing, TestFlight or App Store, push credentials.
- Documentation: quickstart, "adopt your existing host", troubleshooting (the system was AI-built with no setup process).
- The Mac as a host: it sleeps, which stops agents.
- Uninstall and data export.

## Open checks and experiments

- Verify each retire candidate before deleting anything: `herdr-ci-fix-sandbox`, the `autospec-e2e-*` repos, `weaveforge-provision-demo`, `pi-engineering-stack`, `minion`, `multi-agent-pipeline`, the Agent Mine playbook and cache (look at `session_history.py`).
- Add candidates to read and decide: `autospec-orchestrator`, `autospec-dispatcher`, `autospec-node`, `inference-hosts`, `herdr-handoff`, `herdr-artifacts`, `herdr-memory`.
- Experiment: copy one account's Claude and Codex logins to a second machine over ssh (without the assistant seeing them) and see whether both keep working for a day or two; check whether `claude setup-token` tokens are usable.
- Check `ai-sync`: it syncs `~/.claude`, so it must exclude credentials.
- Check each vendor's terms for sharing one subscription across many machines and agents.
- Tailscale on Slurm compute nodes: root requirement versus the no-root mode.
- Decide where compute nodes run herdr (per node, or only the head node).

## Pending from the terminal work

- Terminal: Roamgate trial against our own relay (the broker is merged and inert; no relay is rolled out). Decide keep, shelve or remove.
- App PRs #2, #3, #4 (iPad build, iPad polish, terminal view) are open and unmerged; the terminal view only works with a relay.
- If the relay is kept: roll it out through `herdr-fleet` as a role, not through `deploy-fleet-agents.sh`.
- Mac needs Python 3.12 or newer for the relay (and a Python for the other host tools).
- Control-plane log noise: `ConnectionResetError` on idle connections (about 80 tracebacks in 5 minutes); quiet it like the ingest fix.
- Ansible install task for the relay, and a relay restart in the fleet update after a herdr upgrade.
