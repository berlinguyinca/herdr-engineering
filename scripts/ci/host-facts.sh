# Source from a gate script:  . scripts/ci/host-facts.sh; ci_host_facts
# Prints what the exec host looked like when the gate started. The unit gate failed 7 of 7 runs on two hive nodes (hive-as-11-2-39 and
# -40) while others passed, with scattered timeouts in tests that had nothing in common; nothing in the log said how many CPUs the job
# really had or how busy the node was. Read-only and best-effort: a missing file prints `unknown`, it never fails the gate.
ci_host_facts() {
  local root="${HERDR_CGROUP_ROOT:-/sys/fs/cgroup}" quota cpus load tmpfs mem busy
  cpus="$(python3 -c 'import os; print(len(os.sched_getaffinity(0)))' 2>/dev/null || echo unknown)"
  quota="$(cat "$root/cpu.max" 2>/dev/null || { [ -r "$root/cpu/cpu.cfs_quota_us" ] && echo "$(cat "$root/cpu/cpu.cfs_quota_us") $(cat "$root/cpu/cpu.cfs_period_us")"; } || echo unknown)"
  load="$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo unknown)"
  tmpfs="$(df -P -T "${TMPDIR:-/tmp}" 2>/dev/null | awk 'NR==2 {print $2" "$7" ("$5" free of "$3" 1K blocks)"}')"
  mem="$(awk '/MemAvailable/ {printf "%d MiB available", $2/1024}' /proc/meminfo 2>/dev/null)"
  busy="$(ps -eo comm= 2>/dev/null | grep -cE '^(pytest|python3?|postgres)$' || true)"
  echo "host: $(hostname 2>/dev/null || echo unknown)"
  echo "cpus visible: $cpus (nproc: $(nproc 2>/dev/null || echo unknown))"
  echo "cgroup cpu quota: ${quota:-unknown}"
  echo "loadavg: $load; python/postgres processes on the node: ${busy:-unknown}"
  echo "memory: ${mem:-unknown}"
  echo "tmp filesystem: ${tmpfs:-unknown}"
}
