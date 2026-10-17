---
name: fleet-sync
description: >
  Keep the HerdR Engineering fleet version-synced and register every site.
  Use when the operator wants to JOIN a new machine into the herdr dev-fabric
  (make it a fabric "site" and bring its herdr-eng + pi build-gate extension to
  the fleet version), SELF-UPDATE the whole fleet to the current version, or
  check fleet version/registration parity. Triggers on "join a machine/site",
  "add a host to herdr", "self-update the fleet", "sync the fleet",
  "fleet version parity", "is every site registered".
---

# fleet-sync

A one-shot, version-synced way to (1) **join** a new machine into the HerdR
dev-fabric so it becomes a registered fabric "site", (2) **self-update** the
whole fleet to the current version, and (3) **verify** version + registration
parity. Everything is idempotent and non-disruptive to running agents.

## When to use

- "I added a machine to herdr — make it a site and sync its version."
- "Update herdr-engineering everywhere to the same version."
- "Show me fleet status / version parity / which hosts are registered."

## Concepts

- **Site** = a fleet machine that runs the fabric auto-registration agent and
  appears as a `target_machine_id` (its hostname) in the gateway's lease store
  (`herdr-eng devfabric list --json`).
- **Version source of truth** = the public repo `origin/main`
  (berlinguyinca/herdr-engineering). Every host pulls `origin/main` and
  pip-installs it; the controller also copies `build-gate.ts` from its local
  checkout. Keep the controller checkout on `origin/main` before syncing.
- **Inventory** is private and machine-local (`ansible/inventory/hosts.yml`,
  gitignored). The controller holds the union; each host self-enrolls via
  `scripts/install.sh`.

## Working directory (important)

This skill orchestrates the **herdr-engineering repo**'s own scripts and
ansible playbooks — it does not bundle them. Run it from the controller's
repo checkout, e.g. `/home/wohlgemuth/IdeaProjects/herdr-engineering`, and
resolve every path below relative to that repo root. If you don't know where
the checkout is, locate it first (`find ~ -name 'fleet-join.sh' 2>/dev/null`)
and `cd` there before running anything. The controller is the machine that
holds the ansible inventory (`ansible/inventory/hosts.yml`) and SSH keys to
the fleet.

## Operations

### 1. Join a new site (onboard a machine)

On the **controller** (where the ansible inventory + SSH keys live):

```bash
scripts/fleet-join.sh <host> \
    --os linux|macos \
    --ansible-host <tailnet-magicdns-or-ip> \
    --user <fleet-user> \
    [--gateway http://dev.lan:29999]
```

This:
1. Adds `<host>` to the private inventory (`hosts.yml` + `host_vars/<host>.yml`).
2. Runs `ansible/playbooks/fleet-sync.yml -l <host>` to converge:
   - `herdr-eng` (pulled from `origin/main`, pip-installed into the venv)
   - `fabric.env` + the auto-registration agent service (registers this host's
     services → it becomes a fabric site)
   - the pi build-gate extension (`build-gate.ts`)
3. Verifies registration (`devfabric list`) and reports version parity.

If you can't reach the host from the controller, run on the host itself:
```bash
scripts/install.sh
scripts/fabric-agent.sh --gateway http://dev.lan:29999   # linux
scripts/fabric-agent-macos.sh --gateway http://dev.lan:29999   # macos
```
then run `fleet-sync` from the controller once it's reachable.

### 2. Self-update the whole fleet

```bash
scripts/fleet-self-update.sh            # update controller + every host
scripts/fleet-self-update.sh --host <h> # update just one host
```

Pulls the controller checkout (`--no-pull` to skip), reinstalls local herdr-eng
(`--no-local-reinstall` to skip), runs `fleet-sync.yml` across the fleet, then
`fleet-status.yml` to show parity.

### 3. Status / parity

```bash
scripts/fleet-sync-status.sh            # table: host | herdr-eng | build-gate | agent | registered
scripts/fleet-sync-status.sh --json     # machine list
ansible-playbook -i ansible/inventory/hosts.yml \
    ansible/playbooks/fleet-status.yml  # raw read-only fingerprints
```

## Verification (do before claiming success)

- Every reachable host reports the **same** `herdr-eng` version and `build-gate`
  checksum as the controller.
- Each host's agent is `active` and the host appears as a `target_machine_id`
  in `herdr-eng devfabric list --json` (0 leases is OK if it has no running
  services; the agent registers them as they start).

## Safety

- **Never disrupt running agents** — all playbooks are converge-only and
  idempotent; `fleet-status.yml` is read-only.
- **No secrets** in the repo — `host_vars/` is gitignored; `fabric.env` holds a
  tailnet URL, not a credential.
- A host with no `herdr-eng` yet is provisioned by `fabric-agent.yml`; the pi
  build-gate extension no-ops gracefully until then.

## Files

- `scripts/fleet-join.sh`, `scripts/fleet-self-update.sh`, `scripts/fleet-sync-status.sh`
- `scripts/fleet_inventory.py` (private inventory mutation helper)
- `ansible/playbooks/fleet-sync.yml` (converge), `ansible/playbooks/fleet-status.yml` (read-only)
- `ansible/tasks/fleet-fingerprint.yml`, `ansible/tasks/fabric-agent.yml`, `ansible/tasks/pi-build-gate.yml`
