"""`herdr-eng settings status|enable`: turn optional services on in the central fleet-config.yaml.

Configuration is YAML only. Editing is line based so comments and ordering survive (PyYAML would drop them); the result
is re-parsed and compared with the original, so a key outside the requested ones can never change. Output is service
names and on/off only: no other configuration value is read out.
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import yaml

from .settings_catalog import BY_CAPABILITY, SERVICES, Service, safe_services

DEFAULT_PATH = "~/.config/herdr-engineering/fleet/fleet-config.yaml"


class SettingsError(Exception):
    pass


def _load(text: str) -> dict:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SettingsError(f"fleet config is not valid YAML ({type(exc).__name__})") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SettingsError("fleet config must be a YAML mapping")
    return data


def _get(data: dict, key: str) -> object:
    node: object = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def service_state(data: dict, svc: Service) -> str:
    if svc.always_on:
        return "always-on"
    if svc.needs_values:
        return "needs-values"
    return "on" if _get(data, svc.config_key) is True else "off"


# ---- line editing -----------------------------------------------------------------------------------------------

_CODE = re.compile(r"^(\s*)\S")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _key_re(key: str) -> re.Pattern[str]:
    return re.compile(r"^(?P<ind> *)(?P<q>[\"']?)" + re.escape(key) + r"(?P=q) *:(?P<rest>(?: .*)?)$")


def _block_end(lines: list[str], i: int) -> int:
    """Index after the last content line belonging to the mapping entry at line i."""
    end = i + 1
    for j in range(i + 1, len(lines)):
        if _is_content(lines[j]):
            if _indent(lines[j]) <= _indent(lines[i]):
                break
            end = j + 1
    return end


def _inline_value(rest: str) -> str:
    return re.sub(r"\s+#.*$", "", rest).strip().lstrip(":").strip()


def _set_true(text: str, key: str) -> str:
    lines = text.split("\n")
    parts = key.split(".")
    lo, hi, parent_indent = 0, len(lines), -2
    for depth, part in enumerate(parts):
        found = None
        child_indent = None
        for j in range(lo, hi):
            if not _is_content(lines[j]):
                continue
            if child_indent is None:
                child_indent = _indent(lines[j])
                if lines[j].lstrip().startswith("- "):
                    raise SettingsError(f"cannot edit {key}: {'.'.join(parts[:depth]) or 'top level'} is a list")
            if _indent(lines[j]) == child_indent:
                m = _key_re(part).match(lines[j])
                if m:
                    found = (j, m)
                    break
        last = depth == len(parts) - 1
        if found is None:
            return _insert(lines, lo, hi, child_indent, parent_indent, parts[depth:])
        j, m = found
        value = _inline_value(m.group("rest"))
        if last:
            if value == "":
                if _block_end(lines, j) > j + 1:
                    raise SettingsError(f"cannot enable {key}: it is a mapping, set it by hand")
                lines[j] = lines[j].rstrip() + " true"
            else:
                comment = re.search(r"\s+#.*$", m.group("rest"))
                lines[j] = f"{m.group('ind')}{m.group('q')}{part}{m.group('q')}: true" + (comment.group(0) if comment else "")
            return "\n".join(lines)
        if value not in ("",):
            raise SettingsError(f"cannot edit {key}: {'.'.join(parts[:depth + 1])} is not a block mapping, set it by hand")
        lo, hi, parent_indent = j + 1, _block_end(lines, j), _indent(lines[j])
    return "\n".join(lines)


def _insert(lines: list[str], lo: int, hi: int, child_indent: int | None, parent_indent: int, missing: list[str]) -> str:
    indent = child_indent if child_indent is not None else parent_indent + 2
    at = lo
    for j in range(lo, hi):
        if _is_content(lines[j]):
            at = j + 1
    new = [" " * (indent + 2 * k) + f"{name}:" for k, name in enumerate(missing)]
    new[-1] += " true"
    if parent_indent == -2 and at == len(lines) and lines and lines[-1] == "":
        at = len(lines) - 1  # keep the trailing newline last
    lines[at:at] = new
    return "\n".join(lines)


def apply_enables(text: str, services: list[Service]) -> str:
    original = _load(text)
    out = text
    for svc in services:
        out = _set_true(out, svc.config_key)
    result = _load(out)
    expected = yaml.safe_load(yaml.safe_dump(original))
    for svc in services:
        node = expected
        parts = svc.config_key.split(".")
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = True
    if result != expected:
        raise SettingsError("refusing to write: the edit would change keys other than the requested ones; set them by hand")
    return out


def _backup_and_write(path: Path, new_text: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    n = 0
    while True:  # a second write within the same second gets a numbered suffix, never overwrites a backup
        backup = path.with_name(f"{path.name}.bak.{stamp}" + (f"-{n}" if n else ""))
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
    with os.fdopen(fd, "w") as fh:
        fh.write(path.read_text())
    mode = path.stat().st_mode & 0o777
    tmp = tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False)
    try:
        with tmp:
            tmp.write(new_text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp.name, mode)
        os.replace(tmp.name, path)
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
    return backup


# ---- commands ---------------------------------------------------------------------------------------------------

def _read(path: Path) -> str:
    if not path.is_file():
        raise SettingsError(f"fleet config not found: {path}")
    return path.read_text()


def cmd_status(args: argparse.Namespace) -> int:
    try:
        data = _load(_read(Path(args.fleet_config).expanduser()))
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for svc in SERVICES:
        print(f"{service_state(data, svc):<13} {svc.capability:<28} {svc.label}"
              + (f"  [{svc.config_key}]" if svc.config_key else ""))
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    path = Path(args.fleet_config).expanduser()
    try:
        if args.all_safe == bool(args.capability):
            raise SettingsError("name one capability, or pass --all-safe")
        if args.all_safe:
            chosen = list(safe_services())
        else:
            svc = BY_CAPABILITY.get(args.capability)
            if svc is None:
                raise SettingsError(f"unknown capability {args.capability!r}; run `herdr-eng settings status`")
            if svc.always_on:
                raise SettingsError(f"{svc.capability} is always on")
            if svc.needs_values:
                raise SettingsError(f"{svc.capability} needs values, not a flag: set {svc.config_key} by hand in the fleet config")
            chosen = [svc]
        text = _read(path)
        data = _load(text)
        todo = [s for s in chosen if _get(data, s.config_key) is not True]
        if not todo:
            print("nothing to change: already enabled")
            return 0
        new_text = apply_enables(text, todo)
        diff = [d for d in difflib.unified_diff(text.split("\n"), new_text.split("\n"), "current", "proposed", lineterm="", n=0)
                if d[:1] in "+-" and not d.startswith(("+++", "---"))]
        print("\n".join(diff))
        if not args.write:
            print("dry run: nothing written; add --write to apply")
            return 0
        backup = _backup_and_write(path, new_text)
        print(f"wrote {path} (backup {backup.name}); enabled: {', '.join(s.capability for s in todo)}")
        return 0
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def register(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser("settings", help="show or enable optional services in the central fleet config (YAML only)")
    ssub = p.add_subparsers(dest="settings_command", required=True)
    sp = ssub.add_parser("status", help="on/off/needs-values per optional service (names only)")
    sp.add_argument("--fleet-config", default=DEFAULT_PATH, metavar="PATH")
    sp.set_defaults(func=cmd_status)
    ep = ssub.add_parser("enable", help="set a service's boolean flag to true; dry run unless --write")
    ep.add_argument("capability", nargs="?", help="capability name from `settings status`")
    ep.add_argument("--all-safe", action="store_true", help="enable every service that only needs a boolean flag")
    ep.add_argument("--fleet-config", default=DEFAULT_PATH, metavar="PATH")
    ep.add_argument("--write", action="store_true", help="back up to PATH.bak.<timestamp>, then write atomically")
    ep.set_defaults(func=cmd_enable)
