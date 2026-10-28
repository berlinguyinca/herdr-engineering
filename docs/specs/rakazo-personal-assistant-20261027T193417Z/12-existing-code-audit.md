# 12 — Verified repository inventory and mandatory reconciliation

Snapshot identified via accessible GitHub repository directory listings, 2026-10-27. Filenames indicate potential integration points, **not proof of finished implementations**.

### Herdr Engineering currently contains
Frontend: `packages/app.js`, `packages/conversation-composer.js`, `packages/conversation-markdown.js`, `packages/integrations-view.js`, `packages/scheduler-view.js`, `packages/artifacts-view.js`, `packages/attention-view.js`, `packages/activity-view.js`, `packages/herdr-design-system/`, `packages/herdr-web-components/`, `packages/nav-capabilities.js`, `packages/theme-controls.js`, `packages/index.html`.
Backend: `herdr_engineering/control_plane/`, `scheduler.py`, `artifacts.py`, `adapter_registry.py`, `attention.py`, `fabric.py`, `host_policy.py`, `quotas.py`, `llm_proxy.py`, `pi_autospec.py`, `observability.py`, `recovery.py`, `fleet.py`, `public_gateway.py`.
Docs: `README.md`, `DESIGN.md`, `HERDR_COMPATIBILITY.md`, `MASTER_SPEC.md`, `STATUS.md`, `IMPLEMENTATION_RECONCILIATION.md`.
README describes `/api/v1` control plane and SSE events, existing Herdr/Pi responsibility split. DESIGN specifies existing VanillaJS/Web Components and canonical design tokens; do not replace UI stack.

### Rakazo currently contains
`apps/api`, `apps/worker`, `apps/web`, `apps/mobile`, `apps/desktop`, `packages/core`, `packages/memory`, `packages/adapters`, `packages/adapter-kit`, `packages/contracts`, `packages/chat-ui`, `packages/ui-tokens`, `packages/ui-web`, `packages/auth`, `packages/db`; docs `computer-runtime.md`, `run-execution.md`, `tool-activity.md`, `self-host.md`, `agent-verification.md`.
README declares Pi, routines, delegation, browser/shell/desktop computers, voice and integrations; its auth/DB/scheduler stack is separate and must not be copied wholesale.

### Required implementation-time evidence file: `audit/current-state.md`
Create a table per desired feature: currently implemented? owner module/file? API/event? maturity status (production/in-progress/planned)? tests? reuse/refactor/adapt/new action? integration hazard? Link to SHA/line refs. Inspect original Herdr, Pi Engineering, InferWeave, memory, artifact repos locally. Read active spec queue and avoid conflicts with ongoing work. Record locked interfaces and file ownership. If a planned feature already exists, improve it rather than creating duplicate routes/components/services/schemas.
