"""Bounded local resource observations for the existing fabric agent."""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class ResourceProbe:
    def __init__(self, identity_path: Path, workspace: Path, *, proc_root=Path("/proc")):
        self.identity_path, self.workspace, self.proc_root = identity_path, workspace, proc_root
        self.previous_cpu = None

    def sample(self):
        if platform.system() == "Darwin":
            return macos_resource_snapshot(self.identity_path, self.workspace)
        observation = resource_snapshot(self.identity_path, self.workspace, proc_root=self.proc_root)
        observation["cpu_utilization"] = None
        try:
            ticks = [int(value) for value in (self.proc_root / "stat").read_text().splitlines()[0].split()[1:9]]
            current = (sum(ticks), ticks[3] + ticks[4])
            if self.previous_cpu is not None:
                total, idle = (current[i] - self.previous_cpu[i] for i in (0, 1))
                if total > 0 and 0 <= idle <= total:
                    observation["cpu_utilization"] = 1 - idle / total
            self.previous_cpu = current
        except (OSError, ValueError, IndexError):
            observation["source_errors"].append("cpu_utilization_unavailable")
        return observation


def macos_resource_snapshot(identity_path: Path, workspace: Path, *, run=subprocess.run) -> dict:
    """macOS observations have lower confidence; PSI/cgroups are unsupported."""
    errors = ["psi_unsupported", "cgroup_accounting_unsupported"]
    def read(argv):
        result = run(argv, capture_output=True, text=True, timeout=2, check=True)
        return result.stdout
    total = available = utilization = None
    cores = os.cpu_count()
    try:
        total = int(read(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip())
        stats = read(["/usr/bin/vm_stat"])
        page_size = int(re.search(r"page size of (\d+) bytes", stats).group(1))
        pages = {key.strip(): int(value.strip().rstrip(".")) for key, value in
                 (line.split(":", 1) for line in stats.splitlines()[1:] if ":" in line)}
        available = min(total, sum(pages.get(key, 0) for key in
                                   ("Pages free", "Pages inactive", "Pages speculative")) * page_size)
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        errors.append("memory_unavailable")
    try:
        # Only numeric CPU fields are read, never argv or environment.
        values = [float(value) for value in read(["/bin/ps", "-A", "-o", "%cpu="]).split()]
        if values and cores:
            utilization = min(1, max(0, sum(values) / (100 * cores)))
    except (OSError, ValueError, subprocess.SubprocessError):
        errors.append("cpu_utilization_unavailable")
    disk = shutil.disk_usage(workspace)
    gib = 1024 ** 3
    return {"host_uid": host_identity(identity_path), "platform": "darwin",
            "captured_at": datetime.now(UTC).isoformat(), "sequence": time.time_ns(),
            "probe_version": 1, "confidence": "partial", "source_errors": errors,
            "cpu_count": cores, "cpu_utilization": utilization,
            "cpu_source": "process_cpu_estimate", "memory_total_gib": total / gib if total else None,
            "memory_available_gib": available / gib if available is not None else None,
            "disk_total_gib": disk.total / gib, "disk_free_gib": disk.free / gib, "pressure": {}}


def host_identity(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(f"host-{uuid4().hex}\n")
            stream.flush()
            os.fsync(stream.fileno())
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("host identity must be private")
    identity = path.read_text().strip()
    if not re.fullmatch(r"host-[a-f0-9]{32}", identity):
        raise ValueError("host identity is invalid")
    return identity


def resource_snapshot(identity_path: Path, workspace: Path, *, proc_root=Path("/proc")) -> dict:
    errors = []
    memory = {}
    try:
        for line in (proc_root / "meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError):
        errors.append("memory_unavailable")
    pressure = {}
    for kind in ("cpu", "memory", "io"):
        try:
            line = next(line for line in (proc_root / "pressure" / kind).read_text().splitlines()
                        if line.startswith("some "))
            pressure[kind] = float(dict(item.split("=") for item in line.split()[1:])["avg10"]) / 100
        except (OSError, ValueError, KeyError, StopIteration):
            errors.append(f"{kind}_pressure_unavailable")
    total = memory.get("MemTotal")
    available = memory.get("MemAvailable")
    disk = shutil.disk_usage(workspace)
    gib = 1024 ** 3
    return {"host_uid": host_identity(identity_path), "platform": platform.system().lower(),
            "captured_at": datetime.now(UTC).isoformat(), "sequence": time.time_ns(),
            "probe_version": 1, "confidence": "partial" if errors else "high",
            "source_errors": errors, "cpu_count": os.cpu_count(),
            "memory_total_gib": total / gib if total is not None else None,
            "memory_available_gib": available / gib if available is not None else None,
            "disk_total_gib": disk.total / gib, "disk_free_gib": disk.free / gib,
            "pressure": pressure}
