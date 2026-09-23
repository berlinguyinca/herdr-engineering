# HerdR Powerpack — Cumulative Greenfield Master Spec

## Status
Nothing has been implemented yet. This is the complete initial specification, not a migration.

## Goal
Create `berlinguyinca/herdr-powerpack`: one installable HerdR meta-plugin/distribution that provides our curated engineering environment primarily by adopting existing HerdR plugins.

Conceptual install:
`herdr plugin install berlinguyinca/herdr-powerpack`

## Core capabilities
- Existing Chromium/browser plugin integration.
- Existing mobile/PWA HerdR access.
- Plannotator integration.
- Swarm/worktree support.
- File/code annotation.
- GitHub/CI/PR visibility.
- Notifications.
- Cross-machine shared filesystem/workspace for contributors and agents.
- HerdR file browser over that shared workspace.
- Automatic per-agent session panel.
- Reverse-chronological session timeline (newest first).
- Automatic Started / Completed / Current / Next summaries.
- Artifact links among timeline, files, browser, Git and reviews.
- Dependency locking, health/doctor, safe update/rollback.
- Linux/macOS/Ansible deployment.

## Rules
1. REUSE FIRST: freshly search/verify HerdR ecosystem before building functionality.
2. Powerpack is thin integration/configuration/policy.
3. Preserve existing HerdR/Pi/AutoSpec configuration.
4. Pin reviewed/tested upstream revisions.
5. Private-by-default networking, oriented around Tailscale.
6. Never capture/display hidden chain-of-thought. Timeline records observable work/results/status.
7. Avoid competing task/workflow state machines.
8. Shared-storage failure must never corrupt local project work.
