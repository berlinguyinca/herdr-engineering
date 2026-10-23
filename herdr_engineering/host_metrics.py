"""Host telemetry core sampler (stdlib only).

``HostSampler.sample()`` returns a flat dict of CPU / memory / swap / load /
disk / uptime / network metrics. A missing tool or unreadable file omits the
field; it never raises and never reports a fake zero. CPU and network rates need
two readings, so the first ``sample()`` call omits them.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from pathlib import Path

_PSEUDO_FS = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "overlay",
    "squashfs", "autofs", "mqueue", "hugetlbfs", "debugfs", "tracefs", "securityfs",
    "pstore", "configfs", "fusectl", "binfmt_misc", "bpf", "nsfs", "ramfs", "efivarfs",
    "rpc_pipefs", "fuse.lxcfs",
}
MAX_MOUNTS = 8


def _herdr_data_dir() -> str:
    return str(Path.home() / ".local/share/herdr-engineering")


def parse_proc_stat(text: str) -> tuple[int, int]:
    """Return (busy, total) jiffies from the aggregate ``cpu`` line."""
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "cpu":
            vals = [int(x) for x in parts[1:]]
            total = sum(vals[:8])
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return total - idle, total
    raise ValueError("no aggregate cpu line")


def parse_meminfo(text: str) -> dict:
    """Return /proc/meminfo as ``{key: bytes}`` (kB values scaled)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"(\w[\w()]*):\s+(\d+)(?:\s*(kB))?", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * (1024 if m.group(3) else 1)
    return out


def _pct(used: float, total: float) -> float:
    return round(100.0 * used / total, 1)


def _parse_size(s: str) -> int:
    m = re.match(r"([\d.]+)([KMGT]?)", s)
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[m.group(2)]
    return int(float(m.group(1)) * mult)


class HostSampler:
    def __init__(self) -> None:
        self._prev_cpu: tuple[int, int] | None = None
        self._prev_net: tuple[float, int, int] | None = None

    # -- I/O seams (patched in tests) ------------------------------------
    def _read(self, path: str) -> str:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def _run(self, argv: list[str]) -> str:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5,
                              check=True).stdout

    def _statvfs(self, path: str):
        return os.statvfs(path)

    # -- public ----------------------------------------------------------
    def sample(self) -> dict:
        out: dict = {}
        darwin = platform.system() == "Darwin"
        steps = ([self._mac_cpu, self._mac_mem, self._mac_uptime, self._mac_net,
                  self._load, self._mounts_df]
                 if darwin else
                 [self._linux_cpu, self._linux_mem, self._linux_uptime,
                  self._linux_net, self._load, self._mounts_proc])
        for step in steps:
            try:
                out.update(step())
            except Exception:  # noqa: BLE001 - a failed reader omits its fields
                pass
        return out

    # -- shared ----------------------------------------------------------
    def _cpu_delta(self, cur: tuple[int, int]) -> dict:
        prev, self._prev_cpu = self._prev_cpu, cur
        if prev is None or cur[1] <= prev[1]:
            return {}
        return {"cpu": round(100.0 * (cur[0] - prev[0]) / (cur[1] - prev[1]), 1)}

    def _net_delta(self, rx: int, tx: int) -> dict:
        now = time.monotonic()
        prev, self._prev_net = self._prev_net, (now, rx, tx)
        if prev is None or now <= prev[0] or rx < prev[1] or tx < prev[2]:
            return {}
        dt = now - prev[0]
        return {"net_rx_bps": round((rx - prev[1]) / dt),
                "net_tx_bps": round((tx - prev[2]) / dt)}

    def _load(self) -> dict:
        l1, l5, l15 = os.getloadavg()
        return {"load1": l1, "load5": l5, "load15": l15}

    def _usage(self, path: str) -> dict | None:
        st = self._statvfs(path)
        total = st.f_blocks * st.f_frsize
        if total <= 0:
            return None
        used = total - st.f_bfree * st.f_frsize
        return {"path": path, "pct": _pct(used, total), "used_b": used, "total_b": total}

    def _disk_pick(self, mounts: list[dict]) -> dict:
        """Cap the list and set ``disk`` to the mount holding the herdr data dir."""
        out: dict = {}
        if not mounts:
            return out
        data = _herdr_data_dir()
        best = None
        for m in mounts:
            p = m["path"].rstrip("/") or "/"
            if (data == p or data.startswith(p.rstrip("/") + "/")) and (
                    best is None or len(p) > len(best["path"].rstrip("/") or "/")):
                best = m
        out["mounts"] = mounts[:MAX_MOUNTS]
        if best is not None:
            out["disk"] = best["pct"]
            if best not in out["mounts"]:
                out["mounts"] = out["mounts"][:MAX_MOUNTS - 1] + [best]
        return out

    # -- linux -----------------------------------------------------------
    def _linux_cpu(self) -> dict:
        return self._cpu_delta(parse_proc_stat(self._read("/proc/stat")))

    def _linux_mem(self) -> dict:
        mi = parse_meminfo(self._read("/proc/meminfo"))
        total = mi["MemTotal"]
        if total <= 0:
            return {}
        used = total - mi["MemAvailable"]
        out = {"mem": _pct(used, total), "mem_used_b": used, "mem_total_b": total}
        st = mi.get("SwapTotal", 0)
        if st > 0:
            out["swap"] = _pct(st - mi["SwapFree"], st)
        return out

    def _linux_uptime(self) -> dict:
        return {"uptime_s": int(float(self._read("/proc/uptime").split()[0]))}

    def _linux_net(self) -> dict:
        rx = tx = 0
        for line in self._read("/proc/net/dev").splitlines()[2:]:
            name, _, rest = line.partition(":")
            if not rest or name.strip() == "lo":
                continue
            f = rest.split()
            rx += int(f[0])
            tx += int(f[8])
        return self._net_delta(rx, tx)

    def _mounts_proc(self) -> dict:
        mounts, seen = [], set()
        for line in self._read("/proc/mounts").splitlines():
            f = line.split()
            if len(f) < 3 or f[2] in _PSEUDO_FS or not f[0].startswith("/"):
                continue
            path = f[1].replace("\\040", " ")
            if path in seen:
                continue
            seen.add(path)
            try:
                u = self._usage(path)
            except OSError:
                continue
            if u:
                mounts.append(u)
        return self._disk_pick(mounts)

    # -- macOS -----------------------------------------------------------
    def _mac_cpu(self) -> dict:
        txt = self._run(["top", "-l", "1", "-n", "0"])
        m = re.search(r"CPU usage:.*?([\d.]+)% idle", txt)
        return {"cpu": round(100.0 - float(m.group(1)), 1)} if m else {}

    def _mac_mem(self) -> dict:
        total = int(self._run(["sysctl", "-n", "hw.memsize"]).strip())
        vm = self._run(["vm_stat"])
        ps = int(re.search(r"page size of (\d+) bytes", vm).group(1))

        def pages(label: str) -> int:
            m = re.search(rf"{label}:\s+(\d+)", vm)
            return int(m.group(1)) if m else 0
        avail = (pages("Pages free") + pages("Pages inactive")
                 + pages("Pages speculative")) * ps
        used = max(total - avail, 0)
        out = {"mem": _pct(used, total), "mem_used_b": used, "mem_total_b": total}
        try:
            sw = self._run(["sysctl", "-n", "vm.swapusage"])
            t = re.search(r"total = ([\d.]+[KMGT]?)", sw)
            u = re.search(r"used = ([\d.]+[KMGT]?)", sw)
            if t and u and _parse_size(t.group(1)) > 0:
                out["swap"] = _pct(_parse_size(u.group(1)), _parse_size(t.group(1)))
        except Exception:  # noqa: BLE001
            pass
        return out

    def _mac_uptime(self) -> dict:
        m = re.search(r"sec = (\d+)", self._run(["sysctl", "-n", "kern.boottime"]))
        return {"uptime_s": max(int(time.time()) - int(m.group(1)), 0)} if m else {}

    def _mac_net(self) -> dict:
        rx = tx = 0
        seen = set()
        for line in self._run(["netstat", "-ib"]).splitlines()[1:]:
            f = line.split()
            if len(f) < 10 or f[0] in seen or f[0].startswith("lo") or "<Link#" not in line:
                continue
            seen.add(f[0])
            # Address may be empty, so index from the end: ... Ibytes Opkts Oerrs Obytes Coll
            try:
                rx += int(f[-5])
                tx += int(f[-2])
            except ValueError:
                continue
        return self._net_delta(rx, tx)

    def _mounts_df(self) -> dict:
        mounts = []
        for line in self._run(["df", "-k"]).splitlines()[1:]:
            f = line.split()
            if len(f) < 6 or not f[0].startswith("/dev/"):
                continue
            path = " ".join(f[8:]) if len(f) >= 9 else f[-1]
            try:
                u = self._usage(path)
            except OSError:
                continue
            if u and not path.startswith(("/System/Volumes/VM", "/System/Volumes/Preboot",
                                          "/System/Volumes/Update")):
                mounts.append(u)
        return self._disk_pick(mounts)
