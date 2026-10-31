# Assistant package v1 validation

## Final integrated checkpoint

Leader verification on branch `feat/assistant-packages`, base `2a2dd42`:

- `PYTHONPATH=. python3 -m pytest -q tests/test_assistant_packages.py tests/control_plane/test_assistant_packages.py tests/control_plane/test_strict_pi_sandbox.py tests/test_assistant_cli.py tests/test_cli.py tests/control_plane/test_collab_sandbox.py`: **110 passed, 5 skipped**, 10.68 seconds.
- Ruff passed for all changed Python implementation and test files. Python compilation,
  Bash parsing, byte-identical sandbox copies and `git diff --check` passed.
- Independent final code/spec/security review found no remaining substantive findings.
- The sandbox owner additionally ran sandbox/helper/egress regressions:
  **114 passed, 45 platform skips**. Suites overlap; do not add counts.

Real Bubblewrap/cgroup execution is Linux-only and was not verified on this Mac.
The strict launcher checks the actual workload's cgroup before executing the task;
registration still advertises `runtime_launch_supported: false` for every runtime.
No activation, production deployment, Docker/Apptainer launch, dependency installation,
push or merge occurred.

A broader leader helper/egress/task run produced **247 passed, 37 skipped, 1 failed**:
`tests/control_plane/test_collab_helper_runner_minors.py::test_the_egress_proxy_is_stopped_even_when_its_supervisor_is_gone`.
The test indexes `runner.proc_stat(p.pid)[1]` when `proc_stat` returns `None` on this Mac.
The exact failure was reproduced on untouched canonical main `fabc61c` with the single
test (1 failed in 3.46 seconds). No helper-runner source was changed; this unrelated
baseline/platform failure is not claimed fixed. No full-repository green result is asserted.

The earlier incremental checks below are retained as supporting evidence; the final
integrated checkpoint above is the current completion result.

Validated on 2026-10-30 in worktree `.worktrees/herdr-assistant-cli`, branch
`feat/assistant-packages`, base `2a2dd42`.

- Manifest and real control-plane package tests passed for Pi, Docker, and Apptainer
  definition registration, canonical hashing, idempotent replay, same-version conflicts,
  owner-only list/inspect, and agent/cross-user refusal.
- Combined package/backend/assistant/legacy CLI run:
  `PYTHONPATH=. pytest -q tests/test_assistant_packages.py
  tests/control_plane/test_assistant_packages.py tests/test_assistant_cli.py tests/test_cli.py`
  passed 77 tests before the final import-only lint correction; the same focused suite is
  rerun at handoff.
- Ruff covers the new manifest/client and both package test files. Python compilation and
  `git diff --check` are required at handoff.

Security regressions include strict recursive unknown-key rejection; authority, identity,
credential, host mount, environment, privilege, and arbitrary command refusal; 64 KiB,
16-level, and 4,096-node bounds; finite JSON; exact integer validation; private model
policy; immutable procedure/knowledge references; and constrained OCI/ORAS registry,
path, port, and SHA-256 syntax. CLI file loading converts decoder recursion into a
versioned `invalid_request` response.

The registered status is `definition-only`. All records report
`runtime_launch_supported: false` and `capabilities_are_requests: true`. No runtime,
container, task worker, browser, email, purchase, grant, snapshot, clone, or external API
operation was launched. No dependency was added.
