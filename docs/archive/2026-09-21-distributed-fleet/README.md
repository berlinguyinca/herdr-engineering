# Source lineage — Distributed HerdR Fleet (2026-09-21)

This discussion established the thin-client operating model:

- the control laptop/phone is a cockpit, not the compute host;
- remote Herdr sessions continue after the client disconnects;
- Tailscale supplies the private mesh;
- work remains on the machine with its repository, credentials and processes;
- HerdR is not a GPU/model scheduler;
- initial validation hosts are `fry`, `beast`, `bender`, `macbook-m4`;
- fleet software/config should be reproducible through Ansible;
- all lab Pi inference routes through `https://llm.metabolomics.us`.

Later upstream verification showed current Herdr already provides saved multi-machine SSH connectivity and machine-targeted operations, superseding the earlier possibility of a custom aggregation layer.
