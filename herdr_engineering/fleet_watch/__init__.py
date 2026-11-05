"""Merge-triggered fleet rollout watcher, phase 2: read-only core.

Polls GitHub for the head of `main` of each watched repo (ETag + rate-limit backoff), refuses any
SHA that is not published history (decision D3), keeps a private state file, and plans coalesced
rollouts. It never touches a host: no ssh, no events, no lock writes. See
docs/specs/herdr-fleet-rollout-watcher-20261104/spec.md.

Deliberately does not import `herdr_engineering.fleet`, which rewrites `sys.path` at import time.
"""
