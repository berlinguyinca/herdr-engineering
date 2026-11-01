"""Read-only Slurm capacity for assistant placement.

A cluster takes part only when the operator configured it for assistant use and listed the sandbox profiles it has
attested; ``sinfo`` can report capacity but cannot attest a display, an internet path or a sandbox, so those come from
configuration and are never inferred. Capacity is per node, because a runtime lives on one node: a partition lists the
usable nodes' idle CPUs, free memory and GPUs, and a request fits only when a single node covers all of it. Down,
drained and draining nodes never count. Nothing here submits a job. Launching onto a cluster needs a runner installed and attested there, which is a separate change.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from . import placement

SINFO_FORMAT = "%P|%a|%n|%T|%C|%e|%m|%G"
SINFO_ARGS = ["-N", "-h", "-o", SINFO_FORMAT]
_USABLE_STATES = frozenset({"idle", "mixed"})
MAX_NODES = 256
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
    """Per partition: availability and the usable nodes, from ``sinfo -N -h -o SINFO_FORMAT`` lines.

    A node is usable when it is ``idle`` or ``mixed``; ``allocated`` has no idle CPU and ``down``, ``drained``,
    ``draining`` and the like are unavailable. Free memory is the node's reported free memory capped at its size, and
    GPUs count only on a node with no allocated CPU (``sinfo`` does not report GPU use, so this errs towards none)."""
    parts: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        fields = line.strip().split("|")
        if len(fields) != 8:
            continue
        name, avail, node, state, cpus, free_mem, mem, gres = fields
        name = name.rstrip("*")
        counts = cpus.split("/")
        if (not _PARTITION_RE.fullmatch(name) or not _HOST_RE.fullmatch(node) or len(counts) != 4
                or not all(c.isdigit() for c in counts)):
            continue
        alloc, idle, _other, total = (int(c) for c in counts)
        row = parts.setdefault(name, {"up": True, "nodes": []})
        row["up"] = row["up"] and avail.strip() == "up"
        if state.strip().rstrip("*~#!%$@^-") not in _USABLE_STATES or len(row["nodes"]) >= MAX_NODES:
            continue
        size = int(re.sub(r"\D", "", mem) or 0)
        free = int(re.sub(r"\D", "", free_mem) or 0)
        gpu = _GPU_RE.search(gres)
        row["nodes"].append({
            "node": node, "cpus": idle, "memory_mb": min(free, size) if size else free,
            "gpus": int(gpu.group(1)) if gpu and alloc == 0 else 0,
            "gpu_total": int(gpu.group(1)) if gpu else 0, "cpus_total": total})
    return parts


def ssh_argv(login_host: str) -> list[str]:
    """``ssh`` joins its remote arguments with spaces and the remote shell re-parses them, so the constant remote command
    is quoted as one string; otherwise the ``|`` in the format would be read as a pipe."""
    if not _HOST_RE.fullmatch(login_host) or login_host.startswith("-"):
        raise ValueError("invalid login host")
    remote = " ".join(shlex.quote(a) for a in ["sinfo", *SINFO_ARGS])
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", login_host, remote]


def _ssh_runner(login_host: str) -> str:
    """Fixed argv, no user input: the only value interpolated is an operator-validated host name."""
    if not _HOST_RE.fullmatch(login_host) or login_host.startswith("-"):
        raise ValueError("invalid login host")
    done = subprocess.run(ssh_argv(login_host), capture_output=True, text=True, timeout=12, check=False)
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
            nodes = [{k: n[k] for k in ("cpus", "memory_mb", "gpus")} for n in seen["nodes"]] if usable else []
            caps = set(spec["capabilities"])
            if seen and any(n["gpu_total"] for n in seen["nodes"]):
                caps.add("gpu")
            rows.append({
                "id": f"slurm:{cluster['id']}:{name}", "kind": "slurm", "backend": "slurm",
                "label": f"{cluster['label']} / {name}",
                "status": "ready" if usable else "unavailable",
                "capabilities": sorted(caps), "nodes": nodes,
                "free": {key: sum(n[key] for n in nodes) for key in placement.RESOURCE_KEYS},
                "attested_at": 0.0, "profiles": list(spec["profiles"]),
                "role_bindings": list(spec["role_bindings"]), "queue_wait_s": 0,
            })
        with self._lock:
            self._cache[cluster["id"]] = (now, rows)
        return rows
