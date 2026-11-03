# STATUS — 2026-11-02

**Release readiness: BLOCKED (canary not run).** A Hindsight stack is now deployed on `bender` (2026-11-02, see Deployment) but no client credentials are exported, no cross-host verification has occurred, and the canary checklist has not been run. **All results below are MOCKED / local unit tests unless stated.** Nothing is merged or deployed.

| Phase | PR | Commits | Tests (mocked) | State |
|---|---|---|---|---|
| P00 audit | this branch `feat/unified-hindsight-memory` | 56ae9256 | n/a | done; `audit/LIVE_EVIDENCE.md`, `CONTRACT.md` |
| P01 client | herdr-memory#1 | 3501dd2 4a22fda a014489 813bf23 087bdfe | 57 pass / 4 skip (live integration) — re-run by orchestrator | open |
| P01b Pi provider | pi-engineering#556 | 16a43722 86789116 65023098 6bcc7345 | 18 new pass; 142 targeted pass; full unit suite has 775 macOS-env failures, no baseline diff | open |
| P02 installer | herdr-engineering#560 | 8863390f | 19 pass (re-run); 5+1 failures also on main per agent | open |
| P03 handoff | herdr-handoff#1, herdr-engineering#562 | 65bd45c, 3ff6c7ea | 27 pass (re-run); 43 targeted pass | open; conformed to real P01 CLI (handoff c94c85c, control-plane 0c2b2b4b); 30 pass incl. 2 real-CLI tests vs a fake Hindsight server |
| P04 Explorer | herdr-engineering#563 | 2f44a878 cb5d52ba 33ce4ba2 1b491a94 | 13 API pass (re-run in venv), 1046 JS, 1 Playwright; mocked data | open |
| P06a fabric integration | herdr-engineering#564 **MERGED** 562eb9cd | cf586348..3d3145fc | 121 new tests pass (mocked); Linux CI unit-gates green | merged + deployed to bender |
| P05 intelligence | — | — | — | NOT STARTED (needs reliable P01 on a live server) |
| P06 canary | — | — | — | NOT STARTED (needs authorization + endpoint) |

## Known limitations / open items
- Hindsight has no native revision history: `correct` overwrites the document, supersession is metadata + local ledger; `--as-of` cannot reconstruct replaced text. Acceptance #4 only partly met. Assumed endpoints `GET /documents/{id}` and `GET /health` are unverified live.
- Automatic lifecycle: only Claude has native hooks (recall injection); Codex/OpenCode/Pi are MCP/skill fallback; no harness retains automatically.
- Pi promotion flow does not specially handle `queued_durable`; `recallAll()` uses `recall "*"`.
- Install path for the private `herdr-memory` package is undecided; lock pin `c4e0c64` must move to the P01 merge commit.
- Dashboard integration/retrieval-history panels show UNKNOWN (contract v1 has no data). P04 assumes `recall --project` and a `get` shape not in P01 — to verify.
- Rakazo pre-outbox queue drop (`rakazo_hindsight.py`) not yet fixed.
- Handoff CLI usage fixed to match P01 (2 real-CLI tests; Hindsight server still a local fake).
- Cross-host cache invalidation for shared global/infra memory only on reconnect.
- Security: a Context7 API key was printed to an agent's tool output (not persisted); rotation recommended.

## Required to unblock release
1. A reachable Hindsight deployment + scoped credentials (hosts `beast`/`bender` unprobed), authorization to run a single-host canary.
2. Live run of acceptance #2–#6 across two hosts.
3. Review and merge order: herdr-memory#1 → pin bump → #560/#562/#563/handoff#1/pi#556.

## Deployment — bender (2026-11-02, authorized by operator; option 3: merge #564, fresh stack, old candidate untouched)
- Control plane redeployed from origin/main `562eb9cd` via `deploy.sh --with-memory` (exit 0, receipt written, all services healthy; dashboard health 200 at 100.104.39.6:80).
- New stack `herdr-hindsight` + `herdr-hindsight-db` healthy; `/health` 200 on 127.0.0.1:8897; bad token -> 401. Backup timer `herdr-hindsight-backup.timer` active. Fleet config backup: `fleet-backups/fleet-config.yaml.bak-20261102T235913689490Z`.
- Old candidate `herdr-memory-api-1/-db-1` (port 8888, 6 daily dumps) untouched; not migrated.
- **Open:** the fabric agent (`herdr-eng devfabric agent`) mirrors 127.0.0.1:8897 onto the tailnet IP (plain HTTP, bearer-auth enforced). Consider `fabric.agent_exclude_ports: [8897]`. The `memory.dev.lan` gateway route is OFF (port in config is 8888; set to 8897 then `fabric-stack.sh memory-route enable` + gateway recreate). No tenant keys exported to clients; canary checklist, doctor, restore test and cross-host acceptance not run.

## Canary + exposure (bender, 2026-11-02, live)
- Fabric agent no longer mirrors 8897 onto the tailnet (`fabric.agent_exclude_ports: [8897]` in `~/.config/herdr-engineering/config.yaml`; the stale forwarder entry was also removed from agent state, since persisted forwarders are restored on restart). Loopback only; tailnet `:8897` refused.
- Gateway routes `memory.local.metabolomics.us` and `memory.dev.lan` -> `127.0.0.1:8897` added to the production gateway config (`gateway-deploy/deploy/fabric-stack/config.yaml`, edited in place, backup `.bak-*`) and `docker restart herdr-fabric-gateway` (same flags). `https://memory.local.metabolomics.us/health` = 200 from the Mac (public cert); `memory.dev.lan` = 200 on bender (private CA; not resolvable/trusted from this Mac).
- Live canary through the HTTPS route with the real P01 client (herdr-memory 83c5b64, venv on bender), per-tenant keys exported 0600 (project/global/infra; `personal` not exported): retain persisted; paraphrase recall hit; global/infra do not see project memory and vice versa; no/bad bearer 401; global key on the project bank 404; project key 200; data survives api+db restart; backup + `restore-test` pass (100 tables); `doctor` memory checks all ok; db has no host port.
- NOT run: bad-`model_dir` failure drill (would touch the production deploy), second-host acceptance (#2-#6 across hosts, real Claude/Codex/OpenCode/Pi sessions), P05.
- Merged: herdr-memory#1 (83c5b64), herdr-handoff#1 (92fb4fb), herdr-engineering#562, pi-engineering#556. #560 (client pin moved to 83c5b64, conflicts resolved) and #563 (explorer fixed to the real CLI: top-level `--project`, per-scope `get`) waiting on CI.

## Cross-host verification (2026-11-03, live, Mac <-> bender)
- Mac client (herdr-memory 83c5b64, own venv, scoped keys scp'd 0600) retained a project fact through `https://memory.local.metabolomics.us`; bender recalled it (paraphrase query, rev 1); bender `correct` produced rev 2 (`supersedes_revision: 1`); the Mac then recalled rev 2 / `verified` with the corrected text. Test memories forgotten afterwards.
- **Bug found:** `herdr-memory get <id>` on a host that never saw the memory reports `revision: 1`, `status: candidate`, `kind: unknown` even when the server holds rev 2 (revision/status come from the local ledger, not the server). Recall is correct. Affects the Memory Explorer detail view on such hosts. Needs a fix in herdr-memory (read revision/status/kind from server metadata).
- Unit-gates CI on #560 failed on one test (`test_credential_permissions`): it depended on the host umask (CI umask 077 masked `mkdir(mode=0o755)`). Test fixed (explicit chmod); CI rerunning.
