"""Bring a host back to the shared ai-sync standard: ``pull --force`` then ``verify`` for each enabled env.

Runs on every fabric host except the publisher. The publisher is marked locally by
``~/.config/herdr-engineering/ai-sync-publisher`` (the fleet playbook creates it) and is refused before ai-sync is
spawned, because a forced pull would overwrite the very files the publisher is about to push.

Only ``pull --force --env <env>`` and ``verify --env <env> --json`` are ever spawned; ``push`` and every other
subcommand are impossible by construction. ai-sync backs up what a pull overwrites. Nothing but counts, relative
paths and short redacted text is printed, and no file contents leave the host.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sys
from pathlib import Path

from .ai_sync_feed import (
    AiSyncError,
    ParsedVerify,
    _env,
    _spawn,
    parse_verify,
    redact,
    resolve_binary,
)
from .control_plane.ai_sync_config import ENV_ALLOWLIST

PUBLISHER_MARKER_REL = Path(".config/herdr-engineering/ai-sync-publisher")
LOCK_REL = Path(".cache/herdr-engineering/ai-sync-enforce.lock")
PULL_TIMEOUT = 300.0
VERIFY_TIMEOUT = 60.0
EXIT_REFUSED_PUBLISHER = 3
EXIT_NOT_INSTALLED = 2


def pull_argv(binary: str, env: str) -> list[str]:
    _require_env(env)
    return [binary, "pull", "--force", "--env", env]


def verify_argv(binary: str, env: str) -> list[str]:
    _require_env(env)
    return [binary, "--no-update-check", "verify", "--env", env, "--json"]


def _require_env(env: str) -> None:
    if env not in ENV_ALLOWLIST:
        raise ValueError(f"{env!r} is not a known ai-sync environment")


def _pull(binary: str, env: str, home: Path) -> tuple[bool, str | None]:
    label = f"ai-sync pull --force --env {env}"
    try:
        code, out, err = _spawn(pull_argv(binary, env), timeout=PULL_TIMEOUT, env=_env(home, binary), label=label)
    except AiSyncError as error:
        return False, str(error)
    if code == 0:
        return True, None
    first = next((line for line in (err + out).splitlines() if line.strip()), "")
    return False, f"{label} exited {code}: {redact(first)}"


def _verify(binary: str, env: str, home: Path) -> tuple[ParsedVerify | None, str | None]:
    label = f"ai-sync verify --env {env}"
    try:
        code, out, err = _spawn(verify_argv(binary, env), timeout=VERIFY_TIMEOUT, env=_env(home, binary), label=label)
    except AiSyncError as error:
        return None, str(error)
    parsed = parse_verify(out) if code in (0, 1) else None
    if parsed is not None:
        return parsed, None
    first = next((line for line in (err + out).splitlines() if line.strip()), "")
    return None, f"{label} exited {code} without a verify result: {redact(first)}"


def enforce(envs: list[str], *, binary: str, home: Path) -> dict:
    results: dict[str, dict] = {}
    for env in envs:
        pulled, pull_error = _pull(binary, env, home)
        verified, verify_error = _verify(binary, env, home)
        entry: dict = {"pull_ok": pulled, "verify_ok": verified.ok if verified else False}
        if verified is not None:
            entry.update(errors=verified.errors_count, warnings=verified.warnings_count,
                         error_sample=verified.error_sample)
        problems = [p for p in (pull_error, verify_error) if p]
        if problems:
            entry["error"] = "; ".join(problems)[:400]
        results[env] = entry
    return {"ok": all(r["pull_ok"] and r["verify_ok"] for r in results.values()), "envs": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", action="append", choices=ENV_ALLOWLIST, dest="envs",
                        help="environment to enforce (repeatable; default: claude)")
    parser.add_argument("--ai-sync-bin", default=None, help="path to the ai-sync executable (default: search PATH)")
    parser.add_argument("--home", default=None, help="home directory (default: the user's)")
    args = parser.parse_args(argv)
    home = Path(args.home) if args.home else Path.home()
    envs = list(dict.fromkeys(args.envs or ["claude"]))

    if (home / PUBLISHER_MARKER_REL).exists():
        print("this host is the ai-sync publisher; refusing to force-pull over its own files", file=sys.stderr)
        return EXIT_REFUSED_PUBLISHER
    binary = resolve_binary(args.ai_sync_bin, home)
    if binary is None:
        print("ai-sync is not installed", file=sys.stderr)
        return EXIT_NOT_INSTALLED

    lock_path = home / LOCK_REL
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(json.dumps({"ok": True, "skipped": "another enforcement run is in progress"}))
            return 0
        summary = enforce(envs, binary=binary, home=home)
    print(json.dumps(summary))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
