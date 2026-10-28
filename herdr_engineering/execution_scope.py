"""Linux execution fencing using existing sandbox and systemd cgroups."""
from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


class ScopeError(RuntimeError):
    pass


class ScopeStartUncertain(ScopeError):
    def __init__(self, lease, unit, cleanup_attempted):
        super().__init__("execution scope start requires reconciliation")
        self.agent_id, self.host_uid, self.generation = lease.agent_id, lease.host_uid, lease.generation
        self.unit, self.cleanup_attempted = unit, cleanup_attempted


@dataclass(frozen=True)
class ScopeReceipt:
    agent_id: str
    host_uid: str
    generation: int
    unit: str
    control_group: str
    workspace: str
    invocation_id: str = ""

    def to_dict(self):
        return asdict(self)


class SystemdExecutionScope:
    def __init__(self, sandbox: Path, *, run=subprocess.run, cgroup_root=Path("/sys/fs/cgroup")):
        self.sandbox = Path(sandbox).resolve(strict=True)
        self.run = run
        self.cgroup_root = Path(cgroup_root)

    def _call(self, argv):
        result = self.run(argv, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ScopeError("execution scope operation failed; reconciliation required")
        return result.stdout.strip()

    @staticmethod
    def unit_for(lease):
        identity = f"{lease.host_uid}:{lease.agent_id}:{lease.generation}:{lease.token}"
        return "herdr-scheduler-" + hashlib.sha256(identity.encode()).hexdigest()[:32] + ".service"

    def start(self, lease, *, workspace: Path, argv: list[str]):
        work = Path(workspace).resolve(strict=True)
        allowed = (Path.home() / "herdr-agents" / "work").resolve()
        if allowed not in work.parents:
            raise ScopeError("execution workspace must be under the existing sandbox work root")
        if not argv or not all(isinstance(arg, str) and "\x00" not in arg for arg in argv):
            raise ScopeError("invalid execution argv")
        unit = self.unit_for(lease)
        # The existing sandbox enforces mount/PID isolation and clears the
        # environment. systemd owns the entire process tree independently
        # from this coordinator's connection lifetime.
        submitted = False
        try:
            self._call(["systemd-run", "--user", "--unit", unit, "--service-type=exec", "--collect",
                        "--setenv=HERDR_SANDBOX_EXTERNAL_SCOPE=1",
                        "--property=KillMode=control-group", "--property=TimeoutStopSec=10",
                        "--property=TasksMax=512", "--property=MemoryMax=8G", "--property=CPUQuota=200%", "--",
                        "bash", str(self.sandbox), str(work), shlex.join(argv)])
            submitted = True
            identity = self._identity(unit)
            return ScopeReceipt(lease.agent_id, lease.host_uid, lease.generation, unit,
                                identity["ControlGroup"], str(work), identity["InvocationID"])
        except (ScopeError, OSError, subprocess.SubprocessError) as exc:
            if submitted:
                try:
                    self._call(["systemctl", "--user", "stop", unit])
                except (ScopeError, OSError, subprocess.SubprocessError):
                    pass
            raise ScopeStartUncertain(lease, unit, submitted) from exc

    def _identity(self, unit):
        output = self._call(["systemctl", "--user", "show", unit, "--property=ControlGroup",
                             "--property=InvocationID", "--property=KillMode"])
        try:
            identity = dict(line.split("=", 1) for line in output.splitlines())
        except ValueError:
            raise ScopeError("execution scope identity is unavailable") from None
        group = identity.get("ControlGroup", "")
        if (not group.startswith("/user.slice/") or ".." in Path(group).parts
                or not re.fullmatch(r"[a-f0-9]{32}", identity.get("InvocationID", ""))
                or identity.get("KillMode") != "control-group"):
            raise ScopeError("execution scope identity is unavailable")
        return identity

    def _bound_group(self, receipt):
        identity = self._identity(receipt.unit)
        if (identity["ControlGroup"] != receipt.control_group
                or identity["InvocationID"] != receipt.invocation_id):
            raise ScopeError("execution cgroup receipt is not bound to this invocation")
        return self.cgroup_root / identity["ControlGroup"].lstrip("/")

    def stop(self, lease, receipt: ScopeReceipt):
        if (receipt.unit != self.unit_for(lease) or receipt.agent_id != lease.agent_id
                or receipt.host_uid != lease.host_uid or receipt.generation != lease.generation):
            raise ScopeError("execution receipt identity mismatch")
        group = self._bound_group(receipt)
        self._call(["systemctl", "--user", "stop", receipt.unit])
        try:
            events = dict(line.split() for line in (group / "cgroup.events").read_text().splitlines())
        except FileNotFoundError:
            if group.exists():
                raise ScopeError("execution cgroup state is unknown") from None
        else:
            if events.get("populated") != "0":
                raise ScopeError("execution cgroup remains populated")
        return {"agent_id": lease.agent_id, "generation": lease.generation,
                "state": "stopped", "stop_verified": True, "unit": receipt.unit}

    def accounting(self, receipt: ScopeReceipt):
        group = self._bound_group(receipt)
        errors = []
        def read(name):
            try:
                return (group / name).read_text()
            except OSError:
                errors.append(name + "_unavailable")
                return None
        def scalar(name):
            value = read(name)
            try:
                return int(value.strip()) if value is not None else None
            except ValueError:
                errors.append(name + "_invalid")
                return None
        cpu = read("cpu.stat")
        try:
            cpu_fields = dict(line.split() for line in cpu.splitlines()) if cpu else {}
            cpu_seconds = int(cpu_fields["usage_usec"]) / 1_000_000 if "usage_usec" in cpu_fields else None
        except (ValueError, TypeError):
            cpu_seconds = None
            errors.append("cpu.stat_invalid")
        io = read("io.stat")
        io_bytes = {"read_bytes": 0, "write_bytes": 0} if io is not None else None
        if io_bytes is not None:
            try:
                for line in io.splitlines():
                    fields = dict(field.split("=", 1) for field in line.split()[1:])
                    io_bytes["read_bytes"] += int(fields.get("rbytes", 0))
                    io_bytes["write_bytes"] += int(fields.get("wbytes", 0))
            except ValueError:
                io_bytes = None
                errors.append("io.stat_invalid")
        pressure = {}
        for kind in ("cpu", "memory", "io"):
            observed = read(kind + ".pressure")
            if observed is None:
                continue
            try:
                fields = dict(field.split("=", 1) for field in observed.splitlines()[0].split()[1:])
                pressure[kind] = {name: float(fields[name]) for name in ("avg10", "avg60")}
            except (ValueError, KeyError, IndexError):
                errors.append(kind + ".pressure_invalid")
        return {"agent_id": receipt.agent_id, "host_uid": receipt.host_uid,
                "generation": receipt.generation, "source": "cgroup_v2",
                "cpu_seconds": cpu_seconds, "memory_bytes": scalar("memory.current"),
                "memory_peak_bytes": scalar("memory.peak"), "io": io_bytes,
                "pressure": pressure, "source_errors": errors}
