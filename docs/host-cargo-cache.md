# One compiler cache for every harness

`herdr-eng cargo-host` provides a host-level integration for Cargo builds from Codex, Claude Code, Pi, OpenCode, and ordinary terminals. It uses the existing `fabric_build_cache` policy and `build-coordinate` gate.

## Installation

Install the updated herdr-engineering package and sccache, then run:

```sh
herdr-eng cargo-host install
herdr-eng cargo-host status
```

The installer detects the original Cargo executable before installing `~/.local/bin/cargo`. The launcher coordinates build/check/test/clippy and related commands even inside compound shell commands. Built-in aliases and global options are recognized without rewriting arguments. The real Cargo proxy path is retained, including rustup proxies. Global directory-changing `-C` calls are left to the compiler adapter.

The compiler adapter in `~/.cargo/config.toml` covers Cargo invoked by absolute path or from a process with an older PATH. Existing non-Herdr compiler wrappers are preserved: installation refuses to replace one. The installer disables Cargo incremental compilation in its global build defaults, as required for sccache; repository/environment overrides remain operator-controlled.

Managed PATH blocks are added to shell startup files. Existing content is preserved, symlinks/unsupported destinations are rejected before publication, and original files receive `.before-herdr-cargo-host` backups. Publication failures attempt to restore the original files. Restart existing harnesses to inherit the new PATH; the compiler configuration is read by newly invoked Cargo processes.

Disabling `fabric_build_cache` later stops cache use but does not automatically remove the installed `incremental = false` default. Restore that setting deliberately from the retained backup if reverting the integration.

Cache probe failures fall back to the original compiler. Compiler caching alone leaves target directories unchanged. Managed target placement is a separate opt-in described below. Neither feature moves source checkouts, installs remote-cache credentials in compiler environments, or migrates Docker or scratch directories.

## Automatic primary, secondary, and internal storage

Enroll a connected writable APFS volume explicitly:

```sh
herdr-eng cargo-host storage-add --name primary --volume /Volumes/data
herdr-eng cargo-host storage-add --name secondary --volume /Volumes/OtherDrive --priority 1
```

Enrollment creates only a dedicated compiler-cache directory and ownership marker, records the volume UUID in user configuration, and adds an internal fallback if none exists. It does not erase, reformat, or move existing files. Re-enrollment is idempotent. The same disk is recognized after its mount name changes.

Example configuration:

```yaml
fabric_build_cache:
  enabled: true
  mode: local_only
  local:
    capacity_gib: 20
    min_free_disk_percent: 5
    storage_tiers:
      - name: primary
        volume_uuid: 11111111-2222-4333-8444-555555555555
        directory: herdr-build/compiler-cache
      - name: internal
        directory: /Users/example/.local/share/herdr-engineering/state/build-gates/sccache
```

Each new coordinated build selects the first mounted, writable tier with sufficient free space. Absolute/direct Cargo calls select through the compiler adapter. External UUID resolution is supported on macOS; an unavailable/unsupported external tier is skipped. Runtime selection creates no mount directories. An external cache directory must already have been provisioned by enrollment. Cache servers are separated by policy, toolchain, and directory, so a server cannot silently continue using another tier's configuration.

Storage tiers remain separate caches; changing locations may produce a cold cache. No cache objects are copied between drives automatically. The size limit applies independently to each location. If no tier meets the free-space policy, the existing uncached-build behavior remains; storage selection does not weaken the reserve. Internal fallback therefore needs enough internal free space too.

## Verification

Tests use actual Cargo, rustc and sccache, separate target directories, real subprocess/OS-limit failures, and a real writable APFS test volume. GitHub CI creates a disposable APFS disk image rather than replacing storage APIs with mocks. The tests prove a warm dependency cache hit, direct Cargo coverage, supported global options, preservation of operator configuration, and volume availability selection.

Useful diagnostics:

```sh
herdr-eng cargo-host status
herdr-eng build-cache-inventory --json
herdr-eng build-coordinate --status
```

Compiler caching reduces repeated compilation. Its size cap is separate from Cargo's target output footprint and is not a quota for existing worktrees.

## Managed target directories

After enrollment, opt in to placing new Cargo output on the selected tier:

```sh
herdr-eng cargo-host targets-enable
herdr-eng cargo-host status
```

This sets `fabric_build_cache.local.manage_targets: true`, retaining a configuration backup. Normal `cargo build`, `test`, `check`, `run`, `clean` and related built-ins use a workspace-specific directory under the selected cache directory's sibling `targets/` tree. Each worktree has its own directory. A managed `./target` symlink keeps scripts that read local build artifacts working. Existing physical target directories, unrecognized links, and explicit CLI/environment/Cargo configuration overrides remain authoritative.

The first eligible enrolled drive wins on each invocation. If a drive is absent, the next enrolled drive or eligible internal tier is selected. Owned links are updated automatically, including broken links left by an unplugged drive. Outputs are independent per tier; switching drives may require rebuilding. New managed builds fail clearly if no tier meets the free-space reserve. The compiler-cache size cap does **not** bound managed target directories. Unmount drives only after builds using them finish; this does not provide mid-build failover. Direct absolute Cargo invocations receive compiler caching but need the managed launcher for automatic target selection.

Existing default targets can be inspected without moving anything:

```sh
herdr-eng cargo-host targets-migrate --root ~/projects
```

For publication, pause Cargo agents and IDE/build watchers, then use `--apply --quiescent`. The explicit quiescent assertion is necessary because older clients do not participate in the maintenance lock. Migration also refuses open output or active compilers, tracked files, source markers, escaping links, special files, and uncertain ownership. It copies output with hardlinks preserved, verifies SHA256 content and link metadata, publishes the compatibility link, and removes only the verified original. A changed or in-use backup is retained and reported. Source directories never move. Automatic builds do not migrate existing physical targets implicitly.
