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

## Redeploy — bender (2026-11-03 18:06Z, authorized by operator; mode: production smoke, verified)

- Control plane + memory redeployed from `origin/main` = herdr-engineering **`492445c9`** with `deploy.sh --with-memory`; exit 0, receipt `~/.local/state/herdr-engineering/deploy/492445c98a90.json`, log `~/.local/state/herdr-engineering/deploy-memory-20261103b.log`.
  - **Failure mode found:** the first attempt died in the memory image step with `uv: command not found` — `uv` lives in `~/.local/bin`, which a non-interactive ssh shell does not put on `PATH`. The deploy auto-rolled back cleanly (rollback target was the already-running `492445c98a90`, no downtime beyond the recreate). **Always `export PATH=$HOME/.local/bin:$PATH` before `deploy.sh --with-memory` over ssh.** Also requires the herdr-memory clone at `~/.local/share/herdr-engineering/herdr-memory` (now present, gh-authed).
  - Log tail: `ok: control-plane-web control-plane-worker llm-proxy collab-public collab-edge share-public running 492445c98a90`; `memory: building herdr-memory-hindsight:5a672f0c… from herdr-memory c4e0c64 (deploy/build.sh)`; `ok: memory running (hindsight-db, hindsight-api on 127.0.0.1:8897)`.
- #560 (installer) and #563 (explorer) code is live on bender as of this deploy.

## Post-deploy re-verification (2026-11-03T18:23Z, live on bender; mode: production smoke)

- `herdr-hindsight` Up (healthy) `herdr-memory-hindsight:5a672f0cdb89…`, `herdr-hindsight-db` Up (healthy) `pgvector/pgvector`, `herdr-cp-web` on `herdr-engineering:492445c98a90`, `herdr-fabric-gateway` Up (healthy).
- `http://127.0.0.1:8897/health` = **200**; tailnet `http://100.104.39.6:8897/health` = **refused** (the `fabric.agent_exclude_ports: [8897]` exclusion survived the redeploy); `https://memory.local.metabolomics.us/health` = **200** from the Mac.
- Real P01 client on bender, `herdr-memory --project canary --harness pi status` → `{"healthy": true, "pending": 0, "outbox_pending": 0, "dead_letter": 0, "contract": 1}`.

## Pi provider (pi-engineering) — merged + locally re-verified (2026-11-03)

- **pi-engineering #559 MERGED 2026-11-03T18:09:16Z** (`1a86b883`, main = `06d0dbc3`). The three defects the mocked suite hid, all found by running the real provider against the live bender server (mode: real cross-host at provider level):
  1. harness defaulted to `codex` for both credential selection and provenance → now `HERDR_HARNESS=pi` unless the caller overrides;
  2. `retain` requires `--evidence` flags — refs nested only in `--provenance` do not count, so *every* Pi retain was being rejected → each `evidenceIds` entry is now passed as its own `--evidence`;
  3. `--timeout-ms` is accepted only by `recall`/`get`; on `retain`/`correct`/`forget` argparse rejected the whole call → the flag is now action-gated (`TIMEOUT_FLAG_ACTIONS`).
- After the fix, against the live server: retain persisted, search recalled, candidate and secret-bearing records blocked client-side, `forget` ok. Synthetic canary memories were forgotten afterwards.
- Targeted memory suite re-run on the Mac in `.worktrees/pi-harness-id` (`fix/hindsight-harness-id` = merged content), deps installed: **82 pass / 1 fail / 0 skipped** across `hindsight-memory`, `hindsight-harness-env`, `interactive-memory`, `memory-setup`, `blackhole-memory-workers`, `interactive-memory-cache`, `everyday-memory` (mode: local integration, macOS). The single failure is `interactive-memory-cache.test.ts:9` and is the known Linux-only artifact-integrity guard (`ARTIFACT INTEGRITY: secure descriptor-relative artifact access requires Linux`, `src/artifacts/ArtifactStore.ts:86`) reached through `getRuntimeByCwd` — environment, not memory behaviour.
- **Was open, now closed at session level** (see "Pi session-level acceptance" below): recall injection in a real session, promotion through `/remember`, the 250 ms automatic budget, and untrusted-data handling are all now measured in real Pi sessions against the live server.
- Note: the Pi provider passes no `--project`, so cwd must map to a project via `paths` in `~/.config/herdr-memory/config.json`. The Mac config currently carries a temporary `paths` entry for `.worktrees/pi-harness-id` (remove once real harness config replaces the canary).

## Pi session-level acceptance (2026-11-03, Mac ↔ bender, real sessions)

Mode: **real cross-host, real Pi sessions** (`pi 1.3.0 --no-extensions --mode json`,
`PI_HINDSIGHT_ENABLED=1`, real `herdr-memory` CLI, live bender server). Report:
`audit/PI_SESSION_LIVE_20261103.md`; raw artifacts in `audit/evidence/pi-session-20261103/`.
A temporary `paths` entry mapped a throwaway fixture cwd to the `canary` project; five synthetic
memories were seeded through the real CLI (one a deliberate prompt-injection lure) and all five were
forgotten afterwards (recall returns 0, `outbox_pending 0`, `dead_letter 0`); the fixture dir and the
temporary config entry were removed and the config verified identical to its backup apart from that entry.

| Check | Result |
|---|---|
| Recall injected into a real session context | **pass** — five notes reached the model with id / scope / revision / status / sourceRefs under the Hindsight untrusted-data header |
| Retrieved memory handled as untrusted data | **pass** — the model quoted the lure, named it a prompt-injection attempt, made **zero tool calls** in every run |
| `/remember` → `retain` → server | **pass on merged pi#559 code** (`mem-80bd8b17…`, rev 1, `verified`); **fail on the runtime Pi actually runs today** (D1) |
| 250 ms automatic budget | **pass** — a provider sleeping 5 s was cut at 250 ms and the turn continued without memory; a missing CLI returns in 1 ms |
| Fail-open on outage | **pass** — missing CLI and server pointed at `https://127.0.0.1:1` both completed normally, no notes, no crash |
| Harness identification | **pass** — server row metadata records `"harness": "pi"` |

Defects found only by running the real thing:

- **D1 (high, operations)** the runtime Pi actually loads — `~/.pi/pi-engineering/current →
  versions/0.2.0-2dbef666d458`, commit `2dbef666`, the parent of `1a86b883` — **predates pi#559**, so live Pi
  sessions still lose every memory write. Captured argv: `retain … --timeout-ms 5000` with no `--evidence`,
  CLI exit 2, session reports "Memory save was rejected or not confirmed". Remedy: install/pin a runtime
  built from main (`≥ 06d0dbc3`); this is a deploy action, not a code change.
- **D2 (medium, methodology)** `pi -e <checkout>` does **not** run that checkout: `RuntimeHost.startupSources()`
  (`src/runtime/host/extension.ts:978`) prefers `PI_ENGINEERING_RUNTIME_SOURCE`, then installed `current`, then
  `previous`, then the package checkout. Session-level verification must set `PI_ENGINEERING_RUNTIME_SOURCE` (or
  install+pin) and confirm via CLI argv shape, otherwise it silently tests an older runtime.
- **D3 (medium)** `recallAll()` issues `recall "*"`; the live server rejects that with **HTTP 422**, so the
  session-start "N saved notes" path can never return anything (it fails open to zero). Previously listed only
  as a design assumption; it is confirmed broken.
- **D4 (low)** harness attribution is written (`metadata.harness`, `harness:<name>` tags) but dropped by the CLI's
  normalized `results`, so acceptance #2's "which harness touched which memory" cannot be answered from the read side.
- **D5 (low)** headless failure notices are host-blind: `firstNoticeToday()` hashes `new URL(baseUrl).host`, and the
  synthetic Hindsight baseUrl (`hindsight:<cli>:<scopes>`) parses with an empty host, so every host shares one
  marker named `e3b0c44298fc1c14-<date>` (observed on this Mac).
- **D6 (medium, headroom)** live cross-host recall measures **214–231 ms against the 250 ms automatic budget** —
  injection is already marginal, and any slower path silently drops memory with no degraded signal in headless runs.

Still unproven at session level: real Claude Code / Codex / OpenCode sessions (acceptance #2 remainder),
acceptance #4 `correct`/`--as-of` cache invalidation, #5 crash/queue/outbox replay, #10 dashboard, #12 benchmark.

## 2026-11-03 (evening) — adapters installed on both hosts, D1 closed

Full evidence: [`audit/HARNESS_INSTALL_20261103.md`](audit/HARNESS_INSTALL_20261103.md).

- **D1 closed.** The failure was bigger than a stale runtime: real Pi sessions load the runtime named
  in `~/.pi/agent/settings.json` → `packages`, which pointed at
  `~/.local/share/herdr-engineering/pi/releases/4b89237…` — a build with **no `hindsight.ts` at all**,
  so `PI_HINDSIGHT_ENABLED` was inert. Pinned to `~/.pi/pi-engineering/versions/0.2.0-06d0dbc3784f`
  (backup `settings.json.bak-d1-20261103T191741Z`). A plain session with no dev overrides now emits
  `session_start` `recall *` + `before_agent_start` recall, and `/remember` → `retain … --evidence`
  with no `--timeout-ms`, persisted server-side (`mem-87ebf153…`, cleaned up after). Fleet equivalent:
  `herdr-eng fleet rollout --pi-revision <40-char sha>`.
- **Acceptance #2 met** on both hosts: Mac `pi`/`claude`/`codex`/`opencode` and bender
  `pi`/`claude`/`codex` (`opencode` binary absent there → skipped), all `current` on re-run, existing
  MCP servers and Claude hooks preserved. Mac `herdr-eng` had to move off an editable install of a
  feature branch to `origin/main` (#560); bender's `herdr-eng` predates `memory`, so adapters point at
  a side venv `~/.venvs/herdr-eng-main` (production venv untouched).
- **Real sessions:** Codex and OpenCode on the Mac each recalled the seeded fact and wrote through
  `memory_retain` (`mem-79a31670…`, `mem-c929a20a…`); Pi proven above. bender wrote `mem-c95cff3b…`
  which a Mac recall returns — shared cross-host memory. Live Claude Code (both hosts) and bender
  Codex sessions are **provider-quota blocked** (Codex 11-04 06:40 PT, Claude 11-06 10:00 PT), so #4 is
  met via Pi/Codex/OpenCode + cross-host reads but not yet for a live Claude session.
- **New defects D7-D12** (detail in the audit): a *different* `herdr-memory` build labelled `0.1.1`
  was installed on bender with no `retain`/`--scope`/`--evidence`, silently breaking every adapter
  there (fixed by installing the wheel built from herdr-memory `b3c5074`; undetectable through
  `--version`/`doctor`); `mcp-serve`/`hook` resolve the client by PATH (`memory_mcp.py:81`, `:146`) and
  the hook runner exits 0 silently when the lookup fails; there is no default-project fallback at all,
  so unmapped directories get a hard error instead of memory; MCP retains
  carry no provenance.
- **Still open:** #5 failure drill (needs authorization), #10 dashboard, #12 benchmark,
  #4 `correct`/`--as-of` invalidation, and the D3 `recall "*"` HTTP 422 fix in Pi.

## 2026-11-04 — durability/failure drills (acceptance #5) and auth states (#6)

Full evidence: [`audit/DRILLS_20261104.md`](audit/DRILLS_20261104.md). All commands ran on `bender`
against the live stack (Hindsight `0.5.0`); nothing here is mocked.

- **#5 run at container level:** outage (stop/start), SIGKILL of the memory container, bad model
  directory, queue pressure (10 then 30 concurrent writes against a 2-slot worker pool), invalid
  credential. Fail-open held inside Pi's budget (`recall-context.ts` 112 ms / 0 bytes during the
  outage); 20 writes acked `queued_durable` survived SIGKILL and replayed **exactly once** (20/20,
  no duplicates); queue pressure drained to `pending: 0, dead: 0`; with the model asset absent the
  server came up `unhealthy`, reads failed open, writes queued, and everything delivered after the
  asset was restored.
- **#6 was broken on the installed build:** a refused credential returned exit 1 with no degraded
  shape, kept serving cached rows, parked writes into `dead_letter`, while `status` and
  `herdr-eng memory doctor` still reported `healthy` / `OK server reachable` (D13, D14, D16, D18).
- **Fix: [herdr-memory#3](https://github.com/berlinguyinca/herdr-memory/pull/3)** (client `0.1.2`) —
  health now requires an accepted credential; 401/403 became `Rejected` and is never queued; reads
  return the degraded fail-open shape and clear the refused scope's cache; `recall`/`status` drain
  the outbox so recovery needs no hand-run `sync` (D13, D14, D15); `--version` reports client **and**
  contract (D18). `69 passed, 4 skipped, 20 subtests passed`; the 10 new tests fail on `origin/main`
  (`8 failed, 2 passed`) and pass on the branch; GitGuardian green. **Merged** (squash `3e23abf`) and
  promoted: `0.1.2` is the production client on both hosts — bender
  `~/.local/share/uv/tools/herdr-memory/bin/herdr-memory` and the Mac
  `~/.local/bin/herdr-memory` → `~/.venvs/herdr-memory`, each reporting
  `{"client": "0.1.2", "contract": 1}`. Re-run #6 on the promoted build passed live - see
  `audit/DRILLS_20261104.md` §11 (D13, D14, D15, D16, D18 closed on production).
- **Fix: [herdr-memory#6](https://github.com/berlinguyinca/herdr-memory/pull/6)** (client `0.1.3`) —
  outbox ownership moved from transport URL to **credential fingerprint + row provenance**, with
  previously seen addresses in an `aliases` table and `status` now reporting `outbox_total` /
  `outbox_orphans` (D19). `74 passed, 4 skipped, 20 subtests passed` on the branch and again on the
  merged `main`; GitGuardian green. **Merged** (squash `89a8529`) and promoted: `0.1.3` is the
  production client on both hosts — `herdr-memory --version` returns
  `{"client": "0.1.3", "contract": 1, "harness": "codex"}` on bender
  (`uv tool install`, `~/.local/share/uv/tools/herdr-memory`) and on the Mac
  (`~/.venvs/herdr-memory`, `~/.local/bin/herdr-memory`). The address-change drill passes on the shipped
  build: queued while the address was wrong it reported `pending: 1, outbox_total: 1,
  reason: unreachable`, and one plain `status` after correcting the address adopted and drained it
  (`pending: 0, outbox_total: 0`, `acknowledgements` 221 → 222, the note recallable). See
  `audit/DRILLS_20261104.md` §11.1 (D19 closed on production; the ledger half is **not** closed).
- Production was never mutated for the fix proof: the candidate build ran from
  `~/.venvs/herdr-memory-fix` only. Config restored byte-for-byte from the drill backup (verified),
  `/tmp/hm-drill` deleted, `herdr-eng memory install` re-run (`noop ... [current]` for
  pi/claude/codex; `opencode` skipped, binary absent), client back to
  `{"pending": 0, "dead_letter": 0, "healthy": true}`.
- **Open from these drills:** **D17** — writes in flight at SIGKILL were removed from the durable
  outbox during replay with no `dead_letter`/`delivered` record (20 writes silently), so "never
  delivered" is indistinguishable from "delivered"; needs attempt accounting. Also still unrun: the
  Pi-tool-surface latency and concurrent multi-harness halves of #5.
- **Found while re-running the drills:** **D19** — `identity`/`namespace` hash the configured
  `base_url`, so correcting a server address hid queued writes from `pending`, `sync` and
  `dead_letter` (a production row sat in `outbox` while `status` said `pending: 0`). Fixed by
  [herdr-memory#6](https://github.com/berlinguyinca/herdr-memory/pull/6) (credential + provenance
  ownership, `aliases`, `outbox_total`/`outbox_orphans`); the stranded row was adopted and delivered
  by the PR build, and the fix is verified on the promoted `0.1.3` (§11.1). The ledger half of the same
  issue - `verified` claimed before the server acknowledges - is **not** fixed by #6, and
  [#5](https://github.com/berlinguyinca/herdr-memory/issues/5) was **reopened** after its auto-close with
  the live probe: on `0.1.3` a write whose outbox row is physically present (`outbox: 1`,
  `pending: 1, healthy: false, reason: unreachable`) is still stored as
  `('mem-785082e8…', 1, 'verified')` (`client.py:410` derives the ledger status from intent alone).
  **D20** - contract-v1 recall results can omit `document_id` (only `memory_id`), so a record cannot
  be forgotten by the client that wrote it; drill cleanup needed raw `GET /documents` + `DELETE`
  ([herdr-memory#8](https://github.com/berlinguyinca/herdr-memory/issues/8)). Config hygiene was also
  found inert: a hand-authored `projects.canary.base_url` on bender
  (`https://bender.internal/bank`, which does not even resolve - `curl` -> `000`) is read by nothing in
  the client, so an address edited there is a silent no-op
  ([herdr-memory#9](https://github.com/berlinguyinca/herdr-memory/issues/9)).
- **Operational risk on bender:** `herdr-eng-fleet-update.service` exits 1 on every daily run (journal:
  Nov 1, 2, 3 at 11:59 — `controller checkout is dirty; preserve local work before updating`) while its
  timer stays `active/waiting`, so unattended fleet updates are not applying. Verified separately:
  `~/.venvs/herdr-engineering/bin/python` cannot `import herdr_memory`, while the CLI client lives in
  `~/.local/share/uv/tools/herdr-memory` — which is why `herdr-eng memory doctor` reports
  `UNKNOWN package_revision` and why the in-process Rakazo memory mirror cannot activate. A dirty
  checkout must be a reported skip, not a hard failure (herdr-engineering#572); while it fails, a manual
  client upgrade cannot be trusted to survive the next cycle — `0.1.2` and `0.1.3` were both installed by
  hand on both hosts.

Filed for tracking: herdr-engineering#570 (D8 runtime PATH resolution + silent hook failure), #571
(D10 `doctor` cannot see server health/revision outside a mapped project, with the mapped/unmapped
comparison above), #572 (D9 daily fleet-update failure + herdr-eng env without `herdr_memory`),
herdr-memory#4 (D17 attempt accounting after SIGKILL), #5 (D19 URL-keyed queue ownership — **reopened**
for the ledger half after #6 closed the visibility half), #8 (D20 records without `document_id`) and #9
(config key `base_url` accepted but never read). Duplicates #573/#574/#575 were closed into
#570/#571/#572; their original bodies were **withdrawn** (written from assumed code paths, incl. a
module that does not exist) and replaced with reproduced command output, with correction comments on
all six issues.

## Update — memory became reachable from where the harness works (2026-11-04, later)

Answering "what does Pi remember?" exposed the blocking defect: every `herdr-memory` call from
an unmapped directory - which is where sessions actually run - returned
`Current directory has no configured project`, so recall was unreachable rather than empty. The
documented `default_project` fallback had never been implemented
([herdr-memory#10](https://github.com/berlinguyinca/herdr-memory/issues/10), fixed in
[herdr-memory#11](https://github.com/berlinguyinca/herdr-memory/pull/11), client `0.1.4`,
promoted to bender and the Mac; pin now `685cf07` from #580). `doctor` likewise now resolves a
project **and** a credential before probing health (#579 then #580, which also removed the false
`FAIL server` that #579 introduced - D22 in
[`audit/DRILLS_20261104.md`](audit/DRILLS_20261104.md) §13).

With that, durable write/read works from anywhere: five evidenced records (`herdr-global`,
`canary-facts`) are recalled with `degraded: false` from `~/IdeaProjects` on the Mac and from
`/tmp` on bender, including a record written on one host and read on the other. The retention
gate earned its keep - `retain` rejected every write until `--explicit`/`--evidence` were given.
Full evidence: [`audit/DRILLS_20261104.md`](audit/DRILLS_20261104.md) §14, including the one
operator action still open (bender's controller checkout is 41 tracked files dirty at
`06e3d501`, so unattended updates will skip it, not fail it, until it is cleaned).
