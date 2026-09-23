# Health / Doctor / Ansible

Doctor reports:
- HerdR/Powerpack/plugin versions and health
- browser
- mobile/web
- shared filesystem mount/sync/capacity
- file viewer
- session event pipeline
- summary freshness
- Plannotator
- swarm/worktrees
- GitHub
- notifications
- unsafe listeners
- platform degradation

Provide human and machine-readable output.

Ansible:
- install prerequisites/HerdR
- install pinned Powerpack
- configure selected shared-filesystem backend
- reconcile
- doctor/smoke tests
- idempotent Linux/macOS support

Use fry, beast, bender and macbook-m4 as current validation targets where available, but never hard-code hostnames in Powerpack. Headless nodes degrade GUI capabilities gracefully. Do not disturb existing Pi/InferWeave configuration.
