# Contract: ArtifactRef

`ArtifactRef` is the common pointer used by HerdR Engineering to link session activity, tests, CI, reviews, browser captures and candidate comparisons to durable evidence without copying payloads into every event.

```yaml
artifact_ref:
  id: "art_01..."                 # globally unique, stable
  kind: "file|directory|url|git_diff|log|screenshot|video|report|review|test_report|ci_log|other"
  title: "Short human label"
  uri: "file:///...|https://...|herdr-artifact://..."
  machine_id: "bender"            # optional when location matters
  repository: "owner/repo"        # optional
  worktree_id: "wt_..."           # optional
  revision: "git sha"              # optional
  media_type: "text/plain"         # optional
  size_bytes: 1234                  # optional
  created_at: "RFC3339"
  producer:
    system: "herdr-engineering|pi-engineering|autospec|woodpecker|plannotator|browser|other"
    session_id: "..."              # optional
  integrity:
    sha256: "..."                  # required for copied/shared files when practical
```

## Invariants

- A reference must resolve either locally on the owning machine or through an explicitly supported shared/private resolver.
- `ArtifactRef` does not imply that the artifact is safe to publish publicly.
- Secrets and credential stores must never become artifacts by automatic discovery.
- Large content is referenced, not embedded in activity/event payloads.
- Deleting a worktree must not silently invalidate retained artifacts; either materialize evidence first or mark the reference expired with a reason.
- The URI must not contain embedded long-lived credentials.

## Shared workspace behavior

When an artifact is copied into the shared artifact workspace, keep provenance back to its original machine/worktree/revision and use content hashing to avoid accidental ambiguity. Shared artifact storage is evidence/handoff storage, not source-code synchronization.
