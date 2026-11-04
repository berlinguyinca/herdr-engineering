# Core Pi runtime integrated verification

Verified 2026-10-31 on `feat/assistant-pi-runtime`, based on `d30a68c`.

## Automated checks

Leader command:

```sh
PYTHONPATH=. python3 -m pytest -q tests/test_assistant_runtime_cli.py tests/control_plane/test_assistant_runtime.py tests/test_assistant_pi_runner.py tests/control_plane/test_strict_pi_sandbox.py tests/test_assistant_packages.py tests/control_plane/test_assistant_packages.py tests/test_assistant_cli.py tests/test_cli.py tests/control_plane/test_collab_operator.py tests/control_plane/test_collab_tasks.py tests/control_plane/test_collab_helpers_profile.py tests/control_plane/test_collab_egress.py
```

Result: **307 passed, 3 skipped**, 47.34 seconds. Skips require platform facilities
unavailable on the local Mac. Ruff, Python compilation, Bash syntax, identical packaged
and deployed sandbox scripts, and diff checks passed. Independent final review found no
remaining substantive authorization, integrity, lifecycle, cleanup or evidence issues.
The backend lane additionally reported 467 collaboration/task/security tests passing;
those overlap this run and are not additional unique coverage. No full-repository green
result is claimed.

## Real Linux proof

`HOST-RUNNER.md` and `LINUX-PROOF.json` record the controlled Bender experiment:

- Actual cgroup-v2 CPU, memory and process caps were checked inside the workload scope.
- `NoNewPrivs: 1`, read-only system files, hidden operator home/engine socket, and denied
  direct outbound networking were checked in the strict sandbox.
- Existing installed Pi was staged read-only and bound by its content digest; no host
  package or dependency was installed.
- A fresh host attestation enabled an owner-authenticated package activation. Its worker
  established MCP polling, received a real delegated task, and ran Pi through the broker.
- The owner CLI read the exact final response `HERDR_REGISTERED_PI_OK\n` with task state
  `done`. No Codex response was substituted.
- Owner cancellation revoked execution, and the controller verified the worker and named
  systemd scope stopped before reporting `cancelled`, final checkpoint 11, no live lease.

The initial JSON-output experiment returned a truncated Pi protocol transcript; the final
runner uses text print mode, and the repeated registered task verified the actual final
assistant answer. An initial host/package limit mismatch was rejected with HTTP 409; the
host was re-attested with the exact immutable package limits rather than weakening checks.

## Cleanup and limits

Both proof runtimes were cancelled. The owned host controller, gateway broker, SSH reverse
tunnels and temporary API were stopped. The temporary owner token, expired worker credential
files and temporary gateway secret were removed; redacted audit/proof artifacts were retained.
These proof URLs are no longer live. No production service was installed, restarted or changed.

This is an isolated development activation contract, not a deployed secretary integration.
The runner currently supports a fixed no-tools Pi command; browser, calendar, email,
location-history and booking bindings are not registered by this slice. The immutable
Secretary role is digest-bound, but it supplies instructions only. Docker and
Apptainer remain definition-only. A host supervisor/service deployment and the later
tab-supervision operator integration remain separate work. Runtime staging is currently
expensive on cold controller startup; it does not imply a model or task has started.

Existing unrelated macOS runner/CI baseline failures are documented in earlier package/CLI
validation records and were not modified by this work.
