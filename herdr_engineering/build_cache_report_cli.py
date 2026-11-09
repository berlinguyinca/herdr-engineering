"""``herdr-eng build-cache-report``: scheduled, opt-in push of this host's builds to the control plane.

Config is YAML only (``fabric_build_cache.report``); the ingest-role key is read from ``key_file`` on every attempt and
is never accepted on argv, logged, or placed in a unit file. Exit codes: 0 sent, 1 transport/server failure (a loop
keeps going), 2 refused to run (disabled or unsafe configuration).
"""
from __future__ import annotations

import json
import sys
import time
from typing import Callable
from urllib.parse import urlsplit

from .build_cache_config import ReportSection, check_credentials_file, validate
from .build_cache_report import report_builds
from .errors import InvalidError

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_PREFIX = "build-cache-report:"


def register(sub) -> None:
    p = sub.add_parser("build-cache-report",
                       help="push this host's recent builds to the control plane (opt-in; "
                            "fabric_build_cache.report in config.yaml)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="send one report and exit 0/1 (default)")
    mode.add_argument("--loop", action="store_true", help="repeat every report.interval_s; survives failures")
    p.add_argument("--json", action="store_true", help="print the server's answer")
    p.add_argument("--config", default=None, help="config.yaml to read instead of the discovered files")
    p.add_argument("--state-dir", default=None, help="directory holding builds.jsonl (default: the build gate's)")
    p.add_argument("--max-iterations", type=int, default=None, help="stop a --loop after N attempts (tests)")
    p.set_defaults(func=_cmd)


def _cmd(args) -> int:
    return run(args)


def _refuse(msg: str) -> int:
    print(f"{_PREFIX} refused: {msg}", file=sys.stderr)
    return EXIT_REFUSED


def _check_url(url: str | None) -> str:
    if not url:
        raise InvalidError("fabric_build_cache.report.url is not set")
    try:
        u = urlsplit(url)
        host = u.hostname
        u.port  # noqa: B018  (validates the port)
    except ValueError:
        raise InvalidError("fabric_build_cache.report.url is not a valid URL") from None
    if u.username or u.password or u.query or u.fragment or not host:
        raise InvalidError("fabric_build_cache.report.url must be a plain base URL (no credentials, query or fragment)")
    if u.scheme == "https" or (u.scheme == "http" and host in _LOOPBACK):
        return url
    raise InvalidError("fabric_build_cache.report.url must be https (plain http only for 127.0.0.1/localhost)")


def _read_key(key_file: str | None) -> str:
    if not key_file:
        raise InvalidError("fabric_build_cache.report.key_file is not set")
    path = check_credentials_file(key_file, "fabric_build_cache.report.key_file")
    try:
        key = path.read_text().strip()
    except OSError as exc:
        raise InvalidError(f"fabric_build_cache.report.key_file is not readable: {exc.strerror}") from None
    if not key:
        raise InvalidError("fabric_build_cache.report.key_file is empty")
    return key


def _load(args) -> ReportSection:
    from pathlib import Path
    from .config import load_config
    cfg = load_config([Path(args.config)] if args.config else None, strict_build_cache=True)
    rep = validate(cfg.get("fabric_build_cache")).report
    if not rep.enabled:
        raise InvalidError("build-cache reporting is disabled; set fabric_build_cache.report.enabled: true "
                           "(with url and key_file) in config.yaml to opt in")
    _check_url(rep.url)
    _read_key(rep.key_file)        # fail fast at start; re-read on every attempt
    return rep


def _attempt(rep: ReportSection, state_dir, as_json: bool) -> bool:
    try:
        out = report_builds(state_dir, rep.url, _read_key(rep.key_file), host_id=rep.host_id)
    except InvalidError as exc:
        print(f"{_PREFIX} failed: {exc}", file=sys.stderr)
        return False
    except (OSError, ValueError) as exc:     # URLError/timeout/HTTP refusal are OSError; bad JSON is ValueError
        print(f"{_PREFIX} failed: {_clean(exc)}", file=sys.stderr)
        return False
    print(f"{_PREFIX} ok accepted={out.get('accepted')} new={out.get('new')} host_id={out.get('host_id')}",
          file=sys.stderr)
    if as_json:
        print(json.dumps(out, sort_keys=True))
    return True


def _clean(exc: Exception) -> str:
    return " ".join(str(exc).split())[:200] or exc.__class__.__name__


def run(args, sleep: Callable[[float], None] = time.sleep) -> int:
    try:
        rep = _load(args)
    except (InvalidError, ValueError) as exc:
        return _refuse(str(exc))
    from pathlib import Path
    if args.state_dir:
        state = Path(args.state_dir)
    else:
        from .build_coord import _default_state_dir
        state = _default_state_dir()
    if not args.loop:
        return EXIT_OK if _attempt(rep, state, args.json) else EXIT_FAILED
    n, ok = 0, True
    while args.max_iterations is None or n < args.max_iterations:
        if n:
            sleep(rep.interval_s)
        n += 1
        ok = _attempt(rep, state, args.json)
    return EXIT_OK if ok else EXIT_FAILED
