# Validation

## Final integrated checkpoint

Leader verification after the final fixes:

- `PYTHONPATH=. python3 -m pytest -q tests/test_assistant_cli.py tests/control_plane/test_approval_proposals.py tests/test_cli.py tests/control_plane/test_collab_tasks.py tests/control_plane/test_collab.py tests/control_plane/test_ids.py`: **163 passed** in 51.71 seconds.
- Additional existing CLI/task/pentest/hardening run: **345 passed** in 190.10 seconds.
  These suites overlap; their counts must not be added as unique coverage. This broader
  run preceded the final restart-idempotency hardening, which the final run covers.
- Ruff passed on the assistant module, collaboration backend, IDs and changed tests;
  undefined-name checks passed for the legacy CLI. Compilation and diff checks passed.
- Independent final review found no remaining substantive correctness/security issues.
  LSP diagnostics were unavailable; no full-repository test-suite success is claimed.

The integration revalidates current target execution authority for pending proposals,
keeps their details and update cursor out of shared room feeds, and persists new task
idempotency metadata. Historical task events from before this change lack that metadata;
their restart-deduplication keys cannot be reconstructed.

To try this branch without changing the installed CLI, run from this worktree:

```sh
PYTHONPATH=. python3 -m herdr_engineering.cli assistant --help
```

No deployment, installed-command replacement, push, or merge was performed.

Validated on 2026-10-30 in worktree `.worktrees/herdr-assistant-cli`, branch
`feat/assistant-cli`, against base `fabc61c` plus the additive approval-proposal and
targeted-task-read backend changes present in the shared worktree.

- `PYTHONPATH=. pytest -q tests/test_assistant_cli.py tests/control_plane/test_approval_proposals.py tests/test_cli.py`
  passed 35 tests.
- `ruff check` passed for `assistant_cli.py`, `control_plane/collab.py`, and both new
  test files using the existing `chem-evidence/.venv/bin/ruff`; no package was installed.
- `ruff check --select F herdr_engineering/cli.py` passed. The repository intentionally
  ignores several broad style rules in this legacy CLI, so whole-file style rewrites were
  outside this feature.
- `python3 -m compileall -q` passed for the assistant client, CLI, and collaboration backend.
- `git diff --check` passed.
- A real `argv=None` process invocation returned exit 2 and a versioned JSON
  `argument_error` naming the missing `--status` option.

The HTTP tests run a real local `ControlPlaneServer` and `CollabService`. They cover user
and agent authentication, device binding, server-derived ownership, cross-user room
non-enumeration, task idempotency, agent task transitions, targeted task inspection,
pending-only approval proposals, private credential files, refused redirects, bounded
inputs/responses, forged scope-field rejection, and credential-safe transport errors.

Reviewer URL-hardening follow-up added explicit regressions for empty query/fragment
delimiters, empty userinfo, and empty port delimiters. The client now reconstructs one
canonical origin after validation instead of concatenating the supplied string. Fresh
CLI-focused verification (`tests/test_assistant_cli.py tests/test_cli.py`) passed 29 tests;
Ruff, compilation, and diff checks passed again.

No live account, browser, email, purchase, remote API, push, or deployment operation was
performed. No dependency was added. The general role/capability registry, approval
decision/grant path, agent packaging, sandbox launch, model policy, and dashboard remain
future platform slices.
