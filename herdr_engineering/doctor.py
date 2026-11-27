"""``herdr-eng doctor`` — machine-readable capability and health checks.

Spec 0050 / 0170: doctor must have concise human output and stable JSON.
Checks cover versions/capabilities, private/public listeners, Tailscale
reachability, config/lock validity, shared workspace, dev fabric, adopted
plugins and relevant external providers. No secrets are ever printed.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .config import InvalidError, _discover_config_paths, load_config
from .observability import detect_listeners


@dataclass
class DoctorCheck:
    name: str
    status: str          # ok | warn | fail | unknown
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "data": self.data}


@dataclass
class DoctorReport:
    version: str = __version__
    platform: str = ""
    python: str = ""
    checks: list[DoctorCheck] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "",
            data: dict[str, Any] | None = None) -> None:
        self.checks.append(DoctorCheck(name, status, detail, data or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "platform": self.platform,
            "python": self.python,
            "overall": self.overall,
            "checks": [c.to_dict() for c in self.checks],
        }

    @property
    def overall(self) -> str:
        statuses = {c.status for c in self.checks}
        if "fail" in statuses:
            return "fail"
        if "warn" in statuses:
            return "warn"
        return "ok"

    def render_human(self) -> str:
        lines = [f"HerdR Engineering doctor v{self.version}",
                 f"platform: {self.platform}  python: {self.python}",
                 f"overall: {self.overall}"]
        for c in self.checks:
            lines.append(f"  [{c.status:>6}] {c.name} — {c.detail}")
        return "\n".join(lines)


def tailscale_running(returncode: int, stdout: str) -> bool:
    """Decide from `tailscale status --json` output whether the backend is running.

    The JSON `BackendState` is authoritative (`Running` vs `Stopped`, `NeedsLogin`, ...). When the output
    is not the status object, the command's exit code decides.
    """
    try:
        state = json.loads(stdout).get("BackendState")
    except (ValueError, AttributeError):
        return returncode == 0
    if not isinstance(state, str):
        return returncode == 0
    return state == "Running"


def _tailscale_up() -> bool:
    binpath = shutil.which("tailscale")
    if not binpath:
        return False
    import subprocess
    try:
        proc = subprocess.run([binpath, "status", "--json"], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return tailscale_running(proc.returncode, proc.stdout)


#: The directory holding the `herdr_engineering` package; `lock/` sits beside it only in a checkout
#: (or editable install), never in an installed copy.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def resolve_lock_path(env: Mapping[str, str], package_root: Path) -> Path | None:
    """Where `lock/upstreams.yaml` lives, independent of the working directory.

    Order: HERDR_ENGINEERING_LOCK, then `<HERDR_ENGINEERING_REPO>/lock/upstreams.yaml`, then the checkout
    the running package was imported from. None when there is no checkout to look at (an installed copy).
    """
    explicit = env.get("HERDR_ENGINEERING_LOCK")
    if explicit:
        return Path(explicit)
    repo = env.get("HERDR_ENGINEERING_REPO")
    if repo:
        return Path(repo) / "lock" / "upstreams.yaml"
    candidate = package_root / "lock" / "upstreams.yaml"
    return candidate if candidate.exists() else None


def run_doctor(config: dict[str, Any] | None = None,
               herdr_adapter=None, herdr_bin: str = "herdr", strict: bool = False,
               package_root: Path | None = None) -> DoctorReport:
    report = DoctorReport()
    report.platform = platform.platform()
    report.python = platform.python_version()

    # config / lock validity
    discovered = _discover_config_paths()
    if config is None:
        try:
            config = load_config()
            if discovered:
                report.add("config", "ok",
                           f"config loads ({discovered[-1]})",
                           {"files": [str(p) for p in discovered]})
            else:
                report.add("config", "ok",
                           "config loads (built-in defaults, no config file found)")
        except InvalidError as exc:
            report.add("config", "fail", f"invalid config: {exc}")
            config = {}
    else:
        report.add("config", "ok", "config provided")

    lock_path = resolve_lock_path(os.environ, package_root or _PACKAGE_ROOT)
    if lock_path is None:
        report.add("lock", "unknown", "not applicable: no checkout found (installed copy; "
                   "set HERDR_ENGINEERING_REPO to check a checkout's lock)")
    elif lock_path.exists():
        report.add("lock", "ok", "upstreams lock file present")
    else:
        report.add("lock", "warn", f"no upstreams lock at {lock_path}")

    # herdr binary / capabilities
    if shutil.which(herdr_bin):
        try:
            if herdr_adapter is None:
                from .herdr_adapter import NativeHerdrAdapter
                herdr_adapter = NativeHerdrAdapter(herdr_bin)
            caps = herdr_adapter.capabilities(refresh=True)
            report.add("herdr", "ok",
                       f"herdr {caps.version} capabilities: "
                       f"machines={caps.machines} snapshot={caps.snapshot} "
                       f"plugins={caps.plugins} worktrees={caps.worktrees}",
                       {"version": caps.version})
        except Exception as exc:
            report.add("herdr", "warn", f"herdr probe failed: {exc}")
    else:
        report.add("herdr", "warn", f"{herdr_bin} binary not on PATH")

    # private/public listeners
    findings = detect_listeners()
    public = [f for f in findings if f.public]
    if public:
        report.add("listeners", "warn",
                   f"{len(public)} public listener(s) detected",
                   {"public": [f.to_dict() for f in public][:10]})
    else:
        report.add("listeners", "ok", "no public listeners detected")

    # tailscale reachability
    if _tailscale_up():
        report.add("tailscale", "ok", "tailscale up")
    else:
        report.add("tailscale", "warn",
                   "tailscale not up or not reachable (validation fleet unreachable)")

    # shared workspace availability
    from .artifacts import FilesystemArtifactWorkspace
    ws = FilesystemArtifactWorkspace()
    if ws.is_available():
        report.add("workspace", "ok", f"filesystem artifact workspace ready "
                   f"({ws.root})")
    else:
        report.add("workspace", "fail", "shared artifact workspace unavailable")

    # dev fabric
    dev_enabled = (config or {}).get("dev_fabric", {}).get("enabled", True)
    report.add("dev_fabric", "ok" if dev_enabled else "warn",
               "enabled" if dev_enabled else "disabled by config")

    # adopted plugins (from lock)
    if lock_path is None:
        report.add("plugins", "unknown", "not applicable: no checkout found, so no lock to read")
    else:
        adopted = _adopted_plugins(str(lock_path))
        report.add("plugins", "ok" if adopted else "warn",
                   f"{len(adopted)} adopted plugin(s): {', '.join(adopted)}",
                   {"adopted": adopted})

    from .handoff_dependency import check_handoff_dependency
    handoff = check_handoff_dependency()
    report.add("handoff", handoff.status if strict else ("ok" if handoff.status == "ok" else "warn"),
               handoff.detail, {"missing_harnesses": handoff.missing_harnesses,
                                "executable": handoff.executable})

    # automatic handoff triggers: is the next full-context session going to hand itself off?
    try:
        from .handoff_autopilot import doctor_check
        autopilot = doctor_check()
        detail = autopilot["detail"] if not autopilot["fix"] else f"{autopilot['detail']}; fix: {autopilot['fix']}"
        report.add(autopilot["name"], autopilot["status"], detail, autopilot["data"])
    except Exception as exc:  # doctor must never crash on an optional section
        report.add("handoff-autopilot", "warn", f"autopilot check failed: {type(exc).__name__}")

    # the automatic-handoff controller: without a linked plugin or a running controller no session is handed off at all
    from .handoff_controller_state import controller_findings
    controller, side_pane = controller_findings(os.environ, herdr_bin=herdr_bin)
    for name, finding in (("handoff_controller", controller), ("handoff_side_pane", side_pane)):
        ok = finding.state.value in ("running", "switched_off", "linked")
        report.add(name, "ok" if ok else ("fail" if strict and finding.blocking else "warn"),
                   finding.detail + (f"; fix: {finding.fix}" if finding.fix else ""), {"state": finding.state.value})

    # herdr-memory managed adapters (only when opted in; never fake healthy)
    try:
        from .memory_install import MemoryInstaller
        mem = MemoryInstaller()
        if mem.manifest_path.is_file():
            md = mem.doctor()
            report.add("memory", "ok" if md["overall"] == "ok" else
                       ("fail" if md["overall"] == "fail" else "warn"),
                       f"herdr-memory adapters: {md['overall']}",
                       {"checks": [c for c in md["checks"] if c["status"] not in ("ok", "skip")][:10]})
    except Exception as exc:  # doctor must never crash on an optional section
        report.add("memory", "warn", f"memory doctor failed: {type(exc).__name__}")

    # Hindsight shared memory (only where this host runs or uses it; unreachable is "unknown", never ok)
    try:
        from .memory_hindsight_status import check_memory_backend
        for chk in check_memory_backend():
            report.add(chk["name"], chk["status"], chk["detail"], chk["data"])
    except Exception as exc:  # doctor must never crash on an optional section
        report.add("memory_hindsight", "warn", f"memory backend checks failed: {type(exc).__name__}")

    # control-plane database backup age (a "not applicable" result on a host that does not run the control plane)
    try:
        from .control_plane_backup_status import check_control_plane_backup
        for chk in check_control_plane_backup():
            report.add(chk["name"], chk["status"], chk["detail"], chk["data"])
    except Exception as exc:  # doctor must never crash on an optional section
        report.add("control_plane_backup", "warn", f"control-plane backup check failed: {type(exc).__name__}")

    _add_build_cache_toolchain(report)
    _add_build_hygiene(report, config or {})

    # external providers
    ci_offline = not (config or {}).get("ci", {}).get("provider")
    report.add("ci_provider", "warn" if ci_offline else "ok",
               "no CI provider configured (offline-safe)" if ci_offline
               else "CI provider configured")
    return report


_SCCACHE_FIX = "scripts/install.sh --build-cache-tools-only"


def _add_build_cache_toolchain(report: DoctorReport) -> None:
    """Read-only build-cache toolchain check: sccache >= 0.19.0, rustc/cargo, python deps."""
    import importlib.util

    from .build_cache_env import detect_tools
    from .build_cache_storage import _VERSION_RE, MIN_SCCACHE_VERSION

    try:
        tools = detect_tools(os.environ.get("PATH", ""))
    except Exception as exc:  # doctor must never crash on an optional section
        report.add("build_cache_toolchain", "warn", f"toolchain detection failed: {type(exc).__name__}")
        return
    want = ".".join(map(str, MIN_SCCACHE_VERSION))
    m = _VERSION_RE.search(tools.sccache_version or "")
    have = tuple(int(x) for x in m.groups()) if m else None
    if tools.sccache is None:
        report.add("build_cache_sccache", "warn",
                   f"sccache not installed (need >= {want}); fix: {_SCCACHE_FIX}",
                   {"fix": _SCCACHE_FIX})
    elif have is None or have < MIN_SCCACHE_VERSION:
        report.add("build_cache_sccache", "warn",
                   f"sccache {tools.sccache_version or 'version unknown'} is older than {want}; "
                   f"fix: {_SCCACHE_FIX}", {"fix": _SCCACHE_FIX})
    else:
        report.add("build_cache_sccache", "ok", f"{tools.sccache_version} at {tools.sccache}")
    missing = [n for n, p in (("rustc", tools.rustc), ("cargo", tools.cargo)) if p is None]
    report.add("build_cache_rust", "warn" if missing else "ok",
               f"missing on PATH: {', '.join(missing)}" if missing else "rustc and cargo present")
    absent = [m for m in ("yaml",) if importlib.util.find_spec(m) is None]
    report.add("build_cache_python_deps", "warn" if absent else "ok",
               f"missing python modules: {', '.join(absent)}; fix: pip install -e ." if absent
               else "python deps import (PyYAML)")


def _add_build_hygiene(report: DoctorReport, config: dict[str, Any]) -> None:
    """Disk pressure plus whether the cleanup job and cargo wrapper are installed/enabled."""
    from pathlib import Path

    from .build_coord import _default_state_dir
    from .build_hygiene import status
    try:
        merged = config if config.get("build_cleanup") else load_config()
        info = status(Path.home(), merged, _default_state_dir())
    except Exception as exc:  # doctor must never crash on an optional section
        report.add("build_hygiene", "warn", f"disk hygiene check failed: {type(exc).__name__}")
        return
    usage = info["usage_percent"]
    level = "fail" if usage >= 97 else "warn" if usage >= info["pressure_percent"] else "ok"
    report.add("disk_pressure", level,
               f"disk {usage}% used (pressure {info['pressure_percent']:g}%, builds refused at "
               f"{info['refuse_builds_percent']:g}%)", info)
    if not info["build_cleanup_enabled"]:
        report.add("build_cleanup", "warn", "build cleanup disabled by config (build_cleanup.enabled: false)")
    elif not info["build_cleanup_job_installed"]:
        report.add("build_cleanup", "warn",
                   "build cleanup enabled but its timer is not installed; fix: herdr-eng build-hygiene install")
    elif info.get("build_cleanup_job_loaded") is False:
        report.add("build_cleanup", "warn",
                   "build cleanup job is written but not loaded in launchd; fix: herdr-eng build-hygiene install")
    else:
        report.add("build_cleanup", "ok",
                   f"build cleanup job installed; last run {info['build_cleanup_last_run'] or 'never'}")
    if not info["cargo_host_enabled"]:
        report.add("cargo_host", "warn", "cargo-host / managed targets disabled by config")
    elif not info["cargo_host_installed"]:
        report.add("cargo_host", "warn",
                   "cargo-host enabled but the managed cargo launcher is not installed; "
                   "fix: herdr-eng build-hygiene install")
    else:
        report.add("cargo_host", "ok", "managed cargo launcher installed (one managed target per worktree, incremental off)")


def _adopted_plugins(lock_path: str) -> list[str]:
    adopted: list[str] = []
    try:
        if os.path.exists(lock_path):
            import yaml
            data = yaml.safe_load(open(lock_path)) or {}
            deps = data.get("dependencies", {})
            for name, spec in deps.items():
                if isinstance(spec, dict) and spec.get("decision") in (
                        "ADOPT", "ADAPT"):
                    adopted.append(name)
    except Exception:
        pass
    return adopted


def doctor_main(args) -> int:
    # Pass config=None so run_doctor performs default discovery and reports
    # which config file (if any) is actually in effect.
    report = run_doctor(strict=getattr(args, "strict", False))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(report.render_human())
    return 0 if report.overall != "fail" else 1
