# Agent summaries through the fabric LLM

Summaries use a dedicated project key and the registered `herdr-agent-summaries`
model alias through the existing fabric proxy. The proxy owns authentication,
admission identity, usage accounting and routing. InferWeave owns worker selection
and model placement. A separate HerdR GPU scheduler is not introduced.

## Route policy

Register this operator-owned model policy using `POST /api/v1/llm/models`:

```json
{
  "name": "herdr-agent-summaries",
  "project": "herdr-agent-summaries",
  "base_url": "http://agent-summary-model:8080",
  "upstream_model": "qwen2.5-1.5b-instruct-q4_k_m",
  "fabric_model": "qwen2.5-1.5b-instruct-q4_k_m",
  "context_window": 8192
}
```

`fabric_model` opts this registration into fabric preference. The proxy accepts
an exact concrete model or unique advertised alias with hot/warm state, at least
8192 tokens per request and positive slots. Groups, ambiguous aliases, cold
models, insufficient/unknown capacity and incompatible modalities use the
registered CPU fallback. The gateway chooses among its admitted workers. A
nonstream capacity response (429/503) or connection/header failure also permits
CPU fallback before any response is sent. Application errors and started streams
are not replayed.

The bounded catalog cache expires after ten seconds. Catalog requests carry only
the private upstream key, reject redirects and fail closed to CPU on unavailable
or invalid evidence. `X-Herdr-LLM-Route` identifies `fabric` or `cpu` on inference
responses. A ready catalog entry is routing evidence; it does not prove an idle
physical GPU or authorize creating a runtime.

## Configure the summary client

Mint/reuse a project-mode key with the existing `session_env.acquire` helper for
project `herdr-agent-summaries`. It stores the key in the existing private project
cache. Put that secret only in the private central fleet configuration; never
print it, pass it in command arguments or commit it. The control-plane section is
excluded from host configuration slices.

```yaml
agent_summaries:
  enabled: true
  routing: fabric
  endpoint: http://llm-proxy:8088/v1
  model: herdr-agent-summaries
  api_key: <private dedicated project key>
  timeout_seconds: 120
  max_chars: 6000
  max_output_tokens: 350
```

Deploy the compatible application before setting the new transport fields, then
restart the web service to read its configuration. Keep the private CPU service
running while CPU fallback is allowed. Verify the registration, a successful
proxy request with `X-Herdr-LLM-Route`, and project token accounting before
considering migration complete. Summaries send stable conversation affinity and
require registered-model admission. A missing registration returns 503 instead
of forwarding an arbitrary alias upstream. Secret keys are absent from config
repr, summary projections and inference payloads; authenticated redirects fail.

## Automatic hosting on an empty GPU: outstanding fabric prerequisite

The currently deployed Go lab gateway can route registered workers but cannot
issue spec64 dynamic assignments. The Rust dynamic-node path is the placement
owner: `IW_DYNAMIC_MODELS`, operator-enrolled `iw.dynamic-node/1` nodes,
provisioning heartbeat, measured serving profile and checksum-qualified artifact.
Its free-GPU facts exclude busy cards. Existing registered/loaded workloads must
not be evicted to satisfy summary requests.

The authoritative [OPEN_GATES](https://github.com/InferWeave/inferweave-specs/blob/main/OPEN_GATES.md)
states that Nibbler hardware registration is not serving qualification and that a
Go root lacking spec64 cannot issue dynamic assignments. Rust ownership/executor,
profile qualification, admission, watchdog, rollback and floor safeguards remain
required. The hosting/ask path currently uses `NoHooks` and cannot reach READY.
Do not bypass those gates with an SSH launch or report telemetry as serving proof.

To enable automatic GPU hosting, the InferWeave owner must qualify the selected
Qwen artifact/profile on supported dynamic nodes, enforce idle/unreserved GPU
placement without preemption, and advertise the matching concrete model/alias
only after readiness/admission checks. The existing pinned artifact is documented
in [the model benchmark](agent-summary-model-benchmark.md). Once that qualified
capacity is advertised, this route can use it without changing the summary
client; until then the explicitly authorized CPU fallback continues through the
fabric proxy.

## "Let the agent fix this" on blockers and next steps

Operators see a **Let the agent fix this** button beside each item under
*Blockers* and *Next steps* on the summary card (capability `fix_summary_item`;
viewers do not get it). Pressing it calls
`POST /api/v1/sessions/{id}/summary/fix` with `{list, index, text}`.

- **Verified, not trusted.** The server re-reads the stored summary and requires
  `summary[list][index] == text`; otherwise it returns 409 `reason: stale_item`
  and nothing is sent. Only `blockers` and `next_steps` are accepted. The route
  is operator-only, same-origin and `application/json` only (like the CI and
  review POSTs).
- **Grounded packet.** The original session receives a fixed instruction prefix
  plus one JSON block marked as untrusted data: the item, the summary overview
  and where-left-off, branch/worktree/commit/repository, PR and issue from the
  inspector association, recent file and test facts, and the newest recorded
  messages (redacted, the same bounds as summaries). The whole message is capped
  at 6000 characters; the oldest messages are dropped first.
- **Delivery.** It uses the same native `herdr agent prompt` path and durable
  receipts as session messages. The operation id is derived from the session,
  item and summary generation, so a double click, second tab or replay returns
  the first receipt instead of prompting again. A transport failure may be
  retried up to three times; a queued (unknown outcome) receipt is never resent.
  Regenerating the summary allows a fresh request.
- **Harnesses.** pi and claude sessions with a native conversation identity are
  supported. Anything else returns 409 `reason: unsupported_harness`. Herdr
  0.9.1's `agent prompt` itself is not harness-specific, but codex panes report
  no `agent_session`, so delivery could never be confirmed.
- **Dead sessions: a text-only helper.** When the session is closed, has no
  terminal or is not ready, the same packet (with a helper prefix: propose a fix
  as text, nothing is applied) goes to a sandboxed helper in the operator's
  standing **summary-fix** collab room, through that room's requester agent. The
  API answers 202 `{status: "helper_started", delivery: "helper", helper_id,
  tools: false}`, and the card says *Started a helper (no tools): result appears
  under Helpers* with a link to **Collaboration → Helpers → Summary fixes**,
  where the helper's result (at most 20 KB, as the runner returns it) is shown as
  untrusted text. It is never applied, run or sent to anyone. Without a
  configured room the API keeps the 409 `fallback: helper_unavailable`.
- **Configuring the room** (once). As the operator, in the collaboration
  dashboard: create a room (for example `summary-fix`), connect an agent for it
  (for example `summary-fixer`, harness `claude-code` or `pi`), grant it **only**
  *Start helpers* (`spawn_helpers`) in that room, tick the helper family under
  **Helper profile** and leave *Helpers may use tools* off. Then add the two ids
  to the central fleet file and re-render the collab slice:

  ```yaml
  control_plane:
    values:
      collab:
        summary_fix:
          room_id: collab_…            # the room's id
          requester_agent_id: collab_… # the agent's id (an id, never its credential)
  ```

  **Renewal.** The requester is checked like any agent: its credential lives 30
  days from connecting, and its room grant lapses after 30 days unless the owner
  picks a later date (up to a year). The requester never uses its credential,
  but an expired one still refuses (409 `helper_requester_expired`: connect a new
  agent, grant it `spawn_helpers`, update `requester_agent_id`, re-render the
  slice and restart collab), and an expired grant refuses with 409
  `helper_grant_expired` (grant `spawn_helpers` again, with a later expiry).

  Only these two keys are accepted; anything else stops the collab service at
  start-up with `summary_fix: unknown keys`. The collab service refuses to start
  a helper (409, nothing is started) when the agent is not the operator's, is
  revoked or expired, holds anything besides `spawn_helpers` in the room
  (`helper_overgranted`), or when the room lets helpers use tools
  (`helper_tools_allowed`). The usual helper checks still apply to the requester:
  without `spawn_helpers` or an allowed family it is `helper_refused` (409); over
  a limit 429 `helper_limit`; with the panic switch on 503 `helpers_paused`; an
  unreachable collab service is 502 `helper_unreachable`. The dashboard reaches
  the room through `collab_operator_url` (operator listener route `POST
  fleet/summary-fix/helper`, 404 on the public site; `GET
  fleet/summary-fix/helpers` lists the results).
- **Replay safety for helpers.** The helper is requested under the same
  operation id the wake path would use (`summary-fix-<digest>-<attempt>`). The
  collab service stores it with the helper and returns that helper again for the
  same id (also after a restart), so a double click, a second tab or a crash
  between the start and the audit never starts a second helper. Once a helper
  took an item, the same item and summary generation is never also sent to the
  agent, even if the agent comes back; regenerate the summary for a fresh
  request.
- **Audit.** Each new attempt records a durable `SummaryFixRequested` event with
  `delivery: "wake"` (prompted the session; written before sending) or
  `delivery: "helper"` (written once the helper id is known, with `helper_id`),
  the actor (`role`, and whether an API key or open mode authorized it),
  session, list, index, request digest and operation id. The item text is not
  copied into the audit event. The collab log separately audits
  `helper.request` with the operation id.
