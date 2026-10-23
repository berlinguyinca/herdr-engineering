#!/usr/bin/env python3
"""Room secrets on the host that runs helpers: one 0600 JSON file per room.

The collab service and the room profile only ever hold secret NAMES; the values live here, on the runner's
host, and are read by collab-spawn.py when it starts a helper in that room:
  * a header value for a service: {name: {"value": "Bearer sk-...", "host": "api.example.com"}}. The egress
    proxy adds it as the header of a service in the room profile that names it, and ONLY when that service's
    upstream_url is on the host bound here: the room owner (maybe a coworker) sets upstream_url, so binding the
    host is what stops a repointed service from sending the key elsewhere. The helper never sees the value.
  * a key file: {name: {"ssh_b64": "<base64>"}} (--ssh, or --base64). A name listed in the profile's
    ssh_secret_names is copied into the helper's ~/.ssh as a file of that name. Only this shape is ever a key file.
  * an old plain string (stored before these shapes existed) is neither: the runner ignores it and says so; set it
    again with --host or --ssh.

Usage:
  collab-secrets.py set ROOM NAME --host HOST   a header value, bound to HOST (a host name or IP, no port)
  collab-secrets.py set ROOM NAME --ssh         a key file's bytes (cat key | ... --ssh; --base64 is the same)
  collab-secrets.py list ROOM                   names (and bound hosts), never values
  collab-secrets.py rm ROOM NAME
The value is read from stdin (a terminal prompt does not echo), never printed and never taken from an argument.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import ipaddress
import json
import os
import re
import sys
from pathlib import Path

ROOM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)       # the room profile's secret-name rule
MAX_VALUE = 64 * 1024
HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))*\Z", re.ASCII)
NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*", re.ASCII)


def canon_host(text: str) -> str | None:
    """The bound host in the form the egress proxy compares (lowercase, one trailing dot dropped, IP literals
    canonical), or None when it is not a plain host name or IP address."""
    h = text.strip().lower()
    h = h[:-1] if h.endswith(".") else h
    try:
        return str(ipaddress.ip_address(h[1:-1] if h.startswith("[") and h.endswith("]") else h))
    except ValueError:
        pass
    if len(h) > 253 or not HOSTNAME.match(h) or NUMERIC_LABEL.fullmatch(h.rsplit(".", 1)[-1]):
        return None
    return h


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
    kind = s.add_mutually_exclusive_group(required=True)
    kind.add_argument("--host", help="a header value for a service on this host only (no port, no scheme)")
    kind.add_argument("--ssh", "--base64", dest="ssh", action="store_true",
                      help="an ssh key file: stdin's bytes, stored base64-encoded")
    sub.add_parser("list").add_argument("room")
    r = sub.add_parser("rm")
    r.add_argument("room")
    r.add_argument("name")
    args = ap.parse_args(argv)
    if not ROOM.match(args.room):
        ap.error("invalid room id")
    if args.cmd in ("set", "rm") and not NAME.match(args.name):
        ap.error("invalid secret name")
    host = None
    if args.cmd == "set" and args.host is not None:
        host = canon_host(args.host)
        if host is None:
            ap.error("--host must be a plain host name or IP address (no scheme, port or path)")
    d = Path(args.dir)
    os.makedirs(d, mode=0o700, exist_ok=True)
    if d.is_symlink() or not d.is_dir():
        raise SystemExit("the secrets directory is not a directory")
    os.chmod(d, 0o700)                       # mkdir's mode is subject to the umask
    path = d / f"{args.room}.json"
    data = load(path)
    if args.cmd == "list":
        for k in sorted(data):
            v = data[k]
            if isinstance(v, dict) and isinstance(v.get("host"), str):
                print(f"{k} host={v['host']}")
            elif isinstance(v, dict) and isinstance(v.get("ssh_b64"), str):
                print(f"{k} ssh key")
            else:
                print(f"{k} (old format, not used: set it again with --host HOST for a service or --ssh for a key)")
        return 0
    if args.cmd == "rm":
        if args.name not in data:
            print(f"{args.name}: not set", file=sys.stderr)
            return 1
        del data[args.name]
        save(d, path, data)
        return 0
    value = read_value(args.ssh)
    if not value:
        raise SystemExit("empty value; nothing stored")
    data[args.name] = {"value": value, "host": host} if host is not None else {"ssh_b64": value}
    save(d, path, data)
    print(f"stored {args.name} for {args.room}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
