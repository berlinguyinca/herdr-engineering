# Learning trails

When a pull request ends, the control plane writes **one durable record** of how agents got to it, so a training or analysis
pipeline can pull it later, after the 24 h live window has passed. Nothing here learns anything by itself: it keeps the evidence.

**Rules (the same as the PR trail and the agent Evolution tab):** a value is `null` with a reason unless there is positive evidence. A
figure that cannot be shown is never 0 or false. A frozen record never silently holds nulls caused by a cold start or a transient
outage: it is written only when its sources are readable, or it says `incomplete: true` and names what was missing. Personal data is
not exported.

## What is stored

One `PullRequestTrail` event on the `pr_trail` stream of the PR (kept out of the global feed), written once per *end* of the PR:

- `schema_version` (1), `kind`, `assembled_at`, `pr_id`, `end_seq`, `learning_seq` (the export counter)
- `ended_at` and `ended_source`: `reported` (the provider's own merge/close time) or `observed` (the first moment this server saw the
  PR merged or closed, which can be later than the real merge). `end_time_confidence` is `high` for a reported time or an observed one
  noticed within `low_confidence_after_minutes`, otherwise `low`. For `low`, the signals that depend on the end time (`duration_s`,
  `ci_green_first_try`, `ci_reruns`, `merged_without_rework`) are set to `null` with a "Withheld" reason and listed in `withheld_signals`
- `pull_request` (host, repository, number, status, branch, base, times)
- `signals`: `merged_without_rework`, `ci_green_first_try`, `ci_reruns`, `cost_usd_estimate`, `duration_s`, `review_verdicts`,
  `handoff_problems`, each `{value, reason}`
- `sessions`, `ambiguous`, `handoffs`, `reviews`, `ci_runs`: ids, states, counts, timestamps and rules only
- `agents`: for each evidence-linked agent its `learning_record` from the Evolution tab (tool calls, handoffs, restarts, outcome, cost)
- `sources`: what was read and its state; `incomplete` and `missing_sources`; `omitted`; `trimmed`

### Privacy

An allowlist is stored, not a copy of the trail. Handoff goals, agent names, reviewer names, CI URLs and agent ids are not stored.
The remaining text is passed once, at write time, through the redactor (key-hint masking such as `password`/`token`), then scrubbed of
e-mail addresses (`[EMAIL]`) and home paths (`/home/<u>`, `/Users/<u>`, `C:\Users\<u>` → `[USER]`), keys included. Free text such as a
branch is cut to a short length with a visible `…[truncated]` marker and named in `trimmed` (`text:<field>`). The JSON and JSONL exports
return the stored record as is: they are identical in content and do not depend on any other redaction setting.

### Size

A record is hard-capped at 128 KiB. When over, whole lists are replaced by `{"omitted": n, "reason": ...}` in this order: `ci_runs`,
`handoffs`, `reviews`, `ambiguous`, `sessions`, `agents`; then signal reasons are shortened; as a last resort only the core fields stay.
`trimmed` names what was dropped.

## When a record is written

Only for a PR whose record carries a merge or close time (PR records that predate `end_count` count as one end). A PR that ended before
end times were stored is not assembled.

Assembly is a sweep: the server's maintenance pass (every 5 minutes), plus a background sweep started by an export read. An export
never builds a trail and never waits for one. A pass writes at most `sweep_batch` records, builds each trail **outside** any lock,
re-reads the PR just before writing and skips the write if its status, times or end count moved.

**Readiness.** A record is not written while:

- the process is inside `warmup_minutes` after start (CI and event buffers refill after a restart);
- the PR trail could not be built (for example the 2 s gate timeout);
- a critical source (`ci`, `handoffs`, `reviews`, `plan_reviews`) is not `ok`, or an agent's Evolution sources (`ci`, `handoffs`,
  `reviews`, `plan_reviews`, `event_stream`, `tools`) are not `ok`, or an agent's record could not be read.

It is retried every `retry_interval_minutes`. After `ready_retry_minutes` without a complete read it is stored anyway with
`incomplete: true` and `missing_sources` (for example `["ci"]`). Training code should filter on `incomplete`.

**Late evidence.** A record is never rewritten. A provider-reported end time arriving after an observed one, or a signal that was `null`
and is known on a later bounded re-check (`late_checks` times, `late_check_interval_minutes` apart, inside `late_window_hours`, only
when all sources are readable), is appended as a `PullRequestTrailLateEvidence` event (whitelisted keys; deduplicated by a content
digest) and exported as its own line. A reopened PR that ends again gets a new record (`end_seq` 2).

## Export

`GET /api/v1/learning/trails` and `trails.jsonl`. **Scope: the export is fleet-wide.** There is no per-project scoping in the control
plane, so access is by role: `require_role` (default `operator`; `viewer` opens it to read-only keys). A viewer key under the default
gets 403; a missing or wrong key gets 401.

| Parameter | Meaning |
| --- | --- |
| `limit` | 1 to `page_max` (default `page_default`) |
| `cursor` | the `next_cursor` of the previous page; records after it, oldest first |
| `since` | an ISO 8601 time; only records assembled at or after it |

Answers `{schema_version, records, count, has_more, next_cursor, truncated_before, cursor_truncated}`. JSONL answers one JSON object
per line (`application/x-ndjson`) with headers `X-Has-More`, `X-Next-Cursor`, `X-Truncated-Before`, `X-Cursor-Truncated`. A bad
parameter (including a blank one) is a 400; disabled is 404. Reads use an in-memory index (a page costs the page, not the store).

**Retention.** `max_records` (default 20000) bounds what is indexed and exported. The oldest PRs are dropped whole; `truncated_before`
is the cursor before which nothing is available any more, and `cursor_truncated` tells a client its cursor pointed into pruned data.
The durable log keeps the events; the same pruning is applied again after a restart.

## Configuration

YAML only, under `control_plane`; no environment variables:

```yaml
control_plane:
  learning:
    enabled: true
    max_agents: 12                    # 1..50
    sweep_batch: 20                   # 1..100
    page_default: 50
    page_max: 200                     # 1..500
    max_records: 20000                # 100..1000000
    require_role: operator            # operator | viewer
    warmup_minutes: 10                # 0..1440
    retry_interval_minutes: 5         # 1..1440
    ready_retry_minutes: 60           # 0..1440, then stored incomplete
    low_confidence_after_minutes: 15  # 1..10080
    late_checks: 3                    # 0..20
    late_window_hours: 24             # 1..720
    late_check_interval_minutes: 30   # 1..1440
```

Unknown keys are refused at start.

## Limits

- Single process: the sequence counter and the index live in one process (derived from the stored events at start). Two writers on the
  same log would collide; a counter behind the stored records refuses to write.
- Until a source sends the provider's merge time, `ended_source` is `observed`; those records are `low` confidence when noticed late.
- The tool-event buffer behind the agent figures is in memory: after a restart tool figures are `null` and the record waits
  (then is `incomplete` with `agent_evolution.tools`).
- Reviews attach to a PR only through an agent, so `merged_without_rework` is often `null`.
- A permanently unavailable CI yields `incomplete` with `missing_sources: ["ci"]` after `ready_retry_minutes`, by design.
