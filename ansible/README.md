# Ansible fleet bootstrap (spec 0040)

Makes a supported Linux or macOS host reproducibly ready for the HerdR
Engineering ecosystem without ad-hoc SSH edits.

## Layout

```text
ansible/
├── inventory/
│   ├── hosts.yml            # real inventory (private; NOT committed)
│   ├── host_vars/           # per-host connection details (private; NOT committed)
│   └── hosts.example.yml    # committed example (no connection secrets)
├── files/
│   └── fleet-update-state.py  # fleet-update decisions + state (main host)
├── playbooks/
│   ├── group_vars/all.yml   # herdr_pinned_version, fleet_pins, fleet-update cron paths
│   ├── site.yml             # fleet bootstrap entry point
│   └── fleet-update.yml     # daily fleet update, run from the main host
├── roles/
│   ├── herdr_linux/         # Debian/Ubuntu baseline
│   ├── herdr_macos/         # Homebrew baseline
│   ├── tailscale/           # tailnet install + join
│   └── herdr_fleet_update/  # OS + herdr/pi/autospec updates, busy deferral
└── README.md
```

## Validation inventory

`fry`, `beast`, `bender`, `mac`. These are **examples**, never
hard-coded into application logic. Add machines by editing the inventory only.
Connection details (MagicDNS address, SSH user) go in
`inventory/host_vars/<host>.yml`, e.g. for the macOS host:

```yaml
ansible_host: mac.<tailnet>.ts.net
ansible_user: <fleet-user>
ansible_python_interpreter: /usr/bin/python3
```

## Managed baseline (idempotent)

- Herdr at the pinned compatible version (`0.9.1`);
- Pi + provider config pointing all lab LLM work at `https://llm.metabolomics.us`;
- `berlinguyinca/pi-engineering`, `berlinguyinca/autospec`;
- Tailscale (`roles/tailscale`): install, `tailscaled`, and join the tailnet only when the node is not logged in (auth key from Ansible Vault);
- `vim`, `mc`, `btop`;
- HerdR Engineering state + artifact directories.

Existing Herdr/Pi/Tailscale config is merged/backed up, never overwritten
wholesale. No secrets live in the inventory or any committed file.

## Tailscale role

`roles/tailscale` runs in `site.yml` before the platform role. Linux
(Debian/Ubuntu) installs from the official `pkgs.tailscale.com` apt repository
and enables `tailscaled`; macOS uses the Homebrew `tailscale` formula.

- **Already-joined hosts are left alone.** After starting `tailscaled` the role
  waits (10 x 3 s) for a settled `BackendState` from `tailscale status --json`.
  Only `NeedsLogin` triggers
  `tailscale up --hostname=<inventory_hostname> --advertise-tags=tag:fleet`.
  `Running` is left alone; `Stopped` (enrolled but stopped) is reported and left
  alone, run `tailscale up` manually; any other or never-settling state fails the
  play with a fixed message. A second run changes nothing and `--check` never
  joins.
- **Auth key from Vault, never Git.** Variable `tailscale_auth_key`, required
  only for hosts in `NeedsLogin`. Create the vault file on the main host (not
  committed) with an editor, so the key never lands in shell history:

  ```bash
  cd ansible
  mkdir -p inventory/group_vars/herdr_fleet
  ansible-vault create inventory/group_vars/herdr_fleet/vault.yml
  #   in the editor:  tailscale_auth_key: tskey-auth-XXXX   (placeholder)
  ansible-playbook -i inventory/hosts.yml playbooks/site.yml --ask-vault-pass
  # or: --vault-password-file ~/.herdr-vault-pass  (mode 0600)
  ```

  Alternatively keep the key in a 0600 vars file passed as
  `--extra-vars @/path/to/keyfile.yml`. Do not put the key on the command line
  with `-e tailscale_auth_key=...` (it lands in history and `ps`). The key is
  written to a 0600 file inside a 0700 temp directory and consumed via
  `--auth-key=file:<path>`, so it never shows in `ps`; the directory is removed
  afterwards and every task touching the key is `no_log`. This needs
  **Tailscale >= 1.62** on the node; the role checks `tailscale version` and
  fails with an upgrade hint if older. On a failed `tailscale up` the role
  prints the return code and the last 5 stderr lines with `tskey-...` redacted.
  A node that must join without a key fails on Linux with a fixed message.
- **ACL prerequisite.** `tag:fleet` must exist under `tagOwners` in the
  Tailscale access rules. Create the auth key in the admin console as
  reusable, pre-authorized, with tag `tag:fleet`; a 90-day expiry is
  recommended (rotate the Vault file on renewal). Nodes already joined keep
  working after the key expires.
- **macOS is best-effort.** Install and join failures (no Homebrew, daemon/app
  not running, no key) are reported but never fail the play. Use
  `-e herdr_engineering_become=false` for the user-scoped brew path.

## Fleet update (`playbooks/fleet-update.yml`, `roles/herdr_fleet_update`)

Updates are **pushed from the main host** (`herdr_engineering_gateway_host`,
`bender` today). Fleet hosts hold no repo access or credentials for this.
`site.yml` installs a daily cron entry on the main host only
(`tasks/fleet-update-cron.yml`); the play then runs against `herdr_fleet`.

Per host, in order:

1. **Busy check (control plane, read-only).** The host is busy if
   `GET /api/v1/hosts/<id>` reports `latest.agents_running > 0`, or
   `GET /api/v1/sessions?status=active` lists an active session whose
   `host_id` is this host. The fabric agent does not report `agents_running`
   today, so the sessions list is the signal that matters: if it cannot be read
   (control plane down, 401, bad JSON) the busy state is **unknown** and the
   host is deferred with result `deferred: busy state unknown`. It never updates
   blind. `<id>` is `fleet_update_host_id`, default the `nodename` fact (what
   the agent reports as `socket.gethostname()`), not the inventory alias; set
   it per host if they differ. A session matches when its `host_id` is that id,
   the short hostname, the inventory name or one of `fleet_update_host_aliases`
   (sessions report `os.hostname()`, the same value as the agent).
2. **Busy → defer.** No changes are made. `deferred_since` is recorded on the
   first deferral and kept across runs. After `fleet_update_max_defer_days` (7)
   the host still defers; nothing is forced. The control plane then shows
   the "Updates deferred N d" Problem.
3. **Not busy → update.** OS packages first. On Debian/Ubuntu,
   `fleet_update_security_only: true` (the default) upgrades only packages
   that `apt-get -s upgrade` lists from a `*-security` origin.
   `fleet_update_security_only: false` runs a full `apt upgrade`. macOS runs
   `brew update && brew upgrade`. Then the existing tasks run: the platform
   role (herdr pinned-version check, pi presence, pi-engineering and autospec
   clones), `tasks/fabric-agent.yml` and `tasks/pi-build-gate.yml` (only at
   the pinned `fleet_update_herdr_engineering_revision`, a full commit id, or
   `-e herdr_engineering_revision=<sha>`; when it is empty, herdr-eng is not
   touched, so the cron never ships whatever HEAD the main host has checked
   out), `tasks/pi-runtime.yml` (only with `-e pi_engineering_revision=<sha>`)
   and `tasks/fabric-systemd.yml`. During this run `fabric-agent.sh` leaves a
   running agent alone (`HERDR_FABRIC_AGENT_NO_RESTART=1`; only releases that
   contain this change honour it, so an older pinned revision still restarts
   its agent once inside the script). After **all** of these succeed, and only
   when an OS package, Homebrew or the herdr-eng release actually changed
   (staging an already staged revision reports no change), the play re-reads
   the busy state and restarts the fabric agent **once** (systemd user unit, or
   launchd `kickstart` on macOS). If the host became busy (or unknown) since
   the decision, the restart is skipped and the result is
   `updated (agent restart deferred)`; the agent picks up the release at its
   next restart. A small race remains: an agent that starts between the
   re-check and the restart is still interrupted. On macOS the restart is
   best-effort: a LaunchAgent that is not loaded prints a message and records
   `updated (agent restart failed)`, never `failed:`. A failing update step
   yields `failed: <task name>`. Only the task name is recorded, never its
   output.
4. **Always post** one host event (`POST /api/v1/ingest`, entity `host`,
   event `host.fleet_update`). It carries `pins` (from `fleet_pins`, unset pins
   omitted), the full `fleet_update` object
   (`{last_updated, deferred_since, result}`; `last_updated` keeps the previous
   value unless this run updated) and `maintenance`, which is sent every run
   (`false` included), because the control plane keeps the old value when the
   key is missing. It never sends `telemetry`, so the agent's samples are not
   touched. If the control plane is unreachable, the run prints a warning and
   the update still counts. The control plane treats this event as state only:
   it never changes the host's status or last-seen time and never creates a
   host the agent has not registered (only the agent's own heartbeats prove a
   host is alive).

**One host at a time.** The play runs `serial: 1`. With
`fleet_update_stop_on_failure: true` (the default), once a host records
`failed: ...`, every remaining host of that run records
`skipped: an earlier host failed` and changes nothing. Pass
`-e fleet_update_stop_on_failure=false` to keep going. A host that is
unreachable over SSH is dropped from the run and records nothing; its
"last updated" ages until the 14-day Problem appears.

**Security-only is best-effort for apt.** A package counts as a security
update when `apt-get -s upgrade` would take its candidate from a `*-security`
suite. When a newer candidate exists in `-updates`, apt installs that one, so
a security run can pull a non-security version of an affected package.

State for step 2 lives on the main host in
`~/.local/share/herdr-engineering/fleet-update-state.json` (keyed by host id,
0600, written once per run under a lock). `--check` reads the control plane
but writes neither the state file nor the event.

**Pins** (`playbooks/group_vars/all.yml`): `fleet_pins.herdr` reuses
`herdr_pinned_version`, the single herdr pin. `pi`, `autospec` and
`pi_extensions` are empty (unpinned) until set. Write each value as the
version number (`0.9.1`, not `herdr 0.9.1`): hosts report the version token of
`herdr --version`, `pi --version`, `autospec --version` and an extension's
package.json, and herdr-engineering itself as `herdr_eng` (shown, never
pinned). Pins are published so drift is judged against them. Enforced
today: herdr-eng and the build-gate extension at
`fleet_update_herdr_engineering_revision` (when set);
pi-engineering only when a revision is passed. The herdr/pi/autospec pins are
**report-only**: the existing install tasks only check presence, and the base
role clones pi-engineering and autospec at `main` (pre-existing). herdr-eng is
not updated by default (`fleet_update_herdr_engineering_revision` is empty).

**Maintenance.** Set `maintenance: true` for a host in private inventory
(`host_vars/<host>.yml`, see `inventory/hosts.example.yml`) to exempt it from
stale alarms. Remove it, or set it to `false`, to clear the flag on the next
run.

**Force one host** (operator decision; this overrides both "agents running"
and "busy state unknown"):

```bash
ansible-playbook -i inventory/hosts.yml playbooks/fleet-update.yml \
  --vault-password-file ~/.config/herdr-engineering/ansible-vault-pass \
  -e fleet_update_force=true -l beast
```

**Cron + Vault password file (main host).** `site.yml` installs the user
crontab entry `herdr fleet update` (daily 03:30):

```text
cd <repo>/ansible && PATH=... flock -n ~/.local/share/herdr-engineering/fleet-update.lock \
  ansible-playbook -i inventory/hosts.yml playbooks/fleet-update.yml \
  --vault-password-file ~/.config/herdr-engineering/ansible-vault-pass \
  >> ~/.local/share/herdr-engineering/fleet-update.log 2>&1
```

`flock -n` makes a run that would overlap a still-running one exit at once.
The cron line names the Vault password **file** and never holds a secret.
Ansible never creates that file. Create it once on the main host:

```bash
install -d -m 0700 ~/.config/herdr-engineering
( umask 077; read -rs -p 'vault password: ' p; printf '%s\n' "$p" \
    > ~/.config/herdr-engineering/ansible-vault-pass; unset p )
chmod 0600 ~/.config/herdr-engineering/ansible-vault-pass
```

`site.yml` warns when the file is missing or not mode 0600. When the control
plane runs with API keys, put an `ingest`/`operator` key in the Vault as
`fleet_update_api_key`. It is sent as `X-API-Key`, and every HTTP task is
`no_log`, so the key stays out of the log. Overridable paths are
`fleet_update_ansible_dir` (the main host's checkout, which must exist; if it
is missing the entry is skipped with a warning), `fleet_update_inventory`,
`fleet_update_vault_password_file`, `fleet_update_log_file`,
`fleet_update_lock_file` and `fleet_update_cron_path`.

**What the host page shows.** The pins, the fleet-update result and
"last updated", and these Problems: "Updates deferred N d" (deferred over 7
d), "Last updated N d ago" (over 14 d), "Version differs from pin: ..."
(versions vs pins), and none of the stale alarms while `maintenance` is true.

## Usage

`ansible/ansible.cfg` sets `roles_path` and the default inventory, so run from
the `ansible/` directory:

```bash
# install ansible-core (not present on the reference workstation until 2026-09-24;
# then: python -m pip install ansible-core)
cd ansible

# create private inventory from the example (add SSH connection vars)
cp inventory/hosts.example.yml inventory/hosts.yml

# dry run / check mode (no changes)
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --check

# converge one host
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --limit bender

# converge the whole fleet
ansible-playbook -i inventory/hosts.yml playbooks/site.yml
```

On hosts where every task is user-scoped and passwordless sudo is not
available (e.g. macOS), disable privilege escalation:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/site.yml \
  --limit mac -e herdr_engineering_become=false
```

A second converged run is idempotent (no unexpected changes).

### Validated (2026-09-24, ansible-core 2.21.4)

- `--syntax-check` passes for all inventory hosts.
- Full `--check` dry-run on `bender` (this host, `ansible_connection=local`):
  `ok=12 changed=6 failed=0`. The dry run caught and fixed three real bugs:
  `become` evaluated before fact-gathering, role path resolution, and state/
  config dirs being placed under root's home instead of the fleet user's.
- **Real convergence of `mac`** (macOS, over the tailnet, SSH as the fleet
  user, `herdr_engineering_become=false`): run 1 `ok=17 changed=8 failed=0`
  (Homebrew detected, vim/mc/btop/git/python@3.12 ensured, herdr 0.9.1 and
  pi 0.87.1 verified, both repos cloned, state/config dirs + LLM endpoint
  written); run 2 immediately after: `ok=17 changed=0 failed=0` — idempotent.
- **Real convergence of `beast`** (Ubuntu 24.04, over Tailscale SSH — one
  interactive browser check approval) and **`bender`** (local): each
  `ok=15 changed=6` then idempotent `changed=0`. The beast re-runs caught a
  real bug: with `become: true` the config `lineinfile` created the file
  root-owned (unreadable by the fleet user) — fixed in the playbook; the
  re-run itself repaired the live file.
- The live runs found and fixed five bugs in total (see
  `docs/evidence/0180/live-validation.txt`): a brew check that always
  "passed", forced `become` on hosts without passwordless sudo, a PATH
  clobber that hid `~/.local/bin` from every task (breaking herdr
  detection), a tailscale report that could never say "up", and the
  root-owned config file.

## Notes

- Live convergence is proven for `mac`, `beast` and `bender`. `fry` is not
  on the tailnet (absent from `tailscale status`) — blocked for that host
  alone. All three converged hosts have herdr 0.9.1 and pi 0.87.1. Caveat:
  on beast these live in `~/.local/bin`, which is absent from
  non-interactive SSH PATH — the playbook finds them via its play-level
  PATH, but a manual check must use `bash -lc` (a non-interactive
  `command -v herdr` reports MISSING and is wrong).
