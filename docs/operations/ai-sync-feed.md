# ai-sync status feed

Every fleet host runs `herdr_engineering.ai_sync_feed` (user unit `herdr-eng-ai-sync-feed.service`, template
`scripts/herdr-eng-ai-sync-feed.service`, converged by `ansible/tasks/ai-sync-feed.yml` from `fleet-sync.yml` and
`site.yml`; enabled only where `ai-sync` is installed). It posts one `ai_sync` / `status` envelope per host to
`POST /api/v1/ingest`; the dashboard reads `GET /api/v1/ai-sync` (latest per host, `?host_id=` filters) at `#/ai-sync`.

Read-only by construction: the only commands it can run are `ai-sync --version`, `ai-sync status`,
`ai-sync env list` and `ai-sync verify --json` (always `ai-sync --no-update-check <subcommand>`, the global flag first) plus `git rev-list/log/rev-parse` on `~/.ai-sync`. `ai-sync status`
itself runs a `git fetch`, which updates remote-tracking refs only. It never runs `pull`/`push`.

Payload: `host_id, ai_sync_version, envs_enabled, last_sync_ts` (last commit time of the sync repo), `ahead, behind,
branch, drifted_files_count, drift_sample` (<=20 relative paths, no contents), `skipped_links_count, last_error`
(redacted, <=400 chars), `checked_at`, plus the optional verify fields below. The control plane rejects any other field. The dashboard marks a host warn after 1 h
and stale after 24 h without a sync.

## Config-loads check: `ai-sync verify --json`

`verify` checks that the synced config really loads on the machine (skills, agents, commands, hooks, settings). It prints
JSON (`{"ok", "env", "checked": {...}, "errors": [{"path","problem"}], "warnings": [...], "truncated"?}`, or
`{"ok", "envs": [...]}` with several envs) and exits 0 (ok), 1 (errors found, JSON still printed) or 2 (failure). The feeder
accepts exit 0 and 1, parses the JSON defensively and aggregates all envs. An `ai-sync` without the command ("unknown
command") is not an error: the fields below are `null`. Any other failure (exit 2, timeout, unreadable output) is
reported once in `last_error` and the fields stay `null`. Same timeout and process-group kill as the other commands.

Optional payload fields (all may be absent or `null` = unknown; old payloads stay valid):

| field | type |
| --- | --- |
| `verify_ok` | bool, all envs load |
| `verify_errors_count`, `verify_warnings_count` | non-negative int; with `truncated` output the error count is a lower bound |
| `verify_checked` | `{skills, agents, commands, hooks}`, each int or null, summed over envs |
| `verify_error_sample` | <=10 strings `path: problem` (`env: path: problem` with several envs), each <=200 chars, redacted like `last_error` |
| `verify_checked_at` | ISO timestamp of the run (does not by itself trigger a re-post) |

Only counts and `path: problem` text are sent: `configDir`, file contents and any other member of the output are dropped.
The control plane validates these strictly and returns them in `GET /api/v1/ai-sync`. The dashboard shows a **Loads**
column (`OK`, `N errors` with the sample as a tooltip, `—` when unknown) and highlights a row when `verify_ok` is false.

`ai-sync status` has no JSON mode; the plain output is parsed defensively (fixtures in `tests/fixtures/ai_sync`).

## Fabric join: `GET /api/v1/ai-sync/config`

Read-only (same `X-API-Key` rules as every other `/api/v1` read; no write route exists). Source of truth is the central
fleet YAML, `control_plane.ai_sync` (hot-reread; validated at load, so a bad section keeps the last good file):

```yaml
control_plane:
  ai_sync:
    repo_url: https://github.com/acme/ai-config.git   # required: https:// or ssh (ssh://, user@host:path) git URL
    branch: main                                        # default main; safe git ref name
    envs: [claude, pi]                                  # default [claude]; from claude, codex, opencode, antigravity, pi
    merge_enabled: false                                # default false
```

Rejected: `file://` and local paths, whitespace, a leading `-`, credentials in the URL (`user:pw@`), unknown keys, envs
outside the allowlist, refs with `..`, `.lock`, spaces or a leading `-`. Nothing secret is stored or returned.

Response `200`: `{"repo_url": str, "branch": str, "envs": [str], "merge_enabled": bool}`. When unset (or the section is
unusable) `404 {"error": "not configured"}`.

`GET /api/v1/ai-sync` returns `{"hosts": [...]}`; each host carries exactly `host_id, ai_sync_version, envs_enabled,
last_sync_ts, ahead, behind, drifted_files_count, last_error, checked_at`, plus the extras `branch, drift_sample,
skipped_links_count`.

Generic protocol for `ai-sync fabric status/join`: `status` GETs `/api/v1/ai-sync` (optionally `?host_id=`) and prints the
fleet table; `join` GETs `/api/v1/ai-sync/config`, treats `404` as "this fabric has no ai-sync config" (stop, change
nothing), otherwise clones/points the local sync repo at `repo_url`@`branch`, enables `envs`, and honours
`merge_enabled`. The client must re-validate every field (it is untrusted input) and never send credentials to the
control plane. Hosts report back through the status feed above; `join` itself writes nothing to the control plane.

## Enforcement: keep every host on the shared standard

The status feed only observes. Enforcement acts: `python -m herdr_engineering.ai_sync_enforce --env claude` runs
`ai-sync pull --force --env <env>` and then `ai-sync verify --env <env> --json`, and exits non-zero when either fails, so
the unit shows as failed and the feed above reports the host as drifted or failing to load. ai-sync backs up whatever the
forced pull overwrites. Only those two commands can be spawned; `push` and every other subcommand are impossible.

- **Opt-in per host.** `herdr-eng-ai-sync-enforce.timer` (every 30 minutes, jittered) is enabled only when the inventory
  sets `ai_sync_enforce: true` for the host and ai-sync is installed. Default is off.
- **Never on the publisher.** Exactly one host publishes (`ai-sync push`). Set `ai_sync_publisher: true` for it: the
  playbook then creates `~/.config/herdr-engineering/ai-sync-publisher`, never enables the timer, and the enforcer itself
  refuses to run (exit 3) where the marker exists. A forced pull there would overwrite the files about to be pushed.
- **Host-specific values do not survive.** A forced pull replaces local `CLAUDE.md` and `settings.json` edits, so anything a
  host needs that the shared files lack must go into the shared repo first.
- **Overlapping runs** are skipped under a lock, not stacked. `ai-sync` may also self-update on a pull (its normal daily check).
- **Output.** One JSON line: `{"ok": bool, "envs": {"claude": {"pull_ok", "verify_ok", "errors", "warnings", "error_sample"}}}`.
