# Fix devfabric serve forwarder wedge + enroll-sync self-heal (#76, #77)

## Summary

Fixes two fabric bugs and includes a review pass over the devfabric
lease/serve/enroll code paths.

### A1 — Issue #76: `devfabric serve` per-host forwarder wedge

The per-host forwarder kept its listener and accepted TCP handshakes but
never routed requests after an early failure in the worker
(`route_and_pipe`), and a request whose backend never read could sit in
the pipe forever. Fix:

- **Bounded timeouts on the client peek AND the full pipe.**
  `proxy_pair`/`prefixed_pipe`/`rewriting_pipe` now take an
  `inactivity_timeout` (default 30s): if neither direction moves bytes,
  the connection is closed. `DevServiceForwarder` passes its own
  `inactivity_timeout` into the accept-loop worker, so a stuck connection
  cannot hold a worker thread forever.
- **Per-connection worker failures are non-fatal.** `route_and_pipe`
  wraps its body in `try/except`: on any failure it logs the error,
  sends a `502 Bad Gateway` response when the request line was already
  peeked, and closes both sockets. The accept loop only ever spawns
  daemon threads, so a dead worker can never break the loop.
- **A dead/stuck worker can never starve subsequent requests.** Each
  connection gets its own thread with bounded inactivity, so the next
  request is routed immediately (covered by the regression tests).
- Backend dial failures now return a clean `502` instead of a raw
  dropped connection (previously a `TimeoutError` traceback in
  `route_and_pipe`).

### A2 — Issue #77

- **(a) enroll-sync self-heal gap.** When a manifest is present and the
  origin's port is absent from the gateway's `healthz` `bound_ports`,
  enroll-sync now re-enrolls even if the local-registry lease entry has
  expired (90s TTL with no local `serve` renewing it) and/or the sync
  bookkeeping says "unchanged" — the gateway's `bound_ports` is the
  authoritative view of what is actually serving.
- **(b) loud warning + `--require-gateway`.** Enrolling via the local
  registry while a gateway URL is configured
  (`HERDR_ENGINEERING_FABRIC_URL` or `fabric.gateway_url`) or a gateway
  is reachable now prints a loud `WARNING:` to stderr instead of
  silently degrading to `{"via": "local", "gateway": false}`. A new
  `--require-gateway` flag on `enroll` / `enroll-sync` makes the
  local-registry fallback a hard error (non-zero exit).

### A3 — Review pass over devfabric lease/serve/enroll

Trivial findings fixed in this PR:

- `rewriting_pipe` (used by the `rewrite` data-plane path in
  `fabric.py`) now shares the same bounded-inactivity pipe and closes
  both sockets on timeout (previously the rewrite path had no inactivity
  bound at all).
- `route_and_pipe` no longer leaks the client socket on the
  backend-unreachable path (the 502 branch now closes both sockets in a
  `finally`).

Non-trivial findings filed as follow-up issues (not fixed here):

- **Lease TTL/renewal race** — heartbeat rewrites the whole lease record
  (non-atomic store write) and a dead renewal loop silently expires the
  lease while the forwarder keeps the port:
  https://github.com/berlinguyinca/herdr-engineering/issues/89
- **Lease-id churn** — `DevServiceForwarder.start()` can re-register a
  new lease id/port, but the CLI keeps printing the old one:
  https://github.com/berlinguyinca/herdr-engineering/issues/90

## Tests

New regression tests in `tests/test_devfabric_serve_enroll_fixes.py`:

- `test_forwarder_stuck_connection_does_not_wedge_next_requests` — a
  backend that accepts but never reads: the stuck request fails cleanly
  within the bounded inactivity timeout AND the next request is still
  routed to a live backend (the exact wedge from #76).
- `test_forwarder_client_peek_is_bounded` — a client that connects and
  sends an incomplete request line does not wedge the forwarder.
- `test_forwarder_backend_down_fails_cleanly_and_recovers` — backend
  down => clean 5xx (no hang); live backend => routes normally.
- `test_enroll_sync_reenrolls_expired_local_lease_when_gateway_port_missing`
- `test_enroll_sync_reenrolls_when_port_missing_even_if_bookkeeping_unchanged`
- `test_enroll_via_local_with_gateway_configured_warns`
- `test_enroll_with_require_gateway_fails_when_local`

Full suite (worktree, herdr-eng venv): `511 passed, 1 failed, 4 skipped,
1 xfailed` — the single failure
(`tests/test_fleet_autoupdate.py::test_installer_renders_explicit_checkout_and_interpreter_without_enabling`)
is pre-existing/environmental (systemd manager init "No such device or
address" on this host) and fails identically on `origin/main`; it is
unrelated to devfabric/enroll code.

```
$ ~/.venvs/herdr-engineering/bin/python -m pytest tests/ -q -p no:cacheprovider \
    --ignore=tests/test_dashboard_browser.py --ignore=tests/test_browser_smoke.py
511 passed, 1 failed, 4 skipped, 1 xfailed in 42.35s
```
