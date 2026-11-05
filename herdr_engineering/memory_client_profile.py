"""Private runtime configuration for the canonical CLI; no memory transport."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from .control_plane.memory_explorer import Viewer
from .memory_install import PINNED_CLIENT_VERSION

RUNTIME = Path("/run/herdr-memory-client")
CACHE = "/state/herdr-memory-explorer"
MAX_CONFIG = 2 * 1024 * 1024
MAX_KEY = 4096
MAX_ARCHIVE = 4 * 1024 * 1024
KEY_NAME = re.compile(r"credentials/[0-9a-f]{24}\.key")
DENIED = ("personal", "rakazo", "assistant")


class ProfileError(ValueError):
    """Safe-to-display deployment failure without private data or subprocess output."""


def _private_bytes(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > limit):
                raise ProfileError("Profile inputs must be private user-owned regular files")
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ProfileError("Profile input exceeds its size limit")
        return data
    except OSError:
        raise ProfileError("Cannot safely read private profile input") from None


def _private_directory(path):
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ProfileError("Profile directory must be private and user-owned")
    except OSError:
        raise ProfileError("Cannot prepare private profile directory") from None


def _atomic(path, data):
    temporary = None
    try:
        if path.exists() or path.is_symlink():
            _private_bytes(path, MAX_ARCHIVE)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".memory-profile-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise ProfileError("Cannot write private runtime profile") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _client_version_ok(metadata):
    return (isinstance(metadata, dict) and type(metadata.get("contract")) is int
            and metadata["contract"] == 1 and metadata.get("client") == PINNED_CLIENT_VERSION)


def _cli_validation(source, runner):
    try:
        version = runner(["herdr-memory", "--version"], capture_output=True, text=True,
                         check=False, timeout=15)
        metadata = json.loads(version.stdout)
        if version.returncode or not _client_version_ok(metadata):
            raise ProfileError("Canonical memory client contract v1 is required")
        validation = runner(["herdr-memory", "--config", str(source), "validate-config"],
                            cwd=str(source.parent), capture_output=True, text=True,
                            check=False, timeout=15)
        result = json.loads(validation.stdout)
        if validation.returncode or not _client_version_ok(result) or result.get("valid") is not True:
            raise ProfileError("Canonical memory client rejected the source profile")
        return True
    except ProfileError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        raise ProfileError("Cannot validate the canonical memory client profile") from None


def _selection(policy):
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        return None
    entries = policy.get("viewers") or {}
    if not entries:
        return None
    if not isinstance(entries, dict):
        raise ProfileError("Invalid memory viewer policy")
    viewer_projects, scopes, wildcard = set(), set(), False
    for entry in entries.values():
        viewer = Viewer.from_entry(entry)
        if viewer is None or any(s not in ("global", "infra") for s in entry.get("scopes", [])):
            raise ProfileError("Invalid memory viewer policy")
        if any(not viewer.may_read_project(p) for p in viewer.projects):
            raise ProfileError("Personal-assistant projects cannot be exported")
        wildcard |= viewer.all_projects
        viewer_projects.update(viewer.projects)
        scopes.update(viewer.scopes)
    if not wildcard and not viewer_projects and not scopes:
        return None
    return wildcard, viewer_projects, scopes


def policy_info(policy: dict, *, profile_override=False) -> dict:
    """Runtime selection only; never returns policy values or credential paths."""
    if _selection(policy) is None:
        return {"enabled": False, "mode": "clear"}
    command = policy.get("command")
    if command is not None and (not isinstance(command, list) or not command
                                or not isinstance(command[0], str) or not command[0]
                                or any(not isinstance(arg, str) for arg in command)):
        raise ProfileError("Invalid memory client command")
    source = policy.get("client_config_source")
    if not profile_override and source is not None and (not isinstance(source, str) or not source.strip()):
        raise ProfileError("Invalid client profile path")
    managed = command is None or command == ["herdr-memory"] or profile_override or bool(source)
    return {"enabled": managed, "mode": "install" if managed else "unmanaged"}


def prepare_profile(source: Path | None, policy: dict, destination: Path, *, runner=None) -> dict:
    """Project an operator profile into private files; never grant viewer access."""
    selection = _selection(policy)
    if selection is None:
        return {"prepared": False, "reason": "no_viewers"}
    wildcard, viewer_projects, scopes = selection
    if source is None:
        raise ProfileError("Dashboard viewers require an explicit client profile")
    source, destination = Path(source).absolute(), Path(destination).absolute()
    try:
        original = _private_bytes(source, MAX_CONFIG)
        config = json.loads(original)
        projects = config["projects"]
        if (not isinstance(projects, dict) or not isinstance(config.get("url"), str)
                or not config["url"] or any(not isinstance(item, dict) for item in projects.values())):
            raise ValueError()
        engineering = {p for p in projects if isinstance(p, str) and not p.lower().startswith(DENIED)}
        if viewer_projects - engineering:
            raise ProfileError("A viewer project has no engineering client binding")
        selected = set(engineering) if wildcard else set(viewer_projects)
        anchor = config.get("default_project")
        if not isinstance(anchor, str) or anchor not in engineering:
            if len(selected) == 1:
                anchor = next(iter(selected))
            else:
                raise ProfileError("An explicit nonpersonal default project is required")
        # Contract v1 needs a project identity even for a global-only viewer.
        # This credential binding never changes Memory Explorer's bank grants.
        selected.add(anchor)
    except ProfileError:
        raise
    except (ValueError, KeyError, TypeError):
        raise ProfileError("Invalid source client profile") from None
    validated = _cli_validation(source, runner or subprocess.run)
    if _private_bytes(source, MAX_CONFIG) != original:
        raise ProfileError("Source client profile changed during validation")
    credentials = {}
    inputs = {source.resolve()}

    def binding(settings):
        try:
            name = settings["credentials"]["codex"]
            if not isinstance(name, str) or not name:
                raise ValueError()
            path = Path(name).expanduser()
            if not path.is_absolute():
                path = source.parent / path
            raw = _private_bytes(path, MAX_KEY + 2)
            inputs.add(path.resolve())
            token = raw.decode("utf-8").strip()
            if (not token or len(token) > MAX_KEY
                    or any(ord(char) < 33 or ord(char) > 126 for char in token)):
                raise ValueError()
        except ProfileError:
            raise
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise ProfileError("An engineering codex credential binding is required") from None
        relative = "credentials/" + hashlib.sha256(str(path.absolute()).encode()).hexdigest()[:24] + ".key"
        credentials[relative] = token.encode()
        return {"codex": str(RUNTIME / relative)}

    rendered = {"url": config["url"], "cache_dir": CACHE, "default_project": anchor, "projects": {}}
    if "timeout_seconds" in config:
        rendered["timeout_seconds"] = config["timeout_seconds"]
    for project in sorted(selected):
        settings = projects[project]
        item = {key: settings[key] for key in ("tenant", "mission_scopes") if key in settings}
        item["credentials"] = binding(settings)
        rendered["projects"][project] = item
    if scopes:
        rendered["scopes"] = {}
        for scope in sorted(scopes):
            try:
                rendered["scopes"][scope] = {"credentials": binding(config["scopes"][scope])}
            except KeyError:
                raise ProfileError("A viewer scope has no client credential binding") from None
    files = {"config.json": json.dumps(rendered, sort_keys=True).encode(), **credentials}
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.mode, member.uid, member.gid, member.size = 0o600, 0, 0, len(data)
            archive.addfile(member, io.BytesIO(data))
    payload = stream.getvalue()
    if len(payload) > MAX_ARCHIVE:
        raise ProfileError("Prepared profile exceeds its size limit")
    if _private_bytes(source, MAX_CONFIG) != original:
        raise ProfileError("Source client profile changed during preparation")
    if destination.resolve() in inputs:
        raise ProfileError("Profile output must not replace a source input")
    _private_directory(destination.parent)
    _atomic(destination, payload)
    return {"prepared": True, "projects": sorted(selected), "viewer_projects": sorted(viewer_projects),
            "scopes": sorted(scopes), "identity_project": anchor,
            "source_client_validated": validated, "sha256": hashlib.sha256(payload).hexdigest()}


def _managed_names(target):
    manifest = target / "_managed.json"
    if manifest.exists() or manifest.is_symlink():
        try:
            names = json.loads(_private_bytes(manifest, MAX_CONFIG))["files"]
            if not isinstance(names, list) or any(
                    not isinstance(name, str) or
                    name != "config.json" and not KEY_NAME.fullmatch(name) for name in names):
                raise ValueError()
            return set(names)
        except (ValueError, KeyError, TypeError):
            raise ProfileError("Invalid managed runtime profile") from None
    if any(target.iterdir()):
        raise ProfileError("Existing runtime profile is not managed")
    return set()


def _decode_profile(payload):
    if not isinstance(payload, bytes) or len(payload) > MAX_ARCHIVE:
        raise ProfileError("Invalid runtime profile archive")
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            for member in archive.getmembers():
                if (not member.isfile() or member.name in files or member.mode != 0o600
                        or member.uid != 0 or member.gid != 0
                        or not (member.name == "config.json" or KEY_NAME.fullmatch(member.name))
                        or not 0 < member.size <= (MAX_CONFIG if member.name == "config.json" else MAX_KEY)):
                    raise ProfileError("Unsafe runtime profile archive member")
                files[member.name] = archive.extractfile(member).read()
        config = json.loads(files["config.json"])
        if config.get("cache_dir") != CACHE or not isinstance(config.get("projects"), dict):
            raise ValueError()
        references = set()
        for group in (config["projects"], config.get("scopes", {})):
            for settings in group.values():
                bindings = settings["credentials"]
                if set(bindings) != {"codex"}:
                    raise ValueError()
                path = Path(bindings["codex"])
                relative = str(path.relative_to(RUNTIME))
                if not KEY_NAME.fullmatch(relative):
                    raise ValueError()
                references.add(relative)
        if references != set(files) - {"config.json"}:
            raise ValueError()
    except ProfileError:
        raise
    except (tarfile.TarError, OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ProfileError("Invalid runtime profile archive") from None
    return files, config


def check_archive(archive: Path) -> dict:
    payload = _private_bytes(Path(archive), MAX_ARCHIVE)
    _decode_profile(payload)
    return {"valid": True, "sha256": hashlib.sha256(payload).hexdigest()}


def receive_profile(payload: bytes, *, target: Path = RUNTIME) -> None:
    """Validate the entire archive before installing any files; no extractall."""
    files, _ = _decode_profile(payload)
    references = set(files) - {"config.json"}
    target = Path(target)
    _private_directory(target)
    manifest = target / "_managed.json"
    previous = _managed_names(target)
    # Track intended files before writing them, so interrupted first activation
    # and rollback can remove only files this operation owns.
    _atomic(manifest, json.dumps({"files": sorted(previous | set(files))}).encode())
    _private_directory(target / "credentials")
    for name in sorted(references):
        _atomic(target / name, files[name])
    _atomic(target / "config.json", files["config.json"])
    for name in set(previous) - set(files):
        path = target / name
        if path.exists() or path.is_symlink():
            _private_bytes(path, MAX_KEY)
            path.unlink()
    _atomic(manifest, json.dumps({"files": sorted(files)}).encode())


def clear_profile(*, target: Path = RUNTIME) -> None:
    """Remove only managed runtime credentials; never remove the client cache."""
    target = Path(target)
    if not target.exists() and not target.is_symlink():
        return
    _private_directory(target)
    names = _managed_names(target)
    for name in names:
        path = target / name
        if path.exists() or path.is_symlink():
            _private_bytes(path, MAX_CONFIG if name == "config.json" else MAX_KEY)
    credentials = target / "credentials"
    if credentials.exists() or credentials.is_symlink():
        _private_directory(credentials)
    for name in names:
        (target / name).unlink(missing_ok=True)
    (target / "_managed.json").unlink(missing_ok=True)
    if credentials.exists() and not any(credentials.iterdir()):
        credentials.rmdir()
    if not any(target.iterdir()):
        target.rmdir()


def _check_container(container):
    if not isinstance(container, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", container):
        raise ProfileError("Invalid private web container")


def install_profile(archive: Path | None, container: str, *, runner=None) -> None:
    _check_container(container)
    payload = _private_bytes(Path(archive), MAX_ARCHIVE) if archive is not None else b""
    action = "receive" if archive is not None else "clear"
    try:
        result = (runner or subprocess.run)(
            ["docker", "exec", "-i", container, "python", "-m",
             "herdr_engineering.memory_client_profile", action],
            input=payload, capture_output=True, check=False, timeout=30)
        if result.returncode:
            raise ProfileError("Private memory profile installation failed")
    except (OSError, subprocess.SubprocessError):
        raise ProfileError("Cannot install private memory profile") from None


def verify_runtime(container: str, *, runner=None) -> None:
    """Check the installed client version only; no credential or network probe."""
    _check_container(container)
    try:
        result = (runner or subprocess.run)(
            ["docker", "exec", container, "herdr-memory", "--version"],
            capture_output=True, text=True, check=False, timeout=15)
        if result.returncode or not _client_version_ok(json.loads(result.stdout)):
            raise ProfileError("The private web image does not contain the pinned memory client")
    except ProfileError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        raise ProfileError("Cannot verify the private memory client runtime") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("receive")
    actions.add_parser("clear")
    prepare = actions.add_parser("prepare")
    prepare.add_argument("--source", type=Path)
    prepare.add_argument("--fleet-config", required=True, type=Path)
    prepare.add_argument("--out", required=True, type=Path)
    install = actions.add_parser("install")
    install.add_argument("--archive", type=Path)
    install.add_argument("--container", required=True)
    verify = actions.add_parser("verify")
    verify.add_argument("--container", required=True)
    info = actions.add_parser("policy-info")
    info.add_argument("--fleet-config", required=True, type=Path)
    info.add_argument("--profile-override", action="store_true")
    check = actions.add_parser("check")
    check.add_argument("--archive", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "receive":
            receive_profile(sys.stdin.buffer.read(MAX_ARCHIVE + 1))
        elif args.action == "clear":
            clear_profile()
        elif args.action == "install":
            install_profile(args.archive, args.container)
        elif args.action == "verify":
            verify_runtime(args.container)
        elif args.action == "check":
            print(json.dumps(check_archive(args.archive)))
        else:
            from .fleet_config import load_fleet_config
            config = load_fleet_config(args.fleet_config).section("control_plane").get("memory") or {}
            if args.action == "policy-info":
                print(json.dumps(policy_info(config, profile_override=args.profile_override)))
                return 0
            source = args.source
            if source is None and config.get("client_config_source") is not None:
                configured = config["client_config_source"]
                if not isinstance(configured, str) or not configured:
                    raise ProfileError("An explicit client profile path is required")
                source = Path(configured).expanduser()
                if not source.is_absolute():
                    raise ProfileError("Configured client profile path must be absolute")
            if source is None and config.get("enabled") is True and config.get("viewers"):
                raise ProfileError("Dashboard viewers require an explicit client profile")
            receipt = prepare_profile(source, config, args.out)
            print(json.dumps({key: receipt[key] for key in
                              ("prepared", "reason", "sha256", "source_client_validated") if key in receipt}))
        return 0
    except ProfileError as error:
        parser.exit(1, f"Private memory profile operation failed: {error}\n")
    except (OSError, ValueError, TypeError, KeyError):
        parser.exit(1, "Private memory profile operation failed; inputs and credentials withheld\n")


if __name__ == "__main__":
    raise SystemExit(main())
