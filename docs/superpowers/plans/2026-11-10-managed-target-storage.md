# Managed Cargo target storage

User requested continued work on disk pressure after host-wide compiler caching and ISO archival. Approved operational scope: safely offload generated output, retain source and active builds, support disconnected drives. Compiler cache/storage tiers from PR771 are already installed.

## Design

- Add opt-in `fabric_build_cache.local.manage_targets` (default false). Ordinary coordinated Cargo builds compute a unique target directory from the canonical Cargo workspace path. Use the selected storage tier's sibling `targets/` tree, separate from sccache's size budget. Honor explicit environment/CLI/repository target settings.
- Use `cargo locate-project --workspace` rather than dependency-resolving metadata. Keep target planning and builds protected by the existing maintenance read lock. A storage selection with no eligible tier fails clearly for managed builds instead of silently filling the internal SSD.
- Recognize target ownership by a dedicated marker. Never combine two worktrees' targets or overwrite unrelated destinations.
- Migrate only recognized, untracked, current-user Cargo output. Hold the maintenance lock exclusively; inspect native open paths and compiler working directories before/after copy. Copy with hardlinks/symlinks preserved, verify regular-file SHA256 and symlink payloads, atomically publish a compatibility link, then remove only the verified original backup. Preserve original data on any failure.
- Existing explicit target paths remain overrides. Old absolute Cargo invocations need not use the new launcher, so known migrated default target paths keep links; new normal invocations use per-tier managed targets and internal fallback when eligible. Restart old harnesses for common PATH/admission.
- Inactive scratch build archives may be considered after Rust output; never globally redirect macOS temp folders or move active application/VM data blindly.

## Tasks

1. TDD: target resolution/overrides/unique workspaces and conservative migration on real filesystems.
2. Implement target resolver and host integration; expose dry-run/apply migration and status.
3. Run real two-worktree builds and migration verification, lint/types/coverage/build. Independent review and protected PR/CI/merge.
4. Deploy, plan actual idle targets, migrate verified output, enable default placement, and report disk-space delta and limitations.

## Local operator context (do not publish sensitive contents)

- Current primary is the already enrolled writable 4TB APFS external volume. Use installed storage selection; do not hardcode device identifiers.
- Internal had approximately40GiB free at start; external approximately3.4TiB free.
- Existing ISO migration was already user-approved and completed; do not repeat or move remaining security/configuration files.

## Verification ledger

- Existing physical targets are preserved by automatic placement; migration requires an explicit quiet window. The operator authorized pausing clients for the completed migration, and clients were resumed afterward.
- Independent whole-branch review found two important issues: Cargo arguments after `--` crossed the application boundary, and migration omitted persistent alias registration. Both were reproduced with failing real-Cargo/filesystem tests, fixed, and verified green. No critical or minor findings remained.
- Real Cargo/storage suite: 57 passed, including two worktrees, CLI smoke paths, an actual APFS cross-volume migration, copy failure preservation, and disconnected-path alias fallback.
- Cache/gate/metrics/config regressions: 151 passed. An existing absent-cache test now uses an isolated home and working directory so an operator's globally installed wrapper does not affect its premise.
- Each of the four measured modules exceeds 80% statements, branches, and functions. Ruff, mypy, and wheel build passed. CI and installed four-harness verification remain deployment gates.
