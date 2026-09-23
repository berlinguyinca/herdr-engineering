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
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .config import InvalidError, load_config
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


def _tailscale_up() -> bool:
    binpath = shutil.which("tailscale")
    if not binpath:
        return False
    import subprocess
    try:
        out = subprocess.run([binpath, "status"], capture_output=True,
                             text=True, timeout=10).stdout
        return "tailscale" in out.lower() and "Logged out" not in out
    except Exception:
        return False


def run_doctor(config: dict[str, Any] | None = None,
               herdr_adapter=None, herdr_bin: str = "herdr") -> DoctorReport:
    report = DoctorReport()
    report.platform = platform.platform()
    report.python = platform.python_version()

    # config / lock validity
    if config is None:
        try:
            config = load_config()
            report.add("config", "ok", "config loads and validates")
        except InvalidError as exc:
            report.add("config", "fail", f"invalid config: {exc}")
            config = {}
    else:
        report.add("config", "ok", "config provided")

    lock_path = os.environ.get("HERDR_ENGINEERING_LOCK",
                               str(__import__("pathlib").Path(
                                   __import__("os").getcwd()) / "lock/upstreams.yaml"))
    if os.path.exists(lock_path):
        report.add("lock", "ok", "upstreams lock file present")
    else:
        report.add("lock", "warn", "no lock/upstreams.yaml at repo root")

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
    adopted = _adopted_plugins(lock_path)
    report.add("plugins", "ok" if adopted else "warn",
               f"{len(adopted)} adopted plugin(s): {', '.join(adopted)}",
               {"adopted": adopted})

    # external providers
    ci_offline = not (config or {}).get("ci", {}).get("provider")
    report.add("ci_provider", "warn" if ci_offline else "ok",
               "no CI provider configured (offline-safe)" if ci_offline
               else "CI provider configured")
    return report


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
    from .config import load_config
    try:
        cfg = load_config()
    except InvalidError as exc:
        print(f"doctor: config invalid: {exc}", file=__import__("sys").stderr)
        cfg = {}
    report = run_doctor(config=cfg)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(report.render_human())
    return 0 if report.overall != "fail" else 1
