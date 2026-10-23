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

Names. An agent's @name and the herdr labels of its pane (and, for an agent that has a workspace and tab to
itself, the workspace and tab) are kept equal in both directions. The last name both sides agreed on is kept
in names.json: a side that moved away from it is the side that changed; when both did, the web name wins.
A rename made in herdr is only taken if it is a valid @name that is free (otherwise herdr is set back).

Helpers (agents an agent starts with helper_spawn) run on THIS host only: each cycle the runner claims new ones once,
starts a detached supervisor (`collab-spawn.py helper-run DIR`, task on its stdin) that runs the harness in the box
with no network but its own egress proxy, watches running ones through their private state directories (stop on
pause, revoke, cancel or expiry), posts each result and removes its pane, work directory and state. Every ten
minutes it also posts the gateway's model list (--gateway) for the service's model picker.
Usage: collab-spawn.py [once] [--url URL] [--state-dir DIR] [--herdr BIN] [--ssh BIN] [--worker FILE] [--gateway URL]
"""
from __future__ import annotations

import argparse
import base64
import binascii
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
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
FAMILY_ROLE = {"claude-code": "claude", "codex": "codex", "pi": "pi", "agent": "agent"}
FAMILY_BIN = {"claude-code": "claude", "codex": "codex", "pi": "pi"}
AGENT_DIR = "herdr-agents"
PANE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,39}$")

# ---- helpers: agents that agents start (collab helper_spawn). The server mints each helper's agent when this
# runner claims it; the runner runs it HERE (this host only), boxed, with no network but the per-helper egress
# proxy, the task on stdin, and posts its stdout back as the result. Helpers run plain: no MCP wiring yet.
HELPER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
ROOM_ID = HELPER_ID
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")       # leading alnum: never read as a flag
SECRET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")          # the room profile's secret-name rule
SERVICE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
ALLOW_LINE = re.compile(r"[A-Za-z0-9.:\[\]_-]{1,260}")
HELPER_ROLES = ("reviewer", "debugger", "worker", "vision", "successor")
# `--model ID` goes right after these. pi only: catalogue ids are gateway models, which claude and codex can't serve.
MODEL_AFTER = {"pi": "pi"}
# Helpers only: each harness ignores user/project config (the box has none of the operator's anyway: see
# HELPER_LOGIN). Not --bare for claude: it turns off the OAuth login. Inserted right after the given prefix.
HELPER_ISOLATION = {"claude-code": ("claude -p", "--setting-sources ''"),
                    "codex": ("codex exec", "--ignore-user-config --ignore-rules"),
                    "pi": ("pi", "--no-extensions --no-skills --no-prompt-templates --no-context-files")}
# The only files a helper's harness gets from the operator's login: (family -> config dir env, default, files).
# pi's models.json holds the custom (gateway) providers its --model ids need.
HELPER_LOGIN = {"claude-code": ("claude", "CLAUDE_CONFIG_DIR", ".claude", (".credentials.json",)),
                "codex": ("codex", "CODEX_HOME", ".codex", ("auth.json",)),
                "pi": ("pi", "PI_CODING_AGENT_DIR", ".pi/agent", ("auth.json", "models.json"))}
HELPER_RESULT_CAP = 20_000              # bytes of stdout returned; the rest is cut with a marker
HELPER_CLAIM_STALE = 900.0              # a claim a previous process never started is failed after this long
HELPERS_PER_HOST = 6                    # live helpers this host runs at once
HELPER_MARGIN = 30.0                    # stop before the server's expiry, so the result is still accepted
MAX_EGRESS_ASKS = 20                    # host-access requests one helper may cause
HELPER_STDIN_MAX = 256_000
FINISH_BODY_MAX = 48 * 1024             # the service refuses bodies over 64 KiB (413): stay well below
FINISH_ATTEMPTS = 3                     # refused finishes before only {"status": "failed"} is posted
# what the service strips from a result anyway (all control characters but \t and \n, C1, U+2028/9): stripped here
# first, so the 20 KB budget measures what is sent and nothing is inflated into \u escapes
RESULT_BAD = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
CATALOGUE_EVERY = 600.0                 # seconds between model-catalogue posts
CATALOGUE_MAX = 500
ROLE_PROMPT = {
    "reviewer": "Review the work described in the task below. Report problems, most important first.",
    "debugger": "Find the cause of the problem described in the task below and say how to fix it.",
    "worker": "Do the job described in the task below and report what you did.",
    "vision": "Answer the task below about the image(s) it refers to.",
    "successor": "Continue the work from the handoff below.",
}


def proc_stat(pid) -> tuple[str, str] | None:
    """(state, start time) of a process, so a recorded pid is only ever signalled while it is still the same one."""
    try:
        rest = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return rest[0], rest[19]
    except (OSError, ValueError, IndexError, TypeError):
        return None


def group_alive(pgid: int) -> bool:
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                rest = (p / "stat").read_text().rsplit(")", 1)[1].split()
            except (OSError, IndexError):
                continue
            if rest[0] != "Z" and rest[2] == str(pgid):
                return True
    return False


def write_private(path: Path, data, mode: int = 0o600) -> None:
    """Atomic write of a file only its owner can read."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode() if isinstance(data, str) else data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def remove_tree(path: Path) -> None:
    """Delete a directory this runner made, even when the box left read-only directories in it. Links are never
    followed (and never chmod-ed: that would change their target)."""
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True) if path.is_symlink() else None
        return
    for root, dirs, _ in os.walk(path):
        for n in dirs:
            q = os.path.join(root, n)
            if not os.path.islink(q):
                try:
                    os.chmod(q, 0o700)
                except OSError:
                    pass
    shutil.rmtree(path, ignore_errors=True)


def normalize_catalogue(listing) -> list[dict]:
    """A gateway's model list (OpenAI `{"data": [...]}`, or a bare list) as the service's catalogue entries
    {id, family, vision, price_out_per_mtok, context, local}. Entries with an odd id, duplicates and anything past
    CATALOGUE_MAX are skipped; numbers out of range become None."""
    rows = listing.get("data", listing.get("models")) if isinstance(listing, dict) else listing
    out: list[dict] = []
    seen: set = set()

    def num(v, cap):
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and 0 <= v <= cap
        return v if ok else None

    for m in rows if isinstance(rows, list) else []:
        mid = m.get("id") if isinstance(m, dict) else None
        if not isinstance(mid, str) or not MODEL_ID.fullmatch(mid) or mid in seen:
            continue
        seen.add(mid)
        fam = m.get("family", m.get("owned_by"))
        caps = [str(x).lower() for k in ("capabilities", "modalities", "input_modalities")
                for x in (m.get(k) if isinstance(m.get(k), list) else [])]
        ctx = num(m.get("context", m.get("context_length", m.get("max_context"))), 1e9)
        price = num(m.get("price_out_per_mtok"), 1e6)
        out.append({"id": mid, "family": fam if isinstance(fam, str) and len(fam) <= 64 and fam.isprintable() else None,
                    "vision": m.get("vision") is True or "vision" in caps or "image" in caps,
                    "price_out_per_mtok": None if price is None else float(price),
                    "context": int(ctx) if isinstance(ctx, int) else None, "local": m.get("local") is True})
        if len(out) >= CATALOGUE_MAX:
            break
    return out


def helper_prompt(role: str, ro_binds: list[str]) -> str:
    lines = [f"You are a {role} helper, started by another agent in a collaboration room.", ROLE_PROMPT[role],
             "Your answer is what you print on standard output; it is returned to that agent (at most 20 KB).",
             "Your working directory is a fresh, private directory.",
             'You have no direct network access. Connections go through a proxy: a host that is not approved yet answers '
             '403 "access pending" and the operator is asked. Retry such a host a few times a minute at most, and say so '
             "in your answer if it stays blocked."]
    lines += [f"The requesting agent's work directory is visible read-only at {b}" for b in ro_binds]
    return "\n".join(lines) + "\n\n--- task ---\n"


def helper_run(d: Path) -> int:
    """`collab-spawn.py helper-run DIR`: the supervisor of one helper. The runner starts it in its own session and
    hands it the task on stdin; it starts the helper's egress proxy, runs the harness in the box with the task on
    ITS stdin until the deadline, keeps the first HELPER_RESULT_CAP bytes of stdout and writes result.json. It
    outlives the runner cycle that started it; later cycles read the result and clean up."""
    os.umask(0o077)
    spec = json.loads((d / "spec.json").read_text())
    task = sys.stdin.buffer.read(HELPER_STDIN_MAX)
    sys.stdin.close()
    res: dict = {"exit": None, "timed_out": False, "bytes": 0, "error": None, "stderr": ""}
    proxy = None
    try:
        sock = spec["sock"]
        with open(d / "proxy.err", "ab") as perr:
            proxy = subprocess.Popen([sys.executable, spec["egress"], "serve", "--sock", sock,
                                      "--allow-file", str(d / "allow.txt"), "--secrets-file", str(d / "secrets.json"),
                                      "--services-file", str(d / "services.json"), "--log", str(d / "blocked.jsonl")],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=perr)
        for _ in range(100):
            if (os.path.exists(sock) and stat.S_ISSOCK(os.stat(sock).st_mode)) or proxy.poll() is not None:
                break
            time.sleep(0.1)
        if proxy.poll() is not None or not os.path.exists(sock):
            res["error"] = "sandbox-error"
            return 1
        env = dict(os.environ)
        env.update(HERDR_EGRESS_SOCK=sock, HERDR_EGRESS_SERVICES=",".join(spec["services"]))
        if spec.get("ro_binds"):
            env["HERDR_SANDBOX_RO_BINDS"] = "\n".join(spec["ro_binds"])
        if spec.get("ssh_dir"):
            env["HERDR_SANDBOX_SSH_DIR"] = spec["ssh_dir"]
        env["HERDR_SANDBOX_HARNESS_DIR"] = spec["harness_dir"]       # never the operator's own harness config
        h = subprocess.Popen(["bash", spec["sandbox"], spec["work"], spec["cmd"]], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(d))

        def feed():
            try:
                h.stdin.write(task)
                h.stdin.close()
            except OSError:
                pass

        def keep_out():
            with open(d / "out", "wb") as fh:
                while chunk := h.stdout.read(65536):
                    room = HELPER_RESULT_CAP + 1 - res["bytes"]
                    if room > 0:
                        fh.write(chunk[:room])
                        fh.flush()
                    res["bytes"] += len(chunk)

        tail = bytearray()

        def keep_err():
            while chunk := h.stderr.read(65536):
                tail.extend(chunk)
                del tail[:-4000]

        threads = [threading.Thread(target=f, daemon=True) for f in (feed, keep_out, keep_err)]
        for t in threads:
            t.start()
        try:
            rc = h.wait(timeout=max(0.0, float(spec["deadline"]) - time.time()))
        except subprocess.TimeoutExpired:
            res["timed_out"] = True
            h.terminate()                   # the box (bwrap) dies, and with it everything inside it
            try:
                rc = h.wait(timeout=5)
            except subprocess.TimeoutExpired:
                h.kill()
                rc = h.wait()
        for t in threads[1:]:
            t.join(timeout=5)
        res["exit"] = rc
        res["stderr"] = tail.decode("utf-8", "replace")[-2000:]
        if rc == 126 and not res["timed_out"]:
            res["error"] = "sandbox-error"
        return 0
    finally:
        if proxy is not None and proxy.poll() is None:
            proxy.terminate()
            try:
                proxy.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proxy.kill()
        write_private(d / "result.json", json.dumps(res))


class Failure(Exception):
    pass


def label_name(raw) -> str | None:
    """A herdr label as an @name: lowercase letters, digits, hyphens; None when nothing usable is left."""
    if not isinstance(raw, str):
        return None
    name = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]", "-", raw.strip().lower())).strip("-")[:32].strip("-")
    return name or None


def link_target(instance_id) -> tuple[str, str] | None:
    """(host label, pane id) for the two link forms: `pane:HOST:PANE` (set by this runner) and a fabric
    session id `herdr:HOST:SERVER:PANE` (url-encoded pane), which the owner picks on the AI agents page."""
    if not isinstance(instance_id, str):
        return None
    if instance_id.startswith("pane:"):
        host, _, pane = instance_id[5:].partition(":")
    elif instance_id.startswith("herdr:") and instance_id.count(":") >= 3:
        _, host, _, pane = instance_id.split(":", 3)
        pane = urllib.parse.unquote(pane)
    else:
        return None
    return (host, pane) if HOST.fullmatch(host) and PANE_ID.fullmatch(pane) else None


def canon_host(host: str) -> str:
    """The egress proxy's (and collab-secrets.py's) comparison form: lowercase, one trailing dot dropped,
    canonical IP literals."""
    h = host.strip().lower()
    h = h[:-1] if h.endswith(".") else h
    try:
        return str(ipaddress.ip_address(h.strip("[]")))
    except ValueError:
        return h


def trim(text: str, n: int = 200) -> str:
    return " ".join(str(text).split())[:n]


class Runner:
    def __init__(self, url: str, state_dir: Path, herdr: str, ssh: str, worker: Path, host_id: str, say=print,
                 allow_tools: bool = False, sandbox: Path | None = None, gateway: str | None = None):
        self.url, self.state, self.herdr, self.ssh, self.say = url.rstrip("/"), state_dir, herdr, ssh, say
        self.worker_text = worker.read_text(encoding="utf-8")
        self.host_id = host_id
        self.targets: dict[str, str | None] = {}          # host label -> ssh target (None = this machine)
        self.public_url = ""
        self.cmds = FAMILY_CMD_TOOLS if allow_tools else FAMILY_CMD_TEXT_ONLY
        # collab-sandbox.sh: every task then runs in a bubblewrap box where only the agent's own work directory
        # is writable. With a sandbox, a host without a working bwrap is not offered any harness family.
        self.sandbox_text = sandbox.read_text(encoding="utf-8") if sandbox else None
        self.sandbox_path = sandbox.resolve() if sandbox else None
        self.allow_tools = allow_tools
        self.secrets_dir = Path.home() / ".config" / "herdr-engineering" / "collab-secrets"
        self._harness_path: str | None = None
        self.gateway = gateway or None              # the LLM gateway whose model list feeds the helper model picker
        self.families: dict[str, list[str]] = {}          # host label -> harness families it can start

    # ------------------------------------------------------------ the collab service (operator listener)
    def api(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8", "replace") if body is not None else None
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
        # One machine, one host: a second label for the same ssh target would be a way round "no automatic
        # agents", so only the first label (alphabetically) for a target counts.
        seen: set[str] = set()
        for label in sorted(k for k, v in self.targets.items() if v is not None):
            t = self.targets[label]
            if t in seen:
                del self.targets[label]
            seen.add(t)

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
        probe = ("for c in claude codex pi; do command -v $c >/dev/null 2>&1 && echo $c; done; "
                 "bwrap --ro-bind / / --unshare-pid --die-with-parent /usr/bin/true >/dev/null 2>&1 && echo bwrap")
        argv = (["bash", "-lic", probe] if target is None
                else [self.ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", target, f"bash -lic '{probe}' 2>/dev/null"])
        try:
            out = self.run(argv, timeout=30)
        except Failure:
            return []
        have = {line.strip() for line in out.splitlines()}
        if self.sandbox_text is not None and "bwrap" not in have:
            return []                       # no box, no harness: never run task text unboxed
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

    def herd_pane(self, host: str, name: str, prefix: str, parent_pane: str | None) -> tuple[str, str | None]:
        """Where an agent lives in herdr: the workspace `herd-PREFIX` (made on first use), one TAB per agent
        named after it, and an agent started under another agent as a SPLIT of its parent's tab. Returns
        (pane id, tab id)."""
        def pane_of(res):
            p = res["pane"] if "pane" in res else res["root_pane"]
            return p["pane_id"], p.get("tab_id")
        try:
            if parent_pane is not None:
                panes, tabs, _ = self.herdr_state(host)
                tab = tabs.get((panes.get(parent_pane) or {}).get("tab_id"), {})
                direction = "right" if (tab.get("pane_count") or 1) <= 1 else "down"
                res = json.loads(self.run(self.herdr_cmd(host, ["pane", "split", parent_pane, "--direction", direction, "--no-focus"]),
                                          timeout=30))["result"]
                return pane_of(res)
            label = f"herd-{prefix}"
            _, _, spaces = self.herdr_state(host)
            ws = next((w for w in spaces.values() if w.get("label") == label), None)
            if ws is None:                        # the first agent of a fleet uses the workspace's own first tab
                res = json.loads(self.run(self.herdr_cmd(host, ["workspace", "create", "--label", label, "--no-focus"]),
                                          timeout=30))["result"]
            else:
                res = json.loads(self.run(self.herdr_cmd(host, ["tab", "create", "--workspace", ws["workspace_id"], "--label", name, "--no-focus"]),
                                          timeout=30))["result"]
            return pane_of(res)
        except (ValueError, KeyError, TypeError):
            raise Failure("herdr returned an unexpected reply while laying out the agent") from None

    def place(self, host: str, agent: dict, family: str, installed: set, prefix: str = "fleet", parent_pane: str | None = None) -> None:
        name = agent["name"]
        if not NAME.fullmatch(name) or family not in self.cmds:
            raise Failure("refused: unexpected agent name or family")
        if host not in installed:
            base = "'" + self.public_url.replace("'", "'\\''") + "'"      # always single-quoted, like the dashboard's copy
            self.put(host, "worker.sh", self.worker_text.replace("'__HERDR_BASE__'", base), 0o700)
            if self.sandbox_text is not None:
                self.put(host, "collab-sandbox.sh", self.sandbox_text, 0o700)
            installed.add(host)
        self.put(host, f"{name}.json", json.dumps({k: agent[k] for k in ("agent_id", "name", "credential", "device_id", "expires_at")}) + "\n", 0o600)
        pane, tab = self.herd_pane(host, name, prefix, parent_pane)
        if not isinstance(pane, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,39}", pane):
            raise Failure("herdr returned an unexpected pane id")
        cmd = self.cmds[family]
        boxed = (f'HERDR_SANDBOX="$HOME/{AGENT_DIR}/collab-sandbox.sh" HERDR_AUDIT="$HOME/{AGENT_DIR}/audit/{name}.log" '
                 if self.sandbox_text is not None else "")
        line = (f'{boxed}{"HERDR_AGENT_CMD=" + shlex.quote(cmd) + " " if cmd else ""}"$HOME/{AGENT_DIR}/worker.sh" '
                f'"$HOME/{AGENT_DIR}/{name}.json"')
        self.run(self.herdr_cmd(host, ["pane", "run", pane, line]), timeout=30)
        try:        # tie the agent to its pane so the dashboard can show it with the fabric session
            self.ok("POST", f"agents/{agent['agent_id']}/link", {"instance_id": f"pane:{host}:{pane}"})
        except Exception:
            pass    # cosmetic: the agent works without the link
        self.report_session(host, pane, family, name)
        try:        # the pane and tab carry the name too; the first sync cycle would do it, this just is not late
            if parent_pane is None and isinstance(tab, str) and PANE_ID.fullmatch(tab):
                self.run(self.herdr_cmd(host, ["tab", "rename", tab, name]), timeout=20)
            self.run(self.herdr_cmd(host, ["pane", "rename", pane, name]), timeout=20)
            self.remember_name(agent["agent_id"], name)
        except Failure:
            pass

    def report_session(self, host: str, pane: str, family: str, name: str) -> None:
        """Tell the fabric about the pane, so the agent shows up with a host and status even before (or
        without) herdr's own plugin reporting an agent in it. Best effort and cosmetic. The host id is the
        fabric's name for this machine, which is what the plugin reports: the herdr label."""
        env = {"entity_type": "session", "entity_id": f"collab:{host}:{pane}", "event_type": "session.start",
               "source": "collab-spawn",
               "payload": {"host_id": host, "pane_id": pane, "agent_role": FAMILY_ROLE.get(family, ""),
                           "mission_id": pane.split(":", 1)[0], "summary": f"collab agent {name}", "status": "active"}}
        try:
            req = urllib.request.Request(f"{self.url}/api/v1/ingest", data=json.dumps(env).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).close()          # noqa: S310 (operator-configured URL)
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------ names, both ways
    def _names_file(self) -> Path:
        return self.state / "names.json"

    def load_names(self) -> dict:
        try:
            d = json.loads(self._names_file().read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_names(self, names: dict) -> None:
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._names_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(names))
        os.replace(tmp, self._names_file())

    def remember_name(self, agent_id: str, name: str) -> None:
        names = self.load_names()
        names[agent_id] = name
        self.save_names(names)

    def herdr_state(self, host: str) -> tuple[dict, dict, dict]:
        """panes, tabs and workspaces of one host, each by id."""
        def listing(args, key, id_key):
            rows = json.loads(self.run(self.herdr_cmd(host, args), timeout=30))["result"][key]
            return {r[id_key]: r for r in rows if isinstance(r, dict) and isinstance(r.get(id_key), str)}
        try:
            return (listing(["pane", "list"], "panes", "pane_id"), listing(["tab", "list"], "tabs", "tab_id"),
                    listing(["workspace", "list"], "workspaces", "workspace_id"))
        except (ValueError, KeyError, TypeError):
            raise Failure("herdr returned an unexpected reply to list") from None

    def sync_names(self, up: list[str]) -> None:
        """One pass over every agent tied to a herdr pane: keep @name and the herdr labels equal."""
        links = [a for a in self.ok("GET", "fleet/agent-links")["agents"] if isinstance(a, dict)]
        names, by_host = self.load_names(), {}
        for a in links:
            t = link_target(a.get("instance_id"))
            if t and t[0] in up and isinstance(a.get("agent_id"), str) and NAME.fullmatch(str(a.get("name"))):
                by_host.setdefault(t[0], []).append((a, t[1]))
        for host, agents in by_host.items():
            try:
                panes, tabs, spaces = self.herdr_state(host)
            except Failure as exc:
                self.say(f"names: {host}: {trim(exc)}")
                continue
            for a, pane_id in agents:
                try:
                    self.sync_one(host, a, panes.get(pane_id), tabs, spaces, names)
                except Failure as exc:
                    self.say(f"names: {a['name']}: {trim(exc)}")
        live = {a["agent_id"] for a in links}
        self.save_names({k: v for k, v in names.items() if k in live})

    def sync_one(self, host: str, agent: dict, pane: dict | None, tabs: dict, spaces: dict, names: dict) -> None:
        if not pane:
            return
        aid, name, base = agent["agent_id"], agent["name"], names.get(agent["agent_id"])
        tab = tabs.get(pane.get("tab_id"), {})
        seen = {"pane": (label_name(pane.get("label")), pane.get("label"))}
        # The tab of an agent the runner (or the join script) placed carries its name, unless the agent is a helper
        # sharing its parent's tab. A tab the owner linked by hand (a fabric session) is never relabelled, and the
        # workspace (`herd-PREFIX`, shared by the whole fleet) never follows one agent.
        if str(agent.get("instance_id", "")).startswith("pane:") and not agent.get("parent_agent_id") and tab.get("tab_id"):
            default_tab = str(tab.get("number")) == str(tab.get("label"))
            seen["tab"] = (None if default_tab else label_name(tab.get("label")), tab.get("label"))
        target = name
        if base is not None and name == base:                        # the web side did not move: follow herdr
            moved = next((v for v, _ in seen.values() if v and v != base), None)
            if moved:
                try:
                    self.ok("POST", f"agents/{aid}/rename", {"name": moved, "source": "herdr"})
                    target = moved
                    self.say(f"names: {base} -> {moved} (renamed in herdr)")
                except Failure as exc:
                    self.say(f"names: herdr label {moved!r} refused for {base}: {trim(exc)}; setting herdr back")
        todo = [("pane", pane["pane_id"]), ("tab", tab.get("tab_id"))]
        for kind, ident in todo:
            if kind in seen and isinstance(ident, str) and PANE_ID.fullmatch(ident) and seen[kind][1] != target:
                self.run(self.herdr_cmd(host, [kind, "rename", ident, target]), timeout=20)
        names[aid] = target

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
            parent_pane, parent_host = None, None
            if spec.get("parent_agent_id"):
                body["parent_agent_id"] = spec["parent_agent_id"]
                link = next((a for a in self.ok("GET", "fleet/agent-links")["agents"] if a.get("agent_id") == spec["parent_agent_id"]), None)
                target = link_target(link.get("instance_id")) if link else None
                if not target or target[0] not in self.targets:
                    raise Failure("the parent agent has no herdr pane on a host this runner can reach")
                parent_host, parent_pane = target
            made = self.ok("POST", "agents/bulk", body)
            if made.get("error"):
                error = trim(made["error"])
            installed: set = set()
            for agent in made["agents"]:
                host = "-"
                try:
                    if time.monotonic() > deadline:
                        raise Failure("ran out of time")
                    host = parent_host or self.pick_host(eligible) or ""
                    if not host:
                        raise Failure("every host opted out meanwhile")
                    self.place(host, agent, spec["harness_type"], installed, spec["name_prefix"], parent_pane)
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

    # ------------------------------------------------------------ helpers
    @property
    def helpers_dir(self) -> Path:
        return self.state / "helpers"

    def helpers_cycle(self, local_ok: bool = True, claim: bool = True) -> None:
        """Watch the helpers this host runs (finish, stop, refresh their host list), then claim new ones. Every
        cycle is a new process: all that is known about a running helper is in its private state directory."""
        if self.sandbox_path is None:
            return                        # a helper never runs unboxed, and never without its egress proxy
        listing = self.ok("GET", "fleet/helpers")
        paused = listing.get("paused") is True
        rows = [h for h in listing.get("helpers") or [] if isinstance(h, dict)
                and isinstance(h.get("helper_id"), str) and HELPER_ID.fullmatch(h["helper_id"])]
        live = {h["helper_id"]: h for h in rows}
        mine = sorted(p for p in self.helpers_dir.iterdir() if p.is_dir()) if self.helpers_dir.is_dir() else []
        for d in mine:
            try:
                self.watch_helper(d, live.get(d.name), paused)
            except Exception as exc:                          # noqa: BLE001  (one helper must not stall the rest)
                self.say(f"helper {d.name}: {trim(exc)}")
        if paused or not local_ok or not claim:
            return
        have = set(self.families.get(self.host_id, []))
        busy = sum(1 for p in self.helpers_dir.iterdir() if p.is_dir()) if self.helpers_dir.is_dir() else 0
        for h in rows:
            hid = h["helper_id"]
            if h.get("status") != "requested" or (self.helpers_dir / hid).exists():
                continue
            if h.get("family") not in have:
                self.say(f"helper {hid}: {h.get('family')} is not installed on {self.host_id}")
                continue
            if busy >= HELPERS_PER_HOST:
                self.say(f"helpers: {HELPERS_PER_HOST} already run on {self.host_id}")
                break
            busy += 1
            self.claim_and_start(h)

    def load_state(self, d: Path) -> dict:
        try:
            st = json.loads((d / "state.json").read_text())
            if isinstance(st, dict):
                return st
        except (OSError, ValueError):
            pass
        return {"helper_id": d.name, "phase": "claiming", "since": d.stat().st_mtime}

    def save_state(self, d: Path, st: dict) -> None:
        write_private(d / "state.json", json.dumps(st))

    def claim_and_start(self, row: dict) -> None:
        hid = row["helper_id"]
        self.helpers_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.helpers_dir.chmod(0o700)
        d = self.helpers_dir / hid
        d.mkdir(mode=0o700)
        st = {"helper_id": hid, "phase": "claiming", "since": time.time()}
        self.save_state(d, st)                    # before the claim: a crash after it must not lead to a second start
        status, c = self.api("POST", f"fleet/helpers/{hid}/claim", {})
        if status != 200:
            remove_tree(d)
            if status not in (409, 503):
                self.say(f"helper {hid}: claim {status}")
            return
        # Helpers run without MCP for now, so the helper agent's credential is not needed here: it is never written
        # anywhere, and finishing the helper revokes it.
        c.pop("credential", None)
        c.pop("device_id", None)
        st.update(phase="claimed", since=time.time(), room_id=c.get("room_id"), agent_id=c.get("helper_agent_id"),
                  name=c.get("agent_name"), role=c.get("role"), family=c.get("family"))
        self.save_state(d, st)
        try:
            self.start_helper(d, st, c)
        except Exception as exc:                              # noqa: BLE001
            self.end_helper(d, st, "failed", trim(exc))

    def helper_command(self, c: dict) -> tuple[str, list[str]]:
        """The fixed harness command for the helper's family: tool-enabled only when BOTH the room profile allows
        tools and this runner runs with --allow-tools. The model goes in through the family's --model flag, and
        only when it is a plain id."""
        family = c.get("family")
        prof = c.get("profile") if isinstance(c.get("profile"), dict) else {}     # none: no tools, services or keys
        tools = prof.get("tools_allowed") is True and self.allow_tools
        cmd = (FAMILY_CMD_TOOLS if tools else FAMILY_CMD_TEXT_ONLY).get(family) if isinstance(family, str) else None
        if not cmd:
            raise Failure(f"refused: {trim(family, 20)} helpers need tools, which this room or this runner does not allow")
        prefix, flags = HELPER_ISOLATION.get(family, ("", ""))
        if prefix and (cmd == prefix or cmd.startswith(prefix + " ")):
            cmd = f"{prefix} {flags}{cmd[len(prefix):]}"
        notes: list[str] = []
        model = c.get("model")
        if model is not None:
            if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
                raise Failure("refused: the model id is not a plain model id")
            after = MODEL_AFTER.get(family)
            if after and cmd.startswith(after + " "):
                cmd = f"{after} --model {shlex.quote(model)}{cmd[len(after):]}"
            else:
                notes.append(f"model {model} was not passed: no model flag is known for {family}")
        return cmd, notes

    def room_secrets(self, room) -> dict:
        """The room's secret values (collab-secrets.py). Used only when the file is a regular file of the runner's
        own user that nobody else can read; otherwise ignored, with a warning."""
        if not isinstance(room, str) or not ROOM_ID.fullmatch(room):
            return {}
        path = self.secrets_dir / f"{room}.json"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        except OSError:
            self.say(f"room secrets for {room}: ignored: not a regular file")
            return {}
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                self.say(f"room secrets for {room}: ignored: the file must be yours with mode 0600")
                return {}
            with os.fdopen(fd, "rb", closefd=False) as fh:
                data = json.loads(fh.read(1_000_000) or b"{}")
        except (OSError, ValueError):
            self.say(f"room secrets for {room}: ignored: unreadable")
            return {}
        finally:
            os.close(fd)
        if not isinstance(data, dict):
            return {}
        # a plain string is a key file (--base64); {value, host} is a header value bound to one host (--host)
        return {k: v for k, v in data.items() if isinstance(k, str) and (isinstance(v, str) or (
            isinstance(v, dict) and isinstance(v.get("value"), str) and isinstance(v.get("host"), str)))}

    def proxy_secrets(self, services: list[dict], secrets: dict, hid: str) -> dict:
        """The header values the helper's egress proxy may attach: only those bound (collab-secrets.py --host) to
        the host of the service's upstream_url. The room owner sets upstream_url, so a service repointed
        elsewhere does not get the key. The proxy checks the same again."""
        out = {}
        for sv in services:
            n = sv["secret_name"]
            v = secrets.get(n)
            if v is None:
                continue
            if not isinstance(v, dict):
                self.say(f"helper {hid}: room secret {n} has no bound host; service {sv['name']} gets no key "
                         f"(store it again with collab-secrets.py set ROOM {n} --host HOST)")
                continue
            try:
                host = urllib.parse.urlsplit(sv.get("upstream_url") or "").hostname or ""
            except ValueError:
                host = ""
            if canon_host(host) != canon_host(v["host"]):
                self.say(f"helper {hid}: room secret {n} is bound to {trim(v['host'], 80)}, not "
                         f"{trim(host, 80) or '(no host)'}; service {sv['name']} gets no key")
                continue
            out[n] = {"value": v["value"], "host": v["host"]}
        return out

    def harness_login(self, d: Path, family, hid: str) -> Path:
        """A private config dir for the helper's harness holding ONLY its login file(s), copied from the operator's
        (no CLAUDE.md, projects/ memory, hooks, plugins, skills or settings). The box shows it instead of the
        operator's ~/.claude, ~/.codex and ~/.pi; teardown removes it with the helper's state dir."""
        root = d / "harness"
        root.mkdir(mode=0o700)
        root.chmod(0o700)
        for fam, (sub, env, default, files) in HELPER_LOGIN.items():
            dst = root / sub
            dst.mkdir(mode=0o700)
            if fam != family:
                continue
            src = Path(os.environ[env]) if os.environ.get(env, "").startswith("/") else Path.home() / default
            for n in files:
                try:
                    fd = os.open(src / n, os.O_RDONLY)
                except OSError:
                    continue                      # not signed in that way: the harness says so in the result
                try:
                    if stat.S_ISREG(os.fstat(fd).st_mode):
                        with os.fdopen(fd, "rb", closefd=False) as fh:
                            data = fh.read(4_000_000)
                        if (fam, n) == ("claude-code", ".credentials.json"):
                            data = self.claude_login_only(data, hid)
                        if data is not None:
                            write_private(dst / n, data)
                    else:
                        self.say(f"helper {hid}: {src / n} is not a regular file; not copied")
                finally:
                    os.close(fd)
        return root

    def claude_login_only(self, data: bytes, hid: str) -> bytes | None:
        """claude's .credentials.json also holds the operator's MCP connector tokens (mcpOAuth): keep only the
        claude.ai login."""
        try:
            login = json.loads(data).get("claudeAiOauth")
        except (ValueError, AttributeError):
            login = None
        if not isinstance(login, dict):
            self.say(f"helper {hid}: no claude.ai login in the claude credentials file; not copied")
            return None
        return json.dumps({"claudeAiOauth": login}).encode()

    def refresh_allow(self, d: Path, room: str) -> None:
        out = self.ok("GET", f"fleet/egress/{room}")
        lines = [x for x in out.get("allowlist") or [] if isinstance(x, str) and ALLOW_LINE.fullmatch(x)]
        write_private(d / "allow.txt", "".join(x + "\n" for x in lines))

    def harness_path(self) -> str:
        """PATH for the harness: the login shell's (a herdr pane's, where claude/codex/pi and node are found)
        plus this process's own. A systemd unit's PATH alone would not find them."""
        if self._harness_path is None:
            try:
                out = self.run(["bash", "-lic", 'printf "\\n%s" "$PATH"'], timeout=20).splitlines()[-1]
            except (Failure, IndexError):
                out = ""
            parts = [x for x in f"{out}:{os.environ.get('PATH', '')}".split(":") if x.startswith("/")]
            self._harness_path = ":".join(dict.fromkeys(parts)) or "/usr/bin:/bin"
        return self._harness_path

    def sock_base(self) -> Path:
        """A short directory for the proxy's socket (a unix socket path is limited to ~107 bytes)."""
        run = os.environ.get("XDG_RUNTIME_DIR", "")
        return Path(run) if run.startswith("/") and os.path.isdir(run) else Path(tempfile.gettempdir())

    def start_helper(self, d: Path, st: dict, c: dict) -> None:
        hid, name, role, room = st["helper_id"], c.get("agent_name"), c.get("role"), c.get("room_id")
        if (not isinstance(name, str) or not NAME.fullmatch(name) or role not in HELPER_ROLES
                or not isinstance(room, str) or not ROOM_ID.fullmatch(room) or not isinstance(st.get("agent_id"), str)
                or not isinstance(c.get("task"), str)):
            raise Failure("refused: unexpected helper name, role, room or task")
        cmd, notes = self.helper_command(c)
        try:
            lifetime = float(c["expires_epoch"]) - float(c["now"])
        except (KeyError, TypeError, ValueError):
            raise Failure("refused: the claim carries no expiry") from None
        margin = min(HELPER_MARGIN, max(1.0, lifetime / 10))
        if lifetime - margin <= 1:
            raise Failure("refused: no time left")
        prof = c.get("profile") if isinstance(c.get("profile"), dict) else {}
        area = Path.home() / AGENT_DIR / "work"
        area.mkdir(parents=True, exist_ok=True, mode=0o700)
        work = area / f"helper-{hid}"
        st.update(work=str(work), deadline=time.time() + lifetime - margin, notes=notes)
        work.mkdir(mode=0o700)                    # always fresh: an existing directory is an error
        self.save_state(d, st)
        binds = []
        req = c.get("requester") if isinstance(c.get("requester"), dict) else {}
        rname = req.get("name")
        # Only the operator's own agents (fleet/agent-links) have work directories here, and names are not unique
        # across owners: anyone else's agent named like one of them must not get a view of that agent's work.
        link = next((a for a in self.ok("GET", "fleet/agent-links").get("agents") or []
                     if isinstance(a, dict) and a.get("agent_id") == req.get("agent_id")), None)
        if role in ("reviewer", "debugger") and link and isinstance(rname, str) and NAME.fullmatch(rname):
            p = area / rname
            if p.is_dir() and not p.is_symlink():
                binds.append(str(p))
        secrets = self.room_secrets(room)
        services = [sv for sv in prof.get("services") or [] if isinstance(sv, dict)
                    and isinstance(sv.get("name"), str) and SERVICE_NAME.fullmatch(sv["name"])
                    and isinstance(sv.get("secret_name"), str) and SECRET_NAME.fullmatch(sv["secret_name"])]
        write_private(d / "services.json", json.dumps(services))
        write_private(d / "secrets.json", json.dumps(self.proxy_secrets(services, secrets, hid)))
        ssh_dir = None
        # key files are plain (base64) strings only: a header value listed as an ssh name is never written out
        keys = [n for n in prof.get("ssh_secret_names") or [] if isinstance(n, str) and SECRET_NAME.fullmatch(n)
                and isinstance(secrets.get(n), str)]
        if keys:
            ssh_dir = d / "ssh"
            ssh_dir.mkdir(mode=0o700)
            ssh_dir.chmod(0o700)
            for n in keys:
                try:
                    write_private(ssh_dir / n, base64.b64decode(secrets[n], validate=True))
                except (ValueError, binascii.Error):
                    self.say(f"helper {hid}: room secret {n} is not base64; not copied")
        harness = self.harness_login(d, c.get("family"), hid)
        self.refresh_allow(d, room)
        sock_dir = Path(tempfile.mkdtemp(prefix="hh-", dir=self.sock_base()))
        st["sock_dir"] = str(sock_dir)
        self.save_state(d, st)
        write_private(d / "spec.json", json.dumps({
            "sandbox": str(self.sandbox_path), "egress": str(self.sandbox_path.with_name("collab-egress.py")),
            "work": str(work), "cmd": cmd, "deadline": st["deadline"], "sock": str(sock_dir / "egress.sock"),
            "services": [sv["name"] for sv in services], "ro_binds": binds,
            "ssh_dir": str(ssh_dir) if ssh_dir else None, "harness_dir": str(harness)}))
        pane, tab, own_tab = self.helper_pane(c, name, link)
        st.update(pane=pane, phase="launching", since=time.time())
        self.save_state(d, st)
        self.launch_helper(d, st, helper_prompt(role, binds) + c["task"])
        status, out = self.api("POST", f"fleet/helpers/{hid}/finish", {"status": "running"})
        if status != 200:
            raise Failure(f"could not mark the helper running: {status} {trim(out.get('error', ''))}")
        try:                                       # presentation: the pane shows the helper's output as it comes
            self.run(self.herdr_cmd(self.host_id, ["pane", "run", pane,
                                                          f"tail -n +1 -F {shlex.quote(str(d / 'out'))} | cat -v"]),
                     timeout=30)
            if own_tab and isinstance(tab, str) and PANE_ID.fullmatch(tab):
                self.run(self.herdr_cmd(self.host_id, ["tab", "rename", tab, name]), timeout=20)
            self.run(self.herdr_cmd(self.host_id, ["pane", "rename", pane, name]), timeout=20)
        except Failure as exc:
            self.say(f"helper {hid}: herdr: {trim(exc)}")
        try:    # ties the helper to its pane on the dashboard; the service only lets the operator link its OWN agents
            self.api("POST", f"agents/{st['agent_id']}/link", {"instance_id": f"pane:{self.host_id}:{pane}"})
        except Failure:
            pass    # cosmetic
        self.say(f"helper {hid}: started {name} ({role}, {c.get('family')})")

    def helper_pane(self, c: dict, name: str, link: dict | None) -> tuple[str, str | None, bool]:
        """A split of the requesting agent's pane when that agent is the operator's own and sits in a pane on THIS
        host (fleet/agent-links lists only the operator's agents: anyone else could point a link at the operator's
        panes); otherwise a tab of its own in the workspace herd-<requester>. Returns (pane, tab, own tab)."""
        req = c.get("requester") if isinstance(c.get("requester"), dict) else {}
        rname = req.get("name")
        prefix = rname if isinstance(rname, str) and NAME.fullmatch(rname) else "helpers"
        parent = None
        t = link_target(link.get("instance_id")) if link else None
        if t and t[0] == self.host_id:
            panes, _, _ = self.herdr_state(self.host_id)
            parent = t[1] if t[1] in panes else None
        pane, tab = self.herd_pane(self.host_id, name, prefix, parent)
        if not isinstance(pane, str) or not PANE_ID.fullmatch(pane):
            raise Failure("herdr returned an unexpected pane id")
        return pane, tab, parent is None

    def helper_env(self) -> dict:
        keep = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
        return {k: os.environ[k] for k in keep if k in os.environ} | {"PATH": self.harness_path()}

    def launch_helper(self, d: Path, st: dict, stdin: str) -> None:
        """The supervisor runs in a session of its own (its pid is its process group), outliving this cycle."""
        p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "helper-run", str(d)],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=self.helper_env(), cwd=str(d), start_new_session=True, close_fds=True)
        ps = proc_stat(p.pid)
        st.update(pid=p.pid, start=ps[1] if ps else None)
        self.save_state(d, st)
        try:
            p.stdin.write(stdin.encode("utf-8"))
            p.stdin.close()
        except OSError:
            raise Failure("the helper supervisor did not take its task") from None
        st["phase"] = "running"
        self.save_state(d, st)

    def alive(self, st: dict) -> bool:
        ps = proc_stat(st.get("pid"))
        return bool(ps) and st.get("start") is not None and ps[1] == st["start"] and ps[0] != "Z"

    def kill(self, st: dict) -> None:
        """Stop the helper's whole process group (supervisor, proxy, box), only if the recorded pid is still the
        supervisor this runner started."""
        pid, ps = st.get("pid"), proc_stat(st.get("pid"))
        if not isinstance(pid, int) or pid <= 1 or not ps or st.get("start") is None or ps[1] != st["start"]:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            for _ in range(30):
                if not group_alive(pid):
                    return
                time.sleep(0.1)

    def watch_helper(self, d: Path, row: dict | None, paused: bool) -> None:
        st = self.load_state(d)
        if isinstance(st.get("ending"), dict):              # an earlier finish did not reach the service
            self.end_helper(d, st, st["ending"].get("status", "failed"), st["ending"].get("error"))
            return
        if st.get("phase") != "running":
            # Claimed (or half started) by a process that is gone: the claim is once-only, so it is never started
            # again; it is failed once nothing can still be waiting for it.
            if paused or row is None or time.time() - float(st.get("since") or 0) > HELPER_CLAIM_STALE:
                self.end_helper(d, st, "failed", "the runner restarted before the helper started")
            return
        self.drain_blocked(d, st)
        self.save_state(d, st)
        try:
            res = json.loads((d / "result.json").read_text())
        except (OSError, ValueError):
            res = None
        if isinstance(res, dict):
            ok = res.get("exit") == 0 and res.get("timed_out") is not True
            err = None if ok else ("timeout" if res.get("timed_out") is True else
                                   res.get("error") if res.get("error") in ("sandbox-error",) else
                                   f"the helper exited with status {res.get('exit')}")
            self.end_helper(d, st, "done" if ok else "failed", err)
        elif paused or row is None or row.get("status") not in ("claimed", "running"):
            self.end_helper(d, st, "failed", "cancelled")
        elif time.time() > float(st.get("deadline") or 0) + 15:
            self.end_helper(d, st, "failed", "timeout")
        elif not self.alive(st):
            self.end_helper(d, st, "failed", "sandbox-error")
        else:
            self.refresh_allow(d, st["room_id"])

    def drain_blocked(self, d: Path, st: dict) -> None:
        """New complete lines of the proxy's blocked log become host-access requests, one per host:port. A host
        that resolves only to private addresses is never requested (approving it would not help): it is shown
        in the result instead."""
        try:
            with open(d / "blocked.jsonl", "rb") as fh:
                fh.seek(int(st.get("log_off") or 0))
                data = fh.read(1_000_000)
        except (OSError, ValueError):
            return
        off = int(st.get("log_off") or 0)
        asked, forbidden = st.setdefault("asked", []), st.setdefault("forbidden", [])
        for raw in data.splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                break                                     # a line still being written
            try:
                e = json.loads(raw)
            except ValueError:
                e = None
            if isinstance(e, dict) and isinstance(e.get("host"), str) and isinstance(e.get("port"), int) \
                    and "suppressed" not in e:
                host = f"[{e['host']}]" if ":" in e["host"] else e["host"]     # IPv6: the service's [addr] form
                key = f"{host}:{e['port']}"
                if e.get("reason") == "forbidden-address":
                    if key not in forbidden and len(forbidden) < MAX_EGRESS_ASKS:
                        forbidden.append(key)
                elif key not in asked and len(asked) < MAX_EGRESS_ASKS:
                    try:
                        self.api("POST", "fleet/egress/request", {"room_id": st.get("room_id"),
                                                                  "agent_id": st.get("agent_id"),
                                                                  "host": host, "port": e["port"]})
                    except Failure:
                        break                             # the service is unreachable: this line again next cycle
                    asked.append(key)
            off += len(raw)
        st["log_off"] = off

    def helper_result_text(self, d: Path, st: dict) -> str:
        try:
            res = json.loads((d / "result.json").read_text())
        except (OSError, ValueError):
            res = {}
        try:
            raw = (d / "out").read_bytes()[:HELPER_RESULT_CAP + 1]
        except OSError:
            raw = b""
        total = res.get("bytes") if isinstance(res.get("bytes"), int) else len(raw)
        head = "".join(f"[{n}]\n" for n in st.get("notes") or [])
        foot = []
        if total > HELPER_RESULT_CAP:
            foot.append("[output truncated at 20 KB]")
        if st.get("forbidden"):
            foot.append("[blocked, these resolve only to non-public addresses: " + ", ".join(st["forbidden"]) + "]")
        if st.get("asked"):
            foot.append("[host access was requested for: " + ", ".join(st["asked"]) + "]")
        if res.get("exit") not in (0, None) and res.get("stderr"):
            foot.append("[stderr]\n" + str(res["stderr"])[-1500:])
        # the WHOLE posted text fits in HELPER_RESULT_CAP bytes: notes and footers first, the output gets the rest
        head = RESULT_BAD.sub("", head).encode()[:HELPER_RESULT_CAP // 4].decode("utf-8", "ignore")
        tail = RESULT_BAD.sub("", "\n" + "\n".join(foot) if foot else "").encode()[:HELPER_RESULT_CAP // 4]
        out = RESULT_BAD.sub("", raw.decode("utf-8", "ignore")).encode()
        room = HELPER_RESULT_CAP - len(head.encode()) - len(tail)
        marker = b"\n[output truncated at 20 KB]"
        if len(out) > room and b"[output truncated" not in tail:
            tail, room = marker + tail, room - len(marker)
        text = out[:max(0, room)].decode("utf-8", "ignore")
        return head + text + tail.decode("utf-8", "ignore")

    @staticmethod
    def finish_body(status: str, result: str, error: str | None) -> dict:
        """The finish request, serialized below FINISH_BODY_MAX bytes (the result is cut further if need be)."""
        body = {"status": status, "result": result, **({"error": error} if error else {})}
        while True:
            size = len(json.dumps(body, ensure_ascii=False).encode("utf-8", "replace"))
            if size <= FINISH_BODY_MAX or not body["result"]:
                return body
            enc = body["result"].encode("utf-8", "replace")
            keep = min(len(enc) - 1, int(len(enc) * FINISH_BODY_MAX / size) - 64)   # escapes may make it 2x
            body["result"] = enc[:max(0, keep)].decode("utf-8", "ignore")

    def end_helper(self, d: Path, st: dict, status: str, error: str | None) -> None:
        """Stop it, post how it ended (with its output), then remove its pane, work dir, socket dir and state."""
        hid = st.get("helper_id", d.name)
        st["ending"] = {"status": status, "error": error}
        self.save_state(d, st)
        self.kill(st)
        self.drain_blocked(d, st)
        attempts = int(st.get("finish_attempts") or 0)
        while True:
            # after FINISH_ATTEMPTS refusals the output is given up on: a minimal failure still frees the slots
            minimal = attempts >= FINISH_ATTEMPTS
            body = ({"status": "failed", "error": "result could not be delivered"} if minimal
                    else self.finish_body(status, self.helper_result_text(d, st), error))
            try:
                code, out = self.api("POST", f"fleet/helpers/{hid}/finish", body)
            except Failure as exc:                           # the service is unreachable: not counted, retried
                self.save_state(d, st)
                self.say(f"helper {hid}: could not report the result, will retry: {trim(exc)}")
                return
            if 200 <= code < 300 or code in (404, 409):      # 404/409: it already ended on the service side
                break
            attempts += 1
            st["finish_attempts"] = attempts
            self.save_state(d, st)
            self.say(f"helper {hid}: finish {code} {trim(out.get('error', ''))}")
            if minimal and code < 500:                       # even the minimal body is refused: it never will be
                break
            if minimal or attempts < FINISH_ATTEMPTS:
                return                                       # kept: the next cycle tries again
        self.teardown(d, st)
        self.say(f"helper {hid}: {status}" + (f" ({trim(error, 120)})" if error else ""))

    def teardown(self, d: Path, st: dict) -> None:
        hid = st.get("helper_id", d.name)
        pane = st.get("pane")
        if isinstance(pane, str) and PANE_ID.fullmatch(pane):
            try:
                self.run(self.herdr_cmd(self.host_id, ["pane", "close", pane]), timeout=20)
            except Failure:
                pass
        area = (Path.home() / AGENT_DIR / "work").resolve()
        work = Path(st["work"]) if isinstance(st.get("work"), str) else None
        if work is not None and work.name == f"helper-{hid}" and work.parent.resolve() == area:
            remove_tree(work)
        sd = Path(st["sock_dir"]) if isinstance(st.get("sock_dir"), str) else None
        if sd is not None and sd.name.startswith("hh-") and sd.parent.resolve() == self.sock_base().resolve():
            remove_tree(sd)
        if d.parent == self.helpers_dir:
            remove_tree(d)

    # ------------------------------------------------------------ the model catalogue
    def catalogue_cycle(self) -> None:
        """Every CATALOGUE_EVERY seconds: the gateway's model list, normalized for the service's model picker. An
        entry whose id is not a plain id is skipped (the service would refuse the whole post)."""
        mark = self.state / "catalogue.json"
        try:
            last = float(json.loads(mark.read_text()).get("at", 0))
        except (OSError, ValueError, AttributeError, TypeError):
            last = 0.0
        if time.time() - last < CATALOGUE_EVERY:
            return
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(mark, json.dumps({"at": time.time()}))
        if not self.gateway:
            self.say("catalogue: no gateway configured (--gateway or llm_gateway in host.json); nothing posted")
            return
        base = self.gateway.rstrip("/")
        url = base + "/models" if base.endswith("/v1") else base + "/v1/models"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:          # noqa: S310 (operator-configured URL)
                listing = json.loads(resp.read(8_000_000) or b"{}")
        except (OSError, ValueError) as exc:
            self.say(f"catalogue: {trim(exc)}")
            return
        models = self.ok("POST", "fleet/catalogue", {"models": normalize_catalogue(listing)})
        self.say(f"catalogue: {models.get('count')} models")

    # ------------------------------------------------------------ one cycle
    def once(self) -> None:
        try:        # the panic switch, revokes and expiry apply first, whatever happens to the fleet path below
            self.helpers_cycle(claim=False)
        except (Failure, OSError) as exc:
            self.say(f"helpers: {trim(exc)}")
        self.public_url = (self.ok("GET", "config") or {}).get("public_url", "")
        if not self.public_url.startswith("https://"):
            raise Failure("the service has no public https url to give agents")
        self.discover()
        up = self.reachable()
        self.ok("POST", "fleet/hosts", {"hosts": [{"host": h, "kind": "local" if self.targets[h] is None else "machine",
                                                    "families": self.families.get(h, [])} for h in up]})
        flags = {h["host"]: h["auto_agents"] for h in self.ok("GET", "fleet/hosts")["hosts"]}
        eligible = sorted(h for h in up if flags.get(h, True))
        try:
            self.sync_names(up)
        except Failure as exc:
            self.say(f"names: {trim(exc)}")
        for sp in self.ok("GET", "fleet/spawns?status=requested")["spawns"]:
            status, spec = self.api("POST", f"fleet/spawns/{sp['spawn_id']}/claim", {})
            if status == 200:
                self.handle(spec, eligible)
            elif status != 409:
                self.say(f"claim {sp['spawn_id']}: {status}")
        try:
            self.helpers_cycle(local_ok=self.host_id in eligible)       # watches again, then claims
        except (Failure, OSError) as exc:
            self.say(f"helpers: {trim(exc)}")
        try:
            self.catalogue_cycle()
        except Failure as exc:
            self.say(f"catalogue: {trim(exc)}")


def default_url() -> tuple[str, str, str]:
    p = Path.home() / ".config" / "herdr-engineering" / "host.json"
    try:
        d = json.loads(p.read_text())
        return str(d["control_plane_url"]), str(d.get("host_id") or ""), str(d.get("llm_gateway") or "")
    except (OSError, ValueError, KeyError):
        return "", "", ""


def main(argv: list[str]) -> int:
    if argv[:1] == ["helper-run"] and len(argv) == 2:           # the supervisor of one helper (see helper_run)
        return helper_run(Path(argv[1]))
    home = Path.home()
    url, hid, gw = default_url()
    ap = argparse.ArgumentParser(prog="collab-spawn.py", description=__doc__.splitlines()[0])
    ap.add_argument("action", nargs="?", default="once", choices=["once"])
    ap.add_argument("--url", default=url, help="dashboard URL (default: control_plane_url from host.json)")
    ap.add_argument("--host-id", default=hid or socket.gethostname().split(".")[0].lower())
    ap.add_argument("--state-dir", default=str(home / ".local/state/herdr-engineering/collab-spawn"))
    ap.add_argument("--herdr", default="herdr")
    ap.add_argument("--ssh", default="ssh")
    ap.add_argument("--worker", default=str(Path(__file__).with_name("collab-worker.sh")))
    ap.add_argument("--sandbox", default=str(Path(__file__).with_name("collab-sandbox.sh")),
                    help="the box every task runs in (default: collab-sandbox.sh next to this script)")
    ap.add_argument("--no-sandbox", action="store_true", help="run tasks directly on the host (not recommended)")
    ap.add_argument("--gateway", default=gw, help="LLM gateway whose /v1/models feeds the helper model picker "
                                                    "(default: llm_gateway from host.json; none: no catalogue)")
    ap.add_argument("--allow-tools", action="store_true",
                    help="let agents use their harness's tools (shell, files, MCP) on task text from room members")
    args = ap.parse_args(argv)
    if not args.url or not HOST.fullmatch(args.host_id):
        ap.error("no dashboard URL (pass --url) or an invalid --host-id")
    r = Runner(args.url, Path(args.state_dir), args.herdr, args.ssh, Path(args.worker), args.host_id,
               say=lambda m: print(f"collab-spawn: {m}", file=sys.stderr), allow_tools=args.allow_tools,
               sandbox=None if args.no_sandbox else Path(args.sandbox), gateway=args.gateway)
    try:
        r.once()
    except Failure as exc:
        print(f"collab-spawn: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
