"""Per-run resource collector for the fabric host agent: one ``resource_run`` event per ended agent session.

Each tick reads ONE ``ps`` listing (pid, ppid, rss, cpu time, elapsed time, process name; never argv or environment)
and finds the agent sessions the same way the host sampler counts them (``host_metrics.agent_roots``: a process
named like an agent binary with no agent ancestor). For each session it keeps, across ticks, the peak resident memory
of the session's process tree and the CPU time of every process seen in it (macOS, and Linux without a dedicated
cgroup), or on Linux the counters of the session's cgroup v2 when that cgroup holds only the session's processes
(``memory.peak`` / ``memory.current``, ``cpu.stat`` ``usage_usec``, ``memory.events`` ``oom_kill`` / ``max``).
Disk growth is the change in the bytes under the session's working directory between its first and its last
sighting, measured with a bounded walk; a walk that cannot finish inside its budget reports the growth as unknown
(``null``), never as a guessed zero.

A session ends when its process is gone (or its pid now belongs to a different process). The ended run is validated
with the control plane's own allow-list, queued in the state file and only then posted, oldest first. The state file
(sessions in flight, the queue, and the ids already delivered) survives a restart, so a session is reported once:
the server's ``(host, session_id)`` key makes a replay after a crash between "delivered" and "saved" a no-op.

Everything is bounded: rows read, sessions and processes tracked, queue length, delivered ids remembered, and the
time and entries a disk walk may spend. Nothing here learns or estimates; that belongs to the Slurm project.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .control_plane.resource_runs import (
    ENTITY_TYPE,
    EVENT_TYPE,
    valid_project,
    validate_resource_run,
)
from .host_metrics import DEFAULT_AGENT_BINARIES, _num, _run, agent_roots

log = logging.getLogger(__name__)

PS_RUN_ARGV = ["ps", "-A", "-o", "pid=,ppid=,rss=,time=,etime=,comm="]
STATE_VERSION = 1
MAX_PS_ROWS = 20_000
MAX_SESSIONS = 64
MAX_TREE = 512
MAX_OUTBOX = 256
MAX_SENT = 1024
MAX_CGROUP_PROCS = 4096
#: A tracked pid whose start time moved by more than this is a different process (the pid was reused).
START_TOLERANCE_S = 5.0
DISK_MAX_ENTRIES = 200_000
DISK_BUDGET_S = 3.0
#: Answers that mean the event will never be accepted: dropped, the queue moves on.
PERMANENT_STATUSES = frozenset({400, 409, 413, 422})
SOURCE = "fabric-agent"

_DURATION = re.compile(r"(?:(\d{1,5})-)?(?:(\d{1,9}):)?(\d{1,9}):(\d{1,2}(?:\.\d{1,6})?)")


def parse_duration(text: str) -> float | None:
    """``ps`` ``time`` / ``etime``: ``[dd-][hh:]mm:ss[.frac]`` (macOS lets minutes exceed 59) -> seconds."""
    match = _DURATION.fullmatch(text.strip()) if isinstance(text, str) else None
    if not match:
        return None
    days, hours, minutes, seconds = match.groups()
    return int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def parse_ps_runs(output: str) -> list[dict]:
    """Rows of ``PS_RUN_ARGV`` output; ``command`` is the basename of ``comm``, as ``host_metrics.parse_ps_rows``."""
    rows = []
    for line in output.splitlines()[:MAX_PS_ROWS]:
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        pid, ppid, rss = _num(fields[0]), _num(fields[1]), _num(fields[2])
        cpu, elapsed = parse_duration(fields[3]), parse_duration(fields[4])
        if None in (pid, ppid, rss, cpu, elapsed) or rss < 0:
            continue
        command = fields[5].strip().rsplit("/", 1)[-1] or fields[5].strip()
        rows.append({"pid": int(pid), "ppid": int(ppid), "rss_b": int(rss) * 1024, "cpu_s": cpu,
                     "elapsed_s": elapsed, "command": command[:64]})
    return rows


def process_tree(root: int, children: dict[int, list[int]]) -> list[int]:
    """``root`` and its descendants, breadth first, at most ``MAX_TREE`` pids."""
    tree, queue, seen = [], [root], {root}
    while queue and len(tree) < MAX_TREE:
        pid = queue.pop(0)
        tree.append(pid)
        for child in children.get(pid, ()):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return tree


def dir_bytes(path: str | None, deadline: float, max_entries: int = DISK_MAX_ENTRIES) -> int | None:
    """Apparent bytes of the regular files under ``path`` (symlinks not followed). ``None`` when the path is gone,
    unreadable at the top, or the walk would pass ``deadline`` or ``max_entries``: unknown, not zero."""
    if not path:
        return None
    total, entries, stack = 0, 0, [path]
    try:
        os.stat(path)
    except OSError:
        return None
    while stack:
        if time.monotonic() > deadline:
            return None
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    entries += 1
                    if entries > max_entries or time.monotonic() > deadline:
                        return None
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue   # a file removed mid-walk
        except OSError:
            continue               # a directory removed mid-walk or unreadable
    return total


# --------------------------------------------------------------------------- Linux cgroup v2

def cgroup_of(pid: int, proc_root: Path) -> str | None:
    """The cgroup v2 path of ``pid`` (``0::/user.slice/...``), or ``None`` (v1 only, the root, or the process is gone)."""
    try:
        text = (proc_root / str(pid) / "cgroup").read_text()[:4096]
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::/"):
            path = line[3:].strip()
            return path if path != "/" and ".." not in path.split("/") else None
    return None


def _kv(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines()[:64]:
        key, _, value = line.partition(" ")
        if value.strip().isdigit():
            out[key] = int(value)
    return out


def read_cgroup(cgroup_root: Path, path: str) -> dict | None:
    """Counters of one cgroup v2 directory; ``None`` when it has vanished."""
    folder = cgroup_root / path.lstrip("/")
    try:
        procs = set()
        with open(folder / "cgroup.procs") as stream:
            for index, line in enumerate(stream):
                if index >= MAX_CGROUP_PROCS:
                    return None   # far too many processes to be one agent's
                if line.strip().isdigit():
                    procs.add(int(line))
        cpu = _kv((folder / "cpu.stat").read_text())
        events = _kv((folder / "memory.events").read_text())
        peak = None
        for name in ("memory.peak", "memory.current"):
            try:
                peak = int((folder / name).read_text().strip())
                break
            except (OSError, ValueError):
                continue
    except (OSError, ValueError):
        return None
    if peak is None or "usage_usec" not in cpu:
        return None
    return {"procs": procs, "peak_b": peak, "usage_usec": cpu["usage_usec"],
            "oom_kill": events.get("oom_kill", 0), "max": events.get("max", 0)}


# --------------------------------------------------------------------------- defaults wired to the host

def default_cwd_of(pid: int) -> str | None:
    from .fabric_agent import _listener_cwd
    return _listener_cwd(pid, timeout=1.0)


def default_project_of(cwd: str) -> str | None:
    """The main checkout's directory name, so every worktree of a repo is one project. ``None`` outside a git
    checkout: a session started in ``$HOME`` or a folder of many repos has no project and is never reported (the
    cwd's own name would attribute it to the login name or to that folder)."""
    from .fabric_agent import ListenerGitMetadata
    git = shutil.which("git")
    if not git:
        return None
    out = ListenerGitMetadata._git(git, cwd, ["rev-parse", "--path-format=absolute", "--git-common-dir"],
                                   time.monotonic() + 1.0)
    if not out or not os.path.isabs(out):
        return None
    common = Path(out)
    return (common.parent.name if common.name == ".git" else common.name.removesuffix(".git")) or None


_SID = re.compile(r"run-[0-9a-f]{24}")


def _is_num(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and 0 <= value < 2 ** 63


def _opt(value, kind) -> bool:
    return value is None or isinstance(value, kind)


def _valid_cg(cg) -> bool:
    if cg is None:
        return True
    return (isinstance(cg, dict) and all(_is_num(cg.get(k)) for k in ("peak_b", "usage_usec"))
            and all(isinstance(cg.get(k), int) for k in ("oom_kill", "max"))
            and isinstance(cg.get("base"), dict) and all(_is_num(cg["base"].get(k)) for k in ("oom_kill", "max")))


def _valid_session(sid, s) -> bool:
    """A restored session must have every field ``tick`` reads, with the right type; anything else is dropped."""
    return (isinstance(sid, str) and bool(_SID.fullmatch(sid)) and isinstance(s, dict)
            and type(s.get("pid")) is int and s["pid"] > 0 and isinstance(s.get("agent_kind"), str)
            and _is_num(s.get("start")) and _is_num(s.get("last_seen")) and type(s.get("peak_b")) is int
            and s["peak_b"] >= 0 and _opt(s.get("project"), str) and _opt(s.get("cwd"), str)
            and (s.get("disk0") is None or type(s.get("disk0")) is int)
            and isinstance(s.get("cpu"), dict) and len(s["cpu"]) <= MAX_TREE
            and all(isinstance(k, str) and _is_num(v) for k, v in s["cpu"].items())
            and _opt(s.get("cgroup"), str) and _valid_cg(s.get("cg")))


def _valid_envelope(envelope) -> dict | None:
    """A queued event is re-validated with the control plane's allow-list and rebuilt from the clean payload."""
    if not isinstance(envelope, dict) or set(envelope) != {"source", "entity_type", "entity_id", "event_type",
                                                          "payload"}:
        return None
    try:
        payload = validate_resource_run(envelope["entity_id"], envelope["event_type"], envelope["payload"])
    except ValueError:
        return None
    return {"source": SOURCE, "entity_type": ENTITY_TYPE, "entity_id": payload["session_id"],
            "event_type": EVENT_TYPE, "payload": payload}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), UTC).isoformat()


class RunCollector:
    """Call ``tick()`` on the host agent's cadence. ``post(envelope) -> HTTP status | None`` delivers one event."""

    def __init__(self, *, host: str, state_path: Path, post: Callable[[dict], int | None],
                 agent_binaries=DEFAULT_AGENT_BINARIES, ps: Callable[[], str | None] | None = None,
                 cwd_of: Callable[[int], str | None] = default_cwd_of,
                 project_of: Callable[[str], str | None] = default_project_of,
                 linux: bool | None = None, proc_root: Path = Path("/proc"),
                 cgroup_root: Path = Path("/sys/fs/cgroup"), clock: Callable[[], float] = time.time,
                 disk_budget_s: float = DISK_BUDGET_S):
        self.host, self.state_path, self.post = host, Path(state_path), post
        self.binaries = tuple(agent_binaries)
        self.ps = ps or (lambda: _run(PS_RUN_ARGV))
        self.cwd_of, self.project_of = cwd_of, project_of
        self.linux = platform.system() == "Linux" if linux is None else linux
        self.proc_root, self.cgroup_root = Path(proc_root), Path(cgroup_root)
        self.clock, self.disk_budget_s = clock, disk_budget_s
        self.sessions: dict[str, dict] = {}
        self.outbox: list[dict] = []
        self.sent: list[str] = []
        self._load()

    # -- state -------------------------------------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("resource-run state unreadable; starting fresh")
            return
        if (not isinstance(data, dict) or data.get("version") != STATE_VERSION
                or not isinstance(data.get("sessions"), dict) or not isinstance(data.get("outbox"), list)
                or not isinstance(data.get("sent"), list)):
            log.warning("resource-run state has an unknown shape; starting fresh")
            return
        sessions = [(sid, s) for sid, s in data["sessions"].items() if _valid_session(sid, s)]
        outbox = [e for e in (_valid_envelope(e) for e in data["outbox"]) if e is not None]
        sent = [s for s in data["sent"] if isinstance(s, str) and _SID.fullmatch(s)]
        dropped = (len(data["sessions"]) - len(sessions)) + (len(data["outbox"]) - len(outbox)) \
            + (len(data["sent"]) - len(sent))
        if dropped:
            log.warning("resource-run state: dropped %d malformed entries", dropped)
        self.sessions = dict(sessions[:MAX_SESSIONS])
        self.outbox = outbox[-MAX_OUTBOX:]
        self.sent = sent[-MAX_SENT:]

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.state_path.with_suffix(".tmp")
        body = json.dumps({"version": STATE_VERSION, "sessions": self.sessions, "outbox": self.outbox,
                           "sent": self.sent}, separators=(",", ":"))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.state_path)

    # -- one tick ----------------------------------------------------------------------------------------

    def tick(self) -> list[dict]:
        """Observe once, end the sessions that are gone, deliver the queue. Returns the runs queued this tick."""
        now = self.clock()
        deadline = time.monotonic() + self.disk_budget_s
        text = self.ps()
        rows = parse_ps_runs(text) if text else []
        if not rows:
            # Nothing observed is not "every session ended": keep them all and only retry the queue.
            self.flush()
            return []
        children: dict[int, list[int]] = {}
        by_pid = {}
        for row in rows:
            by_pid[row["pid"]] = row
            children.setdefault(row["ppid"], []).append(row["pid"])
        roots = {r["pid"]: r for r in agent_roots(rows, self.binaries)}
        queued, alive = [], set()
        for sid, session in list(self.sessions.items()):
            root = roots.get(session["pid"])
            if (root is not None and root["command"] == session["agent_kind"]
                    and abs((now - root["elapsed_s"]) - session["start"]) <= START_TOLERANCE_S):
                self._sample(session, process_tree(root["pid"], children), by_pid, now)
                alive.add(root["pid"])
            else:
                del self.sessions[sid]
                run = self._finish(sid, session, deadline)
                if run is not None:
                    queued.append(run)
        for pid, root in roots.items():
            if pid in alive:
                continue
            if len(self.sessions) >= MAX_SESSIONS:
                log.warning("resource runs: more than %d agent sessions; the rest are not tracked", MAX_SESSIONS)
                break
            self._start(root, process_tree(pid, children), by_pid, now, deadline)
        self._save()
        self.flush()
        return queued

    def _start(self, root: dict, tree: list[int], by_pid: dict, now: float, deadline: float) -> None:
        start = now - root["elapsed_s"]
        sid = "run-" + hashlib.sha256(f"{self.host}|{root['pid']}|{int(start)}|{root['command']}"
                                      .encode()).hexdigest()[:24]
        cwd = self.cwd_of(root["pid"])
        project = None
        if cwd:
            try:
                project = valid_project(self.project_of(cwd))
            except ValueError:
                project = None   # not a project name the control plane accepts: tracked, never reported
        cgroup = cgroup_of(root["pid"], self.proc_root) if self.linux else None
        session = {"pid": root["pid"], "agent_kind": root["command"], "start": start, "last_seen": now,
                   "project": project, "cwd": cwd if project else None,
                   "disk0": dir_bytes(cwd, deadline) if project else None,
                   "peak_b": 0, "cpu": {}, "cgroup": cgroup, "cg": None}
        self.sessions[sid] = session
        self._sample(session, tree, by_pid, now)

    def _sample(self, session: dict, tree: list[int], by_pid: dict, now: float) -> None:
        session["last_seen"] = now
        session["peak_b"] = max(session["peak_b"], sum(by_pid[p]["rss_b"] for p in tree if p in by_pid))
        cpu = session["cpu"]
        for pid in tree:
            key = str(pid)
            if key in cpu or len(cpu) < MAX_TREE:
                cpu[key] = max(cpu.get(key, 0.0), by_pid[pid]["cpu_s"])
        if session["cgroup"] is None:
            return
        counters = read_cgroup(self.cgroup_root, session["cgroup"])
        if counters is None:
            return   # vanished with its last process: keep the last reading
        if not counters["procs"] or not counters["procs"] <= set(tree):
            # Shared with processes outside this session: its counters are not this run's.
            session["cgroup"], session["cg"] = None, None
            return
        previous = session["cg"]
        base = previous["base"] if previous else {"oom_kill": counters["oom_kill"], "max": counters["max"]}
        session["cg"] = {"peak_b": max(counters["peak_b"], previous["peak_b"] if previous else 0),
                         "usage_usec": counters["usage_usec"], "base": base,
                         "oom_kill": counters["oom_kill"] - base["oom_kill"], "max": counters["max"] - base["max"]}

    def _finish(self, sid: str, session: dict, deadline: float) -> dict | None:
        if session.get("project") is None or sid in self.sent or any(e["entity_id"] == sid for e in self.outbox):
            return None
        cg = session.get("cg") if session.get("cgroup") else None
        if cg:
            peak, cpu = cg["peak_b"], cg["usage_usec"] / 1e6
            oom, hit = cg["oom_kill"] > 0, cg["oom_kill"] > 0 or cg["max"] > 0
        else:
            peak, cpu = session["peak_b"], sum(session["cpu"].values())
            oom = hit = False
        before, after = session.get("disk0"), dir_bytes(session.get("cwd"), deadline)
        growth = max(0, after - before) if before is not None and after is not None else None
        payload = {"project": session["project"], "agent_kind": session["agent_kind"], "session_id": sid,
                   "host": self.host, "started_at": _iso(session["start"]),
                   "ended_at": _iso(max(session["last_seen"], session["start"])),
                   "peak_memory_bytes": int(peak), "cpu_seconds": round(cpu, 3), "disk_growth_bytes": growth,
                   "exit_reason": "oom" if oom else "exited", "hit_limit": hit}
        try:
            payload = validate_resource_run(sid, EVENT_TYPE, payload, now=datetime.fromtimestamp(self.clock(), UTC))
        except ValueError as exc:
            log.warning("resource run %s not reported: %s", sid, exc)
            return None
        self.outbox.append({"source": SOURCE, "entity_type": ENTITY_TYPE, "entity_id": sid,
                            "event_type": EVENT_TYPE, "payload": payload})
        if len(self.outbox) > MAX_OUTBOX:
            dropped = self.outbox.pop(0)
            log.warning("resource-run queue full; dropped the oldest run %s", dropped["entity_id"])
        return payload

    def flush(self) -> int:
        """Deliver the queue oldest first; stop at the first transient failure. Returns how many were delivered."""
        delivered = 0
        changed = False
        while self.outbox:
            envelope = self.outbox[0]
            try:
                status = self.post(envelope)
            except Exception as exc:  # noqa: BLE001 - delivery must never break the host agent
                log.warning("resource run delivery failed: %s", type(exc).__name__)
                status = None
            if status is not None and 200 <= status < 300:
                self.outbox.pop(0)
                self.sent = (self.sent + [envelope["entity_id"]])[-MAX_SENT:]
                delivered += 1
                changed = True
            elif status in PERMANENT_STATUSES:
                self.outbox.pop(0)
                log.warning("control plane refused resource run %s (HTTP %s); dropped", envelope["entity_id"], status)
                changed = True
            else:
                break
        if changed:
            self._save()
        return delivered
