"""Opt-in build instrumentation for ``herdr-eng build-coordinate --record``.

Measures one coordinated build: wall time, CPU time (user+sys incl. reaped
children), peak RSS, block writes, sampled per-process logical write bytes,
host disk write delta, and ``sccache`` counters; emits typed
``build_started`` / ``build_finished`` JSON lines to ``<state>/builds.jsonl``.

Rules this module keeps:

* Never fail a build because of metrics: every measurement is wrapped, and any
  exception is swallowed (one stderr line per process at most).
* Missing data is explicit ``None`` and is named in ``unavailable``; values are
  never guessed or zero-filled.
* Low cardinality / no secrets: only ``cmd_kind`` (allowlist) is recorded --
  never argv values, cwd or environment.
* Honest labels: ``proc_io_write_bytes`` is *logical* (``/proc/<pid>/io``
  write_bytes, sampled; short-lived processes can be missed, so it is a lower
  bound); ``host_disk_write_bytes_delta`` is device- or host-wide and includes
  other activity ("physical-ish").
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NotRequired, TypedDict

SCCACHE_TIMEOUT_S = 2.0
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_EVENT_BYTES = 4000
SAMPLE_INTERVAL_S = 0.1
_SECTOR = 512
_CMD_KINDS = ("cargo build", "cargo test", "cargo check", "cargo clippy", "cargo nextest",
              "tsc", "rustc")
TRACE_ID_MAX = 64
_TRACE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
_SECRETISH = ("token", "secret", "passw", "key", "cred", "auth", "bearer")
_SECRET_PREFIXES = ("sk-", "ghp_", "gho_", "ghs_", "github_pat", "xox", "akia", "asia", "eyj")

FINISHED_FIELDS = (
    "event", "ts", "build_id", "host", "slot", "cmd_kind", "exit_code",
    "elapsed_s", "user_cpu_s", "sys_cpu_s", "max_rss_kb", "rusage_oublock",
    "proc_io_write_bytes", "host_disk_write_bytes_delta", "host_disk_scope",
    "sccache_stats", "unavailable", "cache",
)


class BuildStarted(TypedDict):
    event: str
    ts: str
    build_id: str
    host: str
    slot: int | None
    cmd_kind: str
    cache: dict[str, Any] | None
    trace: NotRequired[dict[str, str]]


class BuildFinished(TypedDict):
    event: str
    ts: str
    build_id: str
    host: str
    slot: int | None
    cmd_kind: str
    exit_code: int
    elapsed_s: float
    user_cpu_s: float | None
    sys_cpu_s: float | None
    max_rss_kb: int | None
    rusage_oublock: int | None
    proc_io_write_bytes: int | None  # logical, sampled lower bound
    host_disk_write_bytes_delta: int | None
    host_disk_scope: str | None  # "device:<name>" | "host_wide_physical_ish"
    sccache_stats: dict[str, float] | None
    unavailable: list[str]
    cache: dict[str, Any] | None  # spec 03 plan outcome; never paths/env values
    trace: NotRequired[dict[str, str]]  # spec 08 agent trace; ids only


_warned = False


def _warn(msg: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        try:
            print(f"build-metrics: {msg} (metrics skipped; build unaffected)",
                  file=sys.stderr)
        except Exception:
            pass


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def sanitize_trace_id(value: object) -> str | None:
    """Return ``value`` if it is a short, plainly non-secret identifier, else None.

    Charset ``[A-Za-z0-9._:-]``, 1..64 chars, first char alphanumeric; anything
    that looks like a credential (known prefixes, secret-ish words, or a long
    separator-free alphanumeric blob) is refused rather than recorded.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= TRACE_ID_MAX:
        return None
    if _TRACE_ID_RE.fullmatch(value) is None:
        return None
    low = value.lower()
    if low.startswith(_SECRET_PREFIXES) or any(w in low for w in _SECRETISH):
        return None
    if len(value) >= 40 and value.isalnum():
        return None
    return value


def make_trace(session: object, mission: object = None) -> dict[str, str] | None:
    """Agent trace fields for build events: ids only, never argv/env/secrets.

    A session id is required (without one there is nothing to correlate);
    an invalid mission id is dropped on its own.
    """
    sid = sanitize_trace_id(session)
    if sid is None:
        return None
    trace = {"session": sid, "source": "pi"}
    mid = sanitize_trace_id(mission)
    if mid is not None:
        trace["mission"] = mid
    return trace


def classify_cmd(cmd: list[str]) -> str:
    """Map a command to a small allowlisted kind; never returns argv values."""
    try:
        toks = [os.path.basename(cmd[0])] + list(cmd[1:])
        if toks[0] == "npx" and len(toks) > 1:
            toks = toks[1:]
        if toks[0] == "tsc":
            return "tsc"
        if toks[0] == "rustc":
            return "rustc"
        if toks[0] == "cargo":
            rest = [t for t in toks[1:] if not t.startswith("+")]
            if rest:
                kind = f"cargo {rest[0]}"
                if kind in _CMD_KINDS:
                    return kind
    except (IndexError, TypeError):
        pass
    return "other"


# --- OS counters ------------------------------------------------------------

def _children_map() -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                data = f.read()
            ppid = int(data[data.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        out.setdefault(ppid, []).append(int(name))
    return out


def _descendants(root: int) -> list[int]:
    kids = _children_map()
    seen, stack = [], [root]
    while stack:
        pid = stack.pop()
        seen.append(pid)
        stack.extend(kids.get(pid, []))
    return seen


def _write_bytes(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/io") as f:
            for line in f:
                if line.startswith("write_bytes:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def read_proc_io_tree(root: int, seen: dict[int, int]) -> None:
    """Update ``seen`` (pid -> max write_bytes observed) for root + descendants."""
    for pid in _descendants(root):
        wb = _write_bytes(pid)
        if wb is not None and wb >= seen.get(pid, -1):
            seen[pid] = wb


def _diskstats() -> dict[str, tuple[int, int, int]]:
    """name -> (major, minor, sectors_written)."""
    out = {}
    with open("/proc/diskstats") as f:
        for line in f:
            p = line.split()
            if len(p) >= 10:
                out[p[2]] = (int(p[0]), int(p[1]), int(p[9]))
    return out


def host_disk_sectors(cwd: str | os.PathLike[str] | None = None) -> tuple[str, int] | None:
    """(scope, sectors written) for the cwd's device, else host-wide sum."""
    try:
        stats = _diskstats()
    except (OSError, ValueError):
        return None
    try:
        dev = os.stat(cwd or ".").st_dev
        mm = (os.major(dev), os.minor(dev))
        for name, (maj, mnr, sec) in stats.items():
            if (maj, mnr) == mm:
                return (f"device:{name}", sec)
    except OSError:
        pass
    total, found = 0, False
    for name, (_, _, sec) in stats.items():
        if name.startswith(("loop", "ram", "dm-", "zram", "sr", "fd")):
            continue
        if os.path.isdir(f"/sys/block/{name}"):  # whole device, not a partition
            total += sec
            found = True
    return ("host_wide_physical_ish", total) if found else None


def sccache_snapshot(timeout: float = SCCACHE_TIMEOUT_S) -> dict[str, Any] | None:
    exe = shutil.which("sccache")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--show-stats", "--stats-format", "json"],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        return data if isinstance(data, dict) else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _numeric_leaves(obj: Any, prefix: str = "") -> dict[str, float]:
    out: dict[str, float] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_numeric_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out[prefix] = obj
    return out


def diff_stats(before: dict[str, Any], after: dict[str, Any]) -> dict[str, float]:
    """Nonzero deltas of numeric leaves (under ``stats`` if present)."""
    def base(d: dict[str, Any]) -> dict[str, Any]:
        s = d.get("stats")
        return s if isinstance(s, dict) else d
    b, a = _numeric_leaves(base(before)), _numeric_leaves(base(after))
    delta = {k: a[k] - b.get(k, 0) for k in a if a[k] - b.get(k, 0) != 0}
    return dict(list(delta.items())[:64])


# --- event log --------------------------------------------------------------

def append_event(state_dir: Path | str, event: dict[str, Any],
                 max_bytes: int = MAX_LOG_BYTES) -> bool:
    """Append one JSON line (O_APPEND, 0600); rotate to .1 above ``max_bytes``."""
    try:
        d = Path(state_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "builds.jsonl"
        line = json.dumps(event, sort_keys=True, separators=(",", ":"))
        if len(line) > MAX_EVENT_BYTES:
            slim = dict(event, sccache_stats=None)
            slim["unavailable"] = sorted(set(event.get("unavailable", [])) | {"sccache_stats"})
            line = json.dumps(slim, sort_keys=True, separators=(",", ":"))
        try:
            if path.stat().st_size > max_bytes:
                os.replace(path, d / "builds.jsonl.1")
        except FileNotFoundError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)
        return True
    except Exception as exc:  # noqa: BLE001 - metrics must never break a build
        _warn(f"could not write builds.jsonl: {type(exc).__name__}")
        return False


def _pct(vals: list[float], q: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def summarize(path: Path | str) -> dict[str, Any]:
    """Counts, p50/p95 elapsed per cmd_kind, total writes, unavailable fields."""
    elapsed: dict[str, list[float]] = {}
    failed = 0
    io_total = 0
    host_total = 0
    unavailable: set[str] = set()
    n = 0
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") != "build_finished":
            continue
        n += 1
        failed += 1 if e.get("exit_code") else 0
        if isinstance(e.get("elapsed_s"), (int, float)):
            elapsed.setdefault(e.get("cmd_kind", "other"), []).append(e["elapsed_s"])
        io_total += e.get("proc_io_write_bytes") or 0
        host_total += e.get("host_disk_write_bytes_delta") or 0
        unavailable.update(e.get("unavailable", []))
    return {
        "builds": n,
        "failed": failed,
        "by_cmd_kind": {
            k: {"count": len(v), "p50_elapsed_s": _pct(v, 0.5), "p95_elapsed_s": _pct(v, 0.95)}
            for k, v in sorted(elapsed.items())
        },
        "total_proc_io_write_bytes": io_total,
        "total_host_disk_write_bytes_delta": host_total,
        "unavailable_seen": sorted(unavailable),
    }


# --- running a measured child -------------------------------------------------

@dataclass
class Measured:
    exit_code: int
    finished: dict[str, Any]


def _exit_code(status: int) -> int:
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


def run_measured(cmd: list[str], *, state_dir: Path | str | None = None,
                 slot: int | None = None, cwd: str | None = None,
                 emit: bool = True, env: dict[str, str] | None = None,
                 cache: dict[str, Any] | None = None,
                 registry: bool = False,
                 trace: dict[str, str] | None = None) -> Measured:
    """Fork+exec ``cmd``, wait with wait4, return its exit code and metrics.

    The caller keeps any lock fds open for the duration of this call (inheritable
    fds are inherited by the child like with exec). SIGINT/SIGTERM received by
    this process are forwarded to the child only. Raises ``OSError`` if the
    command cannot be started (callers map that to exit 127).

    With ``registry`` (opt-in, spec 05) the build is also recorded in the durable
    build registry with an owner lease; any registry fault is swallowed and named
    ``registry_unavailable`` in the finished event's ``unavailable`` list.
    """
    unavailable: list[str] = []
    build_id = str(uuid.uuid4())
    host = _safe(socket.gethostname, "unknown")
    kind = classify_cmd(cmd)
    disk0 = _safe(lambda: host_disk_sectors(cwd), None)
    scc0 = _safe(sccache_snapshot, None)
    run = None
    extra: dict[str, Any] = {}
    if registry and state_dir is not None:
        from .build_registry import BuildRun
        run = BuildRun.open(
            state_dir, cwd=os.path.realpath(cwd or os.getcwd()), cmd_kind=kind, host=host,
            cmd=cmd, build_id=build_id,
            toolchain_digest=(cache or {}).get("fingerprint"),
            owner_session=(trace or {}).get("session"),
            mission_id=(trace or {}).get("mission"))
        if run.build_id:
            build_id = run.build_id
            extra = run.ids()
    if trace:
        extra = {**extra, "trace": dict(trace)}
    if emit and state_dir is not None:
        append_event(state_dir, {**BuildStarted(
            event="build_started", ts=_now_iso(), build_id=build_id, host=host,
            slot=slot, cmd_kind=kind, cache=cache), **extra})

    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, close_fds=False, env=env)  # may raise OSError
    except BaseException:
        if run is not None:
            run.on_exit(None, spawn_failed=True)
        raise
    pid = proc.pid

    exited = threading.Event()  # set once the child is a zombie: never signal its pid then

    def forward(signum: int, _frame: Any) -> None:
        if exited.is_set():
            return
        if run is not None:
            run.note_forwarded_signal()
        try:
            os.kill(pid, signum)
        except OSError:
            pass

    old: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for s in (signal.SIGINT, signal.SIGTERM):
            old[s] = signal.signal(s, forward)

    if run is not None:
        run.on_spawn(pid)  # after handlers: a signal during this write is forwarded

    seen: dict[int, int] = {}
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            try:
                read_proc_io_tree(pid, seen)
            except Exception as exc:  # noqa: BLE001
                _warn(f"io sampling failed: {type(exc).__name__}")
                return
            stop.wait(SAMPLE_INTERVAL_S)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    try:
        # Wait WITHOUT reaping: while the child is a zombie its pid cannot be
        # recycled, so signal forwarding and the heartbeat/cancel thread are
        # shut down first and only then is the pid released with wait4.
        while True:
            try:
                os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
                break
            except InterruptedError:
                continue
        exited.set()
        if run is not None:
            run.halt()
        while True:
            try:
                _, status, ru = os.wait4(pid, 0)
                break
            except InterruptedError:
                continue
    finally:
        exited.set()
        stop.set()
        th.join(timeout=1)
        for s, h in old.items():
            signal.signal(s, h)
    proc.returncode = 0  # reaped via wait4; keep Popen from re-waiting
    elapsed = time.monotonic() - t0
    code = _exit_code(status)
    if run is not None:
        run.on_exit(code, signaled=os.WIFSIGNALED(status))
        if run.unavailable:
            unavailable.append("registry_unavailable")

    disk1 = _safe(lambda: host_disk_sectors(cwd), None)
    scc1 = _safe(sccache_snapshot, None)
    sccache_delta = None
    if scc0 is not None and scc1 is not None:
        sccache_delta = _safe(lambda: diff_stats(scc0, scc1), None)
    if sccache_delta is None:
        unavailable.append("sccache_stats")
    host_delta = scope = None
    if disk0 and disk1 and disk0[0] == disk1[0]:
        host_delta, scope = max(0, disk1[1] - disk0[1]) * _SECTOR, disk0[0]
    else:
        unavailable += ["host_disk_write_bytes_delta", "host_disk_scope"]
    io_bytes: int | None = sum(seen.values()) if seen else None
    if io_bytes is None:
        unavailable.append("proc_io_write_bytes")
    fin = BuildFinished(
        event="build_finished", ts=_now_iso(), build_id=build_id, host=host,
        slot=slot, cmd_kind=kind, exit_code=code, elapsed_s=round(elapsed, 4),
        user_cpu_s=round(ru.ru_utime, 4), sys_cpu_s=round(ru.ru_stime, 4),
        max_rss_kb=ru.ru_maxrss, rusage_oublock=ru.ru_oublock,
        proc_io_write_bytes=io_bytes, host_disk_write_bytes_delta=host_delta,
        host_disk_scope=scope, sccache_stats=sccache_delta,
        unavailable=sorted(unavailable), cache=cache)
    out = {**fin, **extra}
    if emit and state_dir is not None:
        append_event(state_dir, out)
    return Measured(code, out)


def _safe(fn: Any, default: Any) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        _warn(f"measurement failed: {type(exc).__name__}")
        return default
