#!/usr/bin/env python3
"""run-watched SOFT HARD -- command...

SOFT seconds of wall time: print one notice to stderr, keep running (never kills).
HARD seconds with NO progress: kill the command and everything it started, exit 124. Progress = any new output byte, or CPU time
used by (or a new process appearing in) the command's process tree. The tree is the command's session plus every descendant found
by parent pid, including descendants that called setsid (Postgres via pg_ctl) and survivors reparented to init, which stay tracked
by pid + start time once seen. A slow command that keeps printing or computing is never killed; a hung one is.
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

    def __init__(self, root: int):
        self.root = root
        self.known: dict[int, int] = {root: -1}   # pid -> starttime (-1 until first seen)
        self.cpu: dict[int, float] = {}

    def poll(self) -> bool:
        """Refresh membership; return True if any member's CPU rose or a new member appeared."""
        snap = snapshot()
        alive = {p for p, st in snap.items() if self.known.get(p, st[3]) in (st[3], -1) and st[4] != "Z"}
        members = {p for p in self.known if p in alive}
        grew = True
        while grew:
            grew = False
            for p, st in snap.items():
                if p not in members and st[4] != "Z" and (st[0] in members or st[1] == self.root):
                    members.add(p); grew = True
        progress = False
        for p in members:
            st = snap[p]
            self.known[p] = st[3]
            if p not in self.cpu or st[2] > self.cpu[p]:
                progress = True
            self.cpu[p] = st[2]
        self.known = {p: t for p, t in self.known.items() if p in members or p == self.root}
        return progress

    def signal_all(self, sig: int) -> None:
        for pid in list(self.known):
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
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.1)
    tree.poll()
    tree.signal_all(signal.SIGKILL)
    proc.wait()


def main() -> int:
    if len(sys.argv) < 4 or "--" not in sys.argv:
        print(__doc__, file=sys.stderr)
        return 2
    soft, hard = float(sys.argv[1]), float(sys.argv[2])
    cmd = sys.argv[sys.argv.index("--") + 1:]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    tree = Tree(proc.pid)

    def on_signal(signum, _frame):
        print(f"[run-watched] received signal {signum}; stopping the command", file=sys.stderr)
        stop_tree(proc, tree)
        sys.exit(128 + signum)

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, on_signal)

    start = last_progress = time.monotonic()
    warned = eof = False
    fd = proc.stdout.fileno()
    while True:
        if not eof:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if ready:
                chunk = os.read(fd, 65536)
                if chunk:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                    last_progress = time.monotonic()
                else:
                    eof = True
        else:
            time.sleep(0.5)     # EOF is readable forever: do not spin on select
        if proc.poll() is not None and not tree.poll():
            while select.select([fd], [], [], 0)[0]:     # drain what the command wrote before exiting
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            rc = proc.returncode
            return 128 - rc if rc < 0 else rc
        now = time.monotonic()
        if tree.poll():
            last_progress = now
        if not warned and now - start >= soft:
            warned = True
            print(f"[run-watched] still running after {soft:g}s (soft limit; not killing)", file=sys.stderr)
        if now - last_progress >= hard:
            print(f"[run-watched] no output and no CPU for {hard:g}s -- hung; killing", file=sys.stderr)
            tree.signal_all(signal.SIGKILL)
            proc.wait()
            return 124


if __name__ == "__main__":
    raise SystemExit(main())
