# Architecture and decisions

## Hard ownership boundaries
Herdr Engineering: API, policy, identity, placement, build request lifecycle, cache statistics, quotas, leases, safe cleanup, dashboard and metrics. Pi Engineering: requests builds and consumes status, no independent scheduler. Slurm: allocates CPUs/RAM/GPUs and owns job execution; Herdr adapts to it via audited existing runner interfaces. `sccache`: compiler-result caching and, when enabled, its own distributed compilation scheduler; Herdr allocates eligible workers, never competes per compiler invocation.

## Runtime data paths
A. Local cargo -> sccache local cache + optional authenticated S3 compatible backend; build runs even without controller, cache store or Slurm.
B. Optional distributed compilation -> sccache-dist workers registered only from explicitly reserved capacity. No one-sbatch-per-rustc job strategy.
C. Optional whole-build Slurm jobs -> stage pinned inputs; allocate CPU resources; run immutable toolchain; return attested outputs + logs and exit status. Independent scheduler outage falls back locally when policy permits.

## Storage hierarchy
L0 per-host bounded local cache/RAM if safe; L1 centrally accessible durable S3-compatible object store; L2 published immutable artifacts/provenance with distinct permissions and retention. Do not imply `sccache` native hierarchical semantics without verifying installed upstream version and settings. If native layering unsupported, use tested supported configuration, not a custom proxy.
Keep object store separate from Futurama controller process; select endpoint via inventory verification. Reuse existing S3/RustFS/VersityGW only if actual deployed backend and compatibility proven. No assumed endpoint. Treat cache keys as compiler-defined; add policy namespaces by OS/arch/toolchain/trust domain and manifest metadata.

## Compatibility
Partition by OS, arch, target triple, rustc version + flags, cargo profile and feature set, build environment/toolchain digest, dependencies and trust boundary. Preserve sccache compiler hashing; never force reuse solely via a hand-written namespace. Detect unsupported caching (proc macros/build scripts, incremental/links) and handle misses. Do not claim cross-target object compatibility or guaranteed determinism.

## Head node resilience
Futurama hosts Herdr services and dashboard, while storage/build traffic bypasses the controller. Avoid saturation of the 2013 Mac Pro. Tailscale connectivity permitted; credentials scoped, short-lived where available; no long-lived shared storage credentials in agent prompts/logs.

## Observability
Use existing Herdr API/SSE UI patterns. Export Prometheus-compatible metrics to grafana.metabolomics.us. No local Grafana. Measure actual disk writes where available, CPU time, elapsed time, hit/miss rates, transfer bytes, size, eviction, lease conflicts, remote queue delay, cancellation and job success. Estimate saved writes separately, clearly labeled, with methodology.

## Rollout
Bender pilot: baseline, local sccache, shadow metrics, then write/read central cache. Expand to compatible hosts, then optional Slurm full-build, finally sccache-dist persistent-worker pilot. Default flags OFF for remote execution, shared writes and auto-eviction until operator approves.
