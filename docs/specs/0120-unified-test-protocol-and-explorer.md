# 0120 — Unified Test Protocol and Explorer

## Goal

Provide the familiar IDE test-tree experience across machines/worktrees and languages while executing the project's native test tools.

## UX

The Tests surface must support:

- tree hierarchy with running/passed/failed/skipped/error states;
- aggregate counts and elapsed time;
- click a node for stdout/stderr/logs, assertion message and stack/trace;
- exact source-file/line link in the owning worktree;
- rerun one test, group/suite or entire run;
- cancel when native runner supports it;
- filters for failed/running/flaky/recent;
- historical duration/failure signals;
- correlation to session/mission/commit/CI.

## Adapter architecture

Implement a runner adapter interface that emits `TestEvent`. Required release adapters:

- Python: pytest;
- Go: `go test` structured/JUnit-compatible output as appropriate;
- Rust: Cargo test / compatible structured reporter;
- Java: JUnit reports/framework-native events;
- Scala: sbt/ScalaTest/JUnit-compatible evidence as appropriate.

Do not make all projects adopt one wrapper just to appear in the UI. Where runners have no ideal live protocol, combine process progress + completed standard report import.

## Logs and artifacts

Bound streamed log data. Persist large traces/logs/reports as artifacts. ANSI rendering must be safe; never execute escape sequences that can inject terminal control in web UI.

## Cross-machine isolation

All events carry machine/repository/worktree/revision/run identifiers. Running identical test names in two worktrees must never merge their state.

## Exit gate

Representative fixture projects for Python, Go, Rust, Java and Scala display correct trees, deliberate failures show their traces/source links, rerun operations invoke native commands, and parallel worktrees do not cross-contaminate events.
