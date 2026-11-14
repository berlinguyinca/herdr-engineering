# Managed Cargo target retention

The operator approved separate internal/external target budgets, eviction of owned inactive generated output, and a compact growth/cleanup report. Defaults chosen by implementer: 32GiB internal, 256GiB per external tier, seven days idle; these are soft retention budgets, not filesystem quotas.

## Approach

Use the existing build maintenance lock and ownership markers. An hourly macOS LaunchAgent runs a bounded sweep of enrolled target roots. A manual invocation defaults to dry-run. An inventory-only approach would leave recurring growth unresolved; unconditional `cargo clean` is unsuitable because Cargo's full clean does not acquire its build locks. No extra daemon or dependency is needed.

An apply sweep takes the maintenance lock exclusively and nonblockingly; active coordinated builds cause an immediate busy result. Within a managed workspace target, only profile directories with Cargo `.cargo-lock` files may be reclaimed. Hold each profile's native Cargo lock exclusively/nonblockingly, check native open-file/compiler paths, and preserve all profile lock files and parent directories so waiting or newer Cargo processes keep using the same lock inode. Never remove whole target roots, compatibility aliases or ownership markers. Unknown files outside profiles remain visible in usage totals but are not deleted. Reject source markers, foreign ownership, mount boundaries and escaping links. A busy profile or uncertain inspection is preserved.

Touch target ownership marker mtime on each managed invocation; additionally inspect actual output mtimes for direct Cargo users. Only profiles idle for at least the configured period qualify. Remove oldest eligible profiles' contents until the budget is met; if none qualify, report deferred pressure. Active/recent output can exceed budgets. Keep the existing disk-free admission reserve.

Store a bounded atomic status JSON file with per-tier target usage, budget, growth since previous scan, reclaimed bytes, exclusions and errors. `cargo-host targets-report` gives a compact human report, with `--json` for tooling. Unavailable drives are skipped without creating mount paths. Inventory does not hash output or read file payloads; entry and elapsed-time limits prevent unbounded scans. Cleanup logs remain bounded.

No source, arbitrary workspace targets, Docker, global temp directories, or unregistered volumes are cleanup candidates. No agents are paused. User authorization covers installing the user LaunchAgent and enabling retention after review and CI.
