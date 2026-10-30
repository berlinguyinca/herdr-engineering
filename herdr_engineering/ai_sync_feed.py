"""Read-only ai-sync status reporter: one bounded observation per host, posted to the control plane.

Never runs ``ai-sync pull``/``push`` (or any other mutating subcommand), never sends file contents, tokens or
secrets: only counts, relative paths (<=20) and short redacted error text leave the host.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import host_config as _hc
from .host_config import HostConfigUnavailable, exit_unavailable, resolve_control_plane
from .service_metadata_feed import MetadataClient

# The complete set of ai-sync invocations this module can make. Everything else is refused before a process exists.
ALLOWED_COMMANDS = frozenset({("--version",), ("status",), ("env", "list"), ("verify", "--json")})
DRIFT_SAMPLE_MAX = 20
VERIFY_SAMPLE_MAX = 10
VERIFY_CHECKED_KEYS = ("skills", "agents", "commands", "hooks")
REFRESH_SECONDS = 600
_TOOL_TIMEOUT = 20.0
_GIT_TIMEOUT = 10.0
_EXTRA_PATH = (".local/bin", ".npm-global/bin", ".volta/bin", "bin")
_CHANGE_LINE = re.compile(r"^\s+[A-Z?]{1,2}\s+(\S.*?)\s*$")
_ENV_COUNTERS = re.compile(r"^Env\s+\S+:\s*local-only=(\d+)\s+remote-only=(\d+)\s+both-changed=(\d+)")
_ENV_ENABLED = re.compile(r"^\s+([A-Za-z0-9_-]+)\s+.*\S\s+enabled\s*$")
_USERINFO = re.compile(r"://[^/@\s]+@")
_TOKENISH = re.compile(r"[A-Za-z0-9_\-]{24,}")


class AiSyncError(Exception):
    """A failed, refused or timed-out ai-sync/git invocation; the message is already redacted and bounded."""


@dataclass
class ParsedStatus:
    drifted_paths: list[str] = field(default_factory=list)
    drifted_count: int = 0
    skipped_links_count: int = 0


@dataclass
class ParsedVerify:
    ok: bool
    errors_count: int
    warnings_count: int
    checked: dict[str, int | None]
    error_sample: list[str]


def redact(text: str, limit: int = 200) -> str:
    text = _USERINFO.sub("://[redacted]@", text)
    text = _TOKENISH.sub("[redacted]", text)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(text.split())[:limit]


def parse_status(text: str) -> ParsedStatus:
    """Defensively read plain ``ai-sync status`` output (the tool has no JSON mode)."""
    paths: list[str] = []
    counters = 0
    skipped = 0
    in_changes = False
    for line in text.splitlines():
        if re.match(r"^(Local|Remote) changes:\s*$", line):
            in_changes = True
            continue
        match = _CHANGE_LINE.match(line) if in_changes else None
        if match:
            path = match.group(1)
            if path not in paths and len(path) <= 300 and path.isprintable():
                paths.append(path)
            continue
        in_changes = False
        counter = _ENV_COUNTERS.match(line)
        if counter:
            counters += sum(int(n) for n in counter.groups())
        elif re.search(r"skipped", line, re.IGNORECASE):
            number = re.search(r"\d+", line)
            skipped += int(number.group()) if number else 1
    return ParsedStatus(drifted_paths=paths, drifted_count=max(len(paths), counters), skipped_links_count=skipped)


def _verify_items(value) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        return []
    return [(i["path"], i["problem"]) for i in value
            if isinstance(i, dict) and isinstance(i.get("path"), str) and isinstance(i.get("problem"), str)]


def parse_verify(text: str) -> ParsedVerify | None:
    """Defensively read ``ai-sync verify --json`` (single env or ``{"envs": [...]}``); None when not that contract.

    Only counts and ``path: problem`` strings survive: configDir and any other member are dropped.
    """
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("ok"), bool):
        return None
    envs = doc.get("envs")
    if envs is None:
        models, multi = [doc], False
    elif isinstance(envs, list) and all(isinstance(e, dict) for e in envs):
        models, multi = envs, True
    else:
        return None
    errors = warnings = 0
    sample: list[str] = []
    sums = dict.fromkeys(VERIFY_CHECKED_KEYS, 0)
    invalid: set[str] = set()
    for model in models:
        bad = _verify_items(model.get("errors"))
        errors += len(bad)
        warnings += len(_verify_items(model.get("warnings")))
        prefix = f"{model['env']}: " if multi and isinstance(model.get("env"), str) else ""
        sample.extend(redact(f"{prefix}{path}: {problem}") for path, problem in bad[:VERIFY_SAMPLE_MAX])
        checked = model.get("checked") if isinstance(model.get("checked"), dict) else {}
        for key in VERIFY_CHECKED_KEYS:
            n = checked.get(key)
            if type(n) is int and 0 <= n < 2**31:
                sums[key] += n
            else:
                invalid.add(key)
    checked_totals = {key: None if key in invalid else n for key, n in sums.items()}
    return ParsedVerify(ok=doc["ok"], errors_count=errors, warnings_count=warnings, checked=checked_totals,
                        error_sample=sample[:VERIFY_SAMPLE_MAX])


def parse_env_list(text: str) -> list[str]:
    return [m.group(1) for m in (_ENV_ENABLED.match(line) for line in text.splitlines()) if m]


def resolve_binary(explicit: str | None = None, home: Path | None = None) -> str | None:
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    home = home or Path.home()
    extra = os.pathsep.join(str(home / rel) for rel in _EXTRA_PATH)
    return shutil.which("ai-sync", path=os.pathsep.join(filter(None, [os.environ.get("PATH"), extra])))


def _spawn(argv: list[str], *, timeout: float, env: dict[str, str], label: str) -> tuple[int, str, str]:
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, text=True, errors="replace", start_new_session=True)
    except OSError as error:
        raise AiSyncError(f"{label} could not start: {redact(str(error))}") from None
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.communicate()
        raise AiSyncError(f"{label} timed out after {timeout:g}s") from None
    return process.returncode, out, err


def _exit_error(label: str, code: int, out: str, err: str) -> AiSyncError:
    first = next((line for line in (err + out).splitlines() if line.strip()), "")
    return AiSyncError(f"{label} exited {code}: {redact(first)}")


def _run(argv: list[str], *, timeout: float, env: dict[str, str], label: str) -> str:
    code, out, err = _spawn(argv, timeout=timeout, env=env, label=label)
    if code != 0:
        raise _exit_error(label, code, out, err)
    return out


def _env(home: Path, binary: str | None) -> dict[str, str]:
    path = [os.environ.get("PATH", "")]
    if binary:
        path.insert(0, str(Path(binary).resolve().parent))
    path.extend(str(home / rel) for rel in _EXTRA_PATH)
    return {**os.environ, "HOME": str(home), "PATH": os.pathsep.join(filter(None, path)),
            "GIT_TERMINAL_PROMPT": "0", "NO_COLOR": "1"}


def _checked_argv(binary: str, args: tuple[str, ...]) -> list[str]:
    if tuple(args) not in ALLOWED_COMMANDS:
        raise ValueError(f"ai-sync {' '.join(args)!r} is not a permitted read-only command")
    # The updater is switched off (global flag, before the subcommand) so a probe can never rewrite the tool.
    return [binary, "--no-update-check", *args]


def run_ai_sync(binary: str, args: tuple[str, ...], *, timeout: float, home: Path) -> str:
    return _run(_checked_argv(binary, args), timeout=timeout, env=_env(home, binary),
                label=f"ai-sync {' '.join(args)}")


def run_verify(binary: str, *, timeout: float, home: Path) -> ParsedVerify | None:
    """Run ``ai-sync verify --json``. Exit 1 means "found errors" and still carries JSON, so it is not a failure.

    Returns None for an ai-sync that predates the command; raises AiSyncError for anything else unusable.
    """
    command = ("verify", "--json")
    label = f"ai-sync {' '.join(command)}"
    code, out, err = _spawn(_checked_argv(binary, command), timeout=timeout, env=_env(home, binary), label=label)
    if code in (0, 1):
        parsed = parse_verify(out)
        if parsed is not None:
            return parsed
        if code == 1 and not out.strip() and "unknown command" in err.lower():
            return None
        raise AiSyncError(f"{label} output not understood") if code == 0 else _exit_error(label, code, out, err)
    raise _exit_error(label, code, out, err)


def _git(repo: Path, *args: str, home: Path, timeout: float = _GIT_TIMEOUT) -> str:
    return _run(["git", "-C", str(repo), *args], timeout=timeout, env=_env(home, None), label=f"git {args[0]}")


def _git_state(repo: Path, home: Path) -> tuple[str | None, int | None, int | None, str | None, list[str]]:
    errors: list[str] = []
    branch = last = ahead = behind = None
    if not (repo / ".git").exists():
        return None, None, None, None, [f"sync repo not found at {repo.name}"]
    try:
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD", home=home).strip() or None
        stamp = int(_git(repo, "log", "-1", "--format=%ct", home=home).strip())
        last = datetime.fromtimestamp(stamp, UTC).isoformat()
    except (AiSyncError, ValueError) as error:
        errors.append(str(error))
    try:
        left, right = _git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}", home=home).split()
        ahead, behind = int(left), int(right)
    except (AiSyncError, ValueError) as error:
        errors.append(str(error))
    return branch, ahead, behind, last, errors


def collect_status(host_id: str, *, binary: str | None = None, home: Path | None = None,
                   timeout: float = _TOOL_TIMEOUT) -> dict:
    """Build the ``ai_sync`` payload for this host. Never raises: failures land in ``last_error``."""
    home = Path(home) if home else Path.home()
    errors: list[str] = []
    version, envs, parsed = None, [], ParsedStatus()
    verified: ParsedVerify | None = None
    exe = resolve_binary(binary, home)
    if exe is None:
        errors.append("ai-sync is not installed")
    else:
        for command in (("--version",), ("env", "list"), ("status",)):
            try:
                out = run_ai_sync(exe, command, timeout=timeout, home=home)
            except AiSyncError as error:
                errors.append(str(error))
                continue
            if command == ("--version",):
                version = redact(out.strip().splitlines()[0], 40) if out.strip() else None
            elif command == ("env", "list"):
                envs = parse_env_list(out)
            else:
                parsed = parse_status(out)
        try:
            verified = run_verify(exe, timeout=timeout, home=home)
        except AiSyncError as error:
            errors.append(str(error))
    branch, ahead, behind, last, git_errors = _git_state(home / ".ai-sync", home)
    errors.extend(git_errors)
    return {
        "host_id": host_id, "ai_sync_version": version, "envs_enabled": envs, "last_sync_ts": last,
        "ahead": ahead, "behind": behind, "branch": branch,
        "drifted_files_count": parsed.drifted_count, "drift_sample": parsed.drifted_paths[:DRIFT_SAMPLE_MAX],
        "skipped_links_count": parsed.skipped_links_count,
        "last_error": "; ".join(errors)[:400] or None,
        "checked_at": datetime.now(UTC).isoformat(),
        "verify_ok": verified.ok if verified else None,
        "verify_errors_count": verified.errors_count if verified else None,
        "verify_warnings_count": verified.warnings_count if verified else None,
        "verify_checked": verified.checked if verified else None,
        "verify_error_sample": verified.error_sample if verified else None,
        "verify_checked_at": datetime.now(UTC).isoformat() if verified else None,
    }


class AiSyncFeed:
    def __init__(self, client, *, host_id, binary=None, home=None, clock=time.monotonic):
        if not isinstance(host_id, str) or not host_id or len(host_id) > 200:
            raise ValueError("invalid host identity")
        self.client, self.host_id, self.binary, self.home, self.clock = client, host_id, binary, home, clock
        self.last_fingerprint: str | None = None
        self.last_post = 0.0

    def run_once(self, *, dry_run=False):
        payload = collect_status(self.host_id, binary=self.binary, home=self.home)
        summary = dict(posted=0, unchanged=0, errors=0, dry_run=dry_run, drifted=payload["drifted_files_count"],
                       last_error=payload["last_error"])
        if dry_run:
            return summary
        fingerprint = json.dumps({k: v for k, v in payload.items() if k not in ("checked_at", "verify_checked_at")}, sort_keys=True)
        now = self.clock()
        if fingerprint == self.last_fingerprint and now - self.last_post < REFRESH_SECONDS:
            summary["unchanged"] = 1
            return summary
        envelope = dict(source="ai-sync-feed", entity_type="ai_sync", entity_id=self.host_id,
                        event_type="status", payload=payload)
        try:
            result = self.client.request("/api/v1/ingest", envelope)
            entity = result.get("entity")
            if result.get("ok") is not True or not isinstance(entity, dict) or entity.get("entity_id") != self.host_id:
                raise ValueError("ai-sync status rejected")
        except (OSError, ValueError, TypeError):
            summary["errors"] = 1
            return summary
        self.last_fingerprint, self.last_post = fingerprint, now
        summary["posted"] = 1
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-plane", default=None, help="control plane URL (default: host.json control_plane_url)")
    parser.add_argument("--host-id", default=None, help="host id (default: host.json host_id)")
    parser.add_argument("--api-key-file", default=None, help="read the ingest key from this file (default: ingest.api_key)")
    parser.add_argument("--ai-sync-bin", default=None, help="path to the ai-sync executable (default: search PATH)")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", type=float)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.loop is not None and (not 30 <= args.loop <= 3600 or args.once):
        parser.error("--loop requires 30–3600 seconds and cannot be combined with --once")
    fully_explicit = bool(args.control_plane and args.host_id and args.api_key_file)
    try:
        cfg = _hc.host_config()
    except HostConfigUnavailable as exc:
        if not fully_explicit:
            return exit_unavailable(exc)
        cfg = None
    try:
        key = cfg.api_key if cfg else None
        if args.api_key_file:
            with Path(args.api_key_file).expanduser().open() as stream:
                key = stream.read(8193).strip()
        if key and len(key) > 8192:
            raise ValueError("invalid API key file")
        agent = AiSyncFeed(MetadataClient(resolve_control_plane(args.control_plane), key),
                           host_id=args.host_id or cfg.host_id, binary=args.ai_sync_bin)
    except (OSError, ValueError):
        parser.exit(1, "ai-sync status reporter configuration is unavailable or invalid.\n")
    refresher = None
    if args.loop is not None and not args.dry_run and cfg is not None and not fully_explicit:
        refresher = _hc.ConfigRefresher(cfg)
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        while not stop.is_set():
            summary = agent.run_once(dry_run=args.dry_run)
            print(json.dumps(summary), flush=True)
            if args.loop is None:
                return 1 if summary["errors"] else 0
            if refresher is not None:
                try:
                    refresher.sync(agent.client, follow_url=not args.control_plane, follow_key=not args.api_key_file)
                except HostConfigUnavailable as exc:
                    return exit_unavailable(exc)
            stop.wait(args.loop)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
