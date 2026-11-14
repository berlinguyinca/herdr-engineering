#!/usr/bin/env python3
"""run-watched SOFT HARD -- command...

SOFT seconds of wall time: print one notice to stderr, keep running (never kills).
HARD seconds with NO progress: kill the whole process group. Progress = any new output byte OR any CPU time used
by the command's session. A slow command that keeps printing or computing is never killed; a hung one is.
Exit status is the command's, or 124 when killed as hung.
"""
import os
import select
import signal
import subprocess
import sys
import time


def session_cpu(sid: int) -> int:
    out = subprocess.run(["ps", "-s", str(sid), "-o", "cputimes="], capture_output=True, text=True).stdout
    return sum(int(x) for x in out.split() if x.isdigit())


def main() -> int:
    if len(sys.argv) < 4 or "--" not in sys.argv:
        print(__doc__, file=sys.stderr)
        return 2
    soft, hard = float(sys.argv[1]), float(sys.argv[2])
    cmd = sys.argv[sys.argv.index("--") + 1:]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    start = last_progress = time.monotonic()
    last_cpu, warned = 0, False
    fd = proc.stdout.fileno()
    while True:
        ready, _, _ = select.select([fd], [], [], 1.0)
        if ready:
            chunk = os.read(fd, 65536)
            if chunk:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                last_progress = time.monotonic()
            elif proc.poll() is not None:
                return proc.returncode
        elif proc.poll() is not None:
            return proc.returncode
        now = time.monotonic()
        cpu = session_cpu(proc.pid)
        if cpu > last_cpu:
            last_cpu, last_progress = cpu, now
        if not warned and now - start >= soft:
            warned = True
            print(f"[run-watched] still running after {soft:g}s (soft limit; not killing)", file=sys.stderr)
        if now - last_progress >= hard:
            print(f"[run-watched] no output and no CPU for {hard:g}s -- hung; killing", file=sys.stderr)
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return 124


if __name__ == "__main__":
    raise SystemExit(main())
