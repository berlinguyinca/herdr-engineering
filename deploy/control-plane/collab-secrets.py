#!/usr/bin/env python3
"""Room secrets on the host that runs helpers: one 0600 JSON file per room, {name: value}.

The collab service and the room profile only ever hold secret NAMES; the values live here, on the runner's
host, and are read by collab-spawn.py when it starts a helper in that room:
  * a name bound to a service in the room profile is handed to the egress proxy, which adds it as that
    service's header value (store the whole value, e.g. "Bearer sk-..."); the helper never sees it
  * a name listed in the profile's ssh_secret_names is copied into the helper's ~/.ssh as a file of that
    name: store it with --base64 (the file's bytes, base64-encoded)

Usage:
  collab-secrets.py set ROOM NAME [--base64]   the value is read from stdin (a terminal prompt does not echo)
  collab-secrets.py list ROOM                  names only, never values
  collab-secrets.py rm ROOM NAME
The value is never printed and never taken from an argument.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import sys
from pathlib import Path

ROOM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)       # the room profile's secret-name rule
MAX_VALUE = 64 * 1024


def default_dir() -> Path:
    return Path.home() / ".config" / "herdr-engineering" / "collab-secrets"


def load(path: Path) -> dict:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {}
    if not os.path.isfile(path) or os.path.islink(path) or st.st_uid != os.getuid():
        raise SystemExit(f"{path.name}: not a regular file of yours; refusing to touch it")
    d = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(d, dict):
        raise SystemExit(f"{path.name}: not a JSON object; refusing to touch it")
    return d


def save(d: Path, path: Path, data: dict) -> None:
    tmp = d / f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_value(b64: bool) -> str:
    if sys.stdin.isatty():
        raw = getpass.getpass("value (not echoed): ").encode()
    else:
        raw = sys.stdin.buffer.read(MAX_VALUE + 1)
    if len(raw) > MAX_VALUE:
        raise SystemExit("the value is too long")
    if b64:
        return base64.b64encode(raw).decode()
    value = raw.decode("utf-8")
    value = value[:-1] if value.endswith("\n") else value
    if value.endswith("\r"):
        value = value[:-1]
    if any(c in value for c in "\r\n\0"):
        raise SystemExit("a header value may not contain line breaks (use --base64 for a key file)")
    return value


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="collab-secrets.py", description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=str(default_dir()))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set")
    s.add_argument("room")
    s.add_argument("name")
    s.add_argument("--base64", action="store_true", help="store stdin's bytes base64-encoded (ssh key files)")
    sub.add_parser("list").add_argument("room")
    r = sub.add_parser("rm")
    r.add_argument("room")
    r.add_argument("name")
    args = ap.parse_args(argv)
    if not ROOM.match(args.room):
        ap.error("invalid room id")
    if args.cmd in ("set", "rm") and not NAME.match(args.name):
        ap.error("invalid secret name")
    d = Path(args.dir)
    os.makedirs(d, mode=0o700, exist_ok=True)
    if d.is_symlink() or not d.is_dir():
        raise SystemExit("the secrets directory is not a directory")
    os.chmod(d, 0o700)                       # mkdir's mode is subject to the umask
    path = d / f"{args.room}.json"
    data = load(path)
    if args.cmd == "list":
        for k in sorted(data):
            print(k)
        return 0
    if args.cmd == "rm":
        if args.name not in data:
            print(f"{args.name}: not set", file=sys.stderr)
            return 1
        del data[args.name]
        save(d, path, data)
        return 0
    value = read_value(args.base64)
    if not value:
        raise SystemExit("empty value; nothing stored")
    data[args.name] = value
    save(d, path, data)
    print(f"stored {args.name} for {args.room}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
