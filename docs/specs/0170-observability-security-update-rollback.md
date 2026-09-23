# 0170 — Observability, Security, Updates and Rollback

## Goal

Make the complete system diagnosable and safely operable before calling it production-ready.

## Observability

Use correlated IDs across machine/session/mission/worktree/dev lease/test/CI/artifact. Expose metrics/logs for:

- machine/API connectivity and reconnects;
- plugin capability health;
- event ingestion lag/errors/dedupes;
- artifact publication failures;
- dev lease allocation, active count, route health and expirations;
- test adapter runs/events/errors;
- CI API freshness/errors;
- web/mobile latency and websocket reconnects;
- notification delivery/dedupe;
- version/lock drift.

Prefer OpenTelemetry-compatible instrumentation where practical. Never put secrets or hidden reasoning in telemetry.

## Doctor

`doctor` must have concise human output and stable JSON. Checks include versions/capabilities, private/public listeners, Tailscale reachability, config/lock validity, shared workspace, dev fabric, adopted plugins and relevant external providers.

## Security baseline

- least-privilege tokens/scopes;
- secrets from environment/secret manager, never repo;
- no public listener by default;
- validate URL/path/input boundaries;
- protect terminal/control actions with authz and audit;
- sandbox/escape user-supplied text in web log/diff/test rendering;
- do not allow arbitrary route targets through dev fabric;
- dependency pinning and update audit;
- document threat model and recovery procedures.

## Update lifecycle

```text
refresh audit -> compatibility test -> stage/canary host -> fleet rollout -> post-check
```

Keep previous known-good lock/config. Failed compatibility or doctor check blocks rollout. Rollback restores lock/config and restarts affected managed services safely.

## Exit gate

Fault injection for remote host loss, plugin crash, event duplication, shared storage loss, dev router restart and CI API outage produces bounded degraded behavior; security checks detect an intentionally public listener; update canary rejects a broken dependency; rollback returns to green acceptance checks.
