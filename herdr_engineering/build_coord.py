"""Cross-agent build coordination (build-storm prevention).

Autonomous coding agents (pi, claude, codex, opencode, ...) frequently fire
full-workspace ``cargo check`` / ``tsc --noEmit`` builds independently. With no
shared gate, N agents launch N×``-j`` compiler jobs at once and the host goes
CPU-starved (observed: 24 cores, load 27→46, herdr + the dev-fabric gateway
unresponsive). This module provides a **per-host single-flight build gate**: a
fixed number of flock-based slots that bound the number of concurrently running
builds across ALL agents, plus load-aware backoff so new builds wait when the
machine is already oversubscribed.

Usage (any agent, any shell)::

    herdr-eng build-coordinate [--slots 2] [--max-load 1.5] [--wait 0] -- <cmd...>

The gate holds a slot (an exclusive ``flock`` on a well-known lock file) for the
lifetime of the child command. A slot is released automatically when the child
(and this wrapper) exit, so it cannot leak. The lock file also records which pid
currently holds it, for ``--status`` diagnostics.

The lock lives in a shared per-host directory (default
``~/.cache/herdr-engineering/build-gates/``), so every agent on the machine
contends for the same slots regardless of which repo/worktree it is building.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

try:
    import fcntl  # POSIX only (Linux/macOS); the gate is a no-op elsewhere
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX
    _HAS_FCNTL = False


def _default_state_dir() -> Path:
    return Path(os.environ.get(
        "HERDR_ENGINEERING_STATE",
        str(Path.home() / ".local/share/herdr-engineering/state"))) / "build-gates"


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _load_per_core() -> float:
    """One-minute load average divided by CPU count (0.0 when unknown)."""
    try:
        with open("/proc/loadavg") as f:
            one = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0
    cores = os.cpu_count() or 1
    return one / cores


class BuildGate:
    """Flock-based, per-host slot gate for build commands."""

    def __init__(self, state_dir: Path | str | None = None, slots: int = 2) -> None:
        self.dir = Path(state_dir) if state_dir else _default_state_dir()
        self.slots = max(1, int(slots))
        self.dir.mkdir(parents=True, exist_ok=True)

    def _slot_path(self, index: int) -> Path:
        return self.dir / f"gate.{index}"

    def _read_holder(self, path: Path) -> dict:
        """Best-effort read of the JSON payload in a slot file (no lock)."""
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def _try_acquire(self, index: int, cmd: list[str]) -> tuple[int, int] | None:
        """Try to take slot ``index`` non-blocking.

        Returns ``(fd, index)`` on success, else ``None``. On success the fd
        holds the flock and is intentionally left open across ``exec`` (FD_CLOEXEC
        cleared) so the lock is released only when the child exits.
        """
        if not _HAS_FCNTL:
            return (None, index)  # non-POSIX: no real gating, just pass through
        path = self._slot_path(index)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(fd)
            return None
        # We own the slot: record who holds it (best-effort, atomic-ish write).
        payload = {
            "pid": os.getpid(),
            "host": _hostname(),
            "slot": index,
            "start": _now_iso(),
            "cmd": cmd[:8],
        }
        try:
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps(payload).encode())
            os.fsync(fd)
        except OSError:
            pass
        # Keep the fd open across exec so the lock outlives this process.
        os.set_inheritable(fd, True)
        return (fd, index)

    def acquire(self, cmd: list[str], max_load: float = 0.0,
                wait_seconds: float = 0.0, progress=None) -> tuple[int | None, int]:
        """Acquire any free slot, waiting up to ``wait_seconds`` if all are busy.

        ``max_load`` > 0 means: do not start a new build while the 1-min load
        per core exceeds this ratio (back off even when a slot is free).
        ``wait_seconds`` <= 0 waits indefinitely. Returns ``(fd, slot_index)``.
        """
        deadline = None if wait_seconds <= 0 else time.monotonic() + wait_seconds
        reported = False
        while True:
            if max_load > 0 and _load_per_core() > max_load:
                time.sleep(0.5)
                continue
            for index in range(self.slots):
                got = self._try_acquire(index, cmd)
                if got is not None:
                    return got
            # All slots busy: report once, then wait for one to free up.
            if not reported:
                holders = [
                    self._read_holder(self._slot_path(i))
                    for i in range(self.slots)
                ]
                if progress:
                    progress(holders)
                self._telemetry({
                    "event": "contention",
                    "slots": self.slots,
                    "max_load": max_load,
                    "busy_pids": [h.get("pid") for h in holders if h],
                    "load_core": _load_per_core(),
                })
                reported = True
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    "build gate busy: no slot free within "
                    f"{wait_seconds:.0f}s "
                    f"(slots={self.slots}, holders={holders!r})")
            time.sleep(0.5)

    def _telemetry(self, payload: dict) -> None:
        """Append one JSON line to ``{dir}/telemetry.jsonl`` (best-effort).

        Feeds data-driven tuning of ``--slots``/``--max-load``: over time this
        records how often builds had to wait for a slot and how loaded the host
        was, so an operator can tell whether the gate is too conservative
        (high wait frequency, mostly free slots) or not conservative enough
        (constant contention, load/core still high).
        """
        try:
            payload["ts"] = _now_iso()
            payload["host"] = _hostname()
            with open(self.dir / "telemetry.jsonl", "a") as f:
                f.write(json.dumps(payload, sort_keys=True) + "\n")
        except OSError:
            pass

    def status(self) -> list[dict]:
        """Report current slot occupancy for diagnostics.

        ``held`` reflects the actual flock state (probed non-blocking), not the
        (possibly stale) payload text in the lock file, so a slot that was
        released shows as free even if its holder payload was never cleared.
        """
        out = []
        for index in range(self.slots):
            path = self._slot_path(index)
            holder = self._read_holder(path)
            held = False
            if _HAS_FCNTL:
                try:
                    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
                except OSError:
                    fd = None
                if fd is not None:
                    try:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except (BlockingIOError, OSError):
                            held = True  # another process holds this slot
                    finally:
                        os.close(fd)  # releases any lock we briefly acquired
            out.append({"slot": index, "held": held, "holder": holder})
        return out


def run_gated(cmd: list[str], *, slots: int = 2, max_load: float = 1.5,
              wait_seconds: float = 0.0, json_out: bool = False,
              state_dir: Path | None = None,
              bypass_on_timeout: bool = False) -> int:
    """Run ``cmd`` under the per-host build gate.

    This is the entry point for ``herdr-eng build-coordinate``. It acquires a
    slot, then ``exec``s the child (replacing this process) so the flock is
    held for the child's full lifetime and released automatically on exit.
    Returns the child's exit code (or a structured error for --json).

    When ``bypass_on_timeout`` is set and no slot frees up within
    ``wait_seconds``, the command is run **ungated** instead of failing. This
    makes the gate best-effort coordination: it serializes builds when slots
    are available (the normal case), but never hangs or fails an agent's build
    under sustained overload (where waiting could otherwise deadlock).
    """
    if not cmd:
        if json_out:
            print(json.dumps({"error": "no command given"}, sort_keys=True))
        else:
            print("build-coordinate: no command given", file=sys.stderr)
        return 2

    gate = BuildGate(state_dir=state_dir, slots=slots)

    def _progress(holders: list) -> None:
        if json_out:
            return
        busy = [h.get("pid") for h in holders if h]
        print(
            f"build-coordinate: all {slots} build slot(s) busy "
            f"(load/core={_load_per_core():.2f}); waiting for a slot "
            f"[busy pids: {', '.join(map(str, busy)) or 'none'}]",
            file=sys.stderr)

    try:
        fd, _slot = gate.acquire(cmd, max_load=max_load,
                                 wait_seconds=wait_seconds, progress=_progress)
    except TimeoutError as exc:
        if bypass_on_timeout:
            # Best-effort: run ungated rather than fail the build. Log to
            # stderr so an operator can see builds ran outside the gate.
            print(f"build-coordinate: {exc}; running ungated (bypass-on-timeout)",
                  file=sys.stderr)
            try:
                os.execvp(cmd[0], cmd)
            except OSError as e:
                if json_out:
                    print(json.dumps({"error": f"could not exec {cmd[0]}: {e}"},
                                     sort_keys=True))
                else:
                    print(f"build-coordinate: could not exec {cmd[0]}: {e}",
                          file=sys.stderr)
                return 127
        if json_out:
            print(json.dumps({"error": str(exc), "slots": gate.status()},
                             sort_keys=True))
        else:
            print(f"build-coordinate: {exc}", file=sys.stderr)
        return 1

    try:
        os.execvp(cmd[0], cmd)  # replaces this process; flock held by fd
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if json_out:
            print(json.dumps({"error": f"could not exec {cmd[0]}: {exc}"},
                             sort_keys=True))
        else:
            print(f"build-coordinate: could not exec {cmd[0]}: {exc}",
                  file=sys.stderr)
        return 127
