# 0130 — Woodpecker / Pileated CI Integration

## Goal

Make remote CI feel like another pane of the distributed IDE while preserving the CI system as execution authority.

## Provider layer

Implement a Woodpecker-compatible provider contract using `CIEvent`. Pileated extensions are capability-gated optional fields/actions so normal Woodpecker pipelines remain valid.

## Required views

- repositories and recent pipelines;
- pipeline state/duration/ref/commit;
- step tree and step logs;
- queue order/wait time where exposed;
- runner inventory/state/labels/capacity where exposed;
- commit/PR links;
- correlation back to HerdR session/mission/worktree/test evidence when reliable.

## Actions

When supported and authorized:

- retry pipeline/step;
- cancel run;
- open provider UI;
- create a “Debug with Pi” handoff containing revision, failing step, bounded log summary and artifact references.

“Debug with Pi” starts/targets the owning Pi workflow through its API; it does not make HerdR the mission orchestrator.

## Freshness

Display provider timestamp/refresh state. Network/API failure becomes `stale`/`unavailable`, never guessed success.

## Exit gate

Against a real Woodpecker/Pileated-compatible environment or controlled integration fixture, HerdR shows pipelines, logs, queue and runners; correlates a known commit; securely retries/cancels only when authorized; and can hand a failing run to Pi with bounded structured context.
