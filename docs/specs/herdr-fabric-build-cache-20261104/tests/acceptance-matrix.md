# Required acceptance matrix
1. Baselines: three repeats each, cold and warm, on pinned revision/toolchain; capture local disk bytes written, wall time, CPU-seconds and network bytes.
2. Functional correctness: same source result with cache off/on; toolchain/flags/features differences cause safe misses; no writable shared `target/`.
3. Concurrency: 20 build requests, 4 identical, multiple repos; no deadlock, duplicate work bounded, unrelated requests progress.
4. Active builds vs cleanup: race test with held lease and process; no active file removal, no source/toolchain/cache-download deletion.
5. Invalid inputs: untrusted cache entry, corrupt object, mismatched manifest, bad digest -> reject/quarantine, retry locally.
6. Cache outage: timeouts, TLS failure, 503, disconnection -> bounded retries and local fallback with preserved exit code.
7. Cross-platform: Linux x86_64 vs macOS arm64, distinct rustc/toolchains and profiles -> no invalid hits.
8. Disk pressure: simulated 85/80 thresholds and constrained available bytes; dry-run first, bounded eviction, correct space accounting.
9. Control plane restart while build runs: recover from state and reconcile leases; no duplicate remote launch.
10. Slurm not installed/offline: local operation unaffected; placement lookup not construed as launch capability.
11. Slurm CPU reservation: staging, auth, submission, stdout/stderr, output digest, cancellation, timeout and cleanup verified on test partition.
12. GPU partition: do not consume GPU resources for CPU-only builds by default.
13. Dashboard/API: authz, redaction, pagination, SSE reconnection, empty/loading/error states; no new UI server.
14. Metrics: event counters and histograms have bounded label cardinality; no secret paths in metrics; export to existing collector.
15. Rollback: disable feature flag -> original `build-coordinate` and existing cleanup behavior preserved; previously active jobs reconciled.
16. Performance go/no-go: retain only if cache correctness passes and measured total SSD write volume or build time improves; report uncertainty, not invented savings.
