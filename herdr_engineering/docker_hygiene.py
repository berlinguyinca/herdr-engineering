"""Docker hygiene for agent-started test containers.

Only things labelled ``herdr.ephemeral=1`` are removed by default (containers, volumes).
Unlabelled named volumes (databases, registries, corpora) are never touched. ``docker builder
prune`` has no label filter and ``docker image prune`` removes any dangling image, so both are
opt-in (``builder_prune`` / ``image_prune``) and off by default.

Start throwaway containers with ``docker run $(herdr-eng cleanup --print-run-args) ...`` or set
``HERDR_DOCKER_RUN_LABELS`` (documented in docs/disk-hygiene.md): ``--rm --label herdr.ephemeral=1``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from typing import Any

from .cleanup_command import EPHEMERAL_LABEL, Refusal

RUN_ARGS: tuple[str, ...] = ("--rm", "--label", EPHEMERAL_LABEL)
STOPPED = ("exited", "created", "dead")


def run_args() -> list[str]:
    """Arguments to add to ``docker run``; ``HERDR_DOCKER_RUN_LABELS`` overrides when set."""
    override = os.environ.get("HERDR_DOCKER_RUN_LABELS")
    return override.split() if override else list(RUN_ARGS)


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _docker("info", "--format", "{{.ServerVersion}}", timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _docker(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                          check=False)


def parse_docker_time(value: str) -> float | None:
    """Docker's RFC3339 nanosecond stamps to epoch seconds; the zero time becomes None."""
    if not value or value.startswith("0001-"):
        return None
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)", value)
    if not match:
        return None
    zone = "+00:00" if match.group(3) == "Z" else match.group(3)
    return datetime.fromisoformat(match.group(1) + zone).astimezone(UTC).timestamp()


def _inspect(ref: str) -> dict[str, Any] | None:
    proc = _docker("inspect", ref)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)[0]
    except (ValueError, IndexError):
        return None


def _stopped_age(info: dict[str, Any], now: float) -> float | None:
    state = info.get("State", {})
    if state.get("Status") not in STOPPED:
        return None
    since = parse_docker_time(state.get("FinishedAt", "")) or parse_docker_time(info.get("Created", ""))
    return None if since is None else now - since


def remove_ephemeral_container(ref: str, *, dry_run: bool) -> dict[str, Any]:
    info = _inspect(ref)
    if info is None:
        raise Refusal("not_found", f"no container {ref}")
    if (info.get("Config", {}).get("Labels") or {}).get("herdr.ephemeral") != "1":
        raise Refusal("not_labelled", f"container {ref} lacks label {EPHEMERAL_LABEL}")
    if info.get("State", {}).get("Status") not in STOPPED:
        raise Refusal("running", f"container {ref} is not stopped")
    if not dry_run:
        done = _docker("rm", "-v", info["Id"])
        if done.returncode != 0:
            raise Refusal("docker_refused", done.stderr.strip())
    return {"container": info["Id"][:12]}


def prune_steps(keep_storage: str = "5GB", *, builder_prune: bool = False,
                image_prune: bool = False) -> list[tuple[str, list[str]]]:
    """Prune commands of the sweep. Only the label-filtered volume prune is on by default."""
    steps = [("volumes", ["volume", "prune", "-f", "--all", "--filter", f"label={EPHEMERAL_LABEL}"])]
    if builder_prune:
        steps.append(("builder", ["builder", "prune", "-f", "--keep-storage", keep_storage]))
    if image_prune:
        steps.append(("images", ["image", "prune", "-f"]))
    return steps


def sweep(*, dry_run: bool = False, min_age_seconds: int = 3600, keep_storage: str = "5GB",
          now: float | None = None, builder_prune: bool = False, image_prune: bool = False) -> dict[str, Any]:
    """The nightly job. Returns a JSON-able report; never raises for a docker failure."""
    report: dict[str, Any] = {"job": "docker", "dry_run": dry_run, "containers": [], "steps": []}
    if not docker_available():
        report["status"] = "docker_unavailable"
        return report
    now = time.time() if now is None else now
    listing = _docker("ps", "-aq", "--filter", f"label={EPHEMERAL_LABEL}", "--no-trunc")
    for cid in listing.stdout.split():
        info = _inspect(cid)
        age = None if info is None else _stopped_age(info, now)
        if info is None or age is None or age <= min_age_seconds:
            continue
        entry = {"id": cid[:12], "name": info.get("Name", "").lstrip("/"), "age_seconds": int(age)}
        if not dry_run:
            done = _docker("rm", "-v", cid)
            entry["removed"] = done.returncode == 0
        report["containers"].append(entry)
    steps = prune_steps(keep_storage, builder_prune=builder_prune, image_prune=image_prune)
    for name, args in steps:
        if dry_run:
            report["steps"].append({"step": name, "command": ["docker", *args], "ran": False})
            continue
        done = _docker(*args)
        if done.returncode != 0 and name == "volumes" and "unknown flag" in done.stderr:
            done = _docker("volume", "prune", "-f", "--filter", f"label={EPHEMERAL_LABEL}")
        report["steps"].append({"step": name, "ran": True, "ok": done.returncode == 0,
                                "output": done.stdout.strip().splitlines()[-1:] or done.stderr.strip()[:200]})
    report["status"] = "dry_run" if dry_run else "ok"
    return report
