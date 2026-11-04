Scheduler lease expiry alone cannot prove a writer has stopped. Add a supervised
Linux execution boundary using the existing bubblewrap sandbox inside a named
systemd service cgroup, with CPU, memory, and task containment.

Bind receipts to full lease identity, systemd invocation, ControlGroup, and
KillMode. Verify the exact cgroup is empty or removed after stop; reject forged
or stale receipts. Failed start identity acquisition attempts cleanup and carries
unit metadata for reconciliation. Read process-tree accounting from the bound
cgroup without inspecting command arguments or credentials.

The sandbox's external-scope marker preserves its PID/filesystem isolation and
existing egress behavior while avoiding a second scope outside the supervisor's
process tree. Keep packaged and deployed copies identical.

Validation: five upstream-based checks passed, including a real systemd/bubblewrap
workload whose parent and new-session child stop writing after revocation.
Targeted Ruff, shell syntax, and diff checks passed; the scope module received
independent review. No model calls or production services were started.

This is one component of #290. It does not enable migration or certify native
interactive harness resume; host-runner integration and lifecycle gates remain open.
