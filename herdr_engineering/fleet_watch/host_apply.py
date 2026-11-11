"""`herdr-eng fleet host-apply`: the per-host half of a rollout (spec sections 4 D6/D7, 5, 6.2, 6.3, 14).

Runs on the host being updated, from the INSTALLED build in a neutral working directory (never `python -m` inside a
checkout, D6). One invocation is one phase for one repo:

    precheck   D6 gate on the deployment checkout + installed-sha comparison (idempotency, read-only, offline)
    apply      re-run the gate, take `fleet-auto-update.lock`, update the host (checkout ff-only + install, or Pi stage +
               atomic `current` symlink switch)
    health     the per-repo health checks of spec section 5
    rollback   D8, only where it is a pure pointer flip (Pi `current`, or an AutoSpec runtime that never left its
               previous generation); anything else answers skipped(not_applicable)
    all        precheck, apply, health (default; used by an operator)

The result is ONE JSON object on stdout built only from closed enums, lowercase hex, integers, booleans and UTC
timestamps (decision 2026-11-05: no free text). Everything a human needs to read (command output, example paths of a
dirty checkout) goes to the host-side log store (`logs.LogStore`, 0600) and is referenced by SHA-256.

Operator/test seams (environment, all optional): `HERDR_FLEET_<X>_CMD` replaces the real install, stage, smoke or
service command for `<X>`; the command is split with shlex and receives `HERDR_FLEET_TARGET_SHA`,
`HERDR_FLEET_TARGET_REPO` and `HERDR_FLEET_BEFORE_SHA` in its environment.

Deliberately does not import `herdr_engineering.fleet` at import time (it rewrites sys.path); the optional parity check
imports it lazily.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..control_plane.fleet_rollout import CHECK_NAMES, HOST_ORDER, LOG_KINDS, REASONS, STATUSES
from . import logs

SCHEMA_VERSION = 1
PHASES = ("precheck", "apply", "health", "rollback", "all")
APPLIED_REPOS = ("herdr-engineering", "pi-engineering", "autospec")
PHASE2_REPOS = ("ai-sync", "claude-config")
KEEP_RELEASES = 3
EXAMPLE_PATHS = 8
#: Budget of a single install/stage command, below the runner's 30 minute apply timeout.
COMMAND_TIMEOUT_S = 1500
GIT_TIMEOUT_S = 120
BUSY_RETRIES = 5
BUSY_WAIT_S = 60

_SHA = re.compile(r"[0-9a-f]{40}")
_ID = re.compile(r"[0-9a-f]{16}")


class Busy(RuntimeError):
    """`fleet-auto-update.lock` is held (the nightly timer or another host-apply)."""


class PhaseStop(Exception):
    """Ends a phase early with a closed (status, reason)."""

    def __init__(self, status: str, reason: str | None = None):
        super().__init__(f"{status}:{reason}")
        self.status, self.reason = status, reason


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Context:
    rollout_id: str
    repo: str
    sha: str
    host: str | None
    before_sha: str | None = None            # given by the runner for rollback
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    busy_retries: int = BUSY_RETRIES
    busy_wait_s: float = BUSY_WAIT_S
    log: list[str] = field(default_factory=list)

    @property
    def home(self) -> Path:
        return Path(self.env.get("HOME") or Path.home())

    def note(self, line: str) -> None:
        self.log.append(line)


# --------------------------------------------------------------------------- process + git helpers

def _run(ctx: Context, argv: list[str], *, cwd: str | Path | None = None, timeout: float | None = None,
         extra_env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run a real command in its own process group; 124 on timeout (the whole group is killed). Output is logged."""
    env = {**ctx.env, "HERDR_FLEET_TARGET_SHA": ctx.sha, "HERDR_FLEET_TARGET_REPO": ctx.repo,
           "HERDR_FLEET_BEFORE_SHA": ctx.before_sha or "", "GIT_TERMINAL_PROMPT": "0", **(extra_env or {})}
    timeout = COMMAND_TIMEOUT_S if timeout is None else timeout
    ctx.note("$ " + shlex.join(argv))
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd) if cwd else tempfile.gettempdir(), env=env, text=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except OSError as exc:
        ctx.note(f"cannot start: {exc}")
        return 127, ""
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        ctx.note(f"timed out after {timeout}s")
        ctx.note(out or "")
        return 124, out or ""
    ctx.note(out or "")
    ctx.note(f"exit {proc.returncode}")
    return proc.returncode, out or ""


def _git(ctx: Context, root: Path, *args: str, timeout: float = GIT_TIMEOUT_S) -> tuple[int, str]:
    rc, out = _run(ctx, ["git", "-C", str(root), *args], timeout=timeout)
    return rc, out.strip()


def _override(ctx: Context, name: str) -> list[str] | None:
    value = ctx.env.get(f"HERDR_FLEET_{name}_CMD")
    return shlex.split(value) if value is not None else None


# --------------------------------------------------------------------------- D6 gate

def checkout_gate(ctx: Context, root: Path) -> dict[str, int]:
    """D6: branch main, porcelain (incl. untracked) empty, origin/main..HEAD == 0. Never cleans, stashes or resets.

    Returns the counts; raises PhaseStop(blocked, reason) with the counts attached. Example paths go to the log only.
    """
    if not (root / ".git").exists():
        ctx.note(f"no git checkout at {root}")
        raise PhaseStop("failed", "apply_failed")
    rc, branch = _git(ctx, root, "branch", "--show-current")
    rc2, porcelain = _git(ctx, root, "status", "--porcelain", "--untracked-files=all")
    rc3, ahead = _git(ctx, root, "rev-list", "--count", "origin/main..HEAD")
    rc4, behind = _git(ctx, root, "rev-list", "--count", "HEAD..origin/main")
    if rc or rc2 or rc3:
        raise PhaseStop("failed", "apply_failed")
    paths = [line for line in porcelain.splitlines() if line]
    counts = {"dirty_paths": len(paths), "unpublished_commits": int(ahead),
              "behind": int(behind) if rc4 == 0 and behind.isdigit() else 0}
    reason = None
    if branch != "main":
        reason = "not_on_main"
    elif paths:
        reason = "dirty_checkout"
    elif counts["unpublished_commits"]:
        reason = "unpublished_commits"
    if reason:
        for line in paths[:EXAMPLE_PATHS]:
            ctx.note(f"dirty: {line[:160]}")
        stop = PhaseStop("blocked", reason)
        stop.counts = counts            # type: ignore[attr-defined]
        raise stop
    return counts


def _head(ctx: Context, root: Path) -> str | None:
    rc, out = _git(ctx, root, "rev-parse", "HEAD")
    return out if rc == 0 and _SHA.fullmatch(out) else None


def _ff_to(ctx: Context, root: Path, sha: str) -> None:
    """Fetch origin/main, require `sha` to be published (ancestor of origin/main), fast-forward only."""
    rc, _ = _git(ctx, root, "fetch", "--quiet", "origin", "main")
    if rc == 124:
        raise PhaseStop("failed", "timeout")
    if rc:
        raise PhaseStop("failed", "apply_failed")
    if _git(ctx, root, "cat-file", "-e", f"{sha}^{{commit}}")[0] or \
            _git(ctx, root, "merge-base", "--is-ancestor", sha, "refs/remotes/origin/main")[0]:
        raise PhaseStop("blocked", "not_published")
    rc, _ = _git(ctx, root, "merge", "--ff-only", "--quiet", sha)
    if rc:
        raise PhaseStop("failed", "apply_failed")


def _checked(ctx: Context, argv: list[str], *, cwd: str | Path | None = None, reason: str = "apply_failed") -> None:
    rc, _ = _run(ctx, argv, cwd=cwd)
    if rc == 124:
        raise PhaseStop("failed", "timeout")
    if rc:
        raise PhaseStop("failed", reason)


# --------------------------------------------------------------------------- lock (spec 6.3)

def lock_path(env: dict[str, str]) -> Path:
    root = env.get("XDG_STATE_HOME") or str(Path(env.get("HOME") or Path.home()) / ".local" / "state")
    return Path(root) / "herdr-engineering" / "fleet-auto-update.lock"


@contextlib.contextmanager
def update_lock(ctx: Context) -> Iterator[None]:
    """The same flock the nightly updater takes. Held -> retry `busy_wait_s` apart, `busy_retries` times, then Busy."""
    path = lock_path(ctx.env)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as handle:
        for attempt in range(ctx.busy_retries + 1):
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt == ctx.busy_retries:
                    raise Busy("fleet-auto-update.lock is held") from None
                time.sleep(ctx.busy_wait_s)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- herdr-engineering

class HerdrEngineering:
    def root(self, ctx: Context) -> Path:
        configured = ctx.env.get("HERDR_ENGINEERING_REPO")
        if not configured:
            ctx.note("HERDR_ENGINEERING_REPO is not set")
            raise PhaseStop("failed", "apply_failed")
        return Path(configured)

    def installed(self, ctx: Context) -> str | None:
        root = self.root(ctx)
        return _head(ctx, root) if (root / ".git").exists() else None

    def gate(self, ctx: Context) -> dict[str, int]:
        return checkout_gate(ctx, self.root(ctx))

    def apply(self, ctx: Context) -> None:
        root = self.root(ctx)
        _ff_to(ctx, root, ctx.sha)
        install = _override(ctx, "HERDR_INSTALL")
        _checked(ctx, install or [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", str(root)])
        agent = _override(ctx, "HERDR_AGENT")
        if agent is not None:
            if agent:
                _checked(ctx, agent)
        elif sys.platform.startswith("linux") and shutil.which("systemctl"):
            if _run(ctx, ["systemctl", "--user", "is-enabled", "--quiet", "herdr-eng-fabric-agent.service"])[0] == 0:
                _checked(ctx, ["systemctl", "--user", "restart", "herdr-eng-fabric-agent.service"])
        elif sys.platform == "darwin" and shutil.which("launchctl"):
            label = f"gui/{os.getuid()}/com.herdr-engineering.fabric-agent"
            if _run(ctx, ["launchctl", "print", label])[0] == 0:
                _checked(ctx, ["launchctl", "kickstart", "-k", label])

    def checks(self, ctx: Context) -> list[dict]:
        out = [_check("version_matches", self.installed(ctx) == ctx.sha)]
        service = _override(ctx, "HERDR_SERVICE")
        if service is None and sys.platform.startswith("linux") and shutil.which("systemctl"):
            service = ["systemctl", "--user", "is-active", "--quiet", "herdr-eng-fabric-agent.service"]
        elif service is None and sys.platform == "darwin" and shutil.which("launchctl"):
            service = ["launchctl", "print", f"gui/{os.getuid()}/com.herdr-engineering.fabric-agent"]
        out.append(_timed("service_active", lambda: _run(ctx, service, timeout=30)[0] == 0)
                   if service else _check("service_active", None))
        out.append(_check("gateway_registered", None))      # needs the gateway API: not part of step 4
        out.append(self._parity(ctx))
        return out

    def _parity(self, ctx: Context) -> dict:
        override = _override(ctx, "PARITY")
        if override is not None:
            return _timed("parity_ok", lambda: _run(ctx, override, timeout=100)[0] == 0)
        root = self.root(ctx)
        if not ctx.host or not shutil.which("ansible-playbook") or not (root / "ansible" / "inventory" / "hosts.yml").exists():
            return _check("parity_ok", None)

        def parity() -> bool:
            from .. import fleet  # lazy: rewrites sys.path at import time
            status = fleet.fleet_status(inventory=root / "ansible" / "inventory" / "hosts.yml", host=ctx.host)
            return status.get("rc") == 0 and any(
                h.get("host") == ctx.host and h.get("revision") == ctx.sha and h.get("agent") == "active"
                for h in status.get("hosts", []))
        return _timed("parity_ok", parity)

    def rollback(self, ctx: Context) -> str | None:
        return None        # a checkout cannot go back with ff-only and the spec forbids reset: not a pointer flip


# --------------------------------------------------------------------------- autospec

class Autospec:
    def root(self, ctx: Context) -> Path:
        return Path(ctx.env.get("HERDR_FLEET_AUTOSPEC_CHECKOUT") or ctx.home / "autospec-runtime-main")

    def generations(self, ctx: Context) -> Path:
        return Path(ctx.env.get("HERDR_FLEET_AUTOSPEC_GENERATIONS") or ctx.home / ".autospec" / "runtime-generations")

    def active_head(self, ctx: Context) -> str | None:
        """`head=` of the newest generation receipt: the runtime the host is on."""
        receipts = sorted(self.generations(ctx).glob("*/receipt"), key=lambda p: p.stat().st_mtime, reverse=True)
        for receipt in receipts[:1]:
            match = re.search(r"^head=([0-9a-f]{40})$", receipt.read_text(), re.M)
            return match.group(1) if match else None
        return None

    def installed(self, ctx: Context) -> str | None:
        try:
            return self.active_head(ctx)
        except OSError:
            return None

    def gate(self, ctx: Context) -> dict[str, int]:
        return checkout_gate(ctx, self.root(ctx))

    def apply(self, ctx: Context) -> None:
        root = self.root(ctx)
        _ff_to(ctx, root, ctx.sha)
        install = _override(ctx, "AUTOSPEC_INSTALL")
        _checked(ctx, install or ["/bin/bash", str(root / "scripts" / "autospec-runtime-install.sh"),
                                  "--repo-dir", str(root)], cwd=root)

    def checks(self, ctx: Context) -> list[dict]:
        smoke = _override(ctx, "AUTOSPEC_SMOKE") or ["autospec", "--version"]
        return [_check("generation_receipt", self.installed(ctx) == ctx.sha),
                _timed("version_smoke", lambda: _run(ctx, smoke, timeout=60)[0] == 0)]

    def rollback(self, ctx: Context) -> str | None:
        """A failed refresh leaves the previous generation active; that is a rollback only if it still is."""
        return "rolled_back" if ctx.before_sha and self.installed(ctx) == ctx.before_sha else None


# --------------------------------------------------------------------------- pi-engineering (D7)

class Pi:
    def root(self, ctx: Context) -> Path:
        return ctx.home / ".local" / "share" / "herdr-engineering" / "pi"

    def installed(self, ctx: Context) -> str | None:
        current = self.root(ctx) / "current"
        try:
            name = Path(os.readlink(current)).name
        except OSError:
            return None
        return name if _SHA.fullmatch(name) else None

    def complete(self, ctx: Context, sha: str) -> bool:
        root = self.root(ctx)
        release = root / "releases" / sha
        return ((release / "package.json").is_file() and (release / "node_modules").is_dir()
                and (root / "receipts" / f"{sha}.json").is_file())

    def gate(self, ctx: Context) -> dict[str, int]:
        return {}               # releases are immutable; the stage helper itself refuses a dirty one

    def switch(self, ctx: Context, sha: str) -> None:
        """Atomic activation: build a temp symlink beside `current`, then rename it over `current`.

        Running Pi processes keep their open files in the old release directory; nothing is signalled."""
        root = self.root(ctx)
        current = root / "current"
        if current.exists() and not current.is_symlink():
            ctx.note("current exists and is not a symlink; refusing to replace it")
            raise PhaseStop("failed", "apply_failed")
        tmp = root / f".current-{os.getpid()}-{time.monotonic_ns()}"
        os.symlink(Path("releases") / sha, tmp)
        os.replace(tmp, current)

    def prune(self, ctx: Context, keep_extra: set[str]) -> None:
        """Keep the active release, the one it replaced and the newest others, KEEP_RELEASES in total."""
        root = self.root(ctx)
        releases = [p for p in (root / "releases").glob("*") if p.is_dir() and _SHA.fullmatch(p.name)]
        protected = {self.installed(ctx), *keep_extra} - {None}
        newest = sorted((p for p in releases if p.name not in protected), key=lambda p: p.stat().st_mtime,
                        reverse=True)
        for stale in newest[max(0, KEEP_RELEASES - len(protected)):]:
            shutil.rmtree(stale, ignore_errors=True)
            (root / "receipts" / f"{stale.name}.json").unlink(missing_ok=True)
            ctx.note(f"pruned release {stale.name}")

    def apply(self, ctx: Context) -> None:
        if not self.complete(ctx, ctx.sha):
            stage = _override(ctx, "PI_STAGE") or [
                "python3", str(ctx.home / ".local" / "share" / "herdr-engineering" / "stage-release.py"),
                "--kind", "pi", "--home", str(ctx.home), "--revision", ctx.sha]
            _checked(ctx, stage)
            if not self.complete(ctx, ctx.sha):
                ctx.note("staging finished but the release is incomplete")
                raise PhaseStop("failed", "apply_failed")
        before = self.installed(ctx)
        self.switch(ctx, ctx.sha)
        self.prune(ctx, {before} if before else set())

    def checks(self, ctx: Context) -> list[dict]:
        smoke = _override(ctx, "PI_SMOKE") or ["pi", "--version"]
        return [_check("release_complete", self.complete(ctx, ctx.sha)),
                _check("current_symlink", self.installed(ctx) == ctx.sha),
                _timed("version_smoke", lambda: _run(ctx, smoke, timeout=60)[0] == 0)]

    def rollback(self, ctx: Context) -> str | None:
        if not ctx.before_sha or not (self.root(ctx) / "releases" / ctx.before_sha).is_dir():
            return None
        self.switch(ctx, ctx.before_sha)
        return "rolled_back" if self.installed(ctx) == ctx.before_sha else None


ADAPTERS = {"herdr-engineering": HerdrEngineering(), "pi-engineering": Pi(), "autospec": Autospec()}


# --------------------------------------------------------------------------- checks

def _check(name: str, ok: bool | None, ms: int = 0) -> dict:
    assert name in CHECK_NAMES
    return {"name": name, "status": "skipped" if ok is None else "ok" if ok else "failed", "duration_ms": ms}


def _timed(name: str, probe) -> dict:
    began = time.monotonic()
    try:
        ok = bool(probe())
    except Exception:           # noqa: BLE001 - a probe that raises is a failed check, never a crash
        ok = False
    return _check(name, ok, int((time.monotonic() - began) * 1000))


# --------------------------------------------------------------------------- phases

def _result(ctx: Context, phase: str, status: str, reason: str | None, began: datetime, **extra) -> dict:
    assert status in STATUSES and (reason is None or reason in REASONS)
    finished = _now()
    out: dict = {"schema_version": SCHEMA_VERSION, "phase": phase, "rollout_id": ctx.rollout_id, "repo": ctx.repo,
                 "sha": ctx.sha, "status": status, "no_change": False, "before_sha": None, "after_sha": None,
                 "started_at": began.isoformat(), "finished_at": finished.isoformat(),
                 "duration_ms": int((finished - began).total_seconds() * 1000)}
    if reason:
        out["reason"] = reason
    out.update(extra)
    return out


def _store_log(ctx: Context, phase: str) -> dict | None:
    """Keep this phase's text in the host-side store (appended per rollout/host/kind); return the reference."""
    if ctx.host not in HOST_ORDER or not ctx.log or not _ID.fullmatch(ctx.rollout_id):
        return None
    kind = "healthcheck" if phase == "health" else "updater"
    assert kind in LOG_KINDS
    store = logs.LogStore(_log_dir(ctx))
    try:
        previous = store.read_bytes(ctx.rollout_id, ctx.host, kind).decode("utf-8", "replace")
    except logs.LogNotFound:
        previous = ""
    header = f"== {ctx.repo} {phase} {_now().isoformat()}\n"
    try:
        return store.write(ctx.rollout_id, ctx.host, kind, previous + header + "\n".join(ctx.log) + "\n")
    except OSError:
        return None


def _log_dir(ctx: Context) -> Path:
    root = ctx.env.get("XDG_STATE_HOME") or str(ctx.home / ".local" / "state")
    return Path(root) / "herdr-engineering" / "fleet-watch" / "logs"


def _precheck(ctx: Context, adapter, began: datetime) -> dict:
    installed = adapter.installed(ctx)
    ctx.before_sha = installed
    if installed == ctx.sha and (not isinstance(adapter, Pi) or adapter.complete(ctx, ctx.sha)):
        return _result(ctx, "precheck", "ok", None, began, no_change=True, before_sha=installed, after_sha=installed)
    counts = adapter.gate(ctx)
    return _result(ctx, "precheck", "ok", None, began, before_sha=installed, **({"counts": counts} if counts else {}))


def _phase(ctx: Context, phase: str, adapter, began: datetime) -> dict:
    if phase == "precheck":
        return _precheck(ctx, adapter, began)
    if phase == "apply":
        pre = _precheck(ctx, adapter, began)
        if pre["no_change"]:
            return _result(ctx, "apply", "ok", None, began, no_change=True, before_sha=pre["before_sha"],
                           after_sha=pre["after_sha"])
        with update_lock(ctx):
            adapter.apply(ctx)
        return _result(ctx, "apply", "ok", None, began, before_sha=pre["before_sha"], after_sha=adapter.installed(ctx),
                       **({"counts": pre["counts"]} if "counts" in pre else {}))
    if phase == "health":
        checks = adapter.checks(ctx)
        failed = any(c["status"] == "failed" for c in checks)
        installed = adapter.installed(ctx)
        return _result(ctx, "health", "failed" if failed else "ok", "health_failed" if failed else None, began,
                       checks=checks, before_sha=ctx.before_sha, after_sha=installed)
    if phase == "rollback":
        with update_lock(ctx):
            outcome = adapter.rollback(ctx)
        if outcome is None:
            return _result(ctx, "rollback", "skipped", None, began, not_applicable=True)
        return _result(ctx, "rollback", "rolled_back", None, began, before_sha=ctx.sha, after_sha=ctx.before_sha)
    raise ValueError(phase)


def run_phase(phase: str, *, rollout_id: str, repo: str, sha: str, host: str | None = None,
              before_sha: str | None = None, env: dict[str, str] | None = None,
              busy_retries: int = BUSY_RETRIES, busy_wait_s: float = BUSY_WAIT_S) -> dict:
    """One phase for one repo -> the closed-vocabulary result dict."""
    if phase not in PHASES:
        raise ValueError("unknown phase")
    if not _ID.fullmatch(rollout_id) or not _SHA.fullmatch(sha) or (before_sha and not _SHA.fullmatch(before_sha)):
        raise ValueError("rollout_id must be 16 hex, sha 40 hex")
    if host is not None and host not in HOST_ORDER:
        raise ValueError("host must be a fleet host")
    ctx = Context(rollout_id, repo, sha, host, before_sha, env if env is not None else dict(os.environ),
                  busy_retries, busy_wait_s)
    began = _now()
    if repo in PHASE2_REPOS:
        return _result(ctx, phase, "skipped", None, began, not_applicable=True)
    if repo not in APPLIED_REPOS:
        raise ValueError("unknown repo")
    adapter = ADAPTERS[repo]
    phases = ("precheck", "apply", "health") if phase == "all" else (phase,)
    result: dict = {}
    try:
        for name in phases:
            result = _phase(ctx, name, adapter, _now() if name != phases[0] else began)
            if result["status"] != "ok":
                break
        if phase == "all":
            result = {**result, "phase": "all"}
    except PhaseStop as stop:
        extra = {"counts": stop.counts} if hasattr(stop, "counts") else {}
        result = _result(ctx, phase, stop.status, stop.reason, began, before_sha=ctx.before_sha, **extra)
    except Busy:
        result = _result(ctx, phase, "blocked", "busy", began, before_sha=ctx.before_sha)
    except subprocess.TimeoutExpired:
        result = _result(ctx, phase, "failed", "timeout", began, before_sha=ctx.before_sha)
    except OSError as exc:
        ctx.note(f"{type(exc).__name__}: {exc}")
        result = _result(ctx, phase, "failed", "apply_failed", began, before_sha=ctx.before_sha)
    ref = _store_log(ctx, phase if phase != "all" else "apply")
    if ref:
        result["log"] = ref
    return result
