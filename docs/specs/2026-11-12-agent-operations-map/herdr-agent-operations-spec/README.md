# Herdr Agent Operations — implementation pack

Mission: implement a beautiful, animated, real-time lot-map-inspired Agent Operations extension inside **both** native Herdr terminal panes and the existing web dashboard, with Rooms, agent-to-agent messages, task ownership, handoffs, model routing, context health, execution replay, drift warnings, and artifact/test/review lineage.

This pack contains four **generated design concepts**, not captured screenshots of a running Herdr instance. They are visual direction, not a license to replace the existing design system or assume depicted models/metrics exist.

Start with `ONE_SHOT_PROMPT.md`. Read all files in `docs/` before implementation. Discover the actual frontend code and live extension APIs before writing integration code.

## Reference images
- `references/01-workspace-overview.png`: workspace and task inspector
- `references/02-room-collaboration.png`: communication map and messages
- `references/03-terminal-map.png`: TUI map visual direction (terminal fidelity is constrained by renderer)
- `references/04-agent-operations.png`: dedicated Agent Operations screen

## Repositories confirmed by GitHub inspection
- berlinguyinca/herdr-engineering — distributed engineering cockpit, integration/distribution layer
- berlinguyinca/herdr-powerpack — pinned plugin bundle, doctor, rollback
- berlinguyinca/herdr-handoff — `/herdr::toClaude`, `/herdr::toPi`, `/herdr::toCodex`, `/herdr::handoff`, `/herdr::review`, `/herdr::verify`
- berlinguyinca/herdr-artifacts — cross-harness artifact contract
- berlinguyinca/herdr-fleet — host provisioning and drift
- berlinguyinca/herdr-shepherd — inspect current code and scope, do not infer from repo name
- berlinguyinca/pi-engineering — Pi integration
- alexeyw/lot-map — visual reference
- Deemer15/command-mesh — multi-provider reference

A public `berlinguyinca/herdr-dashboard` repository was not located; find the real dashboard code through repository discovery, local worktrees and remote configuration. Do not pretend to have inspected it.
