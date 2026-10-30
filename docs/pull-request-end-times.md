# Pull request end times

A `pull_request` ingest envelope (`POST /api/v1/ingest`, `entity_type: "pull_request"`) may carry the provider's own times:

```json
{"entity_type": "pull_request", "entity_id": "pr-1", "event_type": "pull_request.updated",
 "payload": {"repository": "acme/app", "number": 7, "status": "merged",
             "merged_at": "2026-10-01T10:00:00Z", "closed_at": "2026-10-01T10:00:00Z"}}
```

For GitHub, send the pull request's `merged_at` and `closed_at` as they are. The record then keeps them with
`ended_source: "reported"`.

- A value that is not an ISO-8601 string is ignored.
- Without a usable time, the first moment the control plane sees the PR as `merged` or `closed` is stored and labelled
  `ended_source: "observed"`. That can be later than the real merge, and the PR trail says so in its duration reason.
- A stored time is never overwritten by a later observation. A PR that is `open` again loses its end time.
- `ended_source` is set by the server only. An envelope cannot supply it; the durable event log carries it so a restart keeps
  an observed time observed.

The repository itself does not send these envelopes today: GitHub's webhook intake (`intel_webhook.py`) feeds the project
intelligence store, not these records, and is not reachable until the webhook secret and URL are provided. Whatever agent or
hook posts `pull_request` envelopes has to include `merged_at` and `closed_at` for the times to be `reported`.
