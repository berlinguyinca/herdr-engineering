# Vision delegation on the Handoffs page: end-to-end check against Pi's real wire format

Date: 2026-11-02. herdr-engineering branch `feat/vision-delegation-dashboard`; pi-engineering-runtime branch
`feat/vision-delegation` at `0bfe0c34` (read only, nothing changed there).

**A LIVE control plane was NOT used.** Pi's real emitter posted to a recording fake `fetch`; the captured envelopes were replayed
into herdr's real ingest and API running in-process (`ControlPlaneServer` on 127.0.0.1, port 0, in-memory `ControlPlaneRepo`).
No browser was opened. What is left for a person to check is listed at the end.

## Method (controller ruling H5)

The plan's original step ("after the Pi spike emitted real events to a control plane") was not possible: the Pi acceptance run used a
recording fake emitter, so no control plane ever received anything. Instead:

1. **Pi side, real code.** A Node script outside both worktrees (`<scratchpad>/e2e-vision/capture-pi-envelopes.ts`) imports Pi's
   `createLineageFromConnection` (`src/runtime/herdr/lineageConfig.ts`) and `emitVisionLineage` (`src/visionDelegation/lineage.ts`)
   and builds the real emitter stack: `LineageEmitter` -> `ControlPlaneIngest` -> durable `EnvelopeStore` outbox (temp dir).
   - `conn = { baseUrl: "https://control-plane.invalid", apiKey: <throwaway>, lineage: true, instanceId: "pi-workstation-1" }`
     (no `visibility`, so Pi's default `private` applies);
   - `fetch`: a fake that records URL, method, headers (`x-api-key` replaced by `<redacted>`) and the parsed JSON body and answers
     `200 {"ok":true}`; it also asserts the wiring passed `redirect: "error"`;
   - `os.hostname()` is replaced by `pi-workstation` (with `syncBuiltinESMExports`) and the outbox's `instance-id` file is pre-written,
     because the envelope `source` is `pi-<hostname>-<instance>`; this keeps the real machine name out of the fixture without editing it.
   - Run with the repo's own test preload, which isolates Pi state to temp dirs and switches off any machine herdr config:
     `node --import <pi>/test/support/isolatedState.ts capture-pi-envelopes.ts <pi> <out.json>` (Node 26 strips the types).
   - Five delegations, each with its own UUID request id, text model `local/qwen3.6-35b-a3b`, vision model `google/gemini-3-flash`,
     sessions `pi-text-session-<case>` / `pi-vision-session-<case>`: ok, partial, failed (back outcome `failed`), timeout, and an
     open one (out with no back).
   - **Flushing:** each `handoff()` hands its drafts to `ingest.emitDraft`, which stages them in the outbox and runs `flushOutbox()`;
     the script awaits `emitter.idle()` (resolves once every draft handed to the sink has settled) after each event and once at the end.
2. **Capture.** 30 `POST https://control-plane.invalid/api/v1/ingest` requests, no other URL. Saved, unedited apart from a header
   block, as `tests/control_plane/fixtures/vision_lineage_envelopes.json`.
3. **herdr side, real code.** `tests/control_plane/test_vision_lineage_contract.py` posts every envelope as-is to
   `POST /api/v1/ingest` with an `ingest`-role key, then reads `GET /api/v1/handoffs?limit=50`, `/api/v1/handoffs/<id>` and
   `/api/v1/handoffs/overview`.
4. **Dashboard, real code.** A replay script (`<scratchpad>/e2e-vision/replay_to_rows.py`, same server, pausing 1.5 s before each
   `-back` node so durations are non-zero) saved the API's page rows, detail rows and overview attention as
   `tests/control_plane/fixtures/vision_lineage_api_rows.json`; `<scratchpad>/e2e-vision/render.mjs` ran the real
   `packages/handoffs-view.js` functions over them, and `packages/vision-lineage-contract.test.js` pins the result.

## What Pi sent

```
emitter.stats() = {"emitted":44,"dropped":0,"errors":0}; drafts handed to the sink: 44, distinct event ids: 30; ingest POSTs: 30
headers: {"content-type": "application/json", "x-api-key": "<redacted>"}
source: pi-pi-workstation-5e1f0a2b, source_sequence 1..30, schema_version 2, entity_type lineage, visibility_scope private
```

Per delegation: out = `node.upsert handoff:vision-<id>-out` (state `acknowledged`, kind `vision`, cause `vision_delegate`) +
two `session:` upserts + `edge.add handoff`; back ok/partial = the same four (state `committed`, kind `vision` / `vision_partial`,
cause `vision_return`, sessions reversed); back failed/timeout = only the handoff node (state `failed`, kind `vision_failed` /
`vision_timeout`; Pi adds no edge for a failed state). 4+4+4+4+4+1+4+1+4 = 30. The 14 extra drafts are the endpoint session upserts,
which Pi drafts twice per committed/acknowledged handoff with the same event id; the outbox dedupes them by id before sending.

First envelope (verbatim from the fixture):

```json
{"schema_version": 2, "event_id": "lin_…", "source": "pi-pi-workstation-5e1f0a2b", "source_sequence": 1, "occurred_at": "2026-11-02T21:4…Z",
 "entity_type": "lineage", "entity_id": "handoff:vision-0b7e6f52-3c1a-4d8e-9f20-6a1b2c3d4e01-out", "event_type": "node.upsert",
 "payload": {"node_type": "handoff", "node_id": "handoff:vision-0b7e6f52-3c1a-4d8e-9f20-6a1b2c3d4e01-out", "state": "acknowledged",
   "attrs": {"handoff_kind": "vision", "from_session_id": "session:pi-text-session-ok", "cause": "vision_delegate",
     "from_model": "local/qwen3.6-35b-a3b", "to_model": "google/gemini-3-flash", "to_session_id": "session:pi-vision-session-ok",
     "instance_id": "pi-workstation-1", "host": "pi-workstation-1"}},
 "visibility_scope": "private", "evidence_refs": []}
```

## Results

### Ingest: accepted as-is

All 30 envelopes: HTTP 200. Re-posting all 30 (an outbox retry after a lost ack): 200 each, and still exactly 9 vision rows. No
envelope field had to be changed.

### Real finding (fixed): the page rows dropped the `vision` summary

The first run of the contract test failed:

```
E           KeyError: 'vision'
tests/control_plane/test_vision_lineage_contract.py:64: KeyError
1 failed in 0.73s
```

`row_from_node` adds `vision` to a lineage row, but `GET /api/v1/handoffs` (the endpoint the Handoffs page's `loadPage` uses, via
`client.pageHandoffs`) and the overview's attention list return `HandoffRegistry._summary(row)`, which copied only
`origin/cause/from_model/to_model/model_change/checkpoint`. So with real data `visionRoundTrips`, the round-trip rows, the vision stats
line and the outcome text on cards would never have appeared: Tasks 1-3 were tested on synthetic node views and on dashboard rows
that already had `vision`. Fix (commit `181bfe7d`, unit test first): `_summary` carries `vision` for lineage rows that have one, and
only those; `docs/contracts/lineage-events.md` says so.

### API rows after the fix (`GET /api/v1/handoffs?limit=50`, Pi's key)

| row | state | cause | vision.phase / outcome | model_change | attention_reason |
|---|---|---|---|---|---|
| ok -out / -back | accepted / completed | vision_delegate / vision_return | out ok / back ok | switched | none / none |
| partial -out / -back | accepted / completed | " | out ok / back partial | switched | none / `vision result partial` |
| failed -out / -back | accepted / failed | " | out ok / back failed | switched | none / `failed` |
| timeout -out / -back | accepted / failed | " | out ok / back timeout | switched | none / `failed` |
| open -out | accepted | vision_delegate | out ok | switched | none |

9 rows (4 closed x 2 + 1 open out row); `vision.request_id` equals each delegation's UUID; out rows run text session -> vision
session and back rows the reverse; `from_model`/`to_model` are the text and vision model on both rows. Detail
`/api/v1/handoffs/handoff%3Avision-<partial>-back`: `vision.outcome == "partial"`, `handoff_kind == "vision_partial"`. Overview
attention holds exactly the partial, failed and timeout `-back` rows, each with `vision`. Another key (operator role) sees **none** of
the 9 rows: Pi's default `visibility: private` lets only the writing key read them.

### Dashboard functions over those rows (`render.mjs`, `now` = last row + 60 s)

```
visionRoundTrips: 5 trips
  4fbcad96-… open    open=true  durationMs=null out=…-out back=-
  3eab9c85-… timeout open=false durationMs=1506
  2d9a8b74-… failed  open=false durationMs=1507
  1c8f7a63-… partial open=false durationMs=1506
  0b7e6f52-… ok      open=false durationMs=1507
visionStats: {"total":5,"byOutcome":{"open":1,"timeout":1,"failed":1,"partial":1,"ok":1},"last24h":5,"last7d":5}
visionStatsLine: 5 vision delegations · 5 last 24h · 5 last 7d · 1 ok · 1 partial · 1 failed · 1 timed out · 1 open
filterByCause: vision_delegate 5 / vision_return 4 / null 9
listHtml: 5 data-roundtrip entries (states: open, timed out, analysis failed, partial analysis, analysis complete),
  "qwen3.6-35b-a3b → gemini-3-flash · 1.5s" on closed trips, out/result buttons per row
handoffCard (partial -back): "completed · pi-vision-session-partial → pi-text-session-partial ·
  vision result · model changed · qwen3.6-35b-a3b → gemini-3-flash · partial analysis · vision result partial · just now"
detailHtml (partial -back): "model changed · qwen3.6-35b-a3b → gemini-3-flash · partial analysis · request <code>1c8f7a63-…</code> · cause <code>vision_return</code>"
any rendered string contains "undefined": false, "null": false, "NaN": false
```

Observed, not changed (rendering, outside this task's contract fix): an **out** row's card and detail say "analysis complete",
because an out row's `vision.outcome` is always `ok` and `outcomeText` maps `ok` to that text. For the open delegation the detail
reads `model changed · qwen3.6-35b-a3b → gemini-3-flash · analysis complete · request <code>4fbcad96-…</code> · cause
<code>vision_delegate</code>` although nothing came back. The list collapses vision rows into round trips (which say `open`), so this
shows only where single rows are drawn: the detail panel, and the "Needs attention" cards (which hold only `-back` rows).

## Pinned in CI

- `tests/control_plane/test_vision_lineage_contract.py::test_vision_delegation_events_from_pi_reach_the_handoffs_api` (fixture
  `tests/control_plane/fixtures/vision_lineage_envelopes.json`): ingest accepts the envelopes as-is and idempotently; rows, causes,
  vision summary, models, model change, states, sessions, attention, detail, overview, private scope.
- `tests/control_plane/test_handoff_lineage.py::test_page_and_overview_summaries_carry_the_vision_summary_and_only_on_vision_rows`.
- `packages/vision-lineage-contract.test.js` (fixture `tests/control_plane/fixtures/vision_lineage_api_rows.json`): 5 trips, 4 closed
  + 1 open, outcomes per delegation, durations, stats, cause filter, list collapse, no `undefined`/`null`/`NaN`, and ids, request ids
  and model names escaped (each injected with `"><img src=x onerror=alert(1)>`).

Commands and results:

```
<venv>/bin/python -m pytest tests/control_plane/test_vision_lineage_contract.py tests/control_plane/test_handoff_lineage.py \
    tests/control_plane/test_handoff.py tests/control_plane/test_handoff_overview.py -q      -> 33 passed
<venv>/bin/python -m pytest tests/control_plane -q -k "handoff or lineage or graph"            -> 579 passed, 54 skipped
node --test packages/*.test.js                                                                 -> 216 pass, 0 fail (212 before + 4)
```

## Limits

- No live control plane, no network, no Postgres: the in-memory repo and in-process HTTP server. Durations in the rows fixture are
  the replay's pauses, not a real delegation's time.
- The Pi events were driven by calling `emitVisionLineage` directly with hand-chosen `LineageEvent`s, not produced by a real image
  delegation through `VisionDelegator`; the envelope layer below it (emitter, ingest, outbox, sequence, ids) is Pi's real code.
- The fake fetch always answered 200, so Pi's retry / back-off / dead-letter paths were not exercised (the duplicate re-post only
  shows the server side is idempotent).
- No browser check: Playwright is not installed in this worktree's `node_modules` (only browser binaries exist in the user cache),
  and installing it was out of scope. The HTML strings were checked, not their rendering.

## Left for the user

1. **A live control plane run after a release**: point a Pi with `herdr: lineage: true` at a deployed control plane, delegate an image,
   and confirm two vision rows per delegation in `GET /api/v1/handoffs`. Note Pi's default `visibility: private`: only the key Pi
   writes with can read the rows, so set `visibility: org` (or `room` + `room_id`) in the `herdr:` block, or read with Pi's own key,
   or the Handoffs page shows nothing.
2. **The browser page**: open Handoffs, expand the workspace (`Unassigned` when no mission/repository is known), and check the round
   trip rows, outcomes, durations, the stats line and the cause filter; take a screenshot.
3. **The Pi side from a real delegation**: confirm a real `VisionDelegator` run emits the same envelope shape as
   `tests/control_plane/fixtures/vision_lineage_envelopes.json` (same ids, causes, kinds and states).
