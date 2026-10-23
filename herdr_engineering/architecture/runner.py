"""Supervisor: run the worker in a resource-limited, network-less, env-scrubbed child process."""
from __future__ import annotations

import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from . import gitsource

_WORKER = str(Path(__file__).with_name("worker.py"))
MAX_OUTPUT = 64 * 1024 * 1024


class AnalysisError(RuntimeError):
    pass


class AnalysisCancelled(AnalysisError):
    pass


@dataclass(frozen=True)
class Limits:
    wall_seconds: float = 300.0
    cpu_seconds: int = 240
    memory_bytes: int = 4 * 1024**3
    file_bytes: int = 128 * 1024**2
    open_files: int = 256


def _network_wrapper() -> list[str]:
    """``unshare -rn`` when unprivileged user namespaces work; else none (reported to the caller)."""
    unshare = shutil.which("unshare")
    if not unshare:
        return []
    try:
        ok = subprocess.run([unshare, "-rn", "true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return [unshare, "-rn", "--"] if ok else []


def _limiter(lim: Limits):
    def apply():
        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (lim.cpu_seconds, lim.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (lim.memory_bytes, lim.memory_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (lim.file_bytes, lim.file_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (lim.open_files, lim.open_files))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return apply


def run_isolated(repo_path: str, rev: str, host_id: str, repository_id: str, *, limits: Limits = Limits(),
                 cancel: threading.Event | None = None, worktree_id: str | None = None,
                 config: dict | None = None) -> dict:
    """Analyse ``rev`` of ``repo_path``. Returns {"snapshot", "graph", "sandbox"}."""
    req = {"repo_path": os.path.realpath(repo_path), "rev": rev, "host_id": host_id,
           "repository_id": repository_id, "worktree_id": worktree_id, **(config or {})}
    net = _network_wrapper()
    with tempfile.TemporaryDirectory(prefix="herdr-arch-") as cwd:
        env = gitsource.clean_env(home=cwd)
        cmd = [*net, sys.executable, "-I", _WORKER]
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, preexec_fn=_limiter(limits))
        out: dict = {}
        threads = [threading.Thread(target=lambda k=k, s=s: out.__setitem__(k, s.read(MAX_OUTPUT + 1)), daemon=True)
                   for k, s in (("out", proc.stdout), ("err", proc.stderr))]
        for t in threads:
            t.start()
        try:
            proc.stdin.write(json.dumps(req).encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass
        deadline = limits.wall_seconds
        waited = 0.0
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                _kill(proc)
                raise AnalysisCancelled("analysis cancelled")
            if waited >= deadline:
                _kill(proc)
                raise AnalysisError(f"analysis exceeded the {deadline:g}s wall-clock limit")
            try:
                proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                waited += 0.1
        for t in threads:
            t.join(5)
    if proc.returncode != 0:
        msg = (out.get("err") or b"").decode("utf-8", "replace").strip()[:300]
        raise AnalysisError(msg or f"worker exited {proc.returncode}")
    data = out.get("out") or b""
    if len(data) > MAX_OUTPUT:
        raise AnalysisError("analysis output exceeded the size limit")
    result = json.loads(data)
    result["sandbox"] = {"network_isolated": bool(net), "limits": limits.__dict__}
    return result


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait()
