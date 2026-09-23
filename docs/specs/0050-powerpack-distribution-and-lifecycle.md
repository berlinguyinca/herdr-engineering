# 0050 — Powerpack Distribution and Lifecycle

## Goal

Provide one supported installation profile that pins and safely configures the adopted Herdr extensions/integrations without turning them into a monolithic fork.

## Powerpack responsibilities

- dependency manifest/lock;
- platform-aware installation;
- safe config merge and backup;
- capability/health checks;
- plugin enable/disable policy;
- version compatibility enforcement;
- update preflight;
- rollback to prior known-good lock/config;
- machine-readable `doctor` output.

## Candidate capabilities

The audit may adopt or adapt browser integration, herdr-web, Plannotator integration, swarm/worktree helpers, file annotation, GitHub checks/PR board and notifications. Do not require every candidate to be installed if native Herdr or a better upstream makes it redundant.

## Configuration precedence

Define explicit precedence:

```text
shipped safe defaults
< fleet/site config
< user config
< repository config
< session override
```

Merging must preserve unrelated user settings.

## Failure isolation

- Optional plugin startup failure degrades that capability, not core Herdr.
- Version mismatch is detected before destructive update.
- A rollback command restores dependency lock + managed config snapshot.

## Exit gate

One documented installation/update command produces the same compatible Powerpack on Linux/macOS validation hosts, `doctor --json` accurately reports capability state, and rollback from a deliberately incompatible test revision succeeds.
