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

The command prints each stored log with its SHA-256, which should equal the `log.sha256` in the event. (The watcher
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

Rollback: nothing calls the emitter in this release; removing the ingest type is the only server-side change to undo.
