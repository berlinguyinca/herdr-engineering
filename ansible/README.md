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
├── playbooks/
│   └── site.yml             # fleet bootstrap entry point
├── roles/
│   ├── herdr_linux/         # Debian/Ubuntu baseline
│   └── herdr_macos/         # Homebrew baseline
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
- Tailscale connectivity check (informational, non-fatal; no embedded auth keys);
- `vim`, `mc`, `btop`;
- HerdR Engineering state + artifact directories.

Existing Herdr/Pi/Tailscale config is merged/backed up, never overwritten
wholesale. No secrets live in the inventory or any committed file.

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
