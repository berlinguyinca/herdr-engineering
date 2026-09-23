# 0160 — Attention, Notifications and Review Actions

## Goal

Surface the small subset of a large agent fleet that actually needs human attention without creating notification spam.

## Attention model

Normalize actionable conditions such as:

- agent blocked or awaiting input;
- explicit approval/review requested;
- failed tests after worker completion;
- failed CI for an active mission;
- dev preview/browser validation failure;
- unhealthy host/service that affects active work;
- completed candidate set ready for comparison.

Attention items link to the exact session/mission/test/CI/review evidence and have lifecycle (`open`, `acknowledged`, `resolved`, `superseded`).

## Notifications

Prefer audited native/Herdr notification plugin behavior and add policy/aggregation around it. Support local/browser notifications first; optional external channels may be adapters.

Policy must provide:

- severity thresholds;
- dedupe/coalescing;
- quiet hours/configurable routing;
- per-project/user overrides;
- no secrets/log dumps in notification payloads;
- link back to the private HerdR view.

## Review/approval actions

Actions execute in their owner systems (Pi Engineering, Plannotator, CI, etc.) and require capability + authorization. The attention center records the observable action/result, not a second approval state machine.

## Exit gate

A simulated fleet producing repeated identical blocked/failure events creates one coherent attention item, sends policy-compliant notification(s), links to evidence, supports an authorized action and resolves when the owning system reports completion.
