# ONE-SHOT PROMPT — HERDR AGENT OPERATIONS

You are the lead orchestration agent for a cross-repository Herdr implementation mission. Your objective is to implement the attached `herdr-agent-operations-spec` pack, including all four reference images. Do not stop after planning.

**First** read README.md, all docs/*.md, and inspect each references/*.png visually. The images are generated concepts, not screenshots of the existing Herdr UI. You MUST discover and screenshot the actual frontend before redesigning it.

**Repository discovery**: inspect `berlinguyinca/herdr-engineering`, `herdr-powerpack`, `herdr-handoff`, `herdr-artifacts`, `herdr-fleet`, `herdr-shepherd`, `pi-engineering`, the actual Herdr runtime and web dashboard frontend. Find the frontend from local remotes, worktrees, package manifests and docs; `berlinguyinca/herdr-dashboard` has not been verified. Inspect upstream `alexeyw/lot-map` and `Deemer15/command-mesh` as visual/provider references, respecting licenses. Record evidence, integration points, existing capabilities, and gaps before modifying code.

**Implementation goal**: a native optional Herdr Agent Operations extension working both in terminal panes and online dashboard. Preserve the beautiful glowing animated live map as the central experience. Add Rooms, messages, ownership, task handoffs, model routing, cross-agent collaboration, context health, drift, replay, artifacts/tests/reviews, Work Command Center and fleet views. Reuse actual backend transports, session IDs, room permissions, plugin lifecycle and dashboard design tokens. No parallel orchestrator, dashboard, message bus, task database, or memory engine.

**Agent coordination**: Claude plans and slices micro-specs; Pi with available open-weight models implements backend/adapters/tests; Codex implements UI/UX, screenshots and renderer; independent Claude Opus reviews. Use existing Herdr rooms/panes and verified `/herdr::toClaude`, `/herdr::toPi`, `/herdr::toCodex`, `/herdr::review`, `/herdr::verify` where available. Assign disjoint worktrees/file ownership. Explicitly show delegation and review handoffs in task records. Use tiny changes and frequent tests.

**Operational constraints**: determine actual execution host. If the ZIP is in Mac Downloads, transfer to execution host first and verify its checksum; extract into a unique docs/specs directory. Never deploy to an unexpected host. Respect existing uncommitted work. Test locally after each slice; CI optional. Independent review must clear blocking findings before authorized integration-branch merge. No forced merges or bypass of protections. Preserve rollback paths.

**UX deliverables**: capture baseline screenshots from the real running Herdr UI; implement and capture desktop workspace map, full Agent Operations page, room collaboration, ownership/handoff inspection, execution replay, terminal 120x40 and 80x24, tablet and mobile. Compare actual screenshots with the four reference images, preserving existing Herdr UI conventions. Include accessibility and reduced motion.

**Verification**: prove observed events rather than simulate production success; label unknown provider signals as unknown. Test duplicate/out-of-order events, room authorization, cross-host reconnect, agent crash mid-handoff, model switch vs agent handoff, replay, context accuracy, telemetry redaction, renderer performance, disablement and existing-pane regressions.

Proceed autonomously from discovery through implementation, screenshots, review and authorized merges. Ask only for genuinely blocking access, destructive operations or non-inferable decisions. Finish with an evidence-based summary of commits, tests, screenshots, feature coverage, limitations and rollback instructions.
