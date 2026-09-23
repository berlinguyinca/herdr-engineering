# HerdR Spec Genealogy

This index answers two different chronological questions:

1. **When did we decide each requirement?** — historical chronology below.
2. **In what order should it be implemented?** — dependency chronology in `SPEC_MANIFEST.yaml` and `docs/implementation-roadmap.md`.

## Historical source chronology

| Date | Source | Core requirements | Current normative destination |
|---|---|---|---|
| 2026-09-20 | Pi Engineering ↔ Herdr runtime pack | external Herdr runtime, `AgentRuntime`, structured worker events/results, worktree isolation, artifact-first bounded communication, recovery/canary/rollback, Pi-Web external, Pi Forge excluded | 0030, 0090, 0100, 0170 |
| 2026-09-21 | Distributed HerdR fleet | thin cockpit, persistent remote sessions, Tailscale, Ansible, four validation hosts, lab Pi endpoint | 0030, 0040, 0060, 0170 |
| 2026-09-22 | HerdR Powerpack v1/v2 | audited plugins, browser/mobile/Plannotator, worktrees, GitHub/PR, notifications, shared workspace, per-agent client panel/timeline, summaries, lifecycle/security | 0020, 0050–0100, 0160–0180 |
| 2026-09-22/23 | Transparent dev fabric | one `dev` name, cluster-wide ephemeral port leases, existing random local ports, mobile access, WebSocket/SSE/HMR/TCP, crash recovery | 0110, 0140, 0170 |
| 2026-09-23 | Tests + CI workspace | IDE-style test tree/logs/traces for Python/Go/Rust/Java/Scala, Woodpecker/Pileated pipelines/queues/runners, CI→Pi debug handoff | 0120, 0130, 0150–0170 |
| 2026-09-23 | Master HerdR Engineering program | one canonical repo, explicit dependency gates, current upstream-native Herdr simplifications, full release path | 0000–0180 |

## Superseded assumptions

- Planned separate `berlinguyinca/herdr-powerpack` repository → folded into `berlinguyinca/herdr-engineering` because implementation had not started and later requirements span much more than plugin packaging.
- Custom remote-machine aggregation layer → removed because current Herdr provides native saved SSH machines and machine-targeted operations.
- Per-agent human-managed hostnames for previews → replaced by one stable `dev` service name plus leased ports.
- Shared filesystem as a possible source workspace → explicitly narrowed to artifacts/handoffs; Git worktrees stay source truth.
- Terminal scraping as a general observability mechanism → replaced by structured native/API events whenever available.

## Archived material

- `docs/archive/2026-09-20-pi-engineering-herdr-runtime/`
- `docs/archive/2026-09-21-distributed-fleet/`
- `docs/archive/2026-09-22-powerpack-v2/` — exact recovered predecessor bundle files
- `docs/archive/2026-09-23-transparent-dev-fabric/`
- `docs/archive/2026-09-23-testing-ci/`

Archived content is provenance only. Current numbered specs win when requirements conflict.
