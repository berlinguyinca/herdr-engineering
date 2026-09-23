# Per-Agent Session Panel and Timeline

Every HerdR agent session gets an automatically populated client panel answering:
- How did the session start?
- What was requested?
- What did it work on?
- What was completed?
- What is happening now?
- What is next, if known?
- What artifacts/commits/browser/review evidence exist?

Default timeline order: NEWEST FIRST.

Conceptual UI:

CURRENT
10:54 Implementing responsive character view
      Editing CharacterDashboard.tsx
      Running frontend tests

TIMELINE
10:52 Implementation milestone
10:47 Browser review — screenshot linked
10:34 Plan approved
10:27 Repository analysis
10:21 SESSION STARTED — mission/repo/branch/worktree/model

SUMMARY
Started with:
Completed:
Current:
Next:

ARTIFACTS / RELATED
files, screenshots, reports, generated sites, commits/diffs/PRs, browser sessions, plan/review, issue/mission.

Header should include available agent/mission/state/host/repo/branch/worktree/model context.

Reuse HerdR session/event APIs and an existing timeline plugin such as herdr-insight if verified. Extend/adapt rather than replacing existing tracking.

Pi Engineering should emit semantic structured activity events when possible instead of requiring terminal-text inference.

Event schema should cover IDs, timestamp, type/phase/status, concise summary/details, host/repo/worktree/branch, related files/artifacts/commits/browser refs, mission/issue refs, source and correlation ID. Finalize against real HerdR APIs.

PRIVACY: Never store or display hidden reasoning/chain-of-thought. Store observable actions, user-visible statements, tool activity, build/test results, file/git changes and concise status summaries.

Persistence must survive UI reconnect/restart and support authorized cross-client viewing. Evaluate existing HerdR/plugin storage vs lightweight DB vs shared storage. Shared FS may hold exports/artifacts even if the event index uses a DB.

Retention is configurable. Old fine-grained events may compact into durable summaries while preserving milestones/artifact refs.
