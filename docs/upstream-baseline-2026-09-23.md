# Upstream Baseline — verified 2026-09-23

This is a dated research snapshot, not a permanent dependency lock. Spec 0020 must refresh it before implementation.

## Herdr core

Official/current docs establish these important primitives:

- persistent background sessions with detach/reattach;
- workspaces/tabs/panes and agent states;
- saved SSH machines in one client with independent reconnect behavior;
- machine-targeted CLI/API forwarding using `--machine`;
- native worktree APIs;
- a local socket API with schema export via `herdr api schema --json`;
- plugin manifests with startup hooks, actions, event hooks, panes and link handlers;
- per-plugin config/state directory discovery;
- native Pi integration for lifecycle/session reporting.

Useful references:

- https://herdr.dev/docs/
- https://herdr.dev/docs/connecting-machines/
- https://herdr.dev/docs/cli-reference/
- https://herdr.dev/docs/socket-api/
- https://herdr.dev/docs/integrations/

### Consequence

Do not implement a second multi-machine connection manager, worktree engine, generic agent detector, or separate plugin registry unless a required capability is demonstrably absent.

## Candidate upstream plugins verified as current research leads

These repositories existed and described compatible Herdr functionality at the time of this snapshot:

- `StructuPath/herdr-browser` — browser pane, CDP/browser observation, QA evidence.
- `eyalev/herdr-web` — mobile-first web UI/PWA for Herdr.
- `StructuPath/herdr-swarm` — parallel agents with isolated worktrees and review-first harvesting.
- `eightHundreds/herdr-worktreeinclude` — copies explicitly selected ignored files into new worktrees.
- `JonasBaeumer/herdr-file-annotator` — line-anchored code annotations/review.
- `plannotator/herdr-plannotator` — presents Plannotator reviews inside Herdr Browser.
- `plannotator/herdr-annotate` — terminal/document/agent-reply annotation/review.
- `itisbryan/herdr-gh-checks` — PR/CI/check status and review inside a Herdr pane.
- `cdowell09/herdr-pr-board` — cross-repository PR dashboard.
- `quinnjr/herdr-notifications` — OS notifications for agent status changes.

The audit must also search for newer/better alternatives and determine whether native Herdr features now make any plugin unnecessary.

## Tailscale

Current Tailscale Services provide stable named tailnet services/MagicDNS names decoupled from the host device, with TCP endpoints and service hosts. Tailscale Serve supports HTTP/HTTPS and TCP forwarding. This is a strong fit for the `dev:<port>` concept.

References:

- https://tailscale.com/docs/features/tailscale-services
- https://tailscale.com/docs/reference/tailscale-services-configuration-file
- https://tailscale.com/docs/reference/tailscale-cli/serve

### Consequence

Prefer a tailnet-only Tailscale Service for the stable `dev` name. HerdR Engineering still needs its own authenticated/fenced lease registry and target routing because Tailscale does not know which ephemeral Herdr worktree process owns a random port.

## Woodpecker/Pileated

Woodpecker remains an external CI engine. Pileated CI is the lab's backward-compatible enhanced fork/extension concept. HerdR Engineering should build a provider contract that works with Woodpecker-compatible APIs and adds Pileated-specific fields only as optional capabilities.

Reference:

- https://github.com/woodpecker-ci/woodpecker

## Refresh rule

Before locking any dependency:

1. verify repository identity and canonical owner;
2. inspect latest compatible release/commit;
3. inspect license;
4. inspect build/install hooks;
5. inspect network listeners and credential use;
6. inspect Linux/macOS support;
7. run its doctor/tests against the actual current Herdr version;
8. classify `ADOPT`, `ADAPT`, `OPTIONAL`, `HOLD`, or `REJECT`;
9. pin immutable revision in the dependency lock.
