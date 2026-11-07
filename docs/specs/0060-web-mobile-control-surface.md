# 0060 — Web and Mobile Control Surface

**Status:** retired · **Retired:** 2026-11-07 · **Why:** nobody used it; the control plane's UI (`herdr-eng control-plane web`) is the web surface. Removed: `herdr_engineering/web.py`, `herdr-eng web`, the `herdr-web` compose service, the `herdr-eng-web` user unit and its Ansible task, the Agent Mine fleet playbook (its only reader was this surface's `/api/agent-mine`). A `herdr-web` container left on the control host is removed by the herdr-fleet `control-plane` role's adoption. The text below is kept as the historical record.

## Goal

Make the HerdR cockpit usable from desktop browsers, iPhone and iPad over the private tailnet without replacing the native terminal UI.

## Approach

Prefer the audited `herdr-web` upstream if compatible. Extend/adapt through supported APIs rather than rebuilding all Herdr views.

## Required UX

Responsive navigation must expose at minimum:

- machines/connectivity;
- sessions/agents and status;
- session attach/input where authorized;
- activity/timeline placeholder surface for phase 0090;
- worktree/repository context;
- files/artifacts;
- browser/preview entry point;
- Tests and CI navigation placeholders for later phases;
- attention/approval affordances when those phases land.

Mobile must prioritize status/steering/review over trying to be a full desktop IDE.

## Networking/security

- Bind/private-route so the UI is tailnet/private by default.
- Reuse Tailscale identity/network boundary where suitable; add application authorization for sensitive actions.
- No public fallback listener.
- Reconnect restores view/session selection without restarting work.
- Terminal input is explicit and auditable.

## Exit gate

From an iPhone/iPad-sized browser on Tailscale, the operator can navigate all validation machines, inspect a running Pi session, reconnect after network interruption and send an authorized input without exposing the service publicly.
