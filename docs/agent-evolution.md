# Agent evolution

`GET /api/v1/sessions/<id>/evolution` (viewer role, like `/commits`) returns the life of one agent: ordered phases and a compact
`learning_record`. The Evolution tab on the agent page renders it.

**Rule:** a value is null with a reason unless there is positive evidence. A figure that cannot be shown is never 0 or false.

## Configuration

YAML only, under `control_plane` in the fleet config:

```yaml
control_plane:
  evolution:
    stale_after_minutes: 120   # default; 1..10080
```

A non-terminal status (running, blocked) counts as the `outcome` only while the agent's last report is newer than this. Older, the
page still shows "Reported: running" (flagged stale) but the learning record's outcome is null. Terminal statuses always count.

## When a figure is null

| Figure | Null when |
| --- | --- |
| `restarts` | an event is undated, a sequence number is missing, repeated or out of order, timestamps run backwards, or a status is in no known class |
| `ended_at`, `duration_s` | the stream does not show the move into the terminal status the session still reports (undated, unordered, or a different final status) |
| `handoffs_in`, `handoffs_out.*` | handoffs were unreadable or cut off, or any handoff names neither a session nor an agent id unique to one session on that side |
| `prs` | a pull request's trail cannot be read, or more than 5 are linked |
| a pull request's `signals` | the PR trail does not list this agent (signals cover every agent on the PR) |
| `tool_calls.*` | the inspector buffer is not provably whole (see below) |
| `tool_calls.retried` | any call's arguments are withheld, redacted, truncated or absent, or a call has no usable sequence number |
| `outcome` | the status is unclassified, or non-terminal and older than `stale_after_minutes` |

`tool_calls.failed` counts calls with status `error` only; interrupted or unfinished calls are not counted.

## Inspector buffer completeness

The buffer is bounded by count, bytes and `retention_hours`, and is rebuilt after a control-plane restart from durable events only.
Tool figures are reported only when nothing was evicted, nothing was cut off, and the agent started inside the retention window.
**After a restart completeness cannot be fully proven**: the eviction counter covers only the live buffer. The start-time window is
the guard the server can offer, so a figure for a long-running agent may rest on a buffer that lost events.

## Restart classes

Terminal: finished, failed, completed, done, stopped, exited, cancelled, terminated, error, killed. A restart is any move from a
terminal status to a non-terminal one, in the stream's sequence order.
