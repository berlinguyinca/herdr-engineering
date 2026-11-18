# Private handoff: finish HerdR mission delivery release

**Superseded stop state:** user resumed work in Codex here on2026-10-28.
Do not start a second Pi implementation from this historical handoff. Current
work remains in the recovery worktree; consult its progress ledger and live Git
state. The local queued Pi package is being cancelled to avoid duplicate ownership.

Prepared 2026-10-26 on bender. User explicitly requested STOP and handoff to Pi.
Implementation is paused, NOT complete. No new core release was deployed during
this stop/handoff. Resume only when the user starts Pi with the instructions below.

## Start here in Pi

```bash
cd /home/wohlgemuth/IdeaProjects/herdr-delivery-finish-recovery
pi
```

Paste this as the first message:

> Read /home/wohlgemuth/IdeaProjects/herdr-mission-ui-wt/PI-HANDOFF.md in full and the applicable AGENTS.md. Resume the existing delivery completion task. Preserve all worktrees and uncommitted work. First address the HIGH checkpoint resurrection finding and its real PostgreSQL regressions, then resolve suite failures, verify faithful production-scale restart, review and merge the pinned changes, deploy, update the fleet, and configure feed-pi. The previous user authorized these actions; do not ask again for ordinary implementation or deployment steps within that scope. Never deploy the current checkpoint candidate before fixing the review blocker and measuring recovery. Revalidate runtime, Git, and process state rather than assuming this handoff is current. No new feature scope or perpetual autonomous loop is requested.

The detailed evidence ledger is also at:
`/home/wohlgemuth/IdeaProjects/herdr-delivery-finish-recovery/DELIVERY-FINISH-PROGRESS.md`.
This handoff overrides older optimistic deployment claims in that ledger.

## Goal and completion requirements

Finish these in succession, preserving the original scope:

1. Audit mission/session/service persistence and restart recovery against production.
2. Verify collaboration through the real public URL, including asset imports and private API isolation.
3. Deploy fleet plugins and configure the recorded mission `feed-pi` publisher.
4. Add production-scale startup/restart coverage and fix full Python/npm suites.
5. Review, merge, deploy a pinned reviewed commit, verify restart and feature behavior, and leave a durable receipt.

The original feature was read-only mission tests/builds and reviews/PR pages
(issues #34/#35), escaped user text, HTTP(S)-only external links with noopener,
honest unknown verdict/counts, and count-preserving value redaction. Those changes
were merged earlier; the current outstanding work is release/recovery completion.
Do not substitute passing small tests for the production restart requirement.

## Repositories and source state

- Main implementation worktree: `/home/wohlgemuth/IdeaProjects/herdr-delivery-finish-recovery`.
  Branch `fix/delivery-finish-recovery`, HEAD `a90bed5f5a9ccb1a6357ac24d5a238ed5a3afa80`.
  Based on d58294d and merged remote main e8a5e98 (fleet powerpack changes) cleanly.
- Suite worktree: `/home/wohlgemuth/IdeaProjects/herdr-delivery-final-suite`, branch
  `fix/delivery-final-suite`. Its first four fixes are already cherry-picked into
  the main implementation worktree; its NEW uncommitted lifecycle fix is not.
- Original user workspace: `/home/wohlgemuth/IdeaProjects/herdr-mission-ui-wt`.
  Old merged `fix/mission-delivery-review-followup` branch. Our abandoned cherry-pick
  was backed up as `backup/delivery-abandoned-cherry-pick-20261026`, then aborted.
  This handoff is intentionally uncommitted here.
- `/home/wohlgemuth/IdeaProjects/herdr-engineering` has unrelated dirty user work
  and older local main. Do NOT reset, clean, overwrite, or use it for release edits.
- Origin: `git@github.com:berlinguyinca/herdr-engineering.git`.

Committed on implementation branch:

- `82e6559`: feed-pi explicit `--allow-open`, default still requires key; redirects rejected.
- `3443353`: suppress snapshot publications when only generation time changed.
- `1354e39`: publisher user unit and operational docs; `14724af` clarifies batch semantics.
- `9997d30`: public nginx asset whitelist includes complete collaboration import graph.
- `35a3821`: migration0014 arrival index.
- `f3a7ac8`: faithful recovery stream compaction, private projections and ledger restored.
- `67e83eb`, `30fd3eb`, `b27b8a5`, `f5a17d2`: suite fixes integrated from Godel
  (FD-safe listener discovery, ingest policy test, time-relative JWT fixtures,
  CI/theme/history fixture/layout fixes).

Uncommitted in implementation worktree (preserve):

- Modified `herdr_engineering/control_plane/bridge.py`.
- New `herdr_engineering/control_plane/recovery_checkpoint.py`.
- New `deploy/control-plane/migrations/0015_recovery_checkpoints.sql`.
- New `tests/control_plane/test_recovery_checkpoint.py`.
- Private progress ledger; do not accidentally commit it as product documentation.

Uncommitted in suite worktree (preserve/integrate after tests):

- `herdr_engineering/control_plane/api.py`: maintenance stop Event, tracked threads,
  interruptible waits and shutdown joins; fixes leaked sweep/link workers.
- `tests/control_plane/test_server_lifecycle.py`: repeated real-server shutdown,
  listener release, maintenance thread termination/in-flight work regression.
- `tests/playwright/related.test.mjs`: inspect exact diff; agent-owned test fix.

Suite agent final report: lifecycle regressions3passed (3failed before), related
checks102passed/1timeout (timeout case passed unchanged on recheck); Ruff,
compilation and wheel build passed. IMPORTANT unresolved lifecycle review finding:
shutdown during public-listener construction can miss that listener and hang
joining its thread. Repair startup/shutdown synchronization before integrating.
Related fixture cleanup change reaps process after failed startup; assertions and
timeout unchanged. Related-file run4/5 with startup timeout, not green.

## HIGH: current checkpoint candidate is NOT safe to deploy

Independent reviewer Singer returned provisional REQUEST_CHANGES:

Saved checkpoint rows replay unconditionally. The journal is NOT universally
append-only: token-usage retention in `control_plane/sink.py` deletes records and
`control_plane/collab_repair.py` quarantines forged events. A checkpoint can restore
deleted/quarantined records on restart.

Fix by filtering saved identities against the current journal, or atomic
invalidation that also fences concurrent checkpoint publication. Add real-Postgres
retention/quarantine restart regressions. This is a real blocker, not a theoretical
warning. No fix was made before stopping.

Current design otherwise stores original selected raw rows and the exact covered
`(COALESCE(actor->>'id',''), event_id)` set from ONE repeatable-read snapshot.
Atomic active-generation pointer publishes only after rows/coverage are durable.
Startup uses checkpoint plus an anti-join of uncovered identities, then indexed
tail payload lookups. No timestamp/sequence watermark (late/backdated commits must
survive). All v2 ledger events/private streams must remain faithful. Preserve
previous generations for readers with older MVCC snapshots. No journal deletion
or payload rewriting is authorized as an optimization.

Checkpoint tests: independent run 3 passed, 1 failed in 2.74s; combined bridge batch
15 passed, 1 failed. Failure `test_recovery_checkpoint.py:133`: asyncpg returns actor
JSON as text, but assertion uses `e['actor']['id']`. Earlier projection and ledger
parity checks passed. Correct test representation without weakening assertions.

Still needed: HIGH repair; mid-build concurrent commits; successful publication
with old reader; backdated barriers inside compacted runs (reviewer unconfirmed
availability-timestamp risk); private projection parity; production warm-start
timing; operator build entry point/safe invocation and format/rebuild guidance.
`export_state` alone is insufficient because private stores are omitted after import.

## Production: authoritative observations, recheck before acting

- Core health URL: `http://100.104.39.6/api/v1/health`.
- Core containers `herdr-cp-web`, `herdr-cp-worker`, `herdr-llm-proxy` actually run
  `herdr-engineering:bce35fb967bf`. Earlier claims of newer successful deployment
  were stale. Core is currently old, serving while recovery is being repaired.
- `herdr-collab-public` was restored separately through official Compose overlay,
  image `herdr-engineering:d58294d5e399`, healthy, zero restarts when last checked.
- PostgreSQL/RustFS healthy. Production PostgreSQL container `herdr-cp-postgres`,
  PostgreSQL16, local published port127.0.0.1:15433.
- Structured missions/sessions/services tables each have ZERO rows: events are the
  recovery authority, so d582's startup allowlist drops real state.
- ~1.79M journal rows, ~2GB heap/~2.6GB total. Dominated by services:
  3992 service IDs/~1.515M rows; 391 sessions/~87k rows; 4hosts/~12k rows;
  ~153.7k inspector events. Listener/metadata observations dominate.
- Full faithful production SQL recovery timed out at180s. Count and SQL tuning
  attempts also timed out. Do NOT deploy and simply increase health timeouts.
- Synthetic real-mix regression1,767,806 rows ->173,712 selected, preserving all
 153,719 inspector rows, ~55s synthetic hydrate/restart. This is not production proof.
- Two additive indexes were built CONCURRENTLY in production and flags verified
  indisvalid=true/indisready=true:
  `idx_fabric_events_recovery_arrival` (migration0014), and
  `idx_fabric_events_recovery_identity` (migration0015 index only).
  Checkpoint tables were NOT migrated and no production checkpoint built yet.
  No journal rows were changed by these operations.
- Our old diagnostic containers `herdr-b6-trace3` and `herdr-b6-workerdebug` stopped.
  Do not restart them; they were additional production-connected workers.

Config: `~/.config/herdr-engineering/fleet/fleet-config.yaml`,0600. Real loader is
`herdr_engineering.fleet_config.load_fleet_config`, then `.section('control_plane')`.
Never print values, DSNs, credentials, or raw config. Control plane is intentionally
open on Tailnet; host config has no ingest API key. Do not fabricate one or change
server authentication. Feed opt-in `--allow-open` matches this existing deployment.

Safe DB audit pattern (no password arguments):

```bash
docker exec herdr-cp-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM fabric_events"'
```

Initial checkpoint must be built offline while old core stays online, after fixing
and verifying the candidate, before cutover. Resolve DSN in memory from central
config and override host/port to127.0.0.1:15433; never echo it. Builder writes only
new checkpoint tables, not fabric_events. Verify measured warm recovery/parity
before publishing/deploying final pin.

## Public collaboration verification and live repair

Real URL: `https://localdev.mcp.metabolomics.us/`.
Edge nginx `herdr-collab-edge`, LAN192.168.137.247:8443 expects PROXY protocol from
SNI front192.168.137.250. Direct Tailnet8443 probes give misleading results.

Live nginx whitelist fixed because13 JS/CSS imports were denied and app stayed
blank despite config200. Committed regression9997d30 covers complete public asset
graph. Live file `~/.config/herdr-engineering/collab-edge/nginx.conf` patched in
place, nginx-t and reload succeeded. Backup private below.

Verified headless Chromium: title HerdR Collaboration, body/sign-in visible,
no failed asset paths or JS errors. Root/collab-view.js/config200; rooms401
unauthenticated; private missions API404. No authenticated user flow was tested.
Repeat these checks after final deployment.

## Fleet and feed-pi remaining

Inventory `~/.config/herdr-engineering/fleet-hosts.txt`:
bender local systemd; beast viaSSH systemd; Mac via `wohlgemuth@100.84.185.100` launchd.
SSH BatchMode worked. All plugin links still `fleet-88c4d5e9efe7` (old).
Existing cp-sessions and native mission-feed active; preserve them and active Pi jobs.

Supported aggregate snapshot:
`~/IdeaProjects/.pi-eng/orchestration-snapshot.json`.
Bender contractv3,95missions, actively generated. Beast80active. Mac2missions stale
Oct23: don't infer liveness or blindly publish stale snapshot as current.
Use bender active aggregate for initial publisher; per-repo snapshot is stale.

Real throwaway HTTP test:95accepted twice,95unique pi: mission rows. Polling now
suppresses generation-only duplicate batches. CLI/open39tests passed; installed
publisher unit passed systemd-analyze verify (unrelated TeamViewer warning).

Actual fleet rollout and feed publisher installation HAVE NOT happened.
Official command after server recovery and reviewed published commit:

```bash
deploy/deploy-fleet-agents.sh --ref <PINNED_SHA> --no-fetch
```

Dry-run d582passed all3hosts. Script stages immutable releases, backs up hook
configuration, restarts collectors, and handles rollback. Verify exact revisions,
all3service states, and actual ingest. Do not restart Pi workers.

Stage published publisher checkout under
`~/.local/share/herdr-engineering/pi-snapshot-feed/releases/<fullsha>` and switch
`current` atomically. Unit `deploy/mission-feed/herdr-pi-snapshot-feed.service`
uses existing `~/.venvs/herdr-engineering/bin/python`, PYTHONPATH staged checkout,
active root aggregate snapshot,30s interval and explicit --allow-open. Install
as user unit, daemon-reload, enable/start; verify logs, namespace uniqueness,
publisher restart idempotency. Native live collector remains separate.

## Full suites at stop

Both owned long-running processes stopped (PIDs2598580Python,2969709npm no longer
present after agent stop). Do not mistake interrupted runs for green.

- `/tmp/herdr-delivery-suite-python-full.log`: interrupted at47m54s;
  4failed,4596passed,5skipped,109deselected,2xfailed.
  Failures:
  `test_collab_pentest.py::test_jwt_claim_attacks_rejected[over12]`
  (time-relative fixture fix already integrated but test imported before edit);
  two `test_t16_collab_repair_postgres.py` repair/startup quarantine tests;
  `test_t16_rereview_postgres.py::test_after_a_restart_the_request_replays_and_the_quorum_read_still_answers`
  (201 returned, expected idempotent replay200).
- `/tmp/herdr-delivery-suite-ui-npm-final.log`: stopped after reaching test879,
  no complete final summary. Agent reports two fixture-startup timeouts: GitHub
  narrow detail and related-request error handling. Earlier run938/940; fixtures/layout issues
  fixed and targeted tests passed. Need fresh full final run.
- Python reached1409threads: confirmed sweep/link maintenance threads leak because
  old shutdown never clears _httpd and loops sleep300/30s. Uncommitted lifecycle
  fix belongs to suite worktree; finish focused checks and integrate before rerun.
- Full runs can take45–60min; verify actual live process before deciding stopped.
  Do not restart merely because a wait or log observation times out.
- Repo-wide Ruff has134pre-existing findings; modified-file checks previously
  passed. Report actual checks/gaps rather than claiming whole-repo lint green.

## Deployment and final audit

Use official `deploy/control-plane/deploy.sh --ref <PINNED_SHA> --no-fetch` from
published reviewed source. Correct Dockerfile is `deploy/fabric-stack/Dockerfile`.
Script rolls web/worker/LLM/publiccollab/edge and verifies exact health commit and
container health. Default readiness60s. Current old core/new public mixed stack
needs a concrete rollback plan: old bce config rejects `control_plane.collab.skills`.
Do not blindly rollback modern central configuration with incompatible old image.
Other automation can advance origin/main; deploy a reviewed immutable SHA.

Private state backups:
`~/.local/state/herdr-engineering/rollouts/delivery-finish/pre-recovery.kEHq8g/`
(health/missions/sessions/services,0600) and `nginx-before-assets.conf`.
Potential previous temporary config dirs contain secrets: do not print or casually
remove mounted directories. No active deploy script was observed at stop.

Completion requires fresh full suites, private/ledger/public projection recovery
parity at real scale, warm-start measurement, independent review of final diff,
PR/check results and merge SHA, actual pinned core+fleet+feed rollout, real restart
parity, public browser/auth isolation checks, and durable receipt. No such final
completion claim has been made.

## Stop state and handoff mechanics

All three active agents were explicitly interrupted and told to stop/preserve
files. Recovery agent reported no owned background jobs; reviewer stopped with
the HIGH finding above. Suite agent stopped both long suites. Recheck process
state before Pi starts tests. No Codex workflow should resume itself after pause.

The `herdr-handoff` executable was absent from PATH/expectedvenv; standalone source
exists at `/home/wohlgemuth/IdeaProjects/herdr-handoff/src`. The portable package can
be created/inspected using `PYTHONPATH` and `python3 -m herdr_handoff_cli` without
installing anything. A local queued package is informational; it does not launch
Pi, sync externally, approve a deployment, or mark this goal complete.

Local queued package: `handoff_1003885ea7e844638b100103c4609634`, target pi.
Path:
`/home/wohlgemuth/.local/state/herdr-engineering/handoffs/delivery-finish/outbox/handoff_1003885ea7e844638b100103c4609634.json`.
Created using the standalone CLI; not synced externally. Inspect without install:

```bash
PYTHONPATH=/home/wohlgemuth/IdeaProjects/herdr-handoff/src python3 -m herdr_handoff_cli --root /home/wohlgemuth/.local/state/herdr-engineering/handoffs/delivery-finish inspect handoff_1003885ea7e844638b100103c4609634
```

Pi should claim/accept/start this local package when resuming, using that CLI's
`transition <id> claimed|accepted|in_progress --actor pi` commands sequentially.
The Markdown handoff is the detailed source of truth; the JSON is a bounded pointer.
