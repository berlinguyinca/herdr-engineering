# LLM page design

Status: draft for operator review, 2026-10-23. Statements marked **(guess)** were not verified in code or live data.

## Goal

Add one **LLM** page to the herdr dashboard. Operator requirements:

- Statistics: requests, LLM speed, token generation, and so on.
- Every agent links to its own usage view, and the usage view links back to the agent.
- A structured view of errors and problems.

The page is browsed from any fabric machine. On the operator's own dashboard it needs no sign-in. The public collaboration site must never serve it.

The page stores **metadata only** by default: counts, latency, TTFT, tokens in/out, tokens/s, status, error class, model id, agent id and session id. It never stores prompt or response bodies. Redacted failure samples exist only behind an explicit opt-in with retention limits. All configuration is YAML; nothing reads environment variables.

## What exists today

| Piece | Where | What it gives |
| --- | --- | --- |
| `herdr-llm-proxy` (compose `llm-proxy`, :8088, tailnet bind) | `herdr_engineering/llm_proxy.py`, `deploy/control-plane/compose.yaml` | An OpenAI-compatible forwarder. A per-project `hk-` key is checked by SHA-256. One upstream lab key goes to `llm.metabolomics.us`, which now runs on zapp. Custom models get a `fabric`/`cpu` route with fallback. The proxy caps in-flight requests per project and answers `429 proxy_backpressure` above the cap. |
| Usage record | `Handler._report` → `POST /api/v1/token-usage` → `repo.record_token_usage` | Records `project, key_id, model, success(bool), duration_ms`, plus `input/output/reasoning/cache_read_tokens` taken from the OpenAI `usage` object. Streams carry usage too, because the proxy forces `stream_options.include_usage`. |
| Storage | `repo.py` `record_event("token_usage", project, ...)` | Usage is stored as events in the durable event stream. Separately, migration `0001` defines a `token_usage` SQL table with `agent_id`, `error_code`, `retry_number`, `provider` and `host_id`. Nothing in `herdr_engineering/` inserts into it **(appears unused)**. |
| Aggregation | `repo.token_analytics(project, since)` → `GET /api/v1/analytics/tokens` | Totals plus `by_project`, `by_model` and `by_day`, computed by a full scan of the event map on every call. No percentiles, no latency figures. |
| UI | `packages/app.js` `viewAnalyticsTokens`, route `#/analytics/tokens` | Three metric tiles and three tables. No charts. |
| Session attribution | `herdr_engineering/session_env.py` (`herdr-eng session run`) | In `key_mode: session` it mints one key per session, labelled `session:<id>`, and revokes it on exit. The key reaches the agent through `OPENAI_BASE_URL`/`OPENAI_API_KEY` in the child's environment. |
| Gateway identity | `Handler._identity_headers` | Sends `X-InferWeave-Client-ID=herdr:<project>`. Session and agent ids pass through when they match `[A-Za-z0-9-_.:/]{1,128}`; otherwise the session falls back to `<project>:<key_id>`. |
| Per-session transcript usage | `deploy/herdr-cp-feed-plugin/session-transcript.js` → inspector `usage` events (`control_plane/inspector.py`) | Token deltas and context fill for pi, claude and codex, taken from each agent's own transcript. They add up into `usage_totals` per session. No latency, TTFT, status or errors. |
| Chart helpers | `packages/ci-charts.js`, `packages/ci-performance.js` | Hand-rolled SVG with `<title>`/`<desc>`, a table fallback, and stale/partial disclosures. Reusable as patterns. |
| Public isolation | `api.py` `_public()` + `_PUBLIC_STATIC` | The public listener answers 404 for everything outside an explicit allowlist (collab paths, `/mcp`, `/a2a`, collab static files). The dashboard and `/api/v1/*` do not exist there. |

## Gaps

1. **Most agent traffic bypasses the proxy.** pi sessions on herdr hosts call `llm.metabolomics.us` directly (memory `pi-identity-headers-live-path`), so the proxy never sees them. Claude and codex talk to their own vendors. Only agents started by `herdr-eng session run`, plus tools pointed at :8088, reach the proxy. To be "linkable to all agents", the page needs a second source: the transcript usage the inspector already collects.
2. **The proxy knows the session but drops it.** `_identity_headers` computes the session and agent ids. `_report` sends neither, although `record_token_usage` already accepts `session_id`.
3. **Status is flattened to a bool.** The record has no HTTP status, error class, stream flag, route kind (`fabric`/`cpu`), fallback flag or engine identity.
4. **Proxy backpressure 429s are never recorded.** `_forward` returns before `_forward_slotted` and `_report` run.
5. **No timing breakdown.** `duration_ms` starts after `acquire()`, so it excludes the proxy queue wait. TTFT and generation time are not measured.
6. **Retention is not enforced.** `retention.apply_retention` (30-day event TTL) is called only from tests. Usage events therefore accumulate without limit, and `token_analytics` slows down as they do.
7. **Stale env-var text.** The README still names `FABRIC_LLM_UPSTREAM_KEY` and `HERDR_ENGINEERING_LLM_INJECT`, which contradicts the YAML-only rule. Resolved by a follow-up: the README now describes `llm.upstream_key` in the central fleet file and `llm: {inject: false}`.

## Data model

Each finished proxied request writes one **request record**. It is a superset of today's payload, so `token_analytics` keeps working.

```yaml
# token_usage.recorded, payload v2 (all new fields optional; absent = unknown, never 0)
schema: 2
project: str            # from the key
key_id: str
session_id: str|null    # X-InferWeave-Session-ID / X-Session-Id, else key label "session:<id>"
agent_id: str|null      # X-InferWeave-Agent-ID
source: llm-proxy       # | transcript (see Attribution)
model: str              # requested id; upstream_model if rewritten
route: fabric|cpu|upstream
fallback: bool
stream: bool
status: int             # HTTP status returned to the client (incl. 429 backpressure)
error_class: str|null   # taxonomy below
queue_ms: int           # proxy slot wait
ttft_ms: int|null       # streams only
gen_ms: int|null        # first→last upstream chunk, streams only
duration_ms: int        # acquire start → last byte
input_tokens / output_tokens / reasoning_tokens / cache_read_tokens: int|null
usage_known: bool       # false when upstream sent no usage object
engine: str|null        # only if the upstream reports it; else null = unknown
```

**Rollups.** A rollup is written per `(hour, project, model, session_id, source)` and holds counts, token sums, error-class counts and a fixed-bucket latency histogram per metric. With fixed buckets, p50/p95 are computed from the histogram without re-scanning raw records **(the bucket bounds are a guess: log-spaced, 10 ms to 600 s)**.

**Retention (YAML).** Raw records are kept 14 days and hourly rollups 400 days. Both values are configurable. A sweep on the control plane enforces them for `token_usage` only, independently of the unused generic `apply_retention`. That sweep is the first real caller of any retention code.

**Store decision.** Two options:

- **Recommended:** keep raw records in the event stream, which replay already carries, and add a small materialized rollup projection in the repo.
- **Alternative:** start writing the existing `token_usage` SQL table and aggregate in SQL. It already has the right columns, but it would be the first direct-SQL path for usage. **(Decision 3.)**

## Attribution: agent ↔ usage

Every number shown is tagged with its source, and the page **never sums across sources**. A session that goes through the proxy and also emits transcript usage would otherwise be counted twice.

- **`proxy` (authoritative for requests, latency and errors).** The session id comes from the identity headers, or failing that from the key label `session:<id>`. The proxy has the label at authentication time, because the config table carries the key record.
- **`transcript` (tokens only).** These are the inspector's existing per-session `usage` events. They cover pi sessions that call the gateway directly, as well as claude and codex. Speed, TTFT and error figures are shown as "not reported by this source".
- **Precedence per session.** When a session has any `proxy` records in a window, the page shows the proxy figures, and the transcript figures appear only as a muted cross-check. Otherwise it shows the transcript figures.

**Id join.** The dashboard session id (`#/herd/session/:id`) comes from the control plane and feed plugin. `herdr-eng session run --session S` takes a free-form `S`. **(guess)** These ids are the same only when the launcher passes the dashboard id. Slice 1 must verify the join. If the ids differ, the mapping is recorded once at launch: an association event `{session_id, llm_key_id}`. **(Decision 4.)**

**Links.** Both are relative hash links, so the page works on whichever fabric host serves it.

- The session workspace header and the herd row get a "LLM usage" chip linking to `#/llm/session/<id>`. It shows today's tokens and errors.
- `#/llm/session/<id>` links back with "Open agent" (`#/herd/session/<id>`). Each row in the agent table on `#/llm` links to its usage view.
- Mission pages link to `#/llm?mission=<id>`. The mission id is already on usage events.

## Metric definitions

All metrics are computed per filter window (1 h, 24 h, 7 d, 30 d) and per group.

- **Requests:** the count of request records, including 4xx/5xx and proxy 429s. **Error rate** = records with `error_class != null` divided by requests.
- **TTFT:** `ttft_ms`, measured from the proxy's upstream send to the first upstream body chunk. It is defined for streams only. For non-stream requests it is unknown, never estimated.
- **Generation speed (tokens/s):** `output_tokens / (gen_ms / 1000)`. It is defined for streams with `usage_known` and `gen_ms ≥ 50 ms`.
- **End-to-end throughput:** `output_tokens / duration_ms`. It is shown under that name for non-stream requests and is never labelled "speed".
- **Engine timings:** used only when an upstream returns them (for example a `timings` object). They are stored under `engine_*`, tagged with the engine that reported them, and shown next to the proxy-measured figures, never merged into them. Field names are never assumed from llama.cpp, vLLM or SGLang. A missing field means unknown.
- **Percentiles:** p50 and p95 per group, taken from the histograms. A group needs **≥ 5 samples** before a percentile appears; below that the UI shows "insufficient data". This is the same rule as the CI observability spec.
- **Tokens:** sums of input, output, reasoning and cache-read tokens. Totals from records with `usage_known=false` are labelled as lower bounds.
- **Queue wait:** `queue_ms` p50/p95, which shows whether the per-project cap is binding.

## Error taxonomy

Each record gets one `error_class`. Every class comes from a code path that exists in `llm_proxy.py`, except where marked as a guess.

| Class | Trigger | Blame |
| --- | --- | --- |
| `auth_invalid_key` | 401, missing or unknown `hk-` key | client |
| `request_invalid` | 400 bad length, invalid JSON or non-object body | client |
| `request_too_large` | 413 (> 16 MiB) | client |
| `model_project_mismatch` | 403 | config |
| `model_not_registered` | 503 with `x-herdr-require-registered-model` set | config |
| `proxy_backpressure` | 429, project in-flight cap reached | fabric |
| `upstream_admission` | upstream 429/503 (gateway agent, session or user bucket full) | gateway |
| `upstream_rejected_metadata` | upstream 400 caused by identity headers **(guess: distinguished by body text)** | fabric bug |
| `upstream_client_error` / `upstream_server_error` | other upstream 4xx/5xx | gateway |
| `upstream_unreachable` | 502, connect or OSError before headers | network |
| `stream_truncated` | `IncompleteRead`, or EOF after headers were sent | gateway/network |
| `client_disconnected` | write to the client fails mid-stream **(needs a new except branch)** | client |
| `fallback_to_cpu` | fabric 429/503, then a retry on the custom model's own endpoint. Recorded as a flag; it is not an error when the retry succeeds | info |
| `usage_missing` | 2xx without a `usage` object | info |
| `empty_completion` | 2xx, `output_tokens == 0` **(guess: useful for reasoning models with no headroom)** | info |

**Model availability belongs to the gateway.** A model missing from the catalog is shown as the class above, not raised as a fabric problem (memory `model-availability-is-the-gateways-job`).

The errors view groups records by class, then model, then session. For each group it shows the count, the first and last time seen, and a link to the affected sessions. It does not page anyone; it is a visible list only.

## API routes

All routes are served on the **operator listener only**. Nothing is added to `_PUBLIC_STATIC` or to the collab allowlist.

```
GET /api/v1/llm/stats?window=24h&group=model|project|session|route&project=&model=&mission=
    → {window, groups:[{key, source, requests, errors, tokens{...}, ttft{p50,p95,n},
       speed{p50,p95,n}, e2e{...}, queue{...}}], partial, retained_since}
GET /api/v1/llm/timeseries?window=7d&step=1h&metric=requests|tokens_out|ttft_p95|speed_p50|errors
GET /api/v1/llm/errors?window=24h&class=&model=&session=   → grouped error classes
GET /api/v1/llm/sessions/<id>                              → per-agent view (both sources, labelled)
GET /api/v1/llm/samples?session=&class=                    → only when samples are enabled; else 404
GET /api/v1/analytics/tokens                                → unchanged, kept for compatibility
```

## UI sketch

`#/llm` is a new top-level nav entry, "LLM". The existing `#/analytics/tokens` page redirects to it **(Decision 5)**.

```
LLM                                   [1h|24h|7d|30d]  project▾ model▾  ⟳ 12s ago
┌Requests──┐┌Output tok──┐┌TTFT p50/p95┐┌Speed p50┐┌Errors────┐
│ 4,212    ││ 9.8 M      ││ 0.6 / 2.9 s ││ 41 t/s  ││ 1.8% (76)│
└──────────┘└────────────┘└─────────────┘└─────────┘└──────────┘
[requests/hour stacked by status]   [TTFT & speed p50/p95 by hour]
Models   model            req   out tok  TTFT p95  speed p50  err%
         qwen3.8-27b      3.1k  7.2M     2.4 s     44 t/s     1.1
Agents   agent (link)     src        req   tokens  speed  errors  last
         Amber Swift →    proxy      812   1.9M    39     3       2m
         Cedar Calm  →    transcript —     640k    n/r    n/r     7m
Problems class                  count  model        last   sessions
         upstream_admission     41     qwen3.8-27b  4m     5 →
         stream_truncated       9      glm-…        1h     2 →
```

**`#/llm/session/<id>`.**

- Header: the agent name, the "Open agent ↗" back link, and a source badge.
- Body: the same tiles scoped to that session, the request timeline, the model mix and that session's problems.
- When the data source is `transcript`, the latency tiles say "not reported by this agent's path".

Charts follow the `ci-charts.js` pattern: SVG with `<title>`/`<desc>`, a table fallback, visible `partial`/`retained_since` disclosures, and CSS tokens only. The page must work at phone width.

## Security and privacy

- **Bodies are never stored.** The proxy already holds the non-stream body in memory only to read `usage`, and keeps only the 64 KiB tail of a stream. Both stay transient. A test asserts that no record field contains message text.
- **Opt-in failure samples.** `samples.enabled: false` is the default. When enabled, the proxy stores only for error classes in the allowlist:
  - the status;
  - the upstream error JSON `type`/`code`/`message`, truncated to 512 bytes;
  - the request's `model`, `stream` and `max_tokens` values;
  - the message count and character counts.

  It stores no message content. Secrets (`hk-`, `Bearer`, `sk-`, and any value of an auth-like key) are redacted. Retention is 72 h and at most 500 samples, both in YAML.
- **No keys in records.** Records carry `key_id` only, never the key or its hash.
- **Public isolation.** The isolation is structural, and a regression test enforces it: every new `/api/v1/llm/*` route, `#/llm` and the new JS module return 404 on the public port.
- **Existing gap, out of scope here.** In open mode, anyone on the tailnet can `POST /api/v1/token-usage` and inject fake records. Keyed mode would close that, but it is marked unsupported in `fleet-config.example.yaml`.

## Configuration (YAML only)

```yaml
sections:
  control_plane:
    values:
      llm_proxy:               # existing: max_inflight, queue_wait_seconds
        raw_retention_days: 14
        rollup_retention_days: 400
        samples: {enabled: false, retention_hours: 72, max_samples: 500,
                  classes: [upstream_server_error, stream_truncated, upstream_rejected_metadata]}
```

## Runtime neutrality

- The proxy measures TTFT and speed from bytes on the wire, which works with any OpenAI-compatible engine.
- Engine-reported fields are optional extras tagged with the engine that reported them. If the upstream does not say which engine it is, `engine` is null and the UI shows "unknown".
- No code path branches on llama.cpp. The gateway's `x_*` catalog fields are already read defensively in `_fabric_model`; reuse that style.

## Test plan

- **`tests/test_llm_proxy.py`, with fake upstreams:**
  - Each error class is produced by its trigger.
  - A proxy 429 is recorded.
  - Streaming yields `ttft_ms` ≤ `gen_ms + ttft_ms` ≤ `duration_ms`.
  - A non-stream request has `ttft_ms=None`.
  - A missing `usage` object gives `usage_known=false` with null token fields, not 0.
  - The session id comes from the header first, then from the key label.
  - No body text appears in any record.
- **Repo tests:**
  - Rollup histograms: p50/p95 match a brute-force calculation within one bucket.
  - A group with fewer than 5 samples shows insufficient data.
  - Retention: the sweep drops raw records and keeps rollups.
  - Source precedence: no double counting.
  - Payload v1 records still aggregate.
- **API tests:**
  - Routes, filters and windows return the expected results.
  - Every `/api/v1/llm/*` route and the page module return 404 on the public listener.
  - `samples` returns 404 while sampling is disabled.
- **UI tests (node unit tests plus `tests/playwright/ui.test.mjs`):**
  - The links both ways (herd session ↔ llm session) work.
  - Empty, partial and insufficient-data states render.
  - At 390 px width there is no horizontal scroll.
- **Manual check after deploy (the operator deploys):** run one `herdr-eng session run` agent, then confirm its row, links and timings on `#/llm`.

## Build plan (PR-sized)

1. **Proxy enrichment.**
   - Add payload v2 fields, `queue_ms`/`ttft_ms`/`gen_ms`, `status` and `error_class`.
   - Record backpressure 429s.
   - Forward `session_id`/`agent_id` in the report.
   - Verify the session-id join (Decision 4).

   Tests in `test_llm_proxy.py`.
2. **Rollups, retention and stats API.**
   - Add the histogram projection and the `token_usage` retention sweep, configured in YAML.
   - Add `/api/v1/llm/stats`, `/timeseries` and `/errors`, plus the public-404 test.
3. **`#/llm` overview UI.** Add the tiles, two charts (reusing the `ci-charts` patterns), and the models, agents and problems tables. Add the nav entry and the `#/analytics/tokens` redirect.
4. **Per-agent view and links.** Add `/api/v1/llm/sessions/<id>`, `#/llm/session/<id>`, and the chips on the session workspace and herd rows.
5. **Transcript source.**
   - Expose the inspector's per-session `usage_totals` and deltas as the `transcript` source, with precedence rules.
   - pi, claude and codex sessions then appear even when they never go through the proxy.
6. **Opt-in redacted failure samples.** Add the samples route, the redaction tests and the TTL.
7. **(Later, separate design)** Ingest per-request rows from the gateway's own usage DB (`inferweave_console` on hazy, `reporting.requests_users`). That DB currently stores no session id (memory `usage-reporting-excludes-loadtest`), so it would cover only client-level attribution.

## Decisions needed from the operator

1. **Coverage for direct-to-gateway pi.** Options:
   - (a) Accept transcript-only tokens for those sessions (slice 5).
   - (b) Point the herdr hosts' pi providers at the proxy (:8088), so they get full latency and error figures. This is a pi provider config change and adds a hop.
   - (c) Later, ingest from the gateway DB.

   Recommended: (a) now, (b) per host once slices 1–4 are live.
2. **Retention numbers.** Raw 14 d, rollups 400 d, samples 72 h / 500: OK as proposed?
3. **Store.** Event stream plus a rollup projection (recommended), or start writing the dormant `token_usage` SQL table?
4. **Session id join.** May `herdr-eng session run` record a `{session_id, llm_key_id}` association when the free-form `--session` differs from the dashboard id?
5. **Route.** May `#/llm` replace `#/analytics/tokens`, which would redirect, with the Analytics card pointing to `#/llm`?
6. **Failure samples.** Should the capability ship at all (it stays off by default), and which error classes may it capture?

### Defaults chosen, change on request (2026-10-23)

The operator had not answered the questions above when slice 1 was built, so the doc's own recommendations stand as defaults. Each can be changed on request.

1. **Coverage:** (a). pi sessions that call the gateway directly get transcript-only tokens (slice 5). No pi provider is repointed at the proxy.
2. **Retention:** raw records 14 days, hourly rollups 400 days, configurable as `control_plane.llm_proxy.raw_retention_days` / `rollup_retention_days`. Samples stay unbuilt (item 6).
3. **Store:** usage stays in the existing event stream. The dormant `token_usage` SQL table stays unused.
4. **Session id join:** no association event is recorded; the join is resolved at read time (see "Slice 1 notes" below).
5. **Route:** `#/llm` will **coexist** with `#/analytics/tokens` for now. This overrides the redirect in the UI sketch until the operator decides.
6. **Failure samples:** not built. `samples.enabled` stays false and the samples route will not exist.

### Slice 1 notes (proxy enrichment, as built)

- **Record.** The proxy reports every finished request as a `schema: 2` record with the fields in the data model, plus `upstream_model` (the id actually sent upstream). `model` stays the requested id, so `by_model` in `/api/v1/analytics/tokens` is unchanged. v1 records (no `schema`) keep their old shape, with missing token counts stored as 0.
- **Whitelist.** `POST /api/v1/token-usage` and the proxy both go through `repo.record_token_usage_row`. It keeps only known fields and type-checks each one: ints ≥ 0 or null, error classes from the taxonomy, ids in the gateway metadata charset. Any other field (a prompt, a response, a key) is dropped.
- **Recorded rejections.** The proxy now records its own `429 proxy_backpressure`, the pre-dispatch `503`/`403`, and the `400`/`413` request errors. These records carry `route: null`.
- **401s are not recorded.** They have no project to attribute them to, and recording them would let an anonymous caller write records. This is a deviation from the taxonomy row `auth_invalid_key`, which stays reserved.
- **`upstream_rejected_metadata` is not distinguished.** Telling it apart would mean matching upstream body text, which is a guess. An upstream 400 is recorded as `upstream_client_error`.
- **Info classes are not error classes.** `usage_missing`, `empty_completion` and `fallback_to_cpu` are left out of `error_class`, so that the error rate (`error_class != null`) counts real failures only. They can be derived: `usage_known=false`, `output_tokens == 0`, `fallback=true`.
- **`client_disconnected`** is detected by a dedicated wrapper around client writes. `stream_truncated` covers any upstream read failure after the headers were sent.
- **`success`** = 2xx status and `error_class == null`. A stream cut off after a 200 is recorded with `status: 200` and `error_class: stream_truncated`.
- **`duration_ms`** now runs from the start of the slot wait to the last byte, so it includes `queue_ms`. It used to start after `acquire()`.
- **`engine` is always null.** No OpenAI-compatible upstream signal for it was found. Nothing is assumed about any engine.
- **Session id.** The id comes from the `X-InferWeave-Session-ID` or `X-Session-Id` header, then from the key label `session:<id>` (`llm_config` now carries the non-secret `label`), else null. The gateway-bucket fallback `<project>:<key_id>` is not a session and is never recorded as one.
- **Join check (Decision 4): server-side mapping built, live traffic still missing.** No association event is recorded; the control plane resolves each usage row's raw `session_id` at read time (`ControlPlaneRepo.resolve_usage_session`) and `GET /api/v1/analytics/tokens` returns `by_session`, keyed by dashboard session id or `unmatched:<raw id>`, with `matched_by` naming the rule. The raw id on the record is never rewritten. Rules, in order, never guessing between candidates:
  - `direct`: the value is a dashboard session id. Only ids inside the metadata charset can arrive this way, because the usage-row whitelist and the proxy drop anything else.
  - `decoded`: the dashboard id `herdr:<host>:<server>:<pane>` is built with `encodeURIComponent` per part (`emit.js`), so a pane `w1:p1` becomes `w1%3Ap1`, and `%` is outside `[A-Za-z0-9-_.:/]`. The encoded id can never travel in a header or key label. Its decoded *header form* (`herdr:beast:eng:w1:p1`) can, and is unambiguous because host and server contain no `:`. `herdr_engineering/llm_session_id.py` converts both ways; `herdr-eng session run --session <dashboard id>` now labels its key with the header form.
  - `agent_session`: pi sends its own session id as `X-InferWeave-Session-ID` (pi-engineering `admissionIdentityHeaders`, lane `options.sessionId`). Verified in pi-coding-agent's source: the agent's `sessionId` is `sessionManager.getSessionId()`, which is the session-file header `id` and the `<ts>_<id>.jsonl` file-name suffix. The feed plugin stores that file (or the id) as the session record's `agent_session`, so a native id that names exactly one record maps to it. Suffix matches require a whole UUID; two records with the same id give `unmatched`.
  - `unmatched`: everything else, including the proxy's gateway-bucket fallback.
- **What is still missing for live pi traffic.** pi on herdr hosts calls the gateway directly (Decision 1a), so its headers never reach the proxy and no usage row exists to join. Making the join live needs no pi code change: it needs the hosts' pi provider pointed at the proxy (Decision 1, option b, a pi provider *config* change). Without that, pi sessions keep transcript-only tokens. A pi change would only be needed to send the dashboard id itself; the native-id rule makes that unnecessary.
- **Retention.** `ControlPlaneServer.sweep_once` runs at startup and then every 5 minutes. It drops `token_usage` events older than `raw_retention_days` from memory and deletes them from `fabric_events` (`DurableEventSink.prune_events_before`). No other entity type is touched. `rollup_retention_days` is validated but has no effect until rollups exist (slice 2). A bad value stops `control-plane web` at startup.
