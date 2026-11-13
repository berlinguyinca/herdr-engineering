# Agent communication history

Open **Agents → Communications** (`#/communications`) to see messages submitted
through the native CLI integration and the control-plane session message API.
The history shows sender/recipient, available session links, time, redacted text,
and delivery evidence. A native delivery acknowledgement means transport accepted
the prompt, not that the agent read it or completed the work. Unresolved sender
identity and unknown delivery outcomes remain explicit.

## Native capture

Protocol 22 does not emit a prompt event. This integration does not fork HerdR:
an opt-in CLI shim wraps the original executable and retains it beside the shim
as `herdr.native-untracked`. Commands other than `agent prompt` exec the original
binary. Native prompt arguments, stdout, stderr and exit status are preserved;
an observation is queued before delivery and settled from the native receipt.
No audit retry ever repeats the native command. Raw socket callers and private
Claude/Pi/Codex/OpenCode helper channels are outside this capture path.

Install from a persistent, installed release of herdr-engineering (not a scratch
checkout or a virtual environment you intend to delete):

```sh
herdr-eng communications install --native "$HOME/.local/bin/herdr" \
  --url http://YOUR-CONTROL-PLANE \
  --key-file "$HOME/.config/herdr-engineering/YOUR-INGEST-KEY"
herdr-eng communications status
herdr-eng communications sync
```

Use the existing ingest credential; an audit reporter gains no session-control
permission. The config stores the key **path**, never its contents. Deployment
must update the server before enabling sync: older servers refuse the new
`communication` observation type and the local queue retains those records.
The shim also tries a bounded upload after each prompt. Schedule
`herdr-eng communications sync` every minute with the platform's user scheduler
to drain observations even when no subsequent prompt runs. Use `/dev/null` or
rotated scheduler output, not an unbounded append-only log.

The SQLite outbox lives under `~/.local/state/herdr-engineering`, mode 0600.
Redaction precedes persistence; message bodies are limited to 16 KiB with an
explicit truncation flag. Local limits are 10,000 records, 64 MiB database pages,
and 14 days. Queued and settled observations count separately. SQLite journals
can temporarily require additional space. No message body is written to a raw
command log. Expired observations are removed; if admission or storage fails the
shim emits an audit warning and leaves native delivery working. Such refused
observations cannot be recovered later. Durable events follow existing server
retention, not a new unlimited archive.

The control plane records observations with host-reported evidence, never as
authoritative command receipts. Its own sends use server receipts. Their shared
operation ID gives one history entry, with server receipts preferred. Receiver
identity comes from the actual native acknowledgement where available; stale
preflight identities must not misattribute a prompt after pane reuse. Remote
recipient routes without a verified host mapping retain native identity but do
not link to an arbitrary local session. Sender lookup uses the caller's HerdR
context, never the UI's focused pane; absent/stale context can leave it unknown.

## Rollback

Stop the scheduled sync job first. Restore the retained executable by replacing
the shim with `herdr.native-untracked`; retain the outbox until it has synced or
its audit data is intentionally discarded. Do not blindly run a native binary
updater over the shim: restore/update the original executable first and then
reinstall capture using a new config/backup after reviewing the old queue.

## Verification

The scoped GitHub Actions job uses the actual pinned HerdR 0.9.1 binary,
PostgreSQL 17.6 with the production migrations, real SQLite/HTTP, and Playwright
CLI against the actual dashboard server. It enforces lint, formatting, Python
and TypeScript checks, at least 80% statement/branch/function coverage on the
new Python modules and all four coverage dimensions on the dashboard component,
and a distributable wheel build. No message is sent to an existing agent by
these tests: native delivery tests use an absent target and inspect the refusal.
