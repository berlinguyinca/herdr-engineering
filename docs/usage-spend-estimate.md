# Spend estimate on the Usage page

The Usage page (`#/llm`) has a **Spend (est.)** tab that prices the tokens HerdR has recorded. It is an estimate,
not a bill, and it exists only when you give it prices.

## Enable it

Prices live in the central fleet config, in YAML, as dollars per million tokens. There are no built-in prices
and no environment variables.

```yaml
control_plane:
  usage:
    prices:
      - {model: "claude-sonnet-*", input: 3.0, output: 15.0, cached: 0.3}
      - {model: "qwen*", input: 0, output: 0}        # self-hosted: say so with an explicit zero
```

- The first entry whose `model` glob matches a record's model prices it (`fnmatch`, case-sensitive).
- `input` and `output` are required; `0` is a valid price. `cached` prices cache-read tokens; without it they are
  charged at the `input` rate, which can only overstate.
- `cached_in_input: true` says the provider counts cache reads inside `input_tokens` (OpenAI style), so they are
  not charged twice. The default is the Anthropic style, where cache reads are separate.
- Reasoning tokens are taken to be part of `output_tokens` and are not charged again.
- A bad table stops the control plane at start-up and names the key.

## What it shows

Per model and per UTC day for the selected window and filters, an estimated total, and every model no entry
matches under **unpriced** with its token count. Unpriced models are left out of the total, never guessed.
Only llm-proxy traffic is priced: agent transcripts and the proxy can describe the same call, so they are never
summed. Without a table the tab says how to enable it and shows no figure.

API: `GET /api/v1/llm/spend?window=24h|7d|…` (same filters as `/api/v1/llm/stats`).
