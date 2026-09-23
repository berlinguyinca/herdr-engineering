# 0140 — Multi-Agent Worktree and Candidate Comparison

## Goal

Turn parallel Pi Engineering candidates into an understandable comparison workspace rather than dozens of anonymous sessions and ports.

## Candidate model

A candidate is a projection over existing ownership identifiers, not a new executor:

```text
mission/issue
  -> worker/session
  -> machine
  -> repository/worktree/revision
  -> diff
  -> dev-service lease(s)
  -> tests
  -> CI
  -> browser artifacts
  -> review
```

## UX

For one mission/issue, show candidate cards/table with:

- current status/attention;
- machine/session/worktree/branch;
- concise changed-file/diff summary;
- test and CI status;
- review state/findings summary;
- live `dev:<port>` previews;
- screenshots/browser error count;
- artifacts and latest activity.

Support side-by-side or rapid-switch preview, diff comparison and evidence inspection. Do not impose an opaque AI “winner” score unless an owning workflow explicitly provides evaluation criteria/results; preserve raw evidence and reviewer outputs.

## Lifecycle

Candidate cleanup must coordinate with the owner: close dev leases, materialize retained evidence, expire activity links as needed, then request/observe worktree removal. Never delete another worker's worktree based on path/name heuristics.

## Exit gate

A test mission with at least six concurrent isolated worktrees across multiple hosts can be grouped correctly, each preview/evidence set remains distinct, comparisons remain usable on desktop and reduced mobile view, and cleanup cannot remove another candidate's resources.
