"""Read-only Slurm capacity for assistant placement.

A cluster takes part only when the operator configured it for assistant use and listed the sandbox profiles it has
attested; ``sinfo`` can report capacity but cannot attest a display, an internet path or a sandbox, so those come from
configuration and are never inferred. Capacity is conservative: only fully idle nodes count as free. Nothing here
submits a job. Launching onto a cluster needs a runner installed and attested there, which is a separate change.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from . import placement

SINFO_FORMAT = "%R|%a|%T|%D|%c|%m|%G"
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}\Z")
_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_PARTITION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_GPU_RE = re.compile(r"gpu(?::[A-Za-z0-9_.-]+)?:(\d+)")


def slurm_config(value: Any) -> list[dict[str, Any]]:
    """``personal_assistant.placement.slurm.clusters``. A cluster without ``assistant_use: true`` is ignored."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("placement.slurm.clusters must be a list")
    out, seen = [], set()
    allowed = {"id", "label", "login_host", "assistant_use", "partitions"}
    for raw in value:
        if not isinstance(raw, dict) or set(raw) - allowed or not {"id", "login_host", "partitions"} <= set(raw):
            raise ValueError("a slurm cluster needs id, login_host and partitions")
        cid, host = raw["id"], raw["login_host"]
        if not isinstance(cid, str) or not _ID_RE.fullmatch(cid) or cid in seen:
            raise ValueError("invalid or duplicate slurm cluster id")
        if not isinstance(host, str) or not _HOST_RE.fullmatch(host):
            raise ValueError("invalid slurm login_host")
        if raw.get("assistant_use") is not True:
            seen.add(cid)
            continue
        seen.add(cid)
        parts = raw["partitions"]
        if not isinstance(parts, dict) or not parts or len(parts) > 32:
            raise ValueError("a slurm cluster needs at least one partition")
        cleaned: dict[str, dict[str, Any]] = {}
        for name, spec in parts.items():
            if (not isinstance(name, str) or not _PARTITION_RE.fullmatch(name) or not isinstance(spec, dict)
                    or set(spec) - {"capabilities", "profiles", "role_bindings"}):
                raise ValueError("invalid slurm partition")
            try:
                caps = placement.normalize_capabilities(spec.get("capabilities"), what="slurm capabilities")
            except placement.PlacementError as exc:
                raise ValueError(str(exc)) from None
            profiles = spec.get("profiles", [])
            if not isinstance(profiles, list) or not all(isinstance(p, str) and p for p in profiles):
                raise ValueError("slurm partition profiles must be a list of names")
            roles = spec.get("role_bindings", [])
            if not isinstance(roles, list) or not all(isinstance(r, str) and r for r in roles):
                raise ValueError("slurm partition role_bindings must be a list of id@version:digest keys")
            cleaned[name] = {"capabilities": caps, "profiles": sorted(set(profiles)), "role_bindings": roles}
        out.append({"id": cid, "label": str(raw.get("label") or cid)[:80], "login_host": host,
                    "partitions": cleaned})
    return out


def parse_sinfo(text: str) -> dict[str, dict[str, Any]]:
    """Per partition: availability and fully idle capacity from ``sinfo -h -o SINFO_FORMAT`` lines."""
    parts: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        fields = line.strip().split("|")
        if len(fields) != 7:
            continue
        name, avail, state, nodes, cpus, mem, gres = fields
        name = name.rstrip("*")
        if not _PARTITION_RE.fullmatch(name) or not (nodes.isdigit() and cpus.isdigit()):
            continue
        row = parts.setdefault(name, {"up": avail.strip() == "up", "idle_nodes": 0, "cpus": 0,
                                      "memory_mb": 0, "gpus_per_node": 0})
        row["up"] = row["up"] and avail.strip() == "up"
        row["cpus"] = max(row["cpus"], int(cpus))
        digits = re.sub(r"\D", "", mem)
        row["memory_mb"] = max(row["memory_mb"], int(digits) if digits else 0)
        match = _GPU_RE.search(gres)
        if match:
            row["gpus_per_node"] = max(row["gpus_per_node"], int(match.group(1)))
        if state.strip().rstrip("*~#!%$@^-") == "idle":
            row["idle_nodes"] += int(nodes)
    return parts


def _ssh_runner(login_host: str) -> str:
    """Fixed argv, no user input: the only value interpolated is an operator-validated host name."""
    if not _HOST_RE.fullmatch(login_host) or login_host.startswith("-"):
        raise ValueError("invalid login host")
    done = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", login_host, "sinfo", "-h", "-o", SINFO_FORMAT],
        capture_output=True, text=True, timeout=12, check=False)
    if done.returncode != 0:
        raise OSError("sinfo failed")
    return done.stdout


class SlurmProbe:
    """Cached candidates for every cluster configured for assistant use. ``runner(login_host) -> sinfo text``."""

    def __init__(self, clusters: list[dict[str, Any]], *, runner: Callable[[str], str] | None = None,
                 ttl: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.clusters, self.runner, self.ttl, self.clock = clusters, runner or _ssh_runner, ttl, clock
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    def candidates(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for cluster in self.clusters:
            out.extend(self._cluster(cluster))
        return out

    def _cluster(self, cluster: dict[str, Any]) -> list[dict[str, Any]]:
        now = self.clock()
        with self._lock:
            hit = self._cache.get(cluster["id"])
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        try:
            info = parse_sinfo(self.runner(cluster["login_host"]))
            reachable = True
        except Exception:  # noqa: BLE001 - an unreachable cluster is simply unavailable, never an error to the caller
            info, reachable = {}, False
        rows = []
        for name, spec in cluster["partitions"].items():
            seen = info.get(name)
            usable = reachable and seen is not None and seen["up"]
            idle = seen["idle_nodes"] if usable else 0
            caps = set(spec["capabilities"])
            if seen and seen["gpus_per_node"]:
                caps.add("gpu")
            rows.append({
                "id": f"slurm:{cluster['id']}:{name}", "kind": "slurm", "backend": "slurm",
                "label": f"{cluster['label']} / {name}",
                "status": "ready" if usable else "unavailable",
                "capabilities": sorted(caps),
                "free": {"cpus": idle * seen["cpus"] if usable else 0,
                         "memory_mb": idle * seen["memory_mb"] if usable else 0,
                         "gpus": idle * seen["gpus_per_node"] if usable else 0},
                "attested_at": 0.0, "profiles": list(spec["profiles"]),
                "role_bindings": list(spec["role_bindings"]), "queue_wait_s": 0,
            })
        with self._lock:
            self._cache[cluster["id"]] = (now, rows)
        return rows
