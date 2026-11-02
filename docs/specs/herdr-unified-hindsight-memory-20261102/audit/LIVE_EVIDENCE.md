# LIVE_EVIDENCE — P00 audit (2026-11-02)

Mode: **local source audit + local host inspection**. No live Hindsight server was reachable from this host; nothing below was verified against a running service unless stated. Secrets were never read or echoed.

## Execution host
- `Gerts-MacBook-Pro.local`, Darwin 27.2.0 arm64, user `wohlgemuth`. Tailnet peers: `beast` (linux), `bender` (linux), `macbook-pro` (this host), iOS devices.
- Spec ZIP found locally at `~/Downloads/herdr-unified-hindsight-memory-20261102.zip` (no transfer needed). SHA-256 `0e90da34814b84c00fcdd6ce7fd3434b7ca619e14df039232e9ce3781a6a287f`; 10 entries, no absolute or `..` paths. Extracted to this directory in worktree `~/IdeaProjects/.worktrees/herdr-unified-memory` (branch `feat/unified-hindsight-memory`, from `origin/main` `daa44f4a`). The original checkout (`feat/llm-gateway-prototype`, untracked `personal-agent-system-20261030/`) was not touched.

## Resolved: `herdr-memory` ownership
- `berlinguyinca/herdr-memory` **exists and is private** (the connector 404 was an access artifact). `gh repo view` succeeds as `berlinguyinca`. Cloned to `~/IdeaProjects/.worktrees/repos/herdr-memory`; HEAD `c4e0c64` "feat: deploy private native Hindsight with verified backups"; version 0.1.1; no open PRs; single branch `main`.
- Not installed in this host's `herdr-engineering` venv (`ModuleNotFoundError: herdr_memory`); declared in no `pyproject`/lockfile of herdr-engineering. Only importer: `herdr_engineering/control_plane/rakazo_hindsight.py` (optional, lazy import).
- No `~/.config/herdr-memory/config.json` on this host; no memory/hindsight containers in local `docker ps`. **Live server health, version, bank inventory, token scopes: UNKNOWN from this host.** Remote hosts (`beast`, `bender`) not yet probed.

## Existing client facts (from source, `src/herdr_memory/{client,transport,cli}.py`, 639 LOC)
- Banks: exactly `<project>-facts` and `<project>-summaries`. **No global, infra, or mission scope.** No bank inheritance.
- Harness allow-list: claude, pi, codex, opencode (matches the supported set).
- Recall: `POST /memories/recall`, budget low, 2000 tokens, types world+experience; exact-query SQLite cache keyed by credential fingerprint; stale cache served when `Unavailable`; tombstone filter by `document_id`. Result is passed through raw: **no normalized memory_id/revision/status/provenance**.
- Retain: facts require `explicit=True` or evidence refs; metadata carries project/harness/kind/explicit/evidence only (**no host, session, mission, commit, PR**). Idempotency is by caller-supplied `document_id`; default is a random UUID (not evidence-stable).
- Write path: durable SQLite outbox, ordered, per-namespace flock, credential fingerprint pinning, rejected writes removed (not reported as success). Returns `state` of `queued|delivered|cancelled`. Cache cleared on any write/sync (correction invalidation exists, coarse).
- No supersession/revision: only `forget` (tombstone + DELETE); re-save of forgotten id is refused.
- Transport: HTTPS required off-loopback, redirects refused, 4 MiB response cap, 429/5xx => `Unavailable`; default timeout 5 s (Pi needs 250 ms; must be per-call).
- Deploy assets: `deploy/compose.yaml` (private native Hindsight, pinned image), backup service/timer, validation docs of 2026-10-26/28 (historical).

## Rakazo bridge (`herdr_engineering/control_plane/rakazo_hindsight.py`)
- Confirmed risk: `_submit` drops work on `queue.Full` (MAX_QUEUE=200) with only a log line, **before** the client's durable outbox. Also writes to the shared `fact` bank of the configured project. Tracked as a hardening item; personal memory must stay out of engineering banks.

## Pi (`~/pi-engineering`, `main` `d5d4ca7`, clean)
- `src/blackhole/OpenViking.ts` defines `DurableMemoryRecord` / `DurableMemoryProvider {kind, store, recallAll, search}` + `InMemoryDurableMemory`. Interface lacks bank/scope/revision/status/provenance fields; must extend compatibly.
- `src/blackhole/interactiveMemory.ts` (333 lines locally vs ~446 in the spec's audit; spec figure is stale) holds hooks, keyword match, TTL, bounded lookup.

## Harness surfaces on this host (versions verified)
| Harness | Version | Config present | Native surface observed |
|---|---|---|---|
| Claude Code | 2.1.308 | `~/.claude/settings.json` (has `hooks`) | hooks, `claude mcp` |
| Codex | 0.166.0 | `~/.codex/config.toml` | `codex mcp`; hooks gated by hook-trust (`--dangerously-bypass-hook-trust` exists) |
| OpenCode | 1.18.23 | `~/.config/opencode/` | `opencode mcp`, `opencode plugin` |
| Pi | 1.4.0 | n/a | extension seam via DurableMemoryProvider |
Exact hook event names per harness to be confirmed by P02 before any config edit.

## Herdr Engineering (`origin/main` `daa44f4a`)
- `scripts/install.sh` pins `HERDR_HANDOFF_REF=3051d8c`, installs handoff skills, 8 steps; no memory step. `herdr-eng` CLI (`herdr_engineering/cli.py`) has no `memory` command group. Handoff code: `control_plane/handoffs.py`, `handoff_lineage.py`; schemas dir has handoff schema. Dashboard: `packages/app.js` + view modules (JS tests alongside).
- Open PRs on herdr-engineering: #522 (ai-sync fabric status) only. Many local worktrees exist under `~/IdeaProjects/.worktrees/` (untouched).
- `herdr-fleet` HEAD `bee6f11`: plan/check only; no apply safety (consistent with spec). `herdr-handoff` HEAD `3051d8c` (matches the installer pin).

## Gaps vs. the spec (drive P01–P05)
1. No global/infra/mission banks; 2. no normalized record/provenance/revision; 3. no supersession; 4. outcome vocabulary differs (`delivered/queued/cancelled`); 5. per-call timeout; 6. Pi provider not Hindsight-backed; 7. no `herdr-eng memory` commands; 8. handoff packages carry no memory refs; 9. no dashboard memory route; 10. Rakazo pre-outbox drop; 11. herdr_memory not installed or pinned in herdr-engineering.

## Release readiness: **BLOCKED** for live cross-host verification — no reachable Hindsight endpoint or credentials configured on this host.
