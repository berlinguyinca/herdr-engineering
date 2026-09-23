# Architecture

## 1. System model

HerdR Engineering treats the development environment as several independent ownership planes connected by typed references rather than one monolith.

```text
                 PHONE / TABLET / LAPTOP
                         |
                      TAILSCALE
                         |
        +----------------+----------------+
        |                                 |
  Herdr native TUI                  herdr-web / PWA
  local + saved machines             mobile control surface
        |                                 |
        +---------------+-----------------+
                        |
                 HERDR ENGINEERING
      distribution / adapters / correlated UX
                        |
   +----------+---------+----------+-----------+
   |          |                    |           |
 Browser   Session journal      Tests/CI    Dev fabric
 Preview   + artifacts          views       dev:<port>
   |          |                    |           |
   +----------+-------------+------+-----------+
                          refs/events
                              |
               +--------------+--------------+
               |                             |
        Pi Engineering / AutoSpec      GitHub / Pileated CI
               |                             |
             Pi agents                 pipelines/runners
               |
       Tern via llm.metabolomics.us
```

The Herdr server on each machine remains authoritative for its own panes, processes, workspaces, sessions and worktrees. A local Herdr client can aggregate saved SSH machines using Herdr's native multi-machine support.

## 2. Machine model

Initial validation fleet:

```text
macbook-m4   fry   beast   bender
```

Every machine is independent:

- its repositories remain local to that machine;
- its worktrees remain local to that machine;
- its credentials remain local to that machine;
- its Herdr server survives client disconnects;
- its Pi sessions survive cockpit disconnects according to Herdr/Pi persistence semantics.

Tailscale provides private reachability. Herdr uses normal SSH for machine control. No central HerdR Engineering component is allowed to pretend remote files are local source code.

## 3. Control plane versus data plane

### Control/metadata

HerdR Engineering may correlate:

- machine ID
- Herdr session/workspace/tab/pane/agent IDs
- Pi session/mission/worker IDs
- repository identity
- branch/worktree identity
- dev-service lease
- test-run/test-case IDs
- CI pipeline/step IDs
- artifact references
- Git commit/PR/issue IDs

### Source data

Source code stays in Git repositories/worktrees. Large outputs are referenced as artifacts rather than embedded into the activity ledger.

### Inference

All model/runtime/GPU concerns are outside Herdr Engineering. Pi uses Tern through `https://llm.metabolomics.us`. HerdR Engineering may display model/provider/throughput metadata if available, but may not place models or GPUs.

## 4. Native Herdr integration

HerdR Engineering must prefer, in this order:

1. current stable structured Herdr/socket API;
2. plugin event/action/pane contracts;
3. structured Herdr CLI wrappers where the API does not expose the capability;
4. upstream plugin extension/contribution;
5. a thin local adapter only when no maintained primitive exists.

It must not parse terminal text for state when Herdr/Pi/Test/CI APIs expose a structured signal.

At bootstrap, capture the installed schema with `herdr api schema --json`. Compatibility checks are based on capabilities/schema, not only version strings.

## 5. Web/mobile architecture

`herdr-web` is the preferred starting point for the mobile-first web surface. It remains an external dependency. HerdR Engineering may:

- configure it;
- expose it privately through Tailscale;
- add adapter endpoints/modules when necessary;
- contribute upstream patches;
- temporarily pin a reviewed fork if an upstream patch is pending.

It should not create a new competing terminal renderer or full web client unless the upstream audit proves the existing project cannot meet the requirements and records that decision in an ADR.

The web UI should expose the same conceptual navigation on phone, tablet and desktop:

`Workspaces · Agents · Files · Browser · Tests · CI · Activity`

## 6. Transparent development service fabric

The operator-facing address is stable and host-independent:

```text
http://dev:<leased-port>
```

A dev-service broker allocates a cluster-wide port lease to an agent/worktree. A tailnet-only service endpoint forwards that port to the physical `machine:local_port` target.

Preferred network primitive is current Tailscale Services/MagicDNS when available. The routing layer must support normal browser development traffic including WebSocket/SSE/HMR and, where required, generic TCP.

The dev registry is metadata, not a process scheduler. The agent/process remains owned by the Herdr pane on its source machine.

## 7. Session activity model

Every agent session has a correlated activity projection:

```text
CURRENT
  concise present activity

TIMELINE (newest first)
  semantic milestones/events

SUMMARY
  Started with
  Completed
  Current
  Next

RELATED
  artifacts / browser / tests / CI / commits / PRs / reviews / dev service
```

The activity ledger contains only observable facts and concise status summaries. No hidden reasoning is captured.

## 8. Shared artifact workspace

The shared workspace is for:

- screenshots
- generated reports
- test logs too large for inline display
- browser recordings
- review exports
- handoffs
- temporary cross-host artifacts

It is **not** for synchronizing active Git worktrees or secrets.

A backend decision is made after measuring current Linux/macOS/Tailscale options. The logical contract is backend-independent through `HERDR_SHARED_ROOT` plus `ArtifactRef`.

## 9. Tests

HerdR Engineering introduces a normalized test-event contract, not a new test runner.

Adapters translate native runner output/events into a common tree:

- Python / pytest
- Go
- Rust
- Java
- Scala

Every test node may link to source, stdout/stderr, structured failure/trace, duration, retries, worktree, agent session, and CI evidence.

## 10. CI

Woodpecker/Pileated remains the execution plane. The HerdR view consumes supported APIs and shows:

- repositories/pipelines
- queued/running/completed state
- steps
- logs
- runner/queue state
- commit/worktree correlation
- retry/cancel actions only where the upstream API and user permissions allow them.

HerdR Engineering does not reimplement CI scheduling.

## 11. Worktree comparison

Many agents may work on the same repository concurrently, but each owns an isolated worktree/branch. Comparison groups those candidates by mission/issue and correlates:

- diff summary
- test evidence
- CI evidence
- dev preview
- screenshots/browser evidence
- review status
- agent/session status

Comparison is evidence aggregation, not an automatic merge authority.

## 12. Persistence

Use the smallest durable storage needed for each concern:

- Herdr owns Herdr runtime/session state.
- Plugins own their state under Herdr-provided plugin state/config paths.
- The activity index and dev-service registry may use a lightweight local embedded database if file-based atomic records are insufficient.
- Shared storage holds artifacts, not coordination truth unless an ADR proves otherwise.
- Cross-host state must have explicit ownership and fencing/lease semantics.

## 13. Security

Default network posture:

- listen on loopback when a service is host-local;
- expose cross-device access only through Tailscale/tailnet controls;
- no public Funnel by default;
- no unauthenticated CDP/debug endpoints;
- no secret replication in shared storage;
- plugin install/update uses reviewed/pinned sources;
- no blind execution of marketplace code.

## 14. Failure model

The system must degrade by concern:

- one remote Herdr machine down does not disconnect others;
- shared artifact storage down does not stop source work;
- browser plugin down does not stop terminals/tests;
- CI API down marks CI stale/unavailable without inventing status;
- dev route target down invalidates/marks the lease without redirecting to an unrelated process;
- web UI down does not kill Herdr servers or agents;
- Tern pressure/backpressure affects model work but is not treated as a HerdR scheduling failure.

## 15. Implementation shape

Start as one repository with thin packages/adapters so architecture can settle before splitting repositories. New subpackages are justified only by clear lifecycle or language boundaries.
