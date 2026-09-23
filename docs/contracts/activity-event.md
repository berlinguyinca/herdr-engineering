# Contract: ActivityEvent

`ActivityEvent` is the normalized, observable event envelope that powers the per-session timeline and summaries. It is a projection/observability contract, not a replacement event bus for Pi Engineering or Herdr.

```yaml
activity_event:
  id: "evt_01..."
  occurred_at: "RFC3339"
  observed_at: "RFC3339"
  source: "herdr|pi-engineering|autospec|git|browser|test|ci|plannotator|user|system"
  type: "session.started|status.changed|task.started|task.completed|command.started|command.completed|git.commit|git.diff|test.run|ci.run|review.requested|review.completed|artifact.created|dev.registered|dev.expired|attention.required|note"
  severity: "debug|info|notice|warning|error"
  correlation:
    machine_id: "bender"
    herdr_session_id: "..."        # optional
    pi_session_id: "..."           # optional
    mission_id: "..."              # optional
    autospec_issue_id: "..."       # optional
    repository: "owner/repo"       # optional
    worktree_id: "..."             # optional
    branch: "..."                   # optional
    revision: "..."                 # optional
    test_run_id: "..."              # optional
    ci_run_id: "..."                # optional
  summary: "Human-readable observable fact"
  status: "working|blocked|waiting|idle|done|failed|unknown"  # when applicable
  artifact_refs: []
  metadata: {}                       # bounded, typed by source adapter
```

## Privacy rule

Activity events contain observable work facts only: commands, tool calls, test outcomes, files changed, Git metadata, artifacts, user-facing summaries, statuses and explicit agent handoffs. They must never persist hidden chain-of-thought, private scratch reasoning or synthetic reconstructions of it.

## Ordering and deduplication

- `id` is stable for retry/replay.
- Consumers sort the timeline by `occurred_at`, newest first in the primary UI.
- `observed_at` preserves ingestion delay information.
- Duplicate deliveries must be idempotent.
- Out-of-order arrival is allowed and must not corrupt current-state projection.

## Summary projection

The session client panel derives four bounded fields from structured activity:

- **Started:** why/how this session began and initial task context.
- **Completed:** durable completed work and evidence.
- **Current:** latest active task/status and location.
- **Next:** explicit next action when emitted by the owning workflow; never fabricate one from hidden reasoning.
