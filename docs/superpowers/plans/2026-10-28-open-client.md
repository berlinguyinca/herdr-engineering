# Open generated files on the operator client

Current behavior: discover newly created/changed pictures and HTML during a task; offer all/select/skip at completion. Markdown is explicit-request only. Discover the actual viewing client automatically; do not ask machine/account/viewer questions. Explicit open requests need no second offer.

Implementation uses one Python CLI and one portable skill across Claude/Codex/Pi/OpenCode, with no new dependencies. Never infer the destination from the execution host or first online fabric peer. Resolve live authenticated HerdR bridge peers against fabric identity and desktop capability. Refresh discovery for each open; unresolved evidence leaves files unopened without a setup interview. SSH uses existing authentication and host verification. Record transfer separately from desktop dispatch.

- [x] Test and implement private baseline/discovery state, content-based changes, exclusions, stable selection and duplicate suppression.
- [x] Test and implement selection, source revalidation, bounded verified transfer, desktop capability checks and HTTP(S) URL dispatch (loopback requires an explicit fabric URL).
- [x] Add CLI commands, portable skill and collision-safe installation across harnesses.
- [x] Integrate automatic task lifecycle instructions using supported harness surfaces; explicitly report unavailable native hooks.
- [x] Run targeted tests, lint, CLI smoke checks and independent review.

Tests cover unchanged/deleted/symlink/ignored files, spaces/Unicode/metacharacters, ambiguous target refusal, selection, modified-after-prompt refusal, remote failure and no implicit executor-local open. Live desktop opening is tested only with a user-selected device and selected files.

Validation: 29 feature tests passed; related browser/session checks produced 41 passed and 1 skipped. New files pass Ruff, compileall and skill validation. Existing cli.py import-order diagnostics also reproduce from HEAD. Full suite interrupted after 163 passed and 1 unrelated failure in tests/control_plane/test_artifacts_api.py (ControlPlaneServer does not accept artifact_store). No live viewer launch was attempted without a selected destination and viewing request. Installed instruction-based activation for all four harnesses on this host; repeat installation is unchanged.

Follow-up correction: implemented live bridge/desktop client discovery, removed destination questions, excluded Markdown from automatic discovery, and added explicit `file` and read-only `target` commands. Live discovery identified the Mac desktop while excluding headless Beast. Native API has no pane-to-client identity field, so discovery reports host-level bridge evidence and refuses multiple usable desktops.

Correction validation: 48 focused tests pass, plus related browser/session coverage gives 61 passed and 1 skipped. Ruff and compileall pass for new modules. Automatic execution-host fallback was removed after a wrong-host regression; only a user-supplied override may choose local. Live Mac discovery succeeded while its bridge was attached; after that bridge detached, discovery correctly returned no usable desktop rather than choosing headless Beast. Markdown-only task changes produced no completion offer.
