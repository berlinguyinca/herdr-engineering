"""Disk hygiene on by default: install/inspect the cleanup timer and the cargo wrapper.

``install`` is called by ``scripts/install.sh`` and by the fleet update/host-apply flows, so a
fresh or updated host gets the build-cleanup job (systemd user unit on Linux, launchd agent on
macOS) and the managed cargo launcher without manual steps. ``build_cleanup.enabled: false`` and
``fabric_build_cache.enabled: false`` are the explicit opt-outs.
"""
from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LAUNCHD_LABEL = "com.herdr-engineering.build-cleanup"
SYSTEMD_UNIT = "herdr-eng-build-cleanup.service"


def launchd_plist(herdr_eng: Path, interval_seconds: int) -> bytes:
    """A periodic one-shot sweep (macOS has no /proc, so no server-liveness ``watch`` loop)."""
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(herdr_eng), "build-cleanup", "run"],
        "StartInterval": int(interval_seconds),
        "RunAtLoad": True,
        "Nice": 19,
        "LowPriorityIO": True,
        "ProcessType": "Background",
    })


_UNIT_TEMPLATE = """[Unit]
Description=HerdR Engineering Rust build-cache maintenance

[Service]
Environment="HERDR_ENGINEERING_CONFIG=%h/.config/herdr-engineering/config.yaml"
ExecStart=__HERDR_ENG__ build-cleanup watch
Restart=on-failure
RestartSec=30
Nice=19
IOSchedulingClass=idle
UMask=0077

[Install]
WantedBy=default.target
"""


def _plist_path(home: Path) -> Path:
    return home / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _unit_path(home: Path) -> Path:
    return home / ".config/systemd/user" / SYSTEMD_UNIT


def install_cleanup_job(home: Path, herdr_eng: Path, *, platform: str,
                        interval_seconds: int = 900, activate: bool = True) -> dict[str, Any]:
    """Write (and optionally load) this platform's cleanup job; idempotent."""
    if platform == "darwin":
        path = _plist_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(launchd_plist(herdr_eng, interval_seconds))
        if activate:
            domain = f"gui/{__import__('os').getuid()}"
            subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"],
                           capture_output=True, timeout=30, check=False)
            subprocess.run(["launchctl", "bootstrap", domain, str(path)],
                           capture_output=True, timeout=30, check=True)
        return {"job": str(path), "kind": "launchd", "activated": activate}
    path = _unit_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    binary = str(herdr_eng).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    path.write_text(_UNIT_TEMPLATE.replace("__HERDR_ENG__", f'"{binary}"'))
    if activate:
        for argv in (["daemon-reload"], ["enable", SYSTEMD_UNIT], ["restart", SYSTEMD_UNIT]):
            subprocess.run(["systemctl", "--user", *argv], capture_output=True, timeout=60, check=True)
    return {"job": str(path), "kind": "systemd", "activated": activate}


def install(home: Path, herdr_eng: Path, *, platform: str | None = None,
            activate: bool = True, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Install the cleanup job and cargo launcher unless the config opts out. Never raises."""
    from .config import load_config
    platform = platform or ("darwin" if sys.platform == "darwin" else "linux")
    config = config if config is not None else load_config()
    result: dict[str, Any] = {}
    cleanup = config.get("build_cleanup") or {}
    if cleanup.get("enabled", True):
        try:
            result["build_cleanup"] = install_cleanup_job(
                home, herdr_eng, platform=platform,
                interval_seconds=int(cleanup.get("interval_seconds", 900)), activate=activate)
        except (OSError, subprocess.SubprocessError) as error:
            result["build_cleanup"] = {"error": f"{type(error).__name__}: {error}"}
    else:
        result["build_cleanup"] = {"skipped": "build_cleanup.enabled is false"}
    cache = config.get("fabric_build_cache") or {}
    if cache.get("enabled", True) and (cache.get("local") or {}).get("manage_targets", True):
        from . import cargo_host
        try:
            manifest = cargo_host.host_root(home) / "install.json"
            real = cargo_host._real_cargo(home) if manifest.exists() else Path(shutil.which("cargo") or "")
            result["cargo_host"] = cargo_host.install(home, real, Path(sys.executable))
        except (OSError, ValueError) as error:
            result["cargo_host"] = {"error": f"{type(error).__name__}: {error}"}
    else:
        result["cargo_host"] = {"skipped": "fabric_build_cache disabled"}
    return result


def _last_run(state_dir: Path) -> str | None:
    try:
        return json.loads((state_dir / "build-cleanup.json").read_text()).get("timestamp")
    except (OSError, ValueError):
        return None


def status(home: Path, config: dict[str, Any], state_dir: Path, *, platform: str | None = None,
           ) -> dict[str, Any]:
    """Read-only snapshot used by ``doctor`` and ``build-hygiene status``."""
    from . import cargo_host
    from .build_cleanup import _usage
    platform = platform or ("darwin" if sys.platform == "darwin" else "linux")
    cleanup = config.get("build_cleanup") or {}
    cache = config.get("fabric_build_cache") or {}
    job = _plist_path(home) if platform == "darwin" else _unit_path(home)
    last = _last_run(state_dir)
    return {
        "usage_percent": round(_usage(home), 1),
        "pressure_percent": float(cleanup.get("pressure_percent", 90)),
        "refuse_builds_percent": float(cleanup.get("refuse_builds_percent", 90)),
        "build_cleanup_enabled": bool(cleanup.get("enabled", True)),
        "build_cleanup_job_installed": job.exists(),
        "build_cleanup_last_run": last,
        "cargo_host_enabled": bool(cache.get("enabled", True)
                                   and (cache.get("local") or {}).get("manage_targets", True)),
        "cargo_host_installed": (cargo_host.host_root(home) / "install.json").exists(),
        "generated_at": datetime.now(UTC).isoformat(),
    }


def main(argv: list[str]) -> int:
    import argparse

    from .build_coord import _default_state_dir
    from .config import load_config
    parser = argparse.ArgumentParser(prog="herdr-eng build-hygiene")
    parser.add_argument("action", choices=["install", "status"])
    parser.add_argument("--no-activate", action="store_true")
    args = parser.parse_args(argv)
    home = Path.home()
    if args.action == "install":
        herdr_eng = Path(shutil.which("herdr-eng") or sys.argv[0]).absolute()
        print(json.dumps(install(home, herdr_eng, activate=not args.no_activate), indent=2))
        return 0
    print(json.dumps(status(home, load_config(), _default_state_dir()), indent=2))
    return 0
