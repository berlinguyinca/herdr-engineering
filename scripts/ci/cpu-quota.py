#!/usr/bin/env python3
"""Print the effective CPU quota of this process's cgroup as a whole number >= 1, or nothing when no quota applies.

xdist's `auto` counts the cores in the CPU affinity mask; a cgroup quota does not shrink that. The quota that binds is the smallest one on
the path from the process's cgroup up to the root (Slurm and container runtimes often set it on a parent), on cgroup v2 (`cpu.max`) and
v1 (`cpu.cfs_quota_us` / `cpu.cfs_period_us` under the cpu controller).
Usage: cpu-quota.py [cgroup-root] [proc-cgroup-file]   (defaults /sys/fs/cgroup and /proc/self/cgroup; tests pass real directories)"""
import math
import sys
from pathlib import Path


def _v2(directory: Path):
    try:
        quota, period = (directory / "cpu.max").read_text().split()[:2]
        return None if quota == "max" else float(quota) / float(period)
    except (OSError, ValueError, ZeroDivisionError):
        return None


def _v1(directory: Path):
    try:
        quota = float((directory / "cpu.cfs_quota_us").read_text())
        period = float((directory / "cpu.cfs_period_us").read_text())
        return None if quota <= 0 else quota / period
    except (OSError, ValueError, ZeroDivisionError):
        return None


def _chain(base: Path, relative: str):
    """base, base/a, base/a/b ... for relative 'a/b': the cgroup and every ancestor up to the mount root."""
    parts = [p for p in relative.split("/") if p]
    return [base.joinpath(*parts[:i]) for i in range(len(parts) + 1)]


def effective_quota(root: Path, proc_cgroup: Path):
    v2_path, v1_paths = "", []
    try:
        for line in proc_cgroup.read_text().splitlines():
            _, controllers, path = (line.split(":", 2) + ["", ""])[:3]
            if controllers == "":
                v2_path = path
            elif "cpu" in controllers.split(","):
                v1_paths.append((controllers, path))
    except OSError:
        pass
    limits = [_v2(d) for d in _chain(root, v2_path)]
    for controllers, path in v1_paths:
        for mount in (root / controllers, root / "cpu"):
            limits += [_v1(d) for d in _chain(mount, path)]
    limits += [_v1(root / "cpu"), _v1(root)]
    limits = [x for x in limits if x is not None]
    return max(1, math.floor(min(limits))) if limits else None


if __name__ == "__main__":
    q = effective_quota(Path(sys.argv[1] if len(sys.argv) > 1 else "/sys/fs/cgroup"),
                        Path(sys.argv[2] if len(sys.argv) > 2 else "/proc/self/cgroup"))
    if q is not None:
        print(q)
