"""Disk hygiene on by default: install/inspect the cleanup timer and the cargo wrapper.

``install`` is called by ``scripts/install.sh`` and by the controller step of ``fleet update``
(fleet hosts get it through ``install.sh``), so a fresh or updated host gets the build-cleanup
job (systemd user unit on Linux, launchd agent on macOS) and the managed cargo launcher without
manual steps. ``build_cleanup.enabled: false`` and
``fabric_build_cache.enabled: false`` are the explicit opt-outs.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LAUNCHD_LABEL = "com.herdr-engineering.build-cleanup"
SYSTEMD_UNIT = "herdr-eng-build-cleanup.service"


Command = Path | list[str]


def _argv(command: Command) -> list[str]:
    return [str(command)] if isinstance(command, Path) else [str(part) for part in command]


def job_environment(home: Path) -> dict[str, str]:
    """Config and state the job must share with the builds (a custom state dir moves the locks and pool)."""
    from .build_coord import _default_state_dir
    return {
        "HERDR_ENGINEERING_CONFIG": os.environ.get(
            "HERDR_ENGINEERING_CONFIG", str(home / ".config/herdr-engineering/config.yaml")),
        "HERDR_ENGINEERING_STATE": str(_default_state_dir().parent.resolve()),
    }


def default_command() -> list[str]:
    """This installation's CLI: the venv's ``herdr-eng`` beside the interpreter, else ``-m`` on it."""
    sibling = Path(sys.executable).parent / "herdr-eng"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling)]
    return [sys.executable, "-m", "herdr_engineering.cli"]


def _existing_job_command(path: Path) -> list[str] | None:
    try:
        if path.suffix == ".plist":
            return [str(part) for part in plistlib.loads(path.read_bytes())["ProgramArguments"]]
        match = re.search(r"^ExecStart=(.*)$", path.read_text(), re.MULTILINE)
        return [part.strip('"') for part in re.findall(r'"[^"]*"|\S+', match.group(1))] if match else None
    except (OSError, ValueError, KeyError, plistlib.InvalidFileException):
        return None


def _job_works(path: Path) -> bool:
    command = _existing_job_command(path)
    return bool(command) and Path(command[0]).is_file() and os.access(command[0], os.X_OK)


def launchd_plist(herdr_eng: Command, interval_seconds: int, environment: dict[str, str] | None = None) -> bytes:
    """A periodic one-shot sweep (macOS has no /proc, so no server-liveness ``watch`` loop)."""
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [*_argv(herdr_eng), "build-cleanup", "run"],
        **({"EnvironmentVariables": environment} if environment else {}),
        "StartInterval": int(interval_seconds),
        "RunAtLoad": True,
        "Nice": 19,
        "LowPriorityIO": True,
        "ProcessType": "Background",
    })


_UNIT_TEMPLATE = """[Unit]
Description=HerdR Engineering Rust build-cache maintenance

[Service]
__ENVIRONMENT__
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
    xdg = os.environ.get("XDG_CONFIG_HOME") if home == Path.home() else None
    return (Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".config") / "systemd/user" / SYSTEMD_UNIT


def _has_systemd_user_bus() -> bool:
    try:
        return subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True,
                              timeout=15, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def reload_launchd_job(domain: str, label: str, plist: Path) -> None:
    """(Re)load a launchd job. ``bootout`` is asynchronous, so wait until the old job is gone and retry."""
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, timeout=30, check=False)
    for _ in range(20):  # up to ~10 s for the teardown
        if subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True,
                          timeout=30, check=False).returncode != 0:
            break
        time.sleep(0.5)
    for attempt in range(5):
        try:
            subprocess.run(["launchctl", "bootstrap", domain, str(plist)],
                           capture_output=True, timeout=30, check=True)
            return
        except subprocess.CalledProcessError:
            if attempt == 4:
                raise
            time.sleep(1)


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def install_cleanup_job(home: Path, herdr_eng: Command, *, platform: str,
                        interval_seconds: int = 900, activate: bool = True) -> dict[str, Any]:
    """Write (and optionally load) this platform's cleanup job; idempotent."""
    environment = job_environment(home)
    if platform == "darwin":
        path = _plist_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(launchd_plist(herdr_eng, interval_seconds, environment))
        if activate:
            reload_launchd_job(f"gui/{os.getuid()}", LAUNCHD_LABEL, path)
        return {"job": str(path), "kind": "launchd", "activated": activate}
    path = _unit_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    environment_lines = "\n".join(f"Environment={_systemd_quote(f'{key}={value}')}"
                                  for key, value in environment.items())
    path.write_text(_UNIT_TEMPLATE.replace("__ENVIRONMENT__", environment_lines).replace(
        "__HERDR_ENG__", " ".join(_systemd_quote(part) for part in _argv(herdr_eng))))
    if activate and not _has_systemd_user_bus():
        return {"job": str(path), "kind": "systemd", "activated": False,
                "skipped": "no systemd user bus; unit written, not started"}
    if activate:
        for argv in (["daemon-reload"], ["enable", SYSTEMD_UNIT], ["restart", SYSTEMD_UNIT]):
            subprocess.run(["systemctl", "--user", *argv], capture_output=True, timeout=60, check=True)
    return {"job": str(path), "kind": "systemd", "activated": activate}


def remove_cleanup_job(home: Path, *, platform: str, activate: bool = True) -> dict[str, Any]:
    """Opt-out: stop the job and delete its definition so nothing keeps sweeping."""
    path = _plist_path(home) if platform == "darwin" else _unit_path(home)
    if not path.exists():
        return {"skipped": "build_cleanup.enabled is false"}
    if activate:
        if platform == "darwin":
            argv = ["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"]
        else:
            argv = ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT]
        subprocess.run(argv, capture_output=True, timeout=60, check=False)
    path.unlink()
    return {"skipped": "build_cleanup.enabled is false", "removed": str(path)}


def resolve_real_cargo(home: Path, path_env: str | None = None) -> Path:
    """The cargo to wrap: a valid recorded one, else rustup's, and a distro cargo only as a last resort."""
    from . import cargo_host
    launcher = (home / ".local/bin/cargo").resolve()
    try:
        return cargo_host._real_cargo(home)
    except (OSError, ValueError, KeyError, TypeError):
        pass

    def usable(candidate: Path) -> bool:
        return candidate.is_file() and os.access(candidate, os.X_OK) and candidate.resolve() != launcher

    proxy = home / ".cargo/bin/cargo"
    if usable(proxy):  # rustup's proxy honours rust-toolchain.toml and +toolchain
        return proxy
    try:
        found = subprocess.run(["rustup", "which", "cargo"], capture_output=True, text=True,
                               timeout=15, check=False)
        if found.returncode == 0 and usable(Path(found.stdout.strip())):
            return Path(found.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    which = shutil.which("cargo", path=path_env)
    if which and usable(Path(which)):
        return Path(which)
    raise ValueError("No usable cargo found (install rustup, or add cargo to PATH)")


def install(home: Path, herdr_eng: Command | None = None, *, platform: str | None = None,
            activate: bool = True, config: dict[str, Any] | None = None,
            skip_cleanup_job: bool = False) -> dict[str, Any]:
    """Install the cleanup job and cargo launcher unless the config opts out. Never raises.

    ``skip_cleanup_job`` leaves the job to another installer (the controller's systemd script).
    """
    from .config import load_config
    platform = platform or ("darwin" if sys.platform == "darwin" else "linux")
    config = config if config is not None else load_config()
    herdr_eng = herdr_eng if herdr_eng is not None else default_command()
    result: dict[str, Any] = {}
    cleanup = config.get("build_cleanup") or {}
    if skip_cleanup_job:
        result["build_cleanup"] = {"skipped": "installed by the controller cleanup step"}
    elif cleanup.get("enabled", True):
        job = _plist_path(home) if platform == "darwin" else _unit_path(home)
        try:
            if len(_argv(herdr_eng)) > 1 and _job_works(job):
                # Never replace a working job with the `python -m` fallback.
                result["build_cleanup"] = {"job": str(job), "kept": "existing job has a working executable"}
            else:
                result["build_cleanup"] = install_cleanup_job(
                    home, herdr_eng, platform=platform,
                    interval_seconds=int(cleanup.get("interval_seconds", 900)), activate=activate)
        except (OSError, subprocess.SubprocessError) as error:
            result["build_cleanup"] = {"error": f"{type(error).__name__}: {error}"}
    else:
        try:
            result["build_cleanup"] = remove_cleanup_job(home, platform=platform, activate=activate)
        except (OSError, subprocess.SubprocessError) as error:
            result["build_cleanup"] = {"error": f"{type(error).__name__}: {error}"}
    cache = config.get("fabric_build_cache") or {}
    if cache.get("enabled", True) and (cache.get("local") or {}).get("manage_targets", True):
        from . import cargo_host
        try:
            result["cargo_host"] = cargo_host.install(home, resolve_real_cargo(home), Path(sys.executable))
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


def _launchd_loaded() -> bool | None:
    """Whether launchd really has the job loaded (None when it cannot be asked)."""
    try:
        return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
                              capture_output=True, timeout=30, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
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
        "build_cleanup_job_loaded": _launchd_loaded() if platform == "darwin" else None,
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
    parser.add_argument("--skip-cleanup-job", action="store_true",
                        help="the cleanup job is installed by another step (controller systemd script)")
    args = parser.parse_args(argv)
    home = Path.home()
    if args.action == "install":
        result = install(home, activate=not args.no_activate, skip_cleanup_job=args.skip_cleanup_job)
        print(json.dumps(result, indent=2))
        return 1 if any("error" in part for part in result.values()) else 0
    print(json.dumps(status(home, load_config(), _default_state_dir()), indent=2))
    return 0
