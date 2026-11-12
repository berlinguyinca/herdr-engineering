# Host-wide Cargo cache and automatic storage tiers

Approved scope: user requested shared integration for Codex/Claude/Pi/OpenCode (items 1, 2, 5), then automatic primary/secondary/tertiary cache storage selected from connected drives. No migration of existing targets/scratch/Docker. Also identify easy external-drive migration candidates without moving them.

## Design

- Extend herdr-engineering's existing cache policy/coordinator rather than create a separate cache implementation.
- Cargo launcher shim coordinates ordinary builds, including compound shell commands. A user-wide Cargo compiler adapter covers Cargo invoked by absolute path. Preserve real Cargo/rustup and existing explicit compiler wrappers; prevent recursion and double gating.
- Ordered storage tiers identify external volumes by UUID; resolve their actual current mount point, require mounted/writable volume and configured free-space reserve. Internal cache is the final fallback. Never create a stale /Volumes mount point. Select per invocation; each location gets its own policy-specific sccache server port. Existing target directories stay isolated and in place.
- Keep policy in existing fabric_build_cache YAML. Update inventory to measure the selected cache. Do not export credentials into compiler/build subprocesses.
- Install reversible managed launchers/config with backups. Preserve existing Cargo and harness configuration.
- Verify tiny real Cargo builds from all four harnesses, compiler cache hits, per-worktree target isolation, and availability-based tier fallback without unplugging user drives.

## Implementation tasks

1. Read coordinator/config/environment/CLI/install conventions. Add failing real/pure-input tests for automatic tier selection and managed-wrapper recognition.
2. Implement typed storage configuration and selector; wire selection into cache planning, inventory, and compiler execution.
3. Add host Cargo/compiler adapters and reversible installation; integrate harness PATH/environment only where required.
4. Run unit/integration/smoke/real-harness verification, lint/type/build/coverage and full repository checks. Independent review, fix findings, PR, CI, merge.
5. Deploy reviewed host integration. Report actual coverage of each harness, restart requirements, and easy-to-move local directories.

## Verification evidence

- Real builds succeeded via Codex shell, Claude Bash, Pi RPC shell, and OpenCode session shell API under isolated setup.
- Shared compiler cache recorded one Rust miss followed by three hits; each harness used a different target directory.
- Real OS process limits verify compiler fallback; real file-size limits verify installation rollback.
- Per-file line/branch coverage is enforced in CI, along with lint, typing, build, and real APFS-volume tests.
