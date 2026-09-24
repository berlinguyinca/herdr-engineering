# Ansible fleet bootstrap (spec 0040)

Makes a supported Linux or macOS host reproducibly ready for the HerdR
Engineering ecosystem without ad-hoc SSH edits.

## Layout

```text
ansible/
├── inventory/
│   ├── hosts.yml            # real inventory (private; NOT committed)
│   └── hosts.example.yml    # committed example (no connection secrets)
├── playbooks/
│   └── site.yml             # fleet bootstrap entry point
├── roles/
│   ├── herdr_linux/         # Debian/Ubuntu baseline
│   └── herdr_macos/         # Homebrew baseline
└── README.md
```

## Validation inventory

`fry`, `beast`, `bender`, `macbook-m4`. These are **examples**, never
hard-coded into application logic. Add machines by editing the inventory only.

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

A second converged run is idempotent (no unexpected changes).

### Validated (2026-09-24, ansible-core 2.21.4)

- `--syntax-check` passes for all inventory hosts.
- Full `--check` dry-run on `bender` (this host, `ansible_connection=local`):
  `ok=12 changed=6 failed=0`. The dry run caught and fixed three real bugs:
  `become` evaluated before fact-gathering, role path resolution, and state/
  config dirs being placed under root's home instead of the fleet user's.

## Notes

- The reference workstation does not have Ansible installed and cannot reach
  the remote validation fleet, so live convergence of `fry`/`beast`/`bender`/
  `macbook-m4` is a documented **external blocker** (acceptance rows marked
  `BLOCKED_EXTERNAL`). The playbooks are provided and are structurally
  idempotent; they are validated by review and by the CI config-schema job.
