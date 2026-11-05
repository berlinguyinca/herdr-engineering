# Fleet rollout events (spec step 3, off by default)

Implements section 8 of `docs/specs/herdr-fleet-rollout-watcher-20261104/spec.md`: the event model the fleet rollout
watcher reports into. Nothing emits these events yet (the runner is step 4); this release adds the ingest type, the
read endpoints and the emitter/outbox, so they can be exercised with `curl`.

## Ingest: `entity_type = "fleet_rollout"`

`POST /api/v1/ingest` with `entity_id = rollout_id` (16 lowercase hex, `fleet_watch.plan.rollout_id`) and one of the
event types `rollout.observed|planned|started|resumed|succeeded|halted|superseded|abandoned`, `host.started|step|finished`,
`lock.pinned|refused`, `source.refused|degraded|recovered`. The payload always carries `rollout_id` (must equal
`entity_id`), `ts` (ISO-8601 **UTC**) and `seq` (int >= 0, per stream); the other fields allowed depend on the event type
(`control_plane/fleet_rollout.py::EVENT_FIELDS`). Anything outside that table is a `400` and nothing is stored.

Strict by construction (the validator is the last line of defence against a buggy emitter):

- closed enums: `status` (`pending precheck applying checking ok blocked failed unreachable skipped rolled_back`),
  `reason` (spec 8.1), `repo` (`autospec pi-engineering herdr-engineering ai-sync claude-config`);
- `host` is `[a-z0-9-]{1,40}`; SHAs are 40 lowercase hex; `checks` <= 32 x (`name` <= 40, `ok`); `counts` only
  `dirty_paths|unpublished_commits|behind`; `log_tail` <= 2048 chars and <= 40 lines; `pr_title` <= 120 chars;
- **no secrets**: a `log_tail` or `pr_title` containing credentials in a URL, an `Authorization:` header, `token=`,
  `password=`, `secret=`, `api_key=`, private-key armour, a 24+ character token-like run, a control character or a
  `/home/<user>`, `/Users/<user>`, `/root` path is **rejected** (never truncated or repaired). The general ingest door
  also redacts credential-looking strings before the validator sees them, so such a field is stored as `[REDACTED]`.
- `source.*` events are not tied to one rollout; they use `control_plane.fleet_rollout.source_stream_id(repo)` as stream id.

Idempotent: the envelope identity is `<rollout_id>:<seq>` (`event_id()`). The same `seq` with identical content is a
no-op (`"duplicate": true` in the reply); different content for a stored `seq` is `409`.

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

`Emitter.emit(rollout_id, event_type, **fields)` redacts `log_tail` (last 40 non-empty lines, <= 2048 chars; credential
lines become `[redacted line]`, token-like runs `[redacted]`, home paths `~`) and `pr_title` (dropped if it carries a
secret), validates with the server's own validator, assigns the next `seq`, appends to
`$XDG_STATE_HOME/herdr-engineering/fleet-watch/outbox.jsonl` (0600, fsynced) and then tries to deliver. A control plane
that is down never raises: the event stays queued and goes out, oldest first, on the next `flush()`. The queue stops at
the first transient failure (network, 5xx, 401/403/429) to keep the stream ordered; events the server will never accept
(400/409/413/422, torn lines, > 16 KiB) are parked with the reason in `outbox.rejected.jsonl` so they cannot block the rest.

```
herdr-eng fleet-watch outbox status [--json]
herdr-eng fleet-watch outbox flush [--control-plane URL] [--api-key-file F] [--json]   # exit 1 if events stay queued
```

Rollback: nothing calls the emitter in this release; removing the ingest type is the only server-side change to undo.
