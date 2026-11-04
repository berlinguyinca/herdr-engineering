# Pi assistant activation CLI validation

Validated on 2026-10-31 in worktree `.worktrees/herdr-assistant-cli`, branch
`feat/assistant-pi-runtime`, base `d30a68c`.

The CLI tests use a real local `ControlPlaneServer` and authenticated
`CollabService`. They cover:

- owner-safe host listing and versioned JSON output;
- exact create payload and omission of an unspecified model;
- queued creation, deterministic replay, inspect, bounded status, and cancellation;
- no-host, stale-host, package/runtime-digest mismatch, and model-policy refusal;
- cross-user and agent-credential administration denial;
- absence of public credentials;
- a receipt-driven ready agent accepting work through the existing task contract; and
- parser absence of command, image, path, environment, grant, owner, or credential flags.

The runtime backend tests remain the source of truth for private operator attestation,
claim tokens, leases, monotonic checkpoints, ready receipts, cancellation acknowledgement,
credential revocation, hydration, and private-event behavior. The CLI has no operator or
runner surface.

Fresh verification:

- `PYTHONPATH=. pytest -q tests/test_assistant_runtime_cli.py
  tests/control_plane/test_assistant_runtime.py tests/test_assistant_cli.py tests/test_cli.py`
  passed 46 tests.
- Ruff passed for `assistant_cli.py` and `test_assistant_runtime_cli.py`.
- Python compilation and `git diff --check` passed.
- Adjacent runner plus strict-sandbox unit tests passed 45 tests. This is local contract
  evidence; it is not the controlled-Linux launch attestation.

No runtime was launched by these macOS tests. A `ready` state in local tests is produced by
the authenticated receipt protocol, not by pretending Bubblewrap or cgroup execution took
place. Controlled-Linux integration evidence belongs to the runner lane. Docker and
Apptainer remain unsupported for activation.
