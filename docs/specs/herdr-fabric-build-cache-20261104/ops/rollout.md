# Rollout and recovery
Phase 0 read-only audit, baseline. Phase 1 local-only Bender pilot with safe `RUSTC_WRAPPER` setup and incremental disabled only in validated profiles. Phase 2 authenticated shared read-through against verified backend; test cold/warm and wrong-namespace misses. Phase 3 enable bounded writes for Bender. Phase 4 one compatible second host. Phase 5 dashboard and telemetry. Phase 6 optional Slurm full-build on dedicated CPU test partition. Phase 7 optional distributed worker allocation.

Release gates: independent regression tests, permissions, quotas, observability, rollback rehearsal and operator opt-in before every destructive or cross-host change. Canary <=1 host before fleet enablement. Never assume Futurama or Slurm is reachable.

Rollback: toggle mode `local_only` then disable shared writes; drain jobs; retain metadata and evidence; stop workers after job resolution; do not delete central cache unless approved. Existing host build gate and cleanup remain active in previous configuration. `sccache --stop-server` only when safe and owned; no blanket process kills.

Operational checks: sccache stats/config per host, `herdr-eng build-cleanup --dry-run`, `herdr-eng build-cache status`, Slurm `squeue/sacct` with validated owner, endpoint health, disk bytes, and Grafana remote telemetry. Commands here are contracts, verify actual CLI names before running.
