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
- `sessions`, `ambiguous`, `handoffs`, `reviews`, `ci_runs`, `agents`: ids, states, counts, timestamps and rules only. **Every one of
  these six fields always has the same shape**, `{"items": [...] | null, "omitted": n, "reason": str | null}` (`schema_version` 1):
  `items` is the list, `[]` when it was cut for size, `null` when the source was unavailable; `omitted` counts what is not in `items`
  (cap, size or source), `reason` says why. A consumer never has to branch on list-or-object
- `agents.items[].learning_record`: an allowlist of the Evolution tab's figures: `session_id`, `harness`, `model`, `role`,
  `started_at`, `ended_at`, `duration_s`, `cost_usd_estimate`, `outcome`, `tool_calls.{total,failed,retried}`, `handoffs_in`,
  `handoffs_out.{total,rejected,failed}`, each `{value, reason}` with a scalar `value` (anything else becomes null). Its `prs` (the PR-wide
  signals again) and any other field are not stored
- `sources`: what was read and its state; `incomplete` and `missing_sources`; `omitted`; `trimmed`

### Privacy

An allowlist is stored, not a copy of the trail. Handoff goals, agent names, reviewer names, CI URLs and agent ids are not stored.
The remaining text is passed once, at write time, through the redactor (key-hint masking such as `password`/`token`), then scrubbed of
e-mail addresses (`[EMAIL]`, also `a%40b.com`, fullwidth `＠`, `a@localhost`, `a (at) b.com`) and home paths (`/home/<u>`, `/Users/<u>`,
`/root`, `~<u>`, `C:\Users\<u>`, `C:/Users/<u>` → `[USER]`), keys included. Input is cut to 4 KiB first (every field is clipped far below
that and marked), then NFKC-normalised and stripped of format/control characters (zero-width, bidi overrides), and every pattern is
linear (bounded local part and labels). An address needs a dotted domain with an alphabetic TLD or `localhost`: `lodash@4.17.21`,
`feat@v2` and ssh remotes (`git@github.com:org/repo`, `git@` generally) are left alone. Percent-escapes are decoded up to three rounds for
matching; when only the decoded form exposes something, the encoded text is scrubbed **in place** (a URL keeps its shape, so joins on it
work). Text that needs nothing is kept verbatim. Every string longer than 300 characters (200 for branches) is cut with a visible `…[truncated]` marker and named in
`trimmed` (`text:<field>`), signal reasons and agent text included.

**Branch names and similar identifiers (`fix/john.smith`) are kept as they are, by design:** hashing them would break joins with the
PR, CI and commit data the pipeline already holds. A branch that contains an e-mail address or a home path is scrubbed. The JSON and JSONL exports
return the stored record as is: they are identical in content and do not depend on any other redaction setting.

### Size

A record is hard-capped at 128 KiB. When over, whole lists are emptied (`items: []`, `omitted` = what was dropped, `reason` set) in this
order: `ci_runs`, `handoffs`, `reviews`, `ambiguous`, `sessions`, `agents`; then signal reasons are shortened; as a last resort only the core fields stay.
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
- a critical source (`ci`, `handoffs`, `reviews`, `plan_reviews`) is not `ok` (`ci` is `not_applicable`, null with a reason and not
  waited for, when no CI provider is configured at all), or an agent's Evolution sources (`ci`, `handoffs`,
  `reviews`, `plan_reviews`, `event_stream`, `tools`) are not `ok`, or an agent's record could not be read.

It is retried every `retry_interval_minutes`. A pass builds at most `sweep_batch` trails, never-tried PRs first, then the least recently
tried, so a backlog is worked through fairly and old not-ready PRs cannot starve newer ones. After `ready_retry_minutes` without a complete read it is stored anyway with
`incomplete: true` and `missing_sources` (for example `["ci"]`). Training code should filter on `incomplete`. A PR whose trail cannot be built at all (no repository or number, or never readable) is given
up on after `max_build_attempts` attempts: nothing is stored and a warning is logged. It is not abandoned: it waits `unbuildable_retry_hours`
(default 24) and is tried once more (one failure puts it back to waiting; a success writes the record and clears the state). The export
tells a consumer what is missing: `unbuildable: {count, items[<=20 of {pr_id, end_seq, reason, retry_at}]}` and `skipped_by_horizon`
(PRs left out because their end is older than what retention dropped); on the line form the headers `X-Unbuildable-Count` and
`X-Skipped-By-Horizon`. The bookkeeping is dropped once the PR changes or disappears. Retry clocks are in memory: a restart restarts them, and the warm-up plus the
`sweep_batch` bound keep the restart from releasing a stampede.

**Assembled once.** The set of ends ever assembled (rebuilt from the stored events at start) is separate from the export index. Retention
never removes a key from it, so a pruned PR is never assembled again, and an end older than the newest end retention has already dropped
is not assembled at all (it would be pruned immediately).

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
| `since` | an ISO 8601 time; only lines **written** (`assembled_at`) at or after it. It is a pull cursor by time, not a business date |
| `ended_after` | an ISO 8601 time; only records whose pull request `ended_at` is at or after it (late-evidence lines follow their record) |

Answers `{schema_version, records, count, has_more, next_cursor, truncated_before, cursor_truncated}`. JSONL answers one JSON object
per line (`application/x-ndjson`) with headers `X-Has-More`, `X-Next-Cursor`, `X-Truncated-Before`, `X-Cursor-Truncated`. A bad
parameter (including a blank one) is a 400; disabled is 404. Reads use an in-memory index (a page costs the page, not the store).

**Retention.** `max_records` (default 20000) bounds what is indexed and exported. The oldest PRs are dropped whole; `truncated_before`
is the cursor before which nothing is available any more, and `cursor_truncated` tells a client its cursor pointed into pruned data.
Pruning only trims the in-memory index and the export; the stored events are left alone, and at start the index is rebuilt from them and
pruned the same way, so `truncated_before` is the same after a restart. Late evidence for a pruned record is refused.

**Locks.** Writers take a writer lock (held across a durable write that waits for the sink); readers use only a short index lock, so a
slow sink never blocks an export. A read starts a background sweep only when a record is actually due; when nothing is due it costs O(1) (the repository's
`pull_request_version` and the next retry time say nothing moved), and `shutdown()` stops and joins it. A closed instance writes nothing,
even from a pass already running.

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
    max_build_attempts: 12            # 1..1000, then an unreadable trail waits unbuildable_retry_hours
    unbuildable_retry_hours: 24       # 1..720
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
