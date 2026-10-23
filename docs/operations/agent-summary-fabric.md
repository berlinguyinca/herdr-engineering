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
