#!/usr/bin/env python3
"""Start fleets of collaboration agents on the owner's own hosts, over herdr.

Runs on the HOST (a systemd timer, every few seconds), never in a container: the internet-facing
collab service has no way to start anything, it only records what the dashboard owner asked for.
Each cycle this script:

  1. finds the hosts it can reach (this machine, plus the machines `herdr machine list` knows) and
     tells the collab service which ones are up;
  2. asks the service which of them may receive automatic agents (the owner can opt a host out);
  3. claims each waiting fleet request exactly once, creates the agents through the owner's own
     operator API, then for each agent, round robin across the eligible hosts, writes its credential
     to a 0600 file on that host and starts a worker in a fresh herdr workspace there. herdr keeps
     the pane alive, so the worker survives this script, the ssh session and the dashboard.

Safety rails:
  * credentials travel in a file written over the ssh stdin (never in an argument list, never in a log)
  * every name that reaches a command line is re-validated here; commands are argument lists, and
    the only strings handed to a remote shell are fixed templates with those validated names
  * the harness command per family is fixed in this file; request text never becomes a command
  * at most MAX_AGENTS per request; the service caps open requests and agents per user
  * no environment: flags and host.json only
Usage: collab-spawn.py [once] [--url URL] [--state-dir DIR] [--herdr BIN] [--ssh BIN] [--worker FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MAX_AGENTS = 20
PROBE_EVERY = 60.0
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
HOST = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
SSH_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,126}$")
FAMILY_CMD = {                       # task text arrives on stdin; the family's own non-interactive mode answers it
    "claude-code": "claude -p",
    "codex": "codex exec -",
    "pi": "pi -p",
    "agent": "",                     # no command: the worker acknowledges and reports
}
AGENT_DIR = "herdr-agents"


class Failure(Exception):
    pass


def trim(text: str, n: int = 200) -> str:
    return " ".join(str(text).split())[:n]


class Runner:
    def __init__(self, url: str, state_dir: Path, herdr: str, ssh: str, worker: Path, host_id: str, say=print):
        self.url, self.state, self.herdr, self.ssh, self.say = url.rstrip("/"), state_dir, herdr, ssh, say
        self.worker_text = worker.read_text(encoding="utf-8")
        self.host_id = host_id
        self.targets: dict[str, str | None] = {}          # host label -> ssh target (None = this machine)
        self.public_url = ""

    # ------------------------------------------------------------ the collab service (operator listener)
    def api(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.url}/api/v1/collab/{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"} if method == "POST" else {})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:             # noqa: S310 (operator-configured URL)
                return resp.status, json.loads(resp.read(4_000_000) or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read(100_000) or b"{}")
            except ValueError:
                return exc.code, {}

    def ok(self, method: str, path: str, body: dict | None = None) -> dict:
        status, out = self.api(method, path, body)
        if status >= 400:
            raise Failure(f"{method} {path}: {status} {trim(out.get('error', ''))}")
        return out

    # ------------------------------------------------------------ processes
    def run(self, argv: list[str], stdin: str | None = None, timeout: float = 60.0) -> str:
        try:
            p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Failure(f"{Path(argv[0]).name}: {trim(exc)}") from None
        if p.returncode != 0:
            raise Failure(f"{Path(argv[0]).name} exited {p.returncode}: {trim(p.stderr or p.stdout)}")
        return p.stdout

    def herdr_cmd(self, host: str, args: list[str]) -> list[str]:
        return [self.herdr, *(["--machine", host] if self.targets.get(host) else []), *args]

    # ------------------------------------------------------------ hosts
    def discover(self) -> None:
        """Local machine plus every enabled herdr machine; labels are re-validated, never trusted."""
        self.targets = {self.host_id: None}
        try:
            listing = self.run([self.herdr, "machine", "list"], timeout=20)
        except Failure as exc:
            self.say(f"herdr machine list failed: {exc}")
            return
        for line in listing.splitlines():
            f = line.split()
            if len(f) >= 3 and f[-1] == "enabled" and HOST.fullmatch(f[1]) and SSH_TARGET.fullmatch(f[2]):
                self.targets.setdefault(f[1], f[2])

    def reachable(self) -> list[str]:
        """Probe each machine at most once a minute (a probe is an ssh round trip)."""
        cache_file = self.state / "hosts.json"
        try:
            cache = json.loads(cache_file.read_text())
        except (OSError, ValueError):
            cache = {}
        now, up = time.time(), []
        for host, target in self.targets.items():
            seen = cache.get(host)
            if target is None:
                alive = True
            elif seen and now - seen["at"] < PROBE_EVERY:
                alive = seen["ok"]
            else:
                try:
                    self.run(self.herdr_cmd(host, ["workspace", "list"]), timeout=15)
                    alive = True
                except Failure:
                    alive = False
                cache[host] = {"ok": alive, "at": now}
            if alive:
                up.append(host)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_file.write_text(json.dumps(cache))
        return up

    # ------------------------------------------------------------ placing one agent
    def put(self, host: str, name: str, text: str, mode: int) -> None:
        """Write ~/herdr-agents/NAME on the host. The text goes over stdin, never into an argument."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,40}", name):
            raise Failure("bad file name")
        target = self.targets.get(host)
        if target is None:
            d = Path.home() / AGENT_DIR
            d.mkdir(mode=0o700, exist_ok=True)
            tmp = d / f".{name}.{os.getpid()}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, d / name)
            return
        script = (f'umask 077; mkdir -p "$HOME/{AGENT_DIR}" && cat > "$HOME/{AGENT_DIR}/.{name}.new" && '
                  f'chmod {mode:o} "$HOME/{AGENT_DIR}/.{name}.new" && mv -f "$HOME/{AGENT_DIR}/.{name}.new" "$HOME/{AGENT_DIR}/{name}"')
        self.run([self.ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", target, script], stdin=text, timeout=45)

    def place(self, host: str, agent: dict, family: str, installed: set) -> None:
        name = agent["name"]
        if not NAME.fullmatch(name) or family not in FAMILY_CMD:
            raise Failure("refused: unexpected agent name or family")
        if host not in installed:
            base = "'" + self.public_url.replace("'", "'\\''") + "'"      # always single-quoted, like the dashboard's copy
            self.put(host, "worker.sh", self.worker_text.replace("'__HERDR_BASE__'", base), 0o700)
            installed.add(host)
        self.put(host, f"{name}.json", json.dumps({k: agent[k] for k in ("agent_id", "name", "credential", "device_id", "expires_at")}) + "\n", 0o600)
        out = json.loads(self.run(self.herdr_cmd(host, ["workspace", "create", "--label", f"collab {name}", "--no-focus"]), timeout=30))
        pane = out["result"]["root_pane"]["pane_id"]
        if not re.fullmatch(r"[A-Za-z0-9:_.-]{1,40}", pane):
            raise Failure("herdr returned an unexpected pane id")
        cmd = FAMILY_CMD[family]
        line = (f'{"HERDR_AGENT_CMD=" + shlex.quote(cmd) + " " if cmd else ""}"$HOME/{AGENT_DIR}/worker.sh" '
                f'"$HOME/{AGENT_DIR}/{name}.json"')
        self.run(self.herdr_cmd(host, ["pane", "run", pane, line]), timeout=30)

    # ------------------------------------------------------------ one request
    def next_hosts(self, eligible: list[str], count: int) -> list[str]:
        """Round robin that carries on where the last request stopped."""
        cursor_file = self.state / "rr"
        try:
            cursor = int(cursor_file.read_text())
        except (OSError, ValueError):
            cursor = 0
        order = [eligible[(cursor + i) % len(eligible)] for i in range(count)]
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        cursor_file.write_text(str((cursor + count) % 1_000_000))
        return order

    def handle(self, spec: dict, eligible: list[str]) -> None:
        sid = spec["spawn_id"]
        placements, error = [], None
        try:
            if not eligible:
                raise Failure("no host accepts automatic agents right now")
            count = int(spec["count"])
            if not 1 <= count <= MAX_AGENTS or spec["harness_type"] not in FAMILY_CMD:
                raise Failure("refused: request outside the runner's limits")
            body = {"harness_type": spec["harness_type"], "name_prefix": spec["name_prefix"], "count": count}
            if spec.get("room_id"):
                body.update(room_id=spec["room_id"], actions=spec["actions"],
                            **({"expires_at": spec["expires_at"]} if spec.get("expires_at") else {}))
            made = self.ok("POST", "agents/bulk", body)
            if made.get("error"):
                error = trim(made["error"])
            hosts = self.next_hosts(eligible, len(made["agents"]))
            installed: set = set()
            for host, agent in zip(hosts, made["agents"]):
                try:
                    self.place(host, agent, spec["harness_type"], installed)
                    placements.append({"name": agent["name"], "host": host, "status": "started", "error": None})
                except (Failure, KeyError, ValueError) as exc:
                    placements.append({"name": agent["name"], "host": host, "status": "failed", "error": trim(exc)})
        except Failure as exc:
            error = trim(exc)
        self.ok("POST", f"fleet/spawns/{sid}/done", {"placements": placements, **({"error": error} if error else {})})
        self.say(f"spawn {sid}: {sum(p['status'] == 'started' for p in placements)}/{len(placements)} started"
                 + (f" ({error})" if error else ""))

    # ------------------------------------------------------------ one cycle
    def once(self) -> None:
        self.public_url = (self.ok("GET", "config") or {}).get("public_url", "")
        if not self.public_url.startswith("https://"):
            raise Failure("the service has no public https url to give agents")
        self.discover()
        up = self.reachable()
        self.ok("POST", "fleet/hosts", {"hosts": [{"host": h, "kind": "local" if self.targets[h] is None else "machine"} for h in up]})
        flags = {h["host"]: h["auto_agents"] for h in self.ok("GET", "fleet/hosts")["hosts"]}
        eligible = sorted(h for h in up if flags.get(h, True))
        for sp in self.ok("GET", "fleet/spawns?status=requested")["spawns"]:
            status, spec = self.api("POST", f"fleet/spawns/{sp['spawn_id']}/claim", {})
            if status == 200:
                self.handle(spec, eligible)
            elif status != 409:
                self.say(f"claim {sp['spawn_id']}: {status}")


def default_url() -> tuple[str, str]:
    p = Path.home() / ".config" / "herdr-engineering" / "host.json"
    try:
        d = json.loads(p.read_text())
        return str(d["control_plane_url"]), str(d.get("host_id") or "")
    except (OSError, ValueError, KeyError):
        return "", ""


def main(argv: list[str]) -> int:
    home = Path.home()
    url, hid = default_url()
    ap = argparse.ArgumentParser(prog="collab-spawn.py", description=__doc__.splitlines()[0])
    ap.add_argument("action", nargs="?", default="once", choices=["once"])
    ap.add_argument("--url", default=url, help="dashboard URL (default: control_plane_url from host.json)")
    ap.add_argument("--host-id", default=hid or socket.gethostname().split(".")[0].lower())
    ap.add_argument("--state-dir", default=str(home / ".local/state/herdr-engineering/collab-spawn"))
    ap.add_argument("--herdr", default="herdr")
    ap.add_argument("--ssh", default="ssh")
    ap.add_argument("--worker", default=str(Path(__file__).with_name("collab-worker.sh")))
    args = ap.parse_args(argv)
    if not args.url or not HOST.fullmatch(args.host_id):
        ap.error("no dashboard URL (pass --url) or an invalid --host-id")
    r = Runner(args.url, Path(args.state_dir), args.herdr, args.ssh, Path(args.worker), args.host_id,
               say=lambda m: print(f"collab-spawn: {m}", file=sys.stderr))
    try:
        r.once()
    except Failure as exc:
        print(f"collab-spawn: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
