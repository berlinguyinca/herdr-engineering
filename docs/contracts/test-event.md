# Contract: TestEvent

`TestEvent` normalizes native test-runner output for the HerdR test explorer. Native runners remain authoritative; HerdR Engineering does not invent a new test framework.

```yaml
test_event:
  schema_version: 1
  run_id: "testrun_..."
  event_id: "..."
  timestamp: "RFC3339"
  adapter: "pytest|go|cargo|junit|sbt|custom"
  repository: "owner/repo"
  worktree_id: "wt_..."
  machine_id: "bender"
  revision: "git sha"
  type: "run.started|suite.started|test.started|test.output|test.finished|suite.finished|run.finished"
  test:
    id: "stable adapter test id"
    parent_id: "..."                # suite/class/module hierarchy
    display_name: "test_name"
    source:
      file: "src/..."
      line: 123
  result:
    status: "running|passed|failed|skipped|xfailed|xpassed|errored|cancelled|unknown"
    duration_ms: 42
    message: "bounded failure summary"
    trace: "bounded structured/text trace"
  artifact_refs: []
```

## Required adapter behavior

- Preserve stable test identity when the native framework exposes one.
- Preserve hierarchy (package/module/class/suite/test) rather than flattening everything.
- Stream progress when the runner supports it; otherwise import completed reports.
- Keep stdout/stderr/log payloads bounded and materialize large logs as artifacts.
- Map file/line references to the exact worktree/revision.
- Do not mix events from parallel worktrees or revisions.
- Re-run actions translate back into native runner commands, not into an internal runner.

## Initial adapters

Required for release: pytest/Python, Go, Cargo/Rust, JUnit-compatible Java, and Scala via sbt/ScalaTest/JUnit-compatible reporting as applicable. Adapter discovery must remain extensible.
