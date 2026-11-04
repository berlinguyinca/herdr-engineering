# 04 — Dashboard UX, artifact explorer and iPhone client

## Design principles
Reuse existing Herdr design system, typography, side navigation, dark/light modes, status badges, responsive components, tables, permission gates and search. No brand-new visual language. Desktop supports multi-pane dense data; iPhone supports sequential detail screens and handoffs. Honor accessibility, keyboard navigation, small screens, loading/empty/error and offline states.

## Navigation proposal — adapt to current routes
- Home / Operations overview: current incidents, overdue commitments, active agents, recent artifacts, CI status, connector health, daily summary.
- Projects -> [Project] -> Overview | Timeline | Investigations | Artifacts | Work | Activity | Connections | Settings.
- Investigation detail: compact title/status/owner/evidence completeness, timeline, Evidence/Plots viewer, Related Issues/PRs/Tests, Decisions/Commitments, Agent activity, proposed next actions.
- Artifacts: existing registry UI augmented with related-project graph, source provenance, version differences and time position. Never fork the artifact viewer.
- Assistant: side panel (desktop) / full-screen sheet (mobile) with source-linked replies, previews, action proposals and approval cards.
- Operations -> Automations: policy definitions, incident queue, per-run trace and replay/disable controls.

## Timeline interaction contract
Chronological events grouped by day and investigation; toggle chronological/causal view. Each event has icon/source, actual timestamp + ingestion timestamp on hover, actor, short title, authoritative status, evidence links, inferred/verified label and related work. Search and filters persist as URL state and deep links. Selecting event shows split-pane details on desktop, stacked detail on phone. Support zoom and switch to graph of relations with selectable edges. Data from different security labels must not leak via counts, previews or autocomplete.

## Artifact detail
Thumbnail/plot preview, read-only original, comparison slider or side-by-side version diff, content hash, provenance, extraction history, associated emails, issues, PRs, CI checks, agent outputs, crosslinks, people and action history. Keep existing public/private share controls with explicit authorization. Charts/scientific attachments retain raw downloads and series metadata when supplied; no fabricated data points.

## Assistant actions
Answers must cite project evidence, show uncertainty, and offer specific deep links, never a generic dashboard URL. Typed UI navigation command with allowlisted route+filter schema. Action cards show what will happen, expected effects, authority, approval status, rollback availability and time-bound confirmation. A spoken/iPhone command cannot bypass policy. Remote mobile application uses same API and auth; optional native Rakazo custom-server connection must be validated against deployed API compatibility, not assumed to inherit Herdr features automatically.

## Default overview wireframe (structural)
```
┌ Herdr ─────────────────── Search ───────── Assistant ┐
│ Nav: Projects / Agents / Artifacts / CI / Operations │
├ Project: LC-BinBase                    Status  Owner ┤
│ [Open incidents] [Active agents] [Recent decisions] │
│ Timeline                 │ Selected event / Evidence │
│  • Email + 3 plots       │ plot preview               │
│  • Issue linked          │ source quote / attachment  │
│  • PR + 6 CI checks      │ links / confidence         │
│  • Validation pending    │ proposed follow-up action  │
├ Commitments  •  Linked Artifacts  •  Agent Activities┤
└──────────────────────────────────────────────────────┘
```

## Acceptance UI behavior
- Open any timeline event and follow evidence to actual source with permission checks.
- Selecting an artifact from email displays original, generated derivative, provenance and version history.
- Click PR or CI run and return preserving project context.
- Assistant request 'show GPU node's blocked CI agents' performs safe typed navigation, not brittle simulated clicks.
- Pending approval reachable and actionable on iPhone without exposing elevated credentials to device.
- Real-time updates reconnect without duplicating cards; loading/sync lag are explicit.
