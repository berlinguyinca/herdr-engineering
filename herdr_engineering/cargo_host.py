"""Harness-neutral Cargo launcher and compiler-cache adapter."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from .errors import InvalidError

MARKER = "herdr-managed-cargo-host-v1"
COORDINATED = "HERDR_CARGO_HOST_COORDINATED"


def cargo_subcommand(arguments: list[str]) -> str | None:
    """Identify built-ins after Cargo global options without rewriting argv."""
    values = {"--color", "--config", "-Z", "--manifest-path"}
    aliases = {"b": "build", "c": "check", "t": "test", "r": "run"}
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token in {"--help", "-h", "--version", "-V"} or token.startswith("-C"):
            return None
        if token in values:
            index += 2
        elif token.startswith(("-", "+")):
            index += 1
        else:
            return aliases.get(token, token)
    return None


def host_root(home: Path) -> Path:
    return home / ".local/share/herdr-engineering/cargo-host"


def is_managed_wrapper(value: str, home: Path) -> bool:
    path = Path(value).expanduser()
    expected = host_root(home) / "rustc-cache"
    try:
        return (path == expected and not path.is_symlink()
                and f"# {MARKER}" in path.read_text()[:256])
    except OSError:
        return False


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing to replace symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".herdr-tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _backup(path: Path) -> None:
    backup = path.with_name(path.name + ".before-herdr-cargo-host")
    if path.exists() and not backup.exists():
        _atomic_write(backup, path.read_text())


def cargo_configuration(text: str, wrapper: Path, home: Path) -> str:
    if text and not text.endswith("\n"):
        text += "\n"
    parsed = tomllib.loads(text)
    previous = parsed.get("build", {}).get("rustc-wrapper")
    if previous and not is_managed_wrapper(str(previous), home):
        raise ValueError("Existing Cargo compiler wrapper belongs to the operator; leaving it unchanged")
    line = f"rustc-wrapper = {json.dumps(str(wrapper))}\n"
    section = re.search(r"(?m)^\[build\][^\n]*\n?", text)
    if section:
        end = re.search(r"(?m)^\[", text[section.end():])
        stop = section.end() + end.start() if end else len(text)
        body = text[section.end():stop]
        if re.search(r"(?m)^\s*rustc-wrapper\s*=", body):
            body = re.sub(r"(?m)^\s*rustc-wrapper\s*=[^\n]*\n?", line, body)
        else:
            body = line + body
        if re.search(r"(?m)^\s*incremental\s*=", body):
            body = re.sub(r"(?m)^\s*incremental\s*=[^\n]*\n?", "incremental = false\n", body)
        else:
            body = "incremental = false\n" + body
        updated = text[:section.end()] + body + text[stop:]
    else:
        updated = text.rstrip() + "\n\n[build]\nincremental = false\n" + line
    tomllib.loads(updated)
    return updated


def install(home: Path, real_cargo: Path, python: Path) -> dict[str, str]:
    """Install only managed files; preserve third-party wrappers and configuration."""
    root = host_root(home)
    launcher = home / ".local/bin/cargo"
    compiler = root / "rustc-cache"
    manifest = root / "install.json"
    real_cargo = Path(os.path.abspath(real_cargo))
    if launcher.exists() and not launcher.is_symlink() and MARKER not in launcher.read_text()[:256]:
        raise ValueError(f"Existing Cargo launcher is not ours: {launcher}")
    if launcher.is_symlink() or real_cargo.resolve() == launcher.resolve():
        raise ValueError("Cargo launcher would replace a symlink or recurse")
    # A caller installing an isolated/new home may discover this user's already
    # managed Cargo on PATH. Capture its real executable, not another launcher.
    try:
        with real_cargo.open("rb") as executable:
            managed = MARKER.encode() in executable.read(256)
    except OSError:
        managed = False
    if managed:
        existing_home = real_cargo.resolve().parents[2]
        original = json.loads((host_root(existing_home) / "install.json").read_text())
        real_cargo = Path(original["real_cargo"])
        if not real_cargo.is_absolute() or real_cargo.resolve() == launcher.resolve():
            raise ValueError("Managed Cargo chain would recurse")
    if not real_cargo.is_file() or not os.access(real_cargo, os.X_OK):
        raise ValueError("Real Cargo executable is unavailable")
    config = home / ".cargo/config.toml"
    legacy = home / ".cargo/config"
    if legacy.exists():
        config = legacy
    if config.is_symlink():
        raise ValueError(f"Refusing to replace symlink: {config}")
    profiles = [home / name for name in (".zshenv", ".zprofile", ".zshrc", ".bash_profile", ".bashrc")]
    for path in [compiler, manifest, config, *profiles]:
        if path.is_symlink():
            raise ValueError(f"Refusing to replace symlink: {path}")
        if path.exists() and (not path.is_file() or not os.access(path, os.W_OK)):
            raise ValueError(f"Destination is not a writable regular file: {path}")
    old_config = config.read_text() if config.exists() else ""
    # Validate everything before touching the operator's Cargo configuration.
    new_config = cargo_configuration(old_config, compiler, home)
    prefix = f"#!/bin/sh\n# {MARKER}\nexec {shlex.quote(str(python))} -m herdr_engineering.cargo_host"
    writes = [(compiler, prefix + ' rustc "$@"\n', 0o755),
              (manifest, json.dumps({"version": 1, "real_cargo": str(real_cargo),
                                     "compiler_wrapper": str(compiler)}, indent=2) + "\n", 0o600),
              (config, new_config, config.stat().st_mode & 0o777 if config.exists() else 0o600),
              (launcher, prefix + ' cargo "$@"\n', 0o755)]
    # Noninteractive zsh reads zshenv; login/profile files may subsequently
    # prepend Homebrew, so keep the managed entry point first there too.
    block = ('# >>> herdr-cargo-host >>>\n'
             'export PATH="$HOME/.local/bin:$PATH"\n'
             '# <<< herdr-cargo-host <<<\n')
    for path in profiles:
        text = path.read_text() if path.exists() else ""
        text = re.sub(r"(?ms)^# >>> herdr-cargo-host >>>\n.*?^# <<< herdr-cargo-host <<<\n?", "", text)
        writes.append((path, text.rstrip() + "\n\n" + block,
                       path.stat().st_mode & 0o777 if path.exists() else 0o600))
    previous = {path: (path.read_text(), path.stat().st_mode & 0o777) if path.exists() else None
                for path, _text, _mode in writes}
    for path, _text, _mode in writes:
        _backup(path)
    published: list[Path] = []
    try:
        for path, text, mode in writes:
            _atomic_write(path, text, mode)
            published.append(path)
    except OSError:
        for path in reversed(published):
            old = previous[path]
            try:
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, old[0], old[1])
            except OSError:
                pass  # The original backup remains available even on a full disk.
        raise
    return {"cargo": str(launcher), "compiler_wrapper": str(compiler), "config": str(config)}


def _real_cargo(home: Path) -> Path:
    data = json.loads((host_root(home) / "install.json").read_text())
    real = Path(data["real_cargo"])
    if (not real.is_absolute() or real.resolve() == (home / ".local/bin/cargo").resolve()
            or not os.access(real, os.X_OK)):
        raise ValueError("Managed Cargo installation needs repair: real executable is unavailable")
    return real


def enroll_storage(volume: Path, name: str, directory: str, config_path: Path,
                   priority: int = 0) -> dict[str, str]:
    """Enroll a writable volume without erasing/moving any existing data."""
    import plistlib

    import yaml

    from .build_cache_config import validate
    from .build_cache_location import mounted_volume, parse_storage_tiers
    from .build_coord import _default_state_dir
    result = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", str(volume)],
                            capture_output=True, timeout=3, check=True)
    info = plistlib.loads(result.stdout)
    identity = info.get("VolumeUUID")
    tier = parse_storage_tiers([{"name": name, "directory": directory,
                                 "volume_uuid": identity}])[0]
    mount = mounted_volume(tier.volume_uuid or "")
    if mount is None:
        raise ValueError("Volume is not mounted and writable")
    cache = mount / directory
    if not cache.resolve().is_relative_to(mount.resolve()):
        raise ValueError("Cache directory escapes its volume")
    marker = cache / ".herdr-compiler-cache.json"
    if cache.exists() and any(cache.iterdir()) and not marker.is_file():
        raise ValueError("Existing directory is not a managed compiler cache")
    original = config_path.read_text() if config_path.exists() else ""
    data = yaml.safe_load(original) or {}
    section = data.setdefault("fabric_build_cache", {})
    section.setdefault("enabled", True)
    local = section.setdefault("local", {})
    tiers = [item for item in local.get("storage_tiers", [])
             if item.get("name") != name and str(item.get("volume_uuid", "")).lower() != str(identity).lower()]
    tiers.insert(max(0, priority), {"name": name, "directory": directory, "volume_uuid": identity})
    if not any(not item.get("volume_uuid") for item in tiers):
        tiers.append({"name": "internal", "directory": str(_default_state_dir() / "sccache")})
    local["storage_tiers"] = tiers
    validate(section)
    # Creation is explicit enrollment only; runtime selection creates nothing.
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    if mounted_volume(tier.volume_uuid or "") != mount:
        raise ValueError("Volume changed during enrollment")
    _atomic_write(marker, json.dumps({"version": 1, "volume_uuid": identity}) + "\n")
    if config_path.exists() and config_path.read_text() != original:
        raise ValueError("Configuration changed concurrently; enrollment not saved")
    _backup(config_path)
    _atomic_write(config_path, yaml.safe_dump(data, sort_keys=False))
    return {"name": name, "volume_uuid": str(identity), "cache_directory": str(cache)}


def enable_targets(config_path: Path) -> dict[str, object]:
    import yaml

    from .build_cache_config import validate
    original = config_path.read_text()
    data = yaml.safe_load(original)
    section = data.get("fabric_build_cache", {})
    if not section.get("enabled"):
        raise ValueError("Enable fabric_build_cache before managed targets")
    section.setdefault("local", {})["manage_targets"] = True
    validate(section)
    backup = config_path.with_name(config_path.name + ".before-herdr-target-storage")
    if not backup.exists():
        _atomic_write(backup, original)
    if config_path.read_text() != original:
        raise ValueError("Configuration changed concurrently; no target setting saved")
    _atomic_write(config_path, yaml.safe_dump(data, sort_keys=False))
    from .build_cache_location import select_storage
    from .build_coord import _default_state_dir
    from .cargo_target_storage import seed_existing_aliases
    config = validate(section)
    selected = select_storage(config.local.storage_tiers, _default_state_dir() / "sccache",
                              config.local.min_free_disk_percent)
    seeded = seed_existing_aliases(selected.directory, _default_state_dir()) if selected.directory else 0
    return {"manage_targets": True, "config": str(config_path), "registered_existing_aliases": seeded}


def run_cargo(argv: list[str]) -> int:
    real = _real_cargo(Path.home())
    command = [str(real), *argv]
    subcommand = cargo_subcommand(argv)
    from .cli import _build_cache_config
    config = _build_cache_config()
    already_coordinated = os.environ.get(COORDINATED) == "1"
    if (config is None or subcommand not in {"build", "check", "test", "clippy", "nextest", "run",
                                            "bench", "doc", "rustc", "rustdoc", "clean", "fix", "package"}):
        os.execvpe(str(real), command, dict(os.environ))
        return 127  # pragma: no cover
    from .build_cache_policy import effective_slots
    from .build_coord import _default_slots, _default_state_dir, run_gated
    if config.local.manage_targets:
        from .build_cache_location import select_storage
        from .cargo_target_storage import (
            alias_owned,
            compatibility_alias,
            maintenance_lock,
            prepare_target,
            target_override,
            workspace_root,
        )
        if not target_override(argv, os.environ, Path.cwd()):
            with maintenance_lock(_default_state_dir()):
                selected = select_storage(config.local.storage_tiers, _default_state_dir() / "sccache",
                                          0 if subcommand == "clean" else config.local.min_free_disk_percent)
                if selected.directory is None:
                    raise ValueError("No writable storage tier meets the reserve; managed build was not started")
                if not selected.directory.exists():
                    removable = any(tier.name == selected.name and tier.volume_uuid
                                    for tier in config.local.storage_tiers)
                    if removable:
                        raise ValueError("Selected external storage disappeared")
                    selected.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
                workspace = workspace_root(real, argv, Path.cwd())
                old_target = workspace / "target"
                if (old_target.exists() or old_target.is_symlink()) and not alias_owned(_default_state_dir(), workspace):
                    if already_coordinated:
                        os.execvpe(str(real), command, dict(os.environ))
                        return 127  # pragma: no cover
                    return run_gated(command, slots=effective_slots(config, _default_slots(), _default_state_dir()),
                                     record=True, cache_config=config, max_load=1.5)
                target = prepare_target(selected.directory, workspace)
                if not compatibility_alias(_default_state_dir(), workspace, target):
                    raise ValueError("Default target changed concurrently; no automatic redirection applied")
                os.environ["CARGO_TARGET_DIR"] = str(target)
                if already_coordinated:
                    os.execvpe(str(real), command, dict(os.environ))
                    return 127  # pragma: no cover
                return run_gated(command, slots=effective_slots(config, _default_slots(), _default_state_dir()),
                                 record=True, cache_config=config, max_load=1.5)
    if already_coordinated:
        os.execvpe(str(real), command, dict(os.environ))
        return 127  # pragma: no cover
    return run_gated(command, slots=effective_slots(config, _default_slots(), _default_state_dir()),
                     record=True, cache_config=config, max_load=1.5)


def run_rustc(argv: list[str]) -> int:
    if not argv:
        raise ValueError("Compiler adapter requires a compiler executable")
    from .build_cache_env import Tools, activate_shared, plan
    from .build_cache_location import select_storage
    from .build_cache_storage import check_shared
    from .build_coord import _default_state_dir, _prestart_sccache_server
    from .cli import _build_cache_config
    env = dict(os.environ)
    config = _build_cache_config()
    compiler = argv[0]
    if Path(compiler).resolve() == (host_root(Path.home()) / "rustc-cache").resolve():
        raise ValueError("Recursive compiler adapter invocation")
    try:
        if config is not None:
            state = _default_state_dir()
            selected = select_storage(config.local.storage_tiers, state / "sccache",
                                      config.local.min_free_disk_percent)
            sccache = shutil.which("sccache")
            if sccache and selected.directory is not None:
                vv = subprocess.run([compiler, "-vV"], capture_output=True, text=True, timeout=3)
                version = subprocess.run([sccache, "--version"], capture_output=True, text=True,
                                         timeout=3).stdout.strip() if config.shared_cache.enabled else None
                tools = Tools(Path(sccache), version, Path(compiler), None, vv.stdout)
                command = ["cargo", "build"]
                if "--target" in argv and argv.index("--target") + 1 < len(argv):
                    command += ["--target", argv[argv.index("--target") + 1]]
                if is_managed_wrapper(env.get("RUSTC_WRAPPER", ""), Path.home()):
                    env.pop("RUSTC_WRAPPER", None)
                decision = plan(config, command, env, tools, Path.cwd(), state,
                                check_shared(config, state), cache_dir=selected.directory)
                if decision.shared_setup is not None:
                    decision = activate_shared(decision, tools, env)
                if decision.applied:
                    cache_env = {**env, **decision.env_overrides}
                    _prestart_sccache_server(cache_env)
                    os.execvpe(sccache, [sccache, *argv], cache_env)
                    return 127  # pragma: no cover
    except Exception as error:  # Cache optimization must not prevent native compilation.
        print(f"cargo-host: cache unavailable ({type(error).__name__}); using compiler uncached", file=sys.stderr)
    os.execvpe(compiler, argv, env)
    return 127  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] == "cargo":
            return run_cargo(arguments[1:])
        if arguments and arguments[0] == "rustc":
            return run_rustc(arguments[1:])
        parser = argparse.ArgumentParser(prog="herdr-eng cargo-host")
        parser.add_argument("action", choices=["install", "status", "storage-add", "targets-enable", "targets-migrate"])
        parser.add_argument("--real-cargo", type=Path)
        parser.add_argument("--volume", type=Path)
        parser.add_argument("--name", default="primary")
        parser.add_argument("--directory", default="herdr-build/compiler-cache")
        parser.add_argument("--priority", type=int, default=0)
        parser.add_argument("--config", type=Path,
                            default=Path.home() / ".config/herdr-engineering/config.yaml")
        parser.add_argument("--root", type=Path, action="append", default=[])
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--quiescent", action="store_true",
                            help="confirm uncoordinated Cargo/IDE clients are paused for migration")
        args = parser.parse_args(arguments)
        if args.action == "install":
            real = args.real_cargo
            if real is None:
                manifest = host_root(Path.home()) / "install.json"
                real = _real_cargo(Path.home()) if manifest.exists() else Path(shutil.which("cargo") or "")
            print(json.dumps(install(Path.home(), real, Path(sys.executable)), indent=2))
        elif args.action == "storage-add":
            if args.volume is None:
                raise ValueError("storage-add requires --volume")
            print(json.dumps(enroll_storage(args.volume, args.name, args.directory,
                                            args.config, args.priority), indent=2))
        elif args.action == "targets-enable":
            print(json.dumps(enable_targets(args.config), indent=2))
        elif args.action == "targets-migrate":
            from .cargo_target_migration import migration_run
            print(json.dumps(migration_run(args.root, apply=args.apply, quiescent=args.quiescent), indent=2))
        else:
            from .build_cache_location import select_storage
            from .build_coord import _default_state_dir
            from .cli import _build_cache_config
            config = _build_cache_config()
            selected = select_storage(config.local.storage_tiers, _default_state_dir() / "sccache",
                                      config.local.min_free_disk_percent) if config else None
            print(json.dumps({"installed": (host_root(Path.home()) / "install.json").exists(),
                              "manage_targets": config.local.manage_targets if config else False,
                              "storage_tier": selected.name if selected else None,
                              "cache_directory": str(selected.directory) if selected and selected.directory else None,
                              "skipped": selected.skipped if selected else []}, indent=2))
        return 0
    except (OSError, ValueError, InvalidError, subprocess.SubprocessError) as error:
        print(f"cargo-host: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
