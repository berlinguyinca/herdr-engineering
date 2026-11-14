#!/usr/bin/env python3
"""run-watched SOFT HARD -- command...

SOFT seconds of wall time: print one notice to stderr, keep running (never kills).
HARD seconds with NO progress: kill the command and everything it started, exit 124. Progress = any new output byte, or CPU time
used by (or a new process appearing in) the command's process tree. The tree is every process that is in the command's session, a
descendant by parent pid, or carries this run's unique token in its environment (HERDR_RUN_WATCHED_TOKEN, inherited through fork/exec).
The token is what finds a Postgres started by `pg_ctl start`: pg_ctl forks a setsid child that execs postgres and exits, so the
postmaster is reparented to init before any poll could walk the parent chain, but it keeps the environment. Processes that scrub
their environment or are owned by another user are not seen. A slow command that keeps printing or computing is never killed; a hung
one is.
Once the command itself has exited, survivors in the tree get a bounded grace (HERDR_WATCHED_ORPHAN_GRACE seconds, default 30, and
at most 2 s without any CPU progress), then the tree is stopped and the command's own status is returned.
SIGTERM/SIGINT/SIGHUP to this watchdog stop the whole tree (SIGTERM, brief wait, SIGKILL) and exit 128+signal.
Exit status is the command's (128+signal if it died of a signal), or 124 when killed as hung. Linux only (/proc).
"""
import os
import select
import signal
import subprocess
import sys
import time

TICKS = os.sysconf("SC_CLK_TCK")
TERM_GRACE = 5.0


def read_stat(pid: int):
    """(ppid, session, cpu_seconds, starttime, state) from /proc/<pid>/stat, or None if gone. comm may hold spaces/parens."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            raw = f.read()
    except OSError:
        return None
    rest = raw[raw.rindex(")") + 2:].split()
    return int(rest[1]), int(rest[3]), (int(rest[11]) + int(rest[12])) / TICKS, int(rest[19]), rest[0]


def snapshot() -> dict:
    out = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            st = read_stat(int(name))
            if st:
                out[int(name)] = st
    return out


class Tree:
    """The command's process tree, accumulated across polls so reparented descendants stay known."""

    def __init__(self, root: int, token: str):
        self.root = root
        self.token = b"HERDR_RUN_WATCHED_TOKEN=" + token.encode()
        self.known: dict[int, int] = {root: -1}   # pid -> starttime (-1 until first seen)
        self.cpu: dict[int, float] = {}
        self.tagged: dict[tuple[int, int], bool] = {}

    def has_token(self, pid: int, start: int) -> bool:
        key = (pid, start)
        if key not in self.tagged:
            try:
                with open(f"/proc/{pid}/environ", "rb") as f:
                    self.tagged[key] = self.token in f.read().split(b"\0")
            except OSError:
                self.tagged[key] = False
        return self.tagged[key]

    def poll(self) -> bool:
        """Refresh membership; return True if any member's CPU rose or a new member appeared."""
        snap = snapshot()
        self.tagged = {k: v for k, v in self.tagged.items() if snap.get(k[0], (0, 0, 0, None))[3] == k[1]}
        members = {p for p, st in snap.items() if self.known.get(p, st[3]) in (st[3], -1) and p in self.known and st[4] != "Z"}
        grew = True
        while grew:
            grew = False
            for p, st in snap.items():
                if p in members or st[4] == "Z":
                    continue
                if st[0] in members or st[1] == self.root or self.has_token(p, st[3]):
                    members.add(p); grew = True
        progress = False
        for p in members:
            st = snap[p]
            self.known[p] = st[3]
            if p not in self.cpu or st[2] > self.cpu[p]:
                progress = True
            self.cpu[p] = st[2]
        self.known = {p: t for p, t in self.known.items() if p in members or p == self.root}
        self.alive = {p for p in members if p != self.root}
        return progress

    alive: set = set()

    def signal_all(self, sig: int) -> None:
        for pid, start in list(self.known.items()):
            if pid == self.root:
                continue
            st = read_stat(pid)
            if st and st[3] == start:            # not a reused pid
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
        try:
            os.killpg(self.root, sig)
        except (ProcessLookupError, PermissionError):
            pass


def stop_tree(proc: subprocess.Popen, tree: Tree) -> None:
    tree.poll()
    tree.signal_all(signal.SIGTERM)
    deadline = time.monotonic() + TERM_GRACE
    while time.monotonic() < deadline and (proc.poll() is None or any(read_stat(p) for p in tree.alive)):
        time.sleep(0.1)
    tree.poll()
    tree.signal_all(signal.SIGKILL)
    proc.wait()


class Out:
    """Forwards the command's output; a closed reader (`| head -1`) means keep draining without forwarding."""

    def __init__(self) -> None:
        self.broken = False

    def write(self, chunk: bytes) -> None:
        if self.broken:
            return
        try:
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, ValueError):
            self.broken = True
            os.dup2(os.open(os.devnull, os.O_WRONLY), 1)     # so interpreter shutdown does not flush into the dead pipe


def main() -> int:
    if len(sys.argv) < 4 or "--" not in sys.argv:
        print(__doc__, file=sys.stderr)
        return 2
    soft, hard = float(sys.argv[1]), float(sys.argv[2])
    cmd = sys.argv[sys.argv.index("--") + 1:]
    token = f"{os.getpid()}-{time.time_ns()}"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                            env={**os.environ, "HERDR_RUN_WATCHED_TOKEN": token})
    tree = Tree(proc.pid, token)
    got: list[int] = []         # first signal received; the loop acts on it (nothing blocking runs in the handler)

    def on_signal(signum, _frame):
        if not got:
            got.append(signum)

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, on_signal)

    orphan_grace = float(os.environ.get("HERDR_WATCHED_ORPHAN_GRACE", "30"))
    out = Out()
    start = last_progress = time.monotonic()
    root_exited_at = last_orphan_progress = None
    warned = eof = finished = False
    fd = proc.stdout.fileno()
    try:
        while True:
            if got:
                print(f"[run-watched] received signal {got[0]}; stopping the command", file=sys.stderr)
                return 128 + got[0]         # the finally block stops the tree
            if not eof:
                ready, _, _ = select.select([fd], [], [], 1.0)
                if ready:
                    chunk = os.read(fd, 65536)
                    if chunk:
                        out.write(chunk)
                        last_progress = time.monotonic()
                    else:
                        eof = True
            else:
                time.sleep(0.5)     # EOF is readable forever: do not spin on select
            now = time.monotonic()
            progressed = tree.poll()
            if proc.poll() is not None:
                if root_exited_at is None:
                    root_exited_at = last_orphan_progress = now
                if progressed:
                    last_orphan_progress = now
                if not tree.alive or now - last_orphan_progress >= 2.0 or now - root_exited_at >= orphan_grace:
                    while select.select([fd], [], [], 0)[0]:     # drain what the command wrote before exiting
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            break
                        out.write(chunk)
                    rc = proc.returncode
                    return 128 - rc if rc < 0 else rc       # the finally block stops any survivors
                continue
            if progressed:
                last_progress = now
            if not warned and now - start >= soft:
                warned = True
                print(f"[run-watched] still running after {soft:g}s (soft limit; not killing)", file=sys.stderr)
            if now - last_progress >= hard:
                print(f"[run-watched] no output and no CPU for {hard:g}s -- hung; killing", file=sys.stderr)
                finished = True
                tree.signal_all(signal.SIGKILL)
                proc.wait()
                return 124
    finally:
        if not finished:
            stop_tree(proc, tree)


if __name__ == "__main__":
    raise SystemExit(main())
