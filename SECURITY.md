# Security

This repository controls developer machines, terminals, browser previews, test/CI evidence and private network routes. Treat changes as security-sensitive.

## Baseline

- Internal services are private/tailnet-only by default.
- Secrets are injected through approved local/secret-management mechanisms, never Git.
- Browser/CDP/debug endpoints must not be publicly reachable.
- Dev-service routes may target only registered/policy-approved endpoints.
- Terminal/process/control actions require authenticated authorization and audit.
- Shared artifacts exclude credentials and active source worktrees by default.
- Dependency updates are pinned and audited before rollout.
- Logs/activity do not capture hidden chain-of-thought.

## Reporting

Do not open a public issue containing credentials, exploitable private topology or sensitive logs. Use the repository owner's private security-reporting channel once the GitHub repository is created and configure GitHub private vulnerability reporting if available.
