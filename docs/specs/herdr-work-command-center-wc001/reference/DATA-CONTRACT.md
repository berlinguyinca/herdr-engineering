# Minimal read-only WorkSummary v1

```
WorkSummary {
  work_id: canonical stable ID
  title: string
  source: mission|session|github_item|ticket
  source_ref: string
  project_ref?: string
  mission_key?: string
  session_ids: string[]
  pane_refs?: [{host_id,herdr_session,pane_id,terminal_id,session_id}]
  links: [{kind,entity_id,href}] // href generated with existing entityHref
  lifecycle: running|paused_quota|blocked|review|finished|failed|unconfirmed
  evidence: [{kind,ref,timestamp}]
  progress?: {percent, label, basis, measured_at}
  started_at?: timestamp
  completed_at?: timestamp
  updated_at?: timestamp
  next_action?: {text, source_ref, confidence}
  coverage: complete|partial|unconfirmed
}
```

All ID correlations must be based on existing source IDs and verified explicit relationships. Never correlate by title similarity alone. One mission may own many sessions; do not count both as independent tasks. Sessions without known missions remain standalone and visibly unassociated. Never treat stale reports as live. A completed agent does not mean the parent mission is complete. Completion count must derive from historical completion timestamps; if those are absent, show unknown instead of inferring weekly completions. Weekly intervals are half-open in user-configured time zone and handle DST.

Progress uses `measuredProgress` already in mission-catalog-view.js (acceptanceCoverage; fallback weighted DAG). No percentage for unknown evidence. Show separate verified task progress vs agent status, never turn model-generated estimates into facts.

Native pane target = session record host_id + herdr_session + pane_id + terminal_id; use existing terminal attach API, never construct ssh from arbitrary text. Web navigation = internal session/mission route, reuse entityHref. If stale pane ID, show refresh action, not auto-spawn.
