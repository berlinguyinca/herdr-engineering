# Agent summary model benchmark — 2026-10-22

Use **Qwen2.5-1.5B-Instruct Q4_K_M** for advisory background summaries with
`AgentSummaryService` prompt/schema version 3. The tested CPU configuration needs
four CPUs and a 3 GiB container limit. It does **not** achieve a dependable
two-second response. Preserve transcript links and the AI-generated label; these
summaries are lossy navigation aids, not evidence that work completed.

## Reproducible artifacts

| Artifact | Pinned identity |
| --- | --- |
| Selected model | `Qwen/Qwen2.5-1.5B-Instruct-GGUF`, revision `91cad51170dc346986eccefdc2dd33a9da36ead9`, file `qwen2.5-1.5b-instruct-q4_k_m.gguf` |
| Verified model bytes | `1117320736` |
| Verified model SHA256 | `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e` |
| llama.cpp image | `ghcr.io/ggml-org/llama.cpp:server@sha256:8c74db29f1a2bef3f0a331b52c4d6214601dfceed4bee0b94cc48b3b23ff257d` |
| Rejected model | `Qwen/Qwen3-0.6B-GGUF`, revision `23749fefcc72300e3a2ad315e1317431b06b590a`, file `Qwen3-0.6B-Q8_0.gguf` |
| Rejected model SHA256 | `9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031` |

The selected file was downloaded from the official pinned Hugging Face URL and
verified by byte count and SHA256 before loading. Both model files are cached in
`~/.cache/herdr-engineering/models/`. No commercial fallback was configured.

## Runtime and cases

Benchmarks ran on **bender**, with concurrent host workloads, using one llama.cpp
slot, four inference/batch threads, zero GPU layers, context 8192, and a random
loopback-only port. The selected model consumed approximately **2.16 GiB** during
inference, so a 2 GiB container limit is insufficient. Only the owned benchmark
container was replaced; benchmarking did not modify production.

Requests used alias `session-summary`, an explicit OpenAI-compatible endpoint,
350 maximum output tokens and a 45-second measurement timeout. The final payload
uses temperature zero, no few-shot example, explicit field definitions, bounded
message labels, a citation enum, and `latest_assistant_label` /
`unanswered_user_labels`. Labels reference existing messages without duplicating
transcript text. `chat_template_kwargs.enable_thinking=false` was retained in the
final unmodified service smoke and works with this Qwen2.5 template.

Three synthetic cases were manually assessed for grounded continuation:

1. Dashboard work: registry/worker completed; API integration and model benchmark
   unfinished; no deployment.
2. Database migration: backup/script prepared, staging fails on duplicate order
   184, no migration applied, resolve duplicate and rerun staging.
3. Newer unanswered request: password-reset fix completed; user subsequently
   requests adding keyboard navigation **and** browser verification.

## Observed latency and quality

| Candidate / payload | Samples | Median | Observed p95* | Assessment |
| --- | ---: | ---: | ---: | --- |
| Qwen3-0.6B Q8, tuned few-shot payload | 6 | 5.181 s | 9.906 s | 0/6 acceptable overall; copied example actions and falsely claimed migration completed in all three database runs. Rejected. |
| Qwen2.5-1.5B Q4, same few-shot payload | 6 | 5.746 s | 11.652 s | 4/6 adequate; no false migration completion, but stale steps remained. |
| Qwen2.5-1.5B Q4, selected label-only payload | 9 | 6.145 s | 8.861 s | 9/9 grounded overall handoff cards; no contradictory completion claims. |
| Selected model, unmodified production service smoke | 3 | 9.142 s | 9.260 s | 3/3 grounded; exact service request, including thinking flag, accepted. |

\* Nearest-rank p95 equals the maximum for these small samples. This is not a
production percentile estimate. Earlier prompt variants reached **18.540 s** on
new prompts under contention. A **45-second deployment timeout** provides room;
the service's generic default remains separately configurable.

The selected nine-run timings were:

- Dashboard: 5.296, 8.861, 6.145 seconds.
- Database: 7.599, 5.414, 5.379 seconds.
- Newer user request: 5.218, 7.258, 7.898 seconds.

The subsequent unmodified-service smoke took 9.142, 9.260 and 4.875 seconds,
respectively. The database handoff correctly retained failed staging and pending
repair; it never claimed production migration had completed.

## Limits and deployment recommendation

Enable only the explicit verified model endpoint, with four CPUs, **3 GiB**,
output limit **350**, and timeout **45 seconds**. Keep the single bounded worker,
cache, debounce and retry behavior. A verified GPU endpoint may improve latency;
no Nibbler GPU latency was measured here.

These are short synthetic conversations, not the maximum 12,000-character input
or a representative production distribution. Long transcripts and contention
can increase latency or exhaust the timeout. The model sometimes places an
attempted failed test run in `completed` while clearly identifying failure in the
handoff. On newer user requests, `where_left_off` preserved both implementation
and verification, but `next_steps` sometimes listed only one. Read the card as a
whole and use the source transcript for exact state. Temperature zero did not
make every run's wording identical.

Local raw evidence is retained in
`/tmp/herdr-summary-model-benchmark-v2.json` (rejected 0.6B),
`/tmp/herdr-summary-model-benchmark-v3.json` (initial 1.5B),
`/tmp/herdr-summary-model-benchmark-v8.json` (selected payload), and
`/tmp/herdr-summary-model-benchmark-final-smoke.json` (unmodified service).
These paths are local benchmark artifacts, not deployment dependencies.
