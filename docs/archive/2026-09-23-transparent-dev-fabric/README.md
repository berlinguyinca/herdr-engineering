# Source lineage — Super HerdR Transparent Dev Fabric (2026-09-23)

Requirements folded into normative spec 0110:

- dozens of concurrent agents may launch independent web servers from isolated worktrees;
- existing launch skills already select free local ports;
- users should not manage dozens of per-agent hostnames;
- expose one stable logical `dev` service with distinct leased ports;
- routes must work across `fry`, `beast`, `bender`, `macbook-m4` and future hosts;
- the same address must work from laptop, phone and iPad on Tailscale;
- support HTTP, WebSocket, SSE/HMR and declared TCP protocols;
- leases are cluster-wide, atomic, recoverable and cleaned after crashes;
- use Ansible and private-by-default networking;
- tests must cover collision, stale state, reconnect and protocol behavior.
