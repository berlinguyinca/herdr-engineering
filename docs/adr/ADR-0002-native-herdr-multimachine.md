# ADR-0002 — Build on native Herdr multi-machine primitives

**Status:** Accepted — 2026-09-23

## Decision

Use Herdr's current saved-machine/SSH connectivity and machine-targeted API/CLI behavior as the base multi-machine transport. Do not build the previously discussed custom machine aggregation proxy.

## Rationale

Current Herdr already models independently reconnecting saved SSH machines and exposes machine-targeted operations. Duplicating this would add split-brain state, reconnect logic and authentication surface with little user value.

## Consequences

- HerdR Engineering may add fleet-wide projection/search/UX, but physical session ownership remains with the Herdr server on the owning machine.
- One unreachable machine degrades only that machine's views.
- Any required capability absent from native APIs should first be proposed upstream or implemented as a narrow plugin/adapter.
