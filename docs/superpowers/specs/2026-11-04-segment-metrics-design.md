# Segment metrics in the drift graph — design

## Intent
The drift graph shows where a session changed topic. The reader now also wants to see what each part cost: how long
was actually worked and how many tokens were used. The number that matters most is what the drift itself cost
("everything after the topic change took 55m and 1.4M tokens").

## Goals
- Each graph box shows **time worked** and **tokens** for its segment; a total line sits under the graph.
- The drift marker states the cost of everything from the drift point to the end of the session.
- Harness-neutral: Claude, Pi, Codex and OpenCode, from their own records.
- All numbers are computed by code from the history, never written by the model (as with counts and times today).

## Non-goals / anti-goals
- No dollar cost, no per-model split, no estimates. A segment with no usage data shows no token line; it never shows a guess or a 0.
- No new panes, no new commands; the graph pane and its sidecar carry the data.

## Decisions (user's)
- **Time worked**, with the wall-clock span next to it, e.g. `42m worked · 3h span`.
- Headline tokens are **fresh** (new input + output); cached re-reads are shown smaller and separately,
  e.g. `180k fresh · 3.1M cached`.

## Decisions (mine, to be checked)
- Idle threshold: gaps between consecutive timestamped events longer than 30 minutes count as idle and are dropped.
  A segment's worked time is the sum of the remaining gaps from its first prompt's time to the next segment's start
  (the last segment ends at the last event). Constant `IDLE_GAP_SECONDS = 1800`.
- Wall-clock span = next segment start (or last event) minus first prompt time.
- "Fresh" per harness: Claude `input + cache_creation + output`; Pi `input + cacheWrite + output`; Codex
  `input − cached_input + output` from `last_token_usage` per `token_count` event (the running total is not summed);
  OpenCode `input + cache.write + output + reasoning`. Cached: Claude `cache_read`, Pi `cacheRead`, Codex
  `cached_input`, OpenCode `cache.read`. Codex reasoning is already inside its output.
- Claude writes one usage per assistant message but repeats it on each content-block record: de-duplicate by message id.
- Usage is attributed to a segment by its record timestamp, using the same boundaries as agent counts. A boundary or
  usage record with no timestamp is not attributed (no guessing).
- Sub-agent (child) token usage that lives in separate records is not counted; a note says "main session only" when
  the history reports child records.
- Formatting: `1h05m`, `42m`, `<1m`; tokens `180k`, `3.1M`. Metrics lines are dropped before label lines when the
  pane is narrow (the numbered-list layout keeps them on the one line).

## Left to the builder
Exact box line layout and wording of the total line, within the width rules the renderer already enforces.

## Data shape
`read_history` gains `activity_ts: list[str|None]` (every event's normalized timestamp, in order, computed before the
400-event trim) and `usage: list[{ts, fresh, cached}]`. `build_graph(..., activity_ts=None, usage=None)` adds per
segment `worked_s`, `span_s`, `fresh`, `cached` (each `None` when unknowable) and a graph-level `totals` plus
`drift["worked_s"|"fresh"|"cached"]`.
