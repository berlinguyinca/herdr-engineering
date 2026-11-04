# Immutable Pi role binding v1

Every package procedure reference is a required instruction-role artifact for Pi activation.
The control plane carries only `{id, version, digest}`. It never accepts role content or a host
path, and a role never grants tools, room actions, credentials, or external authority.

The host operator configures a private registry mapping exact references to private local files.
The runner opens both registry and role files without following links, requires owner-only regular
single-link bounded UTF-8 content, verifies SHA-256, and copies at most eight roles into the private
Pi harness as mode-0400 files. Roles are sorted by reference and appended through fixed paths:
`$PI_CODING_AGENT_DIR/role-00.md` through `role-07.md`. Role text is never placed in argv.

Before publishing each fresh host attestation, the runner verifies every configured role file;
failure aborts attestation rather than silently dropping the role. The claim derives required references from the
immutable package, and the ready receipt must contain the exact verified set. Missing, wrong,
tampered or extra bindings prevent ready state. Packages with no procedure references remain
compatible and receive no implicit Secretary role.

The initial reviewed role is `procedure:secretary-system` version `1.0.0`, digest
`sha256:942e5b1002d74a9d52ae2fb7c2f8ef07f4f6b08aa1b9e08a36b8e0504ffab120`, sourced from Git
commit `0ea67580c6926057b41d11b498533219e7adbb61`. Pi remains in no-tools, no-extensions,
no-MCP, no-skills, no-context and no-session mode.

## Isolated validation evidence

An isolated synthetic-owner validation on Bender bound the exact reviewed Secretary role and
launched the fixed no-tools argv with only
`--append-system-prompt "$PI_CODING_AGENT_DIR/role-00.md"`. Task
`grant_01m68dqn040000098t4xb4tdv5` completed with exit zero in 23 seconds, produced 92 bytes,
and changed no workspace files. Its hash-chained audit ended at
`6ac07ab81d711d4e3cd84e28cd65fe28c27ec14e18f972f8d38675a7bcb62c72`. The validation runtime
was cancelled, its worker and systemd task scope were gone, and the development controller was
stopped.

The owner-visible launch receipt included exactly the pinned role ID, version and digest.
The owner CLI independently read task state `done` and the actual Pi answer:

> My assigned role is coordination secretary. No external tools are registered for this task.

`ROLE-PROOF.json` records the redacted receipt. The final role/backend/CLI/runner/strict
suite passed 74 tests; Ruff, compilation and diff checks passed, and independent review
found no substantive code/security issue. The temporary validation API and reverse tunnel
were stopped and its owner token removed. The private model broker was retained only for
the next existing-assistant adapter validation, not as a user-facing assistant.

This proves immutable instruction binding to a sandboxed Pi target. It is not the user's deployed
Personal Assistant and received no patient, calendar, email, venue, or business context. Product
integration must attach this Pi target behind the existing `AssistantService` delegation boundary;
it must not create a competing root assistant or new OAuth path.

The identified deployed source is `herdr-url-fabric` at commit
`daca260689a6633519d14e6344886f08150cb7d6`. Its canonical
`POST /api/v1/assistant-delegations` creates scoped queued requests, and its connector/tool
gates retain the user's existing source access. The next work binds that existing queue
to this runtime; isolated HCN activation is not a substitute for that integration.
