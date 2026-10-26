# Pi mission snapshot publication

`control-plane feed-pi` publishes recorded mission state from one explicit Pi
snapshot. The existing `mission-feed` collector separately supplies live worker
activity and validates ownership through local sockets. Install both when recorded
history and live inspection are required.

Stage a published checkout under
`~/.local/share/herdr-engineering/pi-snapshot-feed/releases/<full-commit>` and
point `current` to it. Install
`deploy/mission-feed/herdr-pi-snapshot-feed.service` as a user unit. The template
reads the aggregate `~/IdeaProjects/.pi-eng/orchestration-snapshot.json` every
30 seconds; override `ExecStart` when the authorized snapshot lives elsewhere.
Confirm its generation time and mission count before enabling publication.

The URL defaults to the registered host's central configuration. Keyed servers
require `HERDR_CP_INGEST_KEY` from a private service credential source. The template
uses explicit `--allow-open` for this fleet's intentionally open Tailnet server;
remove that option for a keyed deployment. It sends no fabricated API key and
does not alter server authentication. HTTP redirects are rejected in both modes.

Polling publishes the first snapshot and changed mission payloads. A change to
`generatedAt` alone does not append mission updates. Missing or malformed files
fail with a fixed diagnostic and systemd retries after 15 seconds. Heartbeat and
progress fields remain reported evidence; publication does not infer liveness.

Verify the installed source revision, `systemctl --user is-active
herdr-pi-snapshot-feed`, and namespaced mission IDs on the control-plane API.
Restart the publisher and check that mission IDs remain unique. Preserve the
previous release symlink for rollback; stopping publication never stops Pi work.
