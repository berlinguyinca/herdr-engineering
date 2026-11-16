# Cargo target retention implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline, with a fresh whole-branch reviewer before merge.

**Goal:** Keep owned target output from growing indefinitely, with separate tier budgets and an inspectable bounded report.
**Architecture:** Existing maintenance flock plus native Cargo profile locks; hourly LaunchAgent; allocation/mtime inventory, no content hashing.
**Tech Stack:** Existing Python, stdlib and PyYAML, actual Cargo and APFS tests.
**Spec:** docs/superpowers/specs/2026-11-10-cargo-target-retention-design.md

## Constraints and review focus

- No deletion while coordinated builds hold maintenance SH; nonblocking busy response.
- Native direct Cargo compatibility: preserve `.cargo-lock` inode and acquire EX, including shared modern Cargo holders; source/foreign/symlink paths remain untouched.
- Bound inventory and report size; no scans on every compiler invocation.
- Separate 32/256GiB budgets and 168h idle, opt-in retention, no hard active-build quota claim.
- Missing volumes and partial scans fail closed. Review lock ordering, waiting Cargo races, special files, malformed markers, config validation and timer idempotence.

## Tasks

1. Write failing real-filesystem tests for budget settings, recent/use tracking, dry-run, owned oldest-first cleanup, lock/open-file protection, ownership/symlink/source refusal, missing roots and reports. Implement minimal retention module and configuration. Verify each test red then green.
2. Add marker use tracking and maintenance/report/enable CLI, bounded hourly macOS job. Test real CLI smoke and real Cargo E2E build→age→evict→rebuild with unchanged lock inode. Verify lint, types, coverage (80% each), regressions and wheel.
3. Independent whole-branch review, reproduce/fix important findings by TDD, PR/required CI/GitHub Actions/merge; immutable internal deployment. Enable schedule, run a live non-destructive inventory, verify policy/report and disk usage without pausing agents.

## Ledger

- Pre-flight: marker mtime from prepare_target feeds retention idle calculation; maintenance lock path must match build_coord state. CLI uses existing configuration and enrolled storage selection.
- Ruling: preserve Cargo profile roots/locks and reclaim only contents, because full `cargo clean` deliberately omits locking and whole-directory renames allow waiters to hold stale inodes. Costs: docs/package output outside recognized profiles is reported but not reclaimed.
- Task 1: config/output safety tests RED (9 missing-feature failures) → GREEN (9 passed). CLI/report feature RED (missing enable function) → GREEN. Native actual Cargo blocks on preserved `.cargo-lock` as expected.
- Independent review: three Important findings reproduced RED (wide directory allocated 1.1MiB despite two-entry limit, custom scheduler config unsupported, mounted target reclaimed output). Fixed by bounded enumeration/fd-relative deletion, persisted job environment, enrolled-root device comparison. Combined suite GREEN: 14 passed including actual APFS mount.
- Ruling: idle tracking is conservatively workspace-wide, so using debug also retains release; this protects active workspace output at the cost of delayed reclamation. Documentation makes that scope explicit.
- Final local validation: 74 real compiler/storage/retention tests, 151 cache/config/gate/metrics regressions, and a separately opted-in real CLI E2E passed. Every measured module exceeds 80% statements, branches and functions. Ruff, mypy and wheel build passed.
- Review minor handling: workspace-wide idle policy documented; report reader raised to bounded 256KiB and writer truncates details at that limit; applied reclamation excludes retained lock/root allocation. Remaining tradeoff: dry-run estimates can include retained directory allocation, so estimated bytes are explicitly approximate.
