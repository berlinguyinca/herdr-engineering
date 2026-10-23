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
- **Dead sessions.** A session that is closed, has no terminal or is not ready
  returns 409 `fallback: helper_unavailable`. Delivery to a helper agent is a
  later change (`summary_fix.helper_fallback` is the seam).
- **Audit.** Each new attempt records a durable `SummaryFixRequested` event with
  the actor (`role`, and whether an API key or open mode authorized it),
  session, list, index, request digest and operation id. The item text is not
  copied into the audit event.
