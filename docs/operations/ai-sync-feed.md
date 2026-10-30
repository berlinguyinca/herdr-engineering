# ai-sync status feed

Every fleet host runs `herdr_engineering.ai_sync_feed` (user unit `herdr-eng-ai-sync-feed.service`, template
`scripts/herdr-eng-ai-sync-feed.service`, converged by `ansible/tasks/ai-sync-feed.yml` from `fleet-sync.yml` and
`site.yml`; enabled only where `ai-sync` is installed). It posts one `ai_sync` / `status` envelope per host to
`POST /api/v1/ingest`; the dashboard reads `GET /api/v1/ai-sync` (latest per host, `?host_id=` filters) at `#/ai-sync`.

Read-only by construction: the only commands it can run are `ai-sync --version`, `ai-sync status` and
`ai-sync env list` (always with `--no-update-check`) plus `git rev-list/log/rev-parse` on `~/.ai-sync`. `ai-sync status`
itself runs a `git fetch`, which updates remote-tracking refs only. It never runs `pull`/`push`.

Payload: `host_id, ai_sync_version, envs_enabled, last_sync_ts` (last commit time of the sync repo), `ahead, behind,
branch, drifted_files_count, drift_sample` (<=20 relative paths, no contents), `skipped_links_count, last_error`
(redacted, <=400 chars), `checked_at`. The control plane rejects any other field. The dashboard marks a host warn after 1 h
and stale after 24 h without a sync.

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
