# 0040 — Fleet Bootstrap and Ansible

## Goal

Make a supported machine reproducibly ready for the HerdR Engineering ecosystem without ad-hoc SSH configuration.

## Validation inventory

Initial names: `fry`, `beast`, `bender`, `macbook-m4`. Treat these as inventory examples, not product constants.

## Managed baseline

Declaratively ensure, preserving user config:

- Herdr at the tested/pinned compatible version;
- Pi;
- Pi provider configuration pointing all lab LLM work at `https://llm.metabolomics.us`;
- `berlinguyinca/pi-engineering`;
- `berlinguyinca/autospec`;
- Tailscale presence/connectivity checks (do not embed reusable auth keys in Git);
- `vim`, `mc`, `btop`;
- prerequisites for adopted Powerpack components;
- directories/permissions for HerdR Engineering state and artifact cache.

## Platform behavior

- Linux and macOS roles may differ internally but expose the same resulting capabilities.
- Existing Herdr/Pi/Tailscale config must be merged/backed up, not overwritten wholesale.
- No secrets in inventory or repository.
- `--check`/dry-run support where practical.
- Second converged run must be idempotent.

## Exit gate

All four validation hosts converge, a second run produces no unexpected changes, and a new example host can be added through inventory alone.
