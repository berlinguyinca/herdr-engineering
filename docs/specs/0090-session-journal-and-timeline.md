# 0090 — Session Journal, Client Panel and Timeline

## Goal

For every HerdR/Pi engineering session, automatically show what it started to do, what it completed, what it is doing now and what explicit next step is known, plus a newest-first evidence timeline.

## Data model

Use `docs/contracts/activity-event.md`. This is a derived activity projection, not a competing Pi Engineering event bus/task state machine.

## Sources

Ingest structured observable facts from:

- native Herdr session/agent lifecycle;
- Pi/Pi Engineering semantic lifecycle events;
- AutoSpec issue/spec transitions;
- Git changes/commits;
- artifact publication;
- browser/dev-service events;
- tests;
- CI;
- Plannotator review;
- explicit human notes/actions.

Adapters should be independently retryable and idempotent.

## Session discovery and search

Provide a recent-session view scoped by repository/directory as well as global search. Operators should be able to find sessions without copy/pasting old session IDs or prompts. Index at least repository, worktree, branch, machine, session start/end time, current status, explicit mission/issue identity, and bounded summary text. Selecting a historical session opens its retained timeline/evidence and, when still live, offers native reattach.

This is a projection over native Herdr/Pi session identity, not a second session runtime.

## Client panel

Each session gets four primary views:

1. **CURRENT** — current observable state, task, repo/worktree/machine, blocking/attention state.
2. **TIMELINE** — newest first; filterable by source/type/severity.
3. **SUMMARY** — Started / Completed / Current / Next, incrementally maintained from explicit structured facts.
4. **RELATED** — artifacts, dev previews, tests, CI, PR/review, mission/issue, sibling candidates.

Do not expose hidden reasoning. Large logs are artifacts, not giant timeline events.

## Persistence

Persist enough normalized projection/event metadata to survive client and service restart. If an owning system can replay canonical events, store correlation/checkpoint metadata and avoid needless duplication of large payloads.

## Exit gate

A Pi session can disconnect/reconnect/restart its UI and retain correct timeline/current summary; duplicate/out-of-order events do not corrupt state; evidence links resolve; no chain-of-thought-like content is persisted by the journal.
