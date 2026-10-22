# Private model for agent summaries

Agent summaries are disabled by default. Enabling them requires an explicit model
endpoint and alias. The service below runs the official Qwen2.5 1.5B Instruct Q4_K_M model on
CPU, inside the control-plane Docker bridge, with no published host port. It has
one inference slot, a four-CPU limit, 3 GiB memory limit and an 8192-token context.
Summary work is asynchronous and cached; opening an agent does not wait for an
inference request. Measure response time on the deployment host before choosing
request timeout and output limits. Initial short synthetic conversations measured
7.7 seconds median and 12 seconds maximum on the four-CPU deployment profile;
this is not a two-second service guarantee. See the
[model benchmark evidence](agent-summary-model-benchmark.md) for quality checks,
measured latency and workload limitations.

## Provision explicitly

From the checked-out release on the control-plane host:

```bash
# Downloads approximately 1.04 GiB only if the verified model is not already cached,
# and pulls the pinned llama.cpp runtime image. Does not start a service.
python3 deploy/control-plane/summary-model.py prepare

# Requires the prepared model, cached runtime image and existing CP Docker network.
# Starts the dedicated service and waits for its HTTP health check.
python3 deploy/control-plane/summary-model.py up
python3 deploy/control-plane/summary-model.py status
```

The default model directory is `~/.cache/herdr-engineering/models`; override with
`--model-dir /absolute/path` on every invocation when needed. Only the specific
model file is mounted, read-only. The directory and file are created with private
permissions. The official model revision and SHA-256 are pinned in
`deploy/control-plane/summary-model.py`; incomplete or corrupt downloads never
replace the cached file. A cached file is verified again before startup.

For a non-default control-plane Compose project, pass `--project PROJECT` on every
invocation. The model runs in its own `PROJECT-summaries` project and joins the
existing `PROJECT_default` network. Stopping it cannot stop application containers.
Ordinary `deploy.sh` runs do not provision, download or restart this model. Its
read-only bind source is an absolute cache path, so removing release worktrees does
not break a running model.

## Enable automatic summaries

In the central fleet file, merge this into the existing
`sections.control_plane.values` mapping (do not replace the other values):

```yaml
agent_summaries:
  enabled: true
  endpoint: http://agent-summary-model:8080/v1
  model: qwen2.5-1.5b-instruct-q4_k_m
  timeout_seconds: 90
  max_output_tokens: 350
```

Deploy the application release after updating the configuration. The configuration
is read at process startup. Check agent summary state and `generated_at` in the UI
or `GET /api/v1/sessions/{id}/summary`. A valid cached summary remains visible while
new conversation content is pending, or when inference is temporarily unavailable.
The UI reports the model and source coverage; summaries cover the bounded retained
conversation, not missing historical messages.

The deployed CPU profile uses a 90-second timeout: one retained conversation took
40.3 seconds, and a larger one completed in 84.5 seconds after exceeding the
earlier 45-second budget. This is background work; cached summaries remain available while
generation runs. Short synthetic benchmarks do not predict full-window latency.

The endpoint is directly attached to this dedicated model. Do not substitute the
shared gateway's unknown-model fallback: a misspelled alias could otherwise use an
expensive upstream model. An existing private GPU inference service may be used
instead by explicitly configuring its compatible endpoint and model; validate its
privacy, latency and output quality before enabling it. This bootstrap does not
connect to Nibbler or change GPU services.

## Disable and remove

Set `agent_summaries.enabled: false` in the central fleet file and redeploy the
application before stopping the model:

```bash
python3 deploy/control-plane/summary-model.py down
```

This removes only the summary service and retains the verified model cache.
Existing persisted summaries remain in storage. Application rollback does not
change the model service; the pinned runtime can continue serving a release that
supports summaries while an older release simply ignores that configuration.

Official artifacts: [Qwen model repository](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF)
and [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
