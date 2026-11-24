"""Periodic `herdr-handoff reap --apply`: close the panes that confirmed handoffs replaced.

`confirm` refuses to close a source pane that is still working (herdr-handoff 0.6.0, `source_working`), and a
takeover that gave up waiting leaves the pane open for good unless something comes back for it. `reap` is that
something. It goes through the same live pane checks as a confirm-side close, so it never forces anything.

Policy is YAML only (``handoff_reap`` in ``~/.config/herdr-engineering/config.yaml``) and defaults to OFF:

    handoff_reap:
      enabled: false          # the timer is only started on a host whose YAML says true
      interval_minutes: 15    # must divide 60: the timer is calendar based (Persistent= needs OnCalendar=)
      legacy_roots: []        # per-project <project>/.herdr/handoffs directories to import first

Which records it covers: the shared store, the one `herdr-handoff` uses with no ``--root`` (``~/.local/share/
herdr-handoff/store``). A per-project root written by an older herdr-handoff is copied into that store with
``store import-legacy <root>`` (copy only, idempotent, the legacy directory is never changed), so listing it
under ``legacy_roots`` is all a project needs. HERDR_HANDOFF_FILE and HERDR_HANDOFF_DIR, which a pane
inherits from the handoff that started it and which would redirect the CLI to one project's root, are
removed from the child environment.

The `--include-unrecorded` flag of `reap` is deliberately never used: a record from before close options were
stored cannot detect pane-id reuse and its keep-source choice is unknown, so closing one is a decision for a
person who has read the dry run.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cleanup_command import user_config_path

DEFAULT_INTERVAL_MINUTES = 15
# OnCalendar=*:0/N fires at minutes 0, N, 2N ... so N must divide the hour or the gap before :00 is short.
VALID_INTERVALS = frozenset({1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60})
_KEYS = frozenset({"enabled", "interval_minutes", "legacy_roots"})
# Variables that point herdr-handoff at one handoff's root instead of the shared store.
_ROOT_OVERRIDES = ("HERDR_HANDOFF_FILE", "HERDR_HANDOFF_DIR")
_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    legacy_roots: tuple[Path, ...] = ()
    config_error: str = ""


def _parse(section: Any, home: Path) -> Policy:
    if section is None:
        return Policy()
    if not isinstance(section, dict):
        raise ValueError("handoff_reap must be a mapping")
    if unknown := sorted(str(k) for k in section.keys() - _KEYS):
        raise ValueError(f"unknown key(s) in handoff_reap: {', '.join(unknown)}")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"handoff_reap.enabled must be true or false, got {enabled!r}")
    interval = section.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)
    if isinstance(interval, bool) or interval not in VALID_INTERVALS:
        raise ValueError(f"handoff_reap.interval_minutes must be one of {sorted(VALID_INTERVALS)}, got {interval!r}")
    roots = section.get("legacy_roots", [])
    if not isinstance(roots, list) or not all(isinstance(r, str) and r.strip() for r in roots):
        raise ValueError("handoff_reap.legacy_roots must be a list of directory paths")
    expanded = tuple(Path(r.strip().replace("~", str(home), 1) if r.strip().startswith("~") else r.strip())
                     for r in roots)
    return Policy(enabled=enabled, interval_minutes=int(interval), legacy_roots=expanded)


def load_policy(home: Path | None = None) -> Policy:
    """The user-level config only (a checkout's own config is untrusted). Fails CLOSED: a malformed section
    or an unreadable file yields a disabled policy carrying the reason, never an enabled one."""
    home = home or Path.home()
    path = user_config_path(home)
    try:
        section: Any = None
        if path.is_file():
            from .config import load_config
            section = load_config([path]).get("handoff_reap")
        return _parse(section, home)
    except Exception as exc:  # noqa: BLE001 - any failure to read the operator's opt-in
        return Policy(enabled=False, config_error=f"{path}: {exc}")


def reap_argv(handoff_bin: str) -> list[str]:
    """The one command the timer runs. No --root (the shared store) and never the unrecorded flag."""
    return [handoff_bin, "reap", "--apply"]


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text.strip()


def _child_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _ROOT_OVERRIDES}
    env["HOME"] = str(home)
    return env


def run(policy: Policy, *, home: Path | None = None, handoff_bin: str | None = None) -> dict[str, Any]:
    if policy.config_error:
        return {"status": "config_unreadable", "reason": policy.config_error}
    if not policy.enabled:
        return {"status": "disabled"}
    home = home or Path.home()
    binary = handoff_bin or shutil.which("herdr-handoff", path=os.pathsep.join(
        [str(home / ".local" / "bin"), os.environ.get("PATH", "")])) or "herdr-handoff"
    env = _child_env(home)
    report: dict[str, Any] = {"status": "ok", "imports": []}
    try:
        for legacy in policy.legacy_roots:
            done = subprocess.run([binary, "store", "import-legacy", str(legacy)], capture_output=True, text=True,
                                  env=env, cwd=home, timeout=_TIMEOUT_SECONDS, check=False)
            report["imports"].append({"root": str(legacy), "returncode": done.returncode,
                                      "result": _json(done.stdout) if done.returncode == 0 else done.stderr.strip()})
        done = subprocess.run(reap_argv(binary), capture_output=True, text=True, env=env, cwd=home,
                              timeout=_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "reason": f"herdr-handoff could not run: {exc}"}
    if done.returncode != 0:
        return {**report, "status": "error", "reason": f"herdr-handoff reap exited {done.returncode}: "
                                                       f"{done.stderr.strip()[:500]}"}
    report["reap"] = _json(done.stdout)
    return report


def register(sub: Any) -> None:
    p = sub.add_parser("handoff-reap", help="close panes replaced by confirmed handoffs (opt-in, periodic)")
    actions = p.add_subparsers(dest="handoff_reap_command", required=True)
    for name in ("run", "status"):
        actions.add_parser(name).set_defaults(func=main_args)


def main_args(args: argparse.Namespace) -> int:
    policy = load_policy()
    if args.handoff_reap_command == "status":
        report: dict[str, Any] = {"policy": {"enabled": policy.enabled, "interval_minutes": policy.interval_minutes,
                                             "legacy_roots": [str(r) for r in policy.legacy_roots]}}
        if policy.config_error:
            report["config_error"] = policy.config_error
    else:
        report = run(policy)
    print(json.dumps(report, default=str))
    return 1 if report.get("status") in ("error", "config_unreadable") else 0
