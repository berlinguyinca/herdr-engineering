# `task-restricted-v1` Pi sandbox profile

Status: opt-in host-runner contract. Registering an agent package does not activate or launch it.
Docker and Apptainer adapters are outside this slice.

## Manifest contract

```json
{
  "profile": "task-restricted-v1",
  "network": "broker-only",
  "filesystem": "task-only",
  "limits": {
    "memory_bytes": 1073741824,
    "process_count": 128,
    "cpu_quota_percent": 100,
    "wall_time_seconds": 900
  }
}
```

Bounds are 256 MiB–16 GiB memory, 16–1024 processes, 10–800 percent CPU, and
30–86400 seconds wall time. Package definitions may request this profile, but remain
`launch_supported: false` until a host runner attests that it can enforce it.

## Runner mapping

The runner, rather than task text or a model, supplies:

- `HERDR_SANDBOX_PROFILE=task-restricted-v1`
- `HERDR_SANDBOX_MEM_BYTES`
- `HERDR_SANDBOX_TASKS`
- `HERDR_SANDBOX_CPU_PERCENT`
- `HERDR_SANDBOX_TIMEOUT_SECONDS`
- `HERDR_SANDBOX_HARNESS_DIR`, a private owner-controlled mode-0700 directory
- `HERDR_EGRESS_SOCK`, an owner-controlled socket in a mode-0700 directory

Strict mode rejects SSH mounts, extra read-only mounts, operator harness state, harness
executables located under the operator home, host networking, absent Bubblewrap, missing
timeout enforcement, unavailable resource controllers, and preexisting sockets, special files,
or hard-linked host files in the workspace/private harness copy. The task workspace is the only
writable project path. Its dedicated work root and direct-child workspace must both be owned by
the runner with mode 0700 and may not resolve through links. Network traffic runs in a private namespace through the existing egress
relay; the relay/broker remains responsible for destination authorization and credential binding.

The launcher creates one transient systemd user scope with `MemoryMax`, `TasksMax`, and
`CPUQuota`. A trusted wrapper inside that same workload scope reads its cgroup-v2 `memory.max`,
`pids.max`, and `cpu.max`; missing, unlimited, or weaker controls abort before Bubblewrap starts.
The wrapper then executes the harness through `timeout`, which sends `TERM` at the declared wall
limit and `KILL` after a fixed ten-second grace period.

## Evidence and limitations

The unit tests exercise validation and exact launch construction with stubs on macOS. They do
not claim macOS kernel isolation. Existing Linux-only tests continue to cover real Bubblewrap,
and a production host must pass an integration check with working cgroup v2, a systemd user
manager, the egress broker, and the intended Pi harness before it can advertise this profile.
The harness executable and Bubblewrap/systemd/timeout binaries remain trusted host-runtime
components; this profile isolates the agent task rather than defending against a compromised host.

Legacy invocations with no `HERDR_SANDBOX_PROFILE` keep their current behavior, including their
documented open-network and uncapped fallback. This compatibility path does not satisfy
`task-restricted-v1` and must not be presented as strict isolation.
