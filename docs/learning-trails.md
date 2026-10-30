# Learning trails

When a pull request ends, the control plane writes **one durable record** of how agents got to it, so a training or analysis
pipeline can pull it later, after the 24 h live window has passed. Nothing here learns anything by itself: it keeps the evidence.

**Rule (the same as the PR trail and the agent Evolution tab):** a value is `null` with a reason unless there is positive evidence. A
figure that cannot be shown is never 0 or false, and nothing is guessed to fill a gap.

## What is stored

One `PullRequestTrail` event on the `pr_trail` stream of the PR (kept out of the global feed), written once per *end* of the PR:

- `schema_version` (1), `kind`, `assembled_at`, `pr_id`, `end_seq`, `learning_seq` (the export counter)
- `ended_at` and `ended_source`: `reported` (the provider's own merge/close time) or `observed` (the first moment this server saw the
  PR merged or closed, which can be later than the real merge)
- `pull_request` (host, repository, number, status, url, branch, base, times)
- `signals`: the PR trail's `merged_without_rework`, `ci_green_first_try`, `ci_reruns`, `cost_usd_estimate`, `duration_s`,
  `review_verdicts`, `handoff_problems`, each `{value, reason}`
- `sessions` (evidence-linked agents with the rule that linked them), `ambiguous`, `handoffs`, `reviews`, `ci_runs`
- `agents`: for each evidence-linked agent its `learning_record` from the Evolution tab (tool calls, handoffs, restarts, outcome, cost),
  each field `{value, reason}`. The PR-wide signals are not repeated per agent
- `sources`: what was read and its state (`ok`, `unavailable`, `truncated`, `partial`), `omitted`: what was cut off, `trimmed`:
  lists dropped to keep the record under 128 KiB

Agents that only share the PR's mission are a hint: they are counted (`inferred_sessions_count`) and never listed or used. All text
passes the redactor and a length cap again before it is stored. No raw tool arguments are stored.

A record is never rewritten. If the provider's own merge time arrives after a record was written with an observed one, one
`PullRequestTrailLateEvidence` event is appended (whitelisted keys only: `merged_at`, `closed_at`, `ended_source`, `note`) and the export
lists it as its own line. A PR that is reopened and ends again gets a new record (`end_seq` 2).

## When a record is written

Only for a PR whose record carries a merge or close time. A PR that ended before end times were stored is not assembled: it could not
be dated, and most of its sources have expired.

Assembly is a sweep, not part of the PR update: it runs with the server's maintenance pass (every 5 minutes) and before each export
read. At most `sweep_batch` records are written per pass; a PR whose trail cannot be read is retried after an hour. So a record appears
within about five minutes of the end, or at the next export.

## Export

`GET /api/v1/learning/trails` (viewer role, like the other read endpoints)

| Parameter | Meaning |
| --- | --- |
| `limit` | 1 to `page_max` (default `page_default`) |
| `cursor` | the `next_cursor` of the previous page; records after it, oldest first |
| `since` | an ISO 8601 time; only records assembled at or after it |

Answers `{schema_version, records, count, has_more, next_cursor}`. `GET /api/v1/learning/trails.jsonl` takes the same parameters and
answers one JSON object per line (`application/x-ndjson`), with `X-Has-More` and `X-Next-Cursor` headers; the last line's `cursor` is the
same value. Incremental pull: remember the cursor, ask again. Records and late-evidence lines are ordered by `learning_seq`, a counter that
only grows, so a cursor stays valid while records are appended. A bad `limit`, `since` or `cursor` (including a blank one) is a 400.
Disabled, both answer 404.

## Configuration

YAML only, under `control_plane` in the fleet config; no environment variables:

```yaml
control_plane:
  learning:
    enabled: true        # default; false writes nothing and the export answers 404
    max_agents: 12       # agents whose learning record is embedded per PR (1..50); more are counted in omitted.agents
    sweep_batch: 20      # records written per pass (1..100)
    page_default: 50     # records per page when `limit` is absent
    page_max: 200        # the largest page (1..500)
```

On by default and bounded: a record is at most about 128 KiB, a pass writes at most `sweep_batch` of them, and a PR whose trail cannot
be read backs off for an hour. Unknown keys are refused at start.

## Limits

- Until a source sends the provider's merge time, every `ended_source` is `observed`. The first export of a PR is therefore dated by when
  this server noticed, and a later correction arrives as late evidence.
- The tool-event buffer behind the agent figures is bounded and in memory: tool figures of an agent that ended before a restart are `null`.
- Reviews attach to a PR only through an agent (or an explicit pull request reference, which no review record carries today), so
  `merged_without_rework` is often `null`.
- A record describes the evidence held when it was assembled. Evidence that arrives later is not merged into it.
