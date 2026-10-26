# Phase 2 — telemetry, load and process accounting
Collect host measurements through existing authenticated fabric heartbeat path or a minimal host-side probe integrated with Herdr; no competing metrics daemon if usable collector exists. First inspect Grafana/Prometheus metrics already available. Sample host every 5s–15s locally; report aggregated state every 10s–30s with jitter and max 60s freshness policy (tunable); resilient to intermittent WAN. Report wall time, monotonic sequence, host UID, probe version, capture age and confidence. Stale = not eligible; never substitute fabricated 0% utilization.

## Metrics
CPU: logical/physical core count, utilization moving averages, load/core (diagnostic not CPU %), CPU PSI `avg10` and `avg60`, steal %, throttling and CPU temperature. Memory: total/available, swap activity, memory PSI, OOM. Storage: free capacity & inodes per workspace volume, IOPS/latency/IO PSI and cache capacity. GPU: device identity, VRAM available, utilization, thermal/throttling and runtime presence. Network: SSH control RTT, heartbeat latency, WAN site and transfer throughput if available. Power/portability: laptop, AC/battery/sleep risk, host priority class. Runtime: agent families/versions, sandboxes, GPU local runtime requirement, repos/caches available. Capture metrics failures and source explicitly.

## Process-level accountability
On Linux prefer systemd scopes+cgroup v2 per agent tree, including child compilers/tests; expose actual CPU-seconds, RSS/high-water, IO bytes and PSI. On macOS collect process-group accounting where supported, mark lower-confidence metrics; do not assume cgroup semantics. Detect non-Herdr consumers separately; capacity considers BOTH global load and reservations. Never gather command arguments/secrets into telemetry.

## Smoothing, thresholds and alarms
Use EWMA with short/medium horizons, reserve interactive OS headroom, and reject on sustained CPU PSI / memory PSI / swapping / thermal throttling / disk pressure. Example defaults for initial tuning (configurable, not universal): CPU PSI avg10 >= 40% or memory PSI avg10 >= 10% sustained 30s -> overload; available memory < 15% or severe swap-in -> block; disk workspace free < max(20GiB,10%) -> block. Sensible recovery hysteresis and dwell times avoid flapping. Validate on representative host load; never rely solely on arbitrary global CPU utilization percent. GPU execution shares inference ownership boundary; do not steal GPUs from InferWeave.

## Tests
Synthetic host telemetry, stale/out-of-order/future timestamps, metric gaps, no GPU, Linux/macOS portability, network partition, 100 simultaneous agents, high load average due to I/O vs runnable queues, unrelated high-load process.
