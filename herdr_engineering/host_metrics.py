"""Host telemetry core sampler (stdlib only).

``HostSampler.sample()`` returns a flat dict of CPU / memory / swap / load /
disk / uptime / network metrics. A missing tool or unreadable file omits the
field; it never raises and never reports a fake zero. CPU and network rates need
two readings, so the first ``sample()`` call omits them.
"""
from __future__ import annotations

import os
import platform
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

_PSEUDO_FS = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "overlay",
    "squashfs", "autofs", "mqueue", "hugetlbfs", "debugfs", "tracefs", "securityfs",
    "pstore", "configfs", "fusectl", "binfmt_misc", "bpf", "nsfs", "ramfs", "efivarfs",
    "rpc_pipefs", "fuse.lxcfs",
}
MAX_MOUNTS = 8
MAX_GPUS = 8
TOP_N = 5
_MIB = 1024 ** 2


def _run(cmd: list[str], timeout: float = 5.0) -> str | None:
    """Run ``cmd``; stdout on success, ``None`` on any failure or timeout."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              check=True).stdout
    except Exception:  # noqa: BLE001 - missing tool, timeout, non-zero exit
        return None


def _num(s) -> float | None:
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def _gpu(name, util=None, used=None, total=None, temp=None) -> dict:
    return {"name": name, "util_pct": util, "vram_used_b": used,
            "vram_total_b": total, "temp_c": temp}


def parse_nvidia_smi(csv: str) -> list[dict]:
    """``nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,
    temperature.gpu --format=csv,noheader,nounits`` (memory in MiB)."""
    out = []
    for line in csv.splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 5 or not f[0]:
            continue
        util, used, total, temp = (_num(x) for x in f[1:5])
        out.append(_gpu(f[0], util,
                        None if used is None else int(used * _MIB),
                        None if total is None else int(total * _MIB), temp))
    return out[:MAX_GPUS]


def parse_rocm_smi(json_text: str) -> list[dict]:
    """``rocm-smi --showproductname --showuse --showmeminfo vram --showtemp --json``."""
    data = json.loads(json_text)
    out = []
    for key in sorted(k for k in data if str(k).startswith("card")):
        c = data[key]
        used, total = _num(c.get("VRAM Total Used Memory (B)")), _num(c.get("VRAM Total Memory (B)"))
        out.append(_gpu(c.get("Card Series") or c.get("Card series") or key,
                        _num(c.get("GPU use (%)")),
                        None if used is None else int(used),
                        None if total is None else int(total),
                        _num(c.get("Temperature (Sensor edge) (C)"))))
    return out[:MAX_GPUS]


def parse_apple_gpu(system_profiler_json: str) -> list[dict]:
    """``system_profiler SPDisplaysDataType -json``: name and core count only."""
    out = []
    for d in json.loads(system_profiler_json).get("SPDisplaysDataType", []):
        name = d.get("sppci_model") or d.get("_name")
        if not name:
            continue
        g = _gpu(name)
        cores = _num(d.get("sppci_cores"))
        if cores is not None:
            g["cores"] = int(cores)
        out.append(g)
    return out[:MAX_GPUS]


def parse_ps(output: str, limit: int = TOP_N) -> dict:
    """Parse ``ps -A -o pid=,user=,pcpu=,pmem=,comm=`` into top-N by cpu and mem."""
    rows = []
    for line in output.splitlines():
        f = line.split(None, 4)
        if len(f) < 5:
            continue
        pid, cpu, mem = _num(f[0]), _num(f[2]), _num(f[3])
        if pid is None or cpu is None or mem is None:
            continue
        cmd = f[4].strip().rsplit("/", 1)[-1] or f[4].strip()
        rows.append({"pid": int(pid), "command": cmd[:64], "user": f[1],
                     "cpu_pct": cpu, "mem_pct": mem, "owner": None})
    return {"cpu": sorted(rows, key=lambda r: -r["cpu_pct"])[:limit],
            "mem": sorted(rows, key=lambda r: -r["mem_pct"])[:limit]}


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
    def __init__(self, agent_lookup: Callable[[int], str | None] = lambda pid: None,
                 agents_running: Callable[[], int] = lambda: 0) -> None:
        self._agent_lookup = agent_lookup
        self._agents_running = agents_running
        self._prev_cpu: tuple[int, int] | None = None
        self._prev_net: tuple[float, int, int] | None = None

    # -- I/O seams (patched in tests) ------------------------------------
    def _read(self, path: str) -> str:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def _run(self, argv: list[str]) -> str:
        out = _run(argv)
        if out is None:
            raise RuntimeError(f"command failed: {argv[0]}")
        return out

    def _statvfs(self, path: str):
        return os.statvfs(path)

    # -- public ----------------------------------------------------------
    def sample(self) -> dict:
        out: dict = {}
        darwin = platform.system() == "Darwin"
        steps = ([self._mac_cpu, self._mac_mem, self._mac_uptime, self._mac_net,
                  self._load, self._disk_pct, self._mounts_df, self._gpus, self._top, self._agents]
                 if darwin else
                 [self._linux_cpu, self._linux_mem, self._linux_uptime,
                  self._linux_net, self._load, self._disk_pct, self._mounts_proc, self._gpus, self._top,
                  self._agents])
        for step in steps:
            try:
                out.update(step())
            except Exception:  # noqa: BLE001 - a failed reader omits its fields
                pass
        if darwin and out.get("gpus") and "mem_total_b" in out:
            for g in out["gpus"]:  # unified memory: report system RAM use
                g["vram_used_b"], g["vram_total_b"] = out["mem_used_b"], out["mem_total_b"]
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
        return {"path": path, "pct": _pct(used, total), "used_b": used, "total_b": total,
                "_dev": (self._st_dev(path), total)}

    def _st_dev(self, path: str):
        try:
            return os.stat(path).st_dev
        except OSError:
            return path

    def _disk_pct(self) -> dict:
        """``disk``: pct used of the fs holding the herdr data dir.

        Independent of the mount list: statvfs on the nearest existing ancestor.
        """
        p = Path(_herdr_data_dir())
        while not p.exists() and p != p.parent:
            p = p.parent
        u = self._usage(str(p))
        return {"disk": u["pct"]} if u else {}

    def _cap_mounts(self, mounts: list[dict]) -> dict:
        mounts = [{k: v for k, v in m.items() if k != "_dev"}
                  for m in sorted(mounts, key=lambda m: m["path"])[:MAX_MOUNTS]]
        return {"mounts": mounts} if mounts else {}

    # -- gpu / top / agents -----------------------------------------------
    def _try(self, argv: list[str]) -> str | None:
        try:
            return self._run(argv)
        except Exception:  # noqa: BLE001
            return None

    def _gpus(self) -> dict:
        gpus: list[dict] = []
        if platform.system() == "Darwin":
            txt = self._try(["system_profiler", "SPDisplaysDataType", "-json"])
            gpus = parse_apple_gpu(txt) if txt else []
            if gpus and self._try(["sudo", "-n", "true"]) is not None:
                pm = self._try(["sudo", "-n", "powermetrics", "--samplers", "gpu_power",
                                "-n", "1", "-i", "200"])
                m = re.search(r"GPU (?:HW )?active residency:\s+([\d.]+)%", pm or "")
                if m:
                    gpus[0]["util_pct"] = float(m.group(1))
        else:
            txt = self._try(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,"
                             "memory.total,temperature.gpu", "--format=csv,noheader,nounits"])
            gpus = parse_nvidia_smi(txt) if txt else []
            if not gpus:
                txt = self._try(["rocm-smi", "--showproductname", "--showuse", "--showmeminfo",
                                 "vram", "--showtemp", "--json"])
                try:
                    gpus = parse_rocm_smi(txt) if txt else []
                except ValueError:
                    gpus = []
        return {"gpus": gpus[:MAX_GPUS]} if gpus else {}

    def _top(self) -> dict:
        txt = self._try(["ps", "-A", "-o", "pid=,user=,pcpu=,pmem=,comm="])
        if not txt:
            return {}
        top = parse_ps(txt)
        for e in top["cpu"] + top["mem"]:
            try:
                e["owner"] = self._agent_lookup(e["pid"])
            except Exception:  # noqa: BLE001
                e["owner"] = None
        return {"top": top} if top["cpu"] else {}

    def _agents(self) -> dict:
        try:
            return {"agents_running": int(self._agents_running())}
        except Exception:  # noqa: BLE001
            return {}

    # -- linux -----------------------------------------------------------
    def _linux_cpu(self) -> dict:
        return self._cpu_delta(parse_proc_stat(self._read("/proc/stat")))

    def _linux_mem(self) -> dict:
        mi = parse_meminfo(self._read("/proc/meminfo"))
        total = mi["MemTotal"]
        if total <= 0:
            return {}
        avail = mi.get("MemAvailable")
        if avail is None:
            avail = mi["MemFree"] + mi.get("Buffers", 0) + mi.get("Cached", 0)
        used = max(total - avail, 0)
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
        return self._cap_mounts(self._dedupe(mounts))

    @staticmethod
    def _dedupe(mounts: list[dict]) -> list[dict]:
        """One entry per (st_dev, size): bind mounts share a device."""
        seen, out = set(), []
        for m in sorted(mounts, key=lambda m: m["path"]):
            if m["_dev"] in seen:
                continue
            seen.add(m["_dev"])
            out.append(m)
        return out

    # -- macOS -----------------------------------------------------------
    def _mac_cpu(self) -> dict:
        # Ruling: macOS keeps `top` as the cpu source. The first `top` sample is
        # since-boot, so take two and use the LAST "CPU usage" line (accurate over
        # 1s). Hence cpu is present on the first sample() on macOS; the "no cpu on
        # first call" rule applies to Linux deltas only.
        txt = self._run(["top", "-l", "2", "-n", "0", "-s", "1"])
        found = re.findall(r"CPU usage:.*?([\d.]+)% idle", txt)
        return {"cpu": round(100.0 - float(found[-1]), 1)} if found else {}

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
        return self._cap_mounts(self._dedupe(mounts))
