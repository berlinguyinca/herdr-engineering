# Contract: CIEvent

`CIEvent` provides a provider-neutral view of CI state while retaining Woodpecker/Pileated as execution authority.

```yaml
ci_event:
  schema_version: 1
  provider: "woodpecker|pileated"
  event_id: "..."
  timestamp: "RFC3339"
  repository: "owner/repo"
  pipeline_id: "..."
  run_number: 123
  revision: "git sha"
  branch: "feature/x"
  event_type: "pipeline|step|queue|runner"
  state: "queued|pending|running|success|failure|killed|blocked|unknown"
  step:
    id: "..."
    name: "test"
  runner:
    id: "..."
    labels: []
  queue:
    position: null
    queued_at: null
  links:
    provider_url: "https://..."
  artifact_refs: []
  capabilities:
    retry: false
    cancel: false
```

## Rules

- Provider APIs are the truth for CI status.
- Missing/stale provider data must display as unavailable/stale rather than inferred success.
- Pileated-only fields are optional capabilities; valid Woodpecker behavior remains supported unchanged.
- Control actions such as retry/cancel require upstream authorization and explicit capability advertisement.
- Correlate CI to Git revision first, then mission/worktree when reliable correlation exists.
