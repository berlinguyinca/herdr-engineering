"""Powerpack distribution and lifecycle (spec 0050).

Dependency lock, platform-aware install, safe config merge/backup, doctor
integration, plugin enable/disable policy, update preflight and rollback to a
prior known-good lock/config snapshot. The doctor is a real command
(``herdr-eng doctor``); powerpack commands orchestrate the lifecycle around it.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import InvalidError, load_config
from .doctor import run_doctor

_ENABLED_KEY = "powerpack_enabled_plugins"


@dataclass
class PreflightResult:
    ok: bool
    incompatible: list[str]
    doctor_overall: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "incompatible": self.incompatible,
                "doctor_overall": self.doctor_overall}


class PowerpackManager:
    """Manages enabled-plugin policy, snapshots and rollback."""

    def __init__(self, state_dir: Path | None = None,
                 lock_path: Path | None = None) -> None:
        self.state_dir = state_dir or Path(
            os.environ.get("HERDR_ENGINEERING_STATE",
                           str(Path.home() / ".local/share/herdr-engineering/state")))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = lock_path or Path(
            os.environ.get("HERDR_ENGINEERING_LOCK",
                           str(Path.cwd() / "lock/upstreams.yaml")))
        self._policy_path = self.state_dir / "plugin_policy.json"

    def _load_policy(self) -> dict[str, Any]:
        if self._policy_path.exists():
            try:
                return json.loads(self._policy_path.read_text())
            except Exception:
                pass
        return {}

    def _save_policy(self, policy: dict[str, Any]) -> None:
        tmp = self._policy_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(policy, indent=2, sort_keys=True))
        os.replace(tmp, self._policy_path)

    def enabled_plugins(self) -> list[str]:
        return sorted(self._load_policy().get(_ENABLED_KEY, []))

    def enable(self, plugin: str, *, reason: str = "") -> bool:
        if not plugin:
            raise InvalidError("plugin name required")
        policy = self._load_policy()
        enabled = set(policy.get(_ENABLED_KEY, []))
        enabled.add(plugin)
        policy[_ENABLED_KEY] = sorted(enabled)
        policy.setdefault("audit", []).append(
            {"action": "enable", "plugin": plugin, "reason": reason,
             "at": time.time()})
        self._save_policy(policy)
        return True

    def disable(self, plugin: str, *, reason: str = "") -> bool:
        policy = self._load_policy()
        enabled = set(policy.get(_ENABLED_KEY, []))
        if plugin not in enabled:
            return False
        enabled.discard(plugin)
        policy[_ENABLED_KEY] = sorted(enabled)
        policy.setdefault("audit", []).append(
            {"action": "disable", "plugin": plugin, "reason": reason,
             "at": time.time()})
        self._save_policy(policy)
        return True

    # -- snapshots / rollback ---------------------------------------------
    def snapshot(self, tag: str) -> Path:
        """Snapshot current lock + config into state_dir for rollback."""
        snap = self.state_dir / f"snapshot-{tag}-{int(time.time())}"
        snap.mkdir(parents=True, exist_ok=True)
        if self.lock_path.exists():
            shutil.copy(self.lock_path, snap / "upstreams.lock")
        cfg_path = _find_config()
        if cfg_path and Path(cfg_path).exists():
            shutil.copy(cfg_path, snap / "herdr-engineering.yaml")
        # the plugin policy is what enable/disable mutate; a rollback that
        # skipped it would leave the "upgrade" half of up/down in place.
        # Capture the policy even when empty so "nothing enabled" is
        # restorable too.
        if self._policy_path.exists():
            shutil.copy(self._policy_path, snap / "plugin_policy.json")
        else:
            (snap / "plugin_policy.json").write_text("{}")
        marker = {"tag": tag, "created_at": time.time()}
        (snap / "snapshot.json").write_text(json.dumps(marker, indent=2))
        return snap

    def update_preflight(self) -> PreflightResult:
        """Run doctor + detect incompatible locked revisions (blocking)."""
        try:
            cfg = load_config()
        except InvalidError as exc:
            return PreflightResult(ok=False, incompatible=[f"config invalid: {exc}"])
        report = run_doctor(config=cfg)
        incompatible: list[str] = []
        deps = _lock_dependencies(self.lock_path)
        for name, spec in deps.items():
            if spec.get("decision") == "REJECT":
                incompatible.append(f"{name}:REJECT")
        ok = report.overall != "fail" and not incompatible
        return PreflightResult(ok=ok, incompatible=incompatible,
                               doctor_overall=report.overall)

    def rollback(self, snapshot: Path | str | None = None) -> Path:
        """Restore the most recent (or given) known-good snapshot.

        ``snapshot`` may be a full path, an expanded ``~`` path, or a tag
        (the most recent ``snapshot-<tag>-*`` is used).
        """
        if snapshot is None:
            snaps = sorted(self.state_dir.glob("snapshot-*"))
            if not snaps:
                raise InvalidError("no snapshot to roll back to")
            snapshot = snaps[-1]
        else:
            candidate = Path(os.path.expanduser(str(snapshot)))
            if candidate.is_dir():
                snapshot = candidate
            else:
                # treat as a tag: most recent snapshot-<tag>-*
                tagged = sorted(self.state_dir.glob(f"snapshot-{snapshot}-*"))
                if not tagged:
                    raise InvalidError(f"no snapshot found for {snapshot!r}")
                snapshot = tagged[-1]
        if (snapshot / "upstreams.lock").exists():
            shutil.copy(snapshot / "upstreams.lock", self.lock_path)
        cfg_path = _find_config()
        if (snapshot / "herdr-engineering.yaml").exists():
            if cfg_path:
                Path(cfg_path).write_bytes(
                    (snapshot / "herdr-engineering.yaml").read_bytes())
        if (snapshot / "plugin_policy.json").exists():
            shutil.copy(snapshot / "plugin_policy.json", self._policy_path)
        return snapshot

    def status(self) -> dict[str, Any]:
        deps = _lock_dependencies(self.lock_path)
        return {
            "enabled_plugins": self.enabled_plugins(),
            "lock_path": str(self.lock_path),
            "lock_exists": self.lock_path.exists(),
            "locked_dependencies": deps,
            "snapshots": sorted(p.name for p in self.state_dir.glob("snapshot-*")),
        }


def _find_config() -> str | None:
    for candidate in ("config/herdr-engineering.yaml",
                      "config/herdr-engineering.example.yaml",
                      "herdr-engineering.yaml"):
        if Path(candidate).exists():
            return candidate
    return None


def _lock_dependencies(lock_path: Path) -> dict[str, dict[str, Any]]:
    if not lock_path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(lock_path.read_text()) or {}
        deps = data.get("dependencies", {})
        return deps if isinstance(deps, dict) else {}
    except Exception:
        return {}
