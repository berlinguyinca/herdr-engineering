# Herdr dashboard information architecture

Top-level tabs: Overview, Mission Graph, Repository Map, Request Flow, Agent & Model Fleet, Infrastructure, Changes & Impact, Timeline & Replay, Artifacts.

Overview: active missions, blocked tasks, queue wait, critical handoffs, failed reviews, stale sources, and repository activity. No fake aggregate numbers; every KPI links to underlying evidence.

Mission Graph: nested mission/task/worker graph, handoff edges labeled with state and elapsed time, live state transitions, critical path, blockers, retries, review gate, progress and honest ETA range. Click an edge for source and destination messages, timestamps, handoff contract, artifacts and failure history.

Repository Map: organizations -> repos -> packages -> services -> modules; cross-repo dependencies and deployed service edges; collapse clusters; search symbols; click through to exact commit/path/line. Distinguish static dependency from observed runtime call.

Request Flow: waterfall and trace graph across UI, Herdr, Pi, gateway, model, GPU, tool calls; include latency breakdown, queueing, retries, token metrics, trace gaps and correlation IDs. Redact prompt bodies by default.

Timeline: time scrubber, graph snapshot, before/after diff, root cause navigation, who/what changed and replay of mission execution; preserve historical topology.

UX: progressive disclosure for 10k+ nodes; clustering, filtering, semantic zoom, saved views, keyboard navigation, accessible contrast, screen reader summaries, viewport virtualization and graceful offline states. Web UI is read-only for artifact authoring; existing permitted operational controls may navigate to their owning workflow, never edit artifact JSON in the viewer.
