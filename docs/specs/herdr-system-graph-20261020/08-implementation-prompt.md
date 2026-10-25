# Paste into Pi Engineering

Implement the Herdr System Intelligence specification from docs/specs/herdr-system-graph-20261020/. First audit actual repositories and current architecture. Identify and reuse all existing mission events, graph/UI code, trace instrumentation, telemetry, artifact tools, memory and storage; avoid parallel implementations. Report verified repo locations and an integration matrix before writing code. Treat any assumptions as unverified until checked.

Run phases P0-P6 with quality gates. Prefer the locally available Qwen workers for planning/implementation/review, and a separate vision-capable worker for UI review, subject to current availability. Preserve separation of duties: implementation and review use distinct workers/models when possible. Track progress continuously in Herdr with accepted feature counts, measured elapsed time, blockers and defensible ETA ranges. Never mark an item complete without passing tests and linked evidence.

Keep artifacts under this unique docs/specs path, don't overwrite other specifications. Create small reviewable PRs, preserve existing functionality, include screenshots and source-backed architectural diagrams, and stop for unresolved repository access or architectural conflicts.
