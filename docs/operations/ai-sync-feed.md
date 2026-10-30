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
