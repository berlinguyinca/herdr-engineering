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
REQUEST_DEADLINE = 480.0              # seconds one request may take; the systemd unit allows 10 minutes
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
HOST = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
SSH_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,126}$")
# The task text arrives on stdin; the family's own non-interactive mode answers it. A task is written by a room
# member (or an agent they started), so by default every harness runs WITHOUT tools: it can answer, but it
# cannot run commands, read the credential files of the other agents on this host, or reach connectors/MCP
# servers configured for your account. `--allow-tools` lifts that for people who want agents that do real work.
#   claude: --tools "" drops the built-ins; --strict-mcp-config with an empty config drops claude.ai connectors
#           (Gmail, Drive...) that `claude -p` would otherwise load; slash commands and skills are off too.
#   pi:     takes its prompt as an argument, never stdin; "--" ends its options and the fixed prefix means a task
#           that starts with "@" or "-" can't be read as a file attachment or a flag.
#   codex:  has no way to be run without file access (even read-only it can read any file), so it is only
#           started with --allow-tools, and then with `--skip-git-repo-check` (a worker's cwd is not a repo).
FAMILY_CMD_TEXT_ONLY = {
    "claude-code": "claude -p --tools '' --strict-mcp-config --mcp-config '{\"mcpServers\":{}}' --disable-slash-commands",
    "pi": 'pi -p --no-session --no-tools -- "Task from a room member: $(cat)"',
    "agent": "",                     # no command: the worker acknowledges and reports
}
FAMILY_CMD_TOOLS = {
    "claude-code": "claude -p",
    "codex": "codex exec --skip-git-repo-check -",
    "pi": 'pi -p --no-session -- "Task from a room member: $(cat)"',
    "agent": "",
}
FAMILY_CMD = FAMILY_CMD_TOOLS       # every family the runner knows; which are started depends on --allow-tools
FAMILY_BIN = {"claude-code": "claude", "codex": "codex", "pi": "pi"}
AGENT_DIR = "herdr-agents"


class Failure(Exception):
    pass


def trim(text: str, n: int = 200) -> str:
    return " ".join(str(text).split())[:n]


class Runner:
    def __init__(self, url: str, state_dir: Path, herdr: str, ssh: str, worker: Path, host_id: str, say=print,
                 allow_tools: bool = False):
        self.url, self.state, self.herdr, self.ssh, self.say = url.rstrip("/"), state_dir, herdr, ssh, say
        self.worker_text = worker.read_text(encoding="utf-8")
        self.host_id = host_id
        self.targets: dict[str, str | None] = {}          # host label -> ssh target (None = this machine)
        self.public_url = ""
        self.cmds = FAMILY_CMD_TOOLS if allow_tools else FAMILY_CMD_TEXT_ONLY
        self.families: dict[str, list[str]] = {}          # host label -> harness families it can start

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
        except (OSError, ValueError) as exc:                                  # URLError, timeouts, bad JSON
            raise Failure(f"{method} {path}: {trim(exc)}") from None

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
            machines = json.loads(self.run([self.herdr, "machine", "list", "--json"], timeout=20))
        except (Failure, ValueError) as exc:
            self.say(f"herdr machine list failed: {trim(exc)}")
            return
        for m in machines if isinstance(machines, list) else []:
            label, target = (m.get("label"), m.get("target")) if isinstance(m, dict) else (None, None)
            if (m.get("enabled") is True and isinstance(label, str) and isinstance(target, str)
                    and HOST.fullmatch(label) and SSH_TARGET.fullmatch(target)):
                self.targets.setdefault(label, target)      # the ssh target herdr itself uses for this machine

    def reachable(self) -> list[str]:
        """Probe each machine at most once a minute (a probe is an ssh round trip)."""
        cache_file = self.state / "hosts.json"
        try:
            cache = json.loads(cache_file.read_text())
        except (OSError, ValueError):
            cache = {}
        now, up = time.time(), []
        self.families = {}
        for host, target in self.targets.items():
            seen = cache.get(host)
            if seen and now - seen["at"] < PROBE_EVERY:
                alive = seen["ok"]
            else:
                alive = True
                if target is not None:
                    try:
                        self.run(self.herdr_cmd(host, ["workspace", "list"]), timeout=15)
                    except Failure:
                        alive = False
                cache[host] = seen = {"ok": alive, "at": now,
                                      "families": self.installed_families(target) if alive else []}
            if alive:
                up.append(host)
                self.families[host] = [f for f in seen.get("families", []) if f in FAMILY_BIN]
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_file.write_text(json.dumps(cache))
        return up

    def installed_families(self, target: str | None) -> list[str]:
        """Which harness CLIs the host's own (interactive) shell can start: that shell is what a herdr pane
        runs. A host without `claude` must not be handed claude agents that would fail every task."""
        probe = "for c in claude codex pi; do command -v $c >/dev/null 2>&1 && echo $c; done"
        argv = (["bash", "-lic", probe] if target is None
                else [self.ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", target, f"bash -lic '{probe}' 2>/dev/null"])
        try:
            out = self.run(argv, timeout=30)
        except Failure:
            return []
        have = {line.strip() for line in out.splitlines()}
        return [f for f, binary in FAMILY_BIN.items() if binary in have]

    # ------------------------------------------------------------ placing one agent
    def put(self, host: str, name: str, text: str, mode: int) -> None:
        """Write ~/herdr-agents/NAME on the host. The text goes over stdin, never into an argument."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,40}", name):
            raise Failure("bad file name")
        target = self.targets.get(host)
        if target is None:
            d = Path.home() / AGENT_DIR
            d.mkdir(mode=0o700, exist_ok=True)
            d.chmod(0o700)
            tmp = d / f".{name}.{os.getpid()}.{os.urandom(4).hex()}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            try:
                os.fchmod(fd, mode)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.replace(tmp, d / name)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            return
        script = (f'umask 077; mkdir -p "$HOME/{AGENT_DIR}" && chmod 700 "$HOME/{AGENT_DIR}" && cat > "$HOME/{AGENT_DIR}/.{name}.new" && '
                  f'chmod {mode:o} "$HOME/{AGENT_DIR}/.{name}.new" && mv -f "$HOME/{AGENT_DIR}/.{name}.new" "$HOME/{AGENT_DIR}/{name}"')
        self.run([self.ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", target, script], stdin=text, timeout=45)

    def place(self, host: str, agent: dict, family: str, installed: set) -> None:
        name = agent["name"]
        if not NAME.fullmatch(name) or family not in self.cmds:
            raise Failure("refused: unexpected agent name or family")
        if host not in installed:
            base = "'" + self.public_url.replace("'", "'\\''") + "'"      # always single-quoted, like the dashboard's copy
            self.put(host, "worker.sh", self.worker_text.replace("'__HERDR_BASE__'", base), 0o700)
            installed.add(host)
        self.put(host, f"{name}.json", json.dumps({k: agent[k] for k in ("agent_id", "name", "credential", "device_id", "expires_at")}) + "\n", 0o600)
        try:
            pane = json.loads(self.run(self.herdr_cmd(host, ["workspace", "create", "--label", f"collab {name}", "--no-focus"]),
                                       timeout=30))["result"]["root_pane"]["pane_id"]
        except (ValueError, KeyError, TypeError):
            raise Failure("herdr returned an unexpected reply to workspace create") from None
        if not isinstance(pane, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,39}", pane):
            raise Failure("herdr returned an unexpected pane id")
        cmd = self.cmds[family]
        line = (f'{"HERDR_AGENT_CMD=" + shlex.quote(cmd) + " " if cmd else ""}"$HOME/{AGENT_DIR}/worker.sh" '
                f'"$HOME/{AGENT_DIR}/{name}.json"')
        self.run(self.herdr_cmd(host, ["pane", "run", pane, line]), timeout=30)
        try:        # tie the agent to its pane so the dashboard can show it with the fabric session
            self.ok("POST", f"agents/{agent['agent_id']}/link", {"instance_id": f"pane:{host}:{pane}"})
        except Exception:
            pass    # cosmetic: the agent works without the link

    # ------------------------------------------------------------ one request
    def pick_host(self, eligible: list[str]) -> str | None:
        """Next host in the rotation that still accepts automatic agents. The flags are read again for every
        agent, so un-ticking a host stops placements at once, not at the next cycle. The rotation carries on
        where the last placement stopped."""
        flags = {h["host"]: h["auto_agents"] for h in self.ok("GET", "fleet/hosts")["hosts"]}
        live = [h for h in eligible if flags.get(h, True)]
        if not live:
            return None
        cursor_file = self.state / "rr"
        try:
            cursor = int(cursor_file.read_text())
        except (OSError, ValueError):
            cursor = 0
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        cursor_file.write_text(str((cursor + 1) % 1_000_000))
        return live[cursor % len(live)]

    def forget(self, host: str, agent: dict) -> None:
        """An agent that could not be started must not stay alive with a credential nobody holds."""
        try:
            self.ok("POST", f"agents/{agent['agent_id']}/revoke", {})
        except (Failure, KeyError):
            pass
        try:
            target = self.targets.get(host)
            if target is None:
                (Path.home() / AGENT_DIR / f"{agent['name']}.json").unlink(missing_ok=True)
            elif NAME.fullmatch(agent["name"]):
                self.run([self.ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", target,
                          f'rm -f "$HOME/{AGENT_DIR}/{agent["name"]}.json"'], timeout=20)
        except (Failure, KeyError, OSError):
            pass

    def handle(self, spec: dict, eligible: list[str]) -> None:
        sid = spec["spawn_id"]
        placements, error = [], None
        deadline = time.monotonic() + REQUEST_DEADLINE
        try:
            if not eligible:
                raise Failure("no host accepts automatic agents right now")
            count = int(spec["count"])
            if not 1 <= count <= MAX_AGENTS or spec["harness_type"] not in FAMILY_CMD:
                raise Failure("refused: request outside the runner's limits")
            if spec["harness_type"] not in self.cmds:
                raise Failure(f"{spec['harness_type']} agents are only started when the runner runs with --allow-tools: "
                              "that CLI cannot be limited to text-only answers")
            eligible = [h for h in eligible if spec["harness_type"] not in FAMILY_BIN
                        or spec["harness_type"] in self.families.get(h, [])]
            if not eligible:
                raise Failure(f"no host that accepts automatic agents has {FAMILY_BIN[spec['harness_type']]} installed")
            body = {"harness_type": spec["harness_type"], "name_prefix": spec["name_prefix"], "count": count}
            if spec.get("room_id"):
                body.update(room_id=spec["room_id"], actions=spec["actions"],
                            **({"expires_at": spec["expires_at"]} if spec.get("expires_at") else {}))
            made = self.ok("POST", "agents/bulk", body)
            if made.get("error"):
                error = trim(made["error"])
            installed: set = set()
            for agent in made["agents"]:
                host = "-"
                try:
                    if time.monotonic() > deadline:
                        raise Failure("ran out of time")
                    host = self.pick_host(eligible) or ""
                    if not host:
                        raise Failure("every host opted out meanwhile")
                    self.place(host, agent, spec["harness_type"], installed)
                    placements.append({"name": agent["name"], "host": host, "status": "started", "error": None})
                except Exception as exc:                          # noqa: BLE001  (one agent must not strand the rest)
                    self.forget(host, agent)
                    placements.append({"name": str(agent.get("name", "?"))[:32], "host": host if HOST.fullmatch(host) else "none",
                                       "status": "failed", "error": trim(exc)})
        except Exception as exc:                                  # noqa: BLE001
            error = trim(exc)
        finally:
            try:
                self.ok("POST", f"fleet/spawns/{sid}/done", {"placements": placements, **({"error": error} if error else {})})
            except Failure as exc:
                self.say(f"spawn {sid}: could not report the result: {exc}")
        self.say(f"spawn {sid}: {sum(p['status'] == 'started' for p in placements)}/{len(placements)} started"
                 + (f" ({error})" if error else ""))

    # ------------------------------------------------------------ one cycle
    def once(self) -> None:
        self.public_url = (self.ok("GET", "config") or {}).get("public_url", "")
        if not self.public_url.startswith("https://"):
            raise Failure("the service has no public https url to give agents")
        self.discover()
        up = self.reachable()
        self.ok("POST", "fleet/hosts", {"hosts": [{"host": h, "kind": "local" if self.targets[h] is None else "machine",
                                                    "families": self.families.get(h, [])} for h in up]})
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
    ap.add_argument("--allow-tools", action="store_true",
                    help="let agents use their harness's tools (shell, files, MCP) on task text from room members")
    args = ap.parse_args(argv)
    if not args.url or not HOST.fullmatch(args.host_id):
        ap.error("no dashboard URL (pass --url) or an invalid --host-id")
    r = Runner(args.url, Path(args.state_dir), args.herdr, args.ssh, Path(args.worker), args.host_id,
               say=lambda m: print(f"collab-spawn: {m}", file=sys.stderr), allow_tools=args.allow_tools)
    try:
        r.once()
    except Failure as exc:
        print(f"collab-spawn: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
