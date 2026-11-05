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
- `host` and `halted_at_host` are the closed fleet enum `beast|bender|macbook-pro` (a new member needs a code change);
  `links.pi_session_rollout` is a 16 to 32 character lowercase-hex rollout id; SHAs are 40 lowercase hex; `checks` <= 32 x (`name` <= 40, `ok`); `counts` only
  `dirty_paths|unpublished_commits|behind`; `log_tail` <= 2048 chars and <= 40 lines; `pr_title` <= 120 chars;
- **no secrets**: `log_tail`, `pr_title` and every `checks[].name` go through the same `unsafe_text` check and are
  **rejected** (never truncated or repaired) when they contain credentials in a URL, an `Authorization:` or `Cookie:`
  header, `token=`/`password=`/`passwd=`/`pwd=`/`secret=`/`api_key=`, a `Basic`/`Bearer` scheme followed by a value,
  private-key armour (with or without dashes), AWS (`AKIA`/`ASIA`), `sk-`, Slack, GitHub and JWT token shapes, an
  ssh key path (`.ssh/`, `id_rsa`...), netrc `machine ... login ... password`, a 24+ character token-like run, a control
  character, a `/home/<user>`, `/Users/<user>` or `/root` path, or the literal `[REDACTED]`. The general ingest door
  runs first and overwrites credential-looking strings (sometimes only the header name) with `[REDACTED]`; the validator
  treats that marker as a rejection, so a half-redacted value is a `400`, never stored. A PR title that trips the check
  is dropped by the emitter.
- **default-deny by shape** (not only known formats): free text is also refused when it has a key-like name followed by a
  value (`pass|pwd|secret|token|key|auth|cred|cookie|sig` then `=`/`:`/JSON colon), a credential CLI flag (`--password x`,
  `-u user:pw`, `-pX` next to mysql/docker/curl/login...), a secret URL query parameter (`sig|key|token|code|auth`), a
  pgpass line, a padded base64 value, a credential file (`hosts.yml`, `.netrc`...), or a run of 20+ characters that is hex,
  mixed upper/lower/digit or high-entropy (24+ characters always, unless it is a path or lowercase hyphenated prose such as
  `make-the-ingest-door-redact-secrets-consistently`). Cost: a log line with a long file name such as a wheel name is
  withheld; the emitter replaces such a line with `[line withheld]`. 40-hex commit SHAs are accepted only in the sha fields.
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
(400/409/413/422, a 200 that is not `ok`, torn lines, > 16 KiB) are parked with the reason in `outbox.rejected.jsonl` so
they cannot block the rest. `flush` holds its file lock only to read and to trim, never across a network call.

`seq` is never reused: the next value is the maximum of `seq.json`, its backup `seq.json.bak`, and every seq still in
the outbox or the parked files, and it is persisted **before** the event is appended (a crash burns a number, a gap is
harmless). If both counter files are gone while every event was already delivered and trimmed, the next seq starts from the clock
(seconds since the epoch, kept below 2^31) instead of 0, so it cannot collide with what the server holds. `source.*`
counters are never pruned.

Bounded growth: at most 2000 unflushed events (older ones are parked as `outbox full` and counted in `outbox.dropped`,
shown by `outbox status` as `dropped`); `outbox.rejected.jsonl` rotates to `.1` above 1 MB (one generation kept);
`seq.json` entries idle for 30 days are pruned.

```
herdr-eng fleet-watch outbox status [--json]
herdr-eng fleet-watch outbox flush [--control-plane URL] [--api-key-file F] [--json]   # exit 1 if events stay queued
```

Rollback: nothing calls the emitter in this release; removing the ingest type is the only server-side change to undo.
