# Fleet rollout events (spec step 3, off by default)

Implements section 8 of `docs/specs/herdr-fleet-rollout-watcher-20261104/spec.md` (with the decision recorded in its
appendix): the event model the fleet rollout watcher reports into. Nothing emits these events yet (the runner is step 4);
this release adds the ingest type, the read endpoints, the emitter/outbox and the host-side log store, so they can be
exercised with `curl`.

## No free text

The model carries **no free text**. Every value is a closed enum, an inventory host, a lowercase-hex id or digest, an
integer, or a UTC timestamp, and unknown fields are rejected, so a secret cannot ride along by construction (regex
redaction of log text was tried and could not be made safe). The spec's redacted `log_tail`, `pr_title` and free-form
check names do not exist; see "Logs" below.

## Ingest: `entity_type = "fleet_rollout"`

`POST /api/v1/ingest` with `entity_id = rollout_id` (16 lowercase hex, `fleet_watch.plan.rollout_id`) and one of the
event types `rollout.observed|planned|started|resumed|succeeded|halted|superseded|abandoned`, `host.started|step|finished`,
`lock.pinned|refused`, `source.refused|degraded|recovered`. The payload always carries `rollout_id` (must equal
`entity_id`), `ts` (ISO-8601 **UTC**) and `seq` (int >= 0, per stream); the other fields allowed depend on the event type
(`control_plane/fleet_rollout.py::EVENT_FIELDS`). Anything outside that table is a `400` and nothing is stored.

| field | allowed values |
|---|---|
| `repo`, `trigger.repo`, `targets[].repo` | `autospec pi-engineering herdr-engineering ai-sync claude-config` |
| `host`, `halted_at_host`, `log.host` | `beast bender macbook-pro` (a new fleet member needs a code change) |
| `status` | `pending precheck applying checking ok blocked failed unreachable skipped rolled_back` |
| `reason` | `dirty_checkout not_on_main unpublished_commits busy timeout health_failed apply_failed ssh_failed halted optional_unreachable not_published rate_limited operator` |
| `trigger` | `repo`, `sha` (40 hex), optional `pr` (int), `merged_at` (UTC) - no title |
| `targets[]` | `repo`, `sha`, optional `before` (40 hex or null), at most 5 |
| `checks[]` (at most 10) | `name` in `version_matches service_active gateway_registered parity_ok release_complete current_symlink version_smoke generation_receipt verify_ok feed_in_sync`, `status` in `ok failed skipped`, optional `duration_ms` |
| `log` | `host`, `path_id` in `updater installer healthcheck`, `sha256` (64 hex), `bytes`, `lines`; `log.host` must equal `host` |
| `counts` | `dirty_paths unpublished_commits behind` (non-negative ints) |
| `links.pi_session_rollout` | 16 to 32 lowercase hex, or null |
| `before_sha`, `after_sha` | 40 hex or null |
| `started_at`, `finished_at` | UTC timestamp; `duration_ms` non-negative int |

`source.*` events are not tied to one rollout; they use `control_plane.fleet_rollout.source_stream_id(repo)` as stream id.

Idempotent: the envelope identity is `<rollout_id>:<seq>` (`event_id()`). The same `seq` with identical content is a
no-op (`"duplicate": true` in the reply); different content for a stored `seq` is `409`.

## Logs stay on the host

Log text (updater, installer, health-check output) never leaves the host that produced it. The emitter keeps it in a
private store (`$XDG_STATE_HOME/herdr-engineering/fleet-watch/logs/<rollout_id>/<host>-<kind>.log`, dirs 0700, files 0600,
last 1 MB kept, files older than 30 days deleted when a new one is written) and the event carries only the reference
`{host, path_id, sha256, bytes, lines}`. The operator reads it where it was written:

```
herdr-eng fleet-watch log show <rollout_id> --host bender [--path-id updater|installer|healthcheck]
```

The command hashes the stored bytes (no newline or encoding translation, so CRLF and invalid UTF-8 logs hash the same as when written) and prints each log with that SHA-256, which equals the `log.sha256` in the event; the text is then decoded with replacement characters for display. (The watcher
that drives `ssh` runs on the controller, so for remote hosts the logs it captured are in the controller's store.)

## Read API (ETag, like ai-sync)

| route | answer |
|---|---|
| `GET /api/v1/fleet-rollouts?limit=50&repo=` | `{"rollouts": [summary...]}` newest first; `limit` 1..200; `repo` matches the trigger or any target |
| `GET /api/v1/fleet-rollouts/{id}` | summary + `hosts` (one row per host x repo, canary order) + ordered `events`; `404` if unknown |
| `GET /api/v1/fleet-rollouts/versions` | `{"hosts": [{"host", "repos": {repo: {sha, since, rollout_id, status}}}], "main_heads", "lock", "sources"}` |

Summary `status` is derived from the newest rollout-level event: `observed planned running succeeded halted superseded
abandoned`. `versions` takes the newest `host.finished` with `ok|rolled_back` per host x repo; `main_heads` is the newest
observed SHA per repo; `lock` the last `lock.pinned`; `sources` the last `source.*` state per repo. Fleet-status
fingerprints for hosts the watcher never rolled are not merged in yet (step 4+).

```
curl -s http://127.0.0.1:8765/api/v1/fleet-rollouts | jq
curl -s http://127.0.0.1:8765/api/v1/fleet-rollouts/versions | jq
```

## Emitter and outbox (`herdr_engineering/fleet_watch/events.py`)

`Emitter.emit(rollout_id, event_type, **fields)` validates with the server's own validator, assigns the next `seq`,
appends to `$XDG_STATE_HOME/herdr-engineering/fleet-watch/outbox.jsonl` (0600, fsynced) and then tries to deliver. A
control plane that is down never raises: the event stays queued and goes out, oldest first, on the next `flush()`. The
queue stops at the first transient failure (network, 5xx, 401/403/429) to keep the stream ordered; events the server will
never accept (400/409/413/422, a 200 that is not `ok`, torn lines, > 16 KiB) are parked with the reason in
`outbox.rejected.jsonl` so they cannot block the rest. `flush` holds its file lock only to read and to trim, never across a
network call.

`seq` is never reused: the next value is the maximum of `seq.json`, its backup `seq.json.bak`, and every seq still in the
outbox or the parked files, and it is persisted **before** the event is appended (a crash burns a number, a gap is
harmless). **Counters are never pruned** (a few bytes per rollout; a dropped counter would restart at 0 and collide with
the server, e.g. a halted rollout resumed weeks later, or a `source.*` stream). If both counter files are gone while every
event was already delivered and trimmed, the next seq starts from the clock (seconds since the epoch, below 2^31).

Bounded growth: at most 2000 unflushed events (older ones are parked as `outbox full` and counted in `outbox.dropped`,
shown by `outbox status` as `dropped`); `outbox.rejected.jsonl` rotates to `.1` above 1 MB; logs expire after 30 days.

```
herdr-eng fleet-watch outbox status [--json]
herdr-eng fleet-watch outbox flush [--control-plane URL] [--api-key-file F] [--json]   # exit 1 if events stay queued
herdr-eng fleet-watch log show <rollout_id> --host H [--path-id K]
```

Rollback: `fleet-watch run --apply` (below) is the only caller of the emitter; removing the ingest type is the only server-side change to undo.

## Applying a rollout (`fleet host-apply`, `fleet-watch run`) - step 4, manual only

```
herdr-eng fleet host-apply --rollout ID --repo R --sha S [--host H] [--phase precheck|apply|health|rollback|all]
herdr-eng fleet-watch run [--apply] [--resume] [--local-host auto|beast|bender|macbook-pro|none] [--rollback-scope failed_host|all_updated]
```

`fleet-watch run` without `--apply` is the dry-run plan (no ssh, no events). With `--apply` it walks beast, bender,
macbook-pro serially, per host per repo: `precheck` (60 s), `apply` (30 min), `health` (120 s) over `ssh -o BatchMode=yes
-o ConnectTimeout=15` (the local host runs the same command without ssh), in `/` with the host's installed `herdr-eng`.
The first row that is not ok halts the rollout and later rows are `skipped(halted)`; an unreachable macbook-pro is
`skipped(optional_unreachable)` and does not halt, an unreachable beast or bender halts. A timeout is `failed(timeout)`.
State is saved after every transition (`state.json`, `rollouts[id]`): a crash resumes at the first unfinished row on the
next `run --apply` (event `rollout.resumed`, no reason); a halted rollout is re-run only with `--resume` (reason
`operator`). The halt is saved in the same write as the failing row, and a crash that still lost it is repaired on
resume: a finished row that is not ok (and not a deliberate `optional_unreachable`/`not_applicable` skip) always halts,
later hosts are never applied and `converged` does not move. A halt that was never emitted is emitted
(`rollout.halted`) before `rollout.resumed`. The lock file is not written yet (step 5).

Which host is "local" (applied without ssh) is verified, not assumed: `--local-host auto` (default) is this machine
when the `host_id` of `~/.config/herdr-engineering/host.json` (else the short hostname) is a fleet host, otherwise
every host goes over ssh. An explicit `--local-host X` that is not this machine is refused (exit 2, no ssh, no events).
A remote answer is accepted only if it echoes the request (rollout id, repo, sha, phase), an ok apply/health ends on
the target sha, and a `skipped` carries a reason or `not_applicable`; anything else is `failed(apply_failed)`.

`host-apply` prints ONE JSON object made of closed enums, hex, ints and UTC timestamps (section 14); log text, including
example dirty paths (at most 8), stays in the host's log store and is read with `fleet-watch log show`. Idempotent: an
installed build equal to the target is `ok` with `no_change`. For herdr-engineering the installed build is a receipt
(`$XDG_STATE_HOME/herdr-engineering/fleet-watch/installed-herdr-engineering.json`) written only after the checkout
fast-forward, the install and the agent restart all succeeded; `no_change` needs receipt == target AND checkout HEAD ==
target, so an install that failed after the fast-forward is retried (no receipt means "apply again", which is idempotent).
The deployment checkout is `$HERDR_ENGINEERING_REPO`, else the one-line file `~/.config/herdr-engineering/
deployment-checkout` (an absolute path; non-interactive ssh has no profile environment), else `failed(apply_failed)`
with the cause in the host log. The checkout's `origin` URL must end in the repo's name (`.../<repo>(.git)`), else
`failed(apply_failed)`. D6 gate: `blocked(dirty_checkout|not_on_main|
unpublished_commits)` with counts, never a clean, stash or reset. It holds `fleet-auto-update.lock` (`blocked(busy)`
after 5 retries, 60 s apart).

Per repo: herdr-engineering = ff-only + pip install + agent restart (no automatic rollback: ff-only cannot go backwards);
pi-engineering = stage release, atomic `current` symlink switch (temp link + `rename`), keep 3 releases (never the current
or previous), no signal to live sessions, rollback = flip the link back to the host's OWN previous release (its precheck `before_sha`), and only if `current` is still the release this rollout activated; autospec = refresh the runtime checkout and
installer, rollback only while the newest generation receipt still equals the before sha. ai-sync and claude-config are
`skipped(not_applicable)`. `--rollback-scope failed_host` (default) undoes the failed row and updated rows of that host;
`all_updated` undoes every updated row, newest first; each rollback emits a further `host.finished(rolled_back)`.
