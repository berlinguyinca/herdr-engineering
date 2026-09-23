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

```bash
# install ansible (not present on the reference workstation — documented blocker)
# python3 -m pip install ansible

# create private inventory from the example (add SSH connection vars)
cp ansible/inventory/hosts.example.yml ansible/inventory/hosts.yml

# dry run / check mode
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/site.yml --check

# converge
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/site.yml
```

A second converged run is idempotent (no unexpected changes).

## Notes

- The reference workstation does not have Ansible installed and cannot reach
  the remote validation fleet, so live convergence of `fry`/`beast`/`bender`/
  `macbook-m4` is a documented **external blocker** (acceptance rows marked
  `BLOCKED_EXTERNAL`). The playbooks are provided and are structurally
  idempotent; they are validated by review and by the CI config-schema job.
