"""Serial rollout runner (spec sections 6 and 7, decisions D5/D6/D8). Manual only: `herdr-eng fleet-watch run --apply`.

For one planned rollout it walks hosts in canary order (beast -> bender -> macbook-pro) and, per host, every target repo:
`host.started`, then the three `fleet host-apply` phases over ssh (precheck 60 s, apply 30 min, health 120 s; ssh
connect 15 s), emitting `host.step` between them and `host.finished` at the end. The first row that is not ok halts the
rollout: later rows are `skipped(halted)`. The only exception is the optional macbook-pro, which when unreachable is
`skipped(optional_unreachable)` and does not halt.

Everything durable lives in `state.json` (`rollouts[<id>]`, saved after every transition), so a crash resumes at the
first row that was not finished; every remote phase is idempotent, so redoing the interrupted row is safe. Events carry
only closed vocabulary (spec section 14); remote results are parsed strictly and anything that does not validate is a
`failed(apply_failed)`, never forwarded. Rollback (D8) is attempted only for a pointer flip or fast-forward, and only
for `failed_host` (default) or `all_updated` scope.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..control_plane.fleet_rollout import validate_fleet_rollout
from . import core, events, lock, state

DEFAULT_REMOTE_BIN = "$HOME/.local/bin/herdr-eng"
DEFAULT_TIMEOUTS = {"precheck": 60.0, "apply": 1800.0, "health": 120.0, "rollback": 600.0}
CONNECT_TIMEOUT_S = 15
SSH_FAILED_RC = 255
MAX_STDOUT = 65536
ROLLBACK_SCOPES = ("failed_host", "all_updated")
#: Row reasons of a `skipped` row that are a deliberate non-event (everything else halts the rollout).
SETTLED_SKIPS = frozenset({"optional_unreachable"})
_ID = re.compile(r"[0-9a-f]{16}")
_SHA = re.compile(r"[0-9a-f]{40}")
_ARG = re.compile(r"[a-z0-9-]{1,40}")


def now() -> datetime:
    return datetime.now(UTC)


def short(repo: str) -> str:
    return repo.split("/", 1)[-1]


# --------------------------------------------------------------------------- transport

@dataclass(frozen=True)
class Outcome:
    rc: int | None
    stdout: str
    timed_out: bool = False


class SshTransport:
    """Runs `herdr-eng fleet host-apply` on a host: over ssh (BatchMode, ConnectTimeout), or directly for the local host.

    The binary on the far side is the host's installed build, started in `/` so a checkout under change is never the
    working directory. A timeout kills the whole local process group; the remote phase may still finish, which is safe
    because host-apply is idempotent and serialised by the host's fleet-auto-update lock.
    """

    def __init__(self, ssh_bin: str | None = None, local_host: str | None = "beast",
                 remote_bin: str = DEFAULT_REMOTE_BIN, connect_timeout: int = CONNECT_TIMEOUT_S):
        self.ssh_bin = ssh_bin or os.environ.get("HERDR_FLEET_WATCH_SSH", "ssh")
        self.local_host = local_host
        self.remote_bin = remote_bin
        self.connect_timeout = connect_timeout

    def command(self, args: list[str]) -> str:
        for arg in args:
            if not _ARG.fullmatch(arg) and not _SHA.fullmatch(arg) and not _ID.fullmatch(arg):
                raise ValueError("unsafe host-apply argument")
        return f"cd / && exec {self.remote_bin} fleet host-apply {shlex.join(args)}"

    def run(self, host: str, args: list[str], timeout: float) -> Outcome:
        command = self.command(args)
        if host == self.local_host:
            argv = ["sh", "-c", command]
        else:
            argv = [self.ssh_bin, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.connect_timeout}",
                    "-T", host, "--", command]
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, start_new_session=True, cwd="/")
        except OSError:
            return Outcome(SSH_FAILED_RC, "")
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.communicate()
            return Outcome(None, "", timed_out=True)
        return Outcome(proc.returncode, out[:MAX_STDOUT].decode("utf-8", "replace"))


# --------------------------------------------------------------------------- strict result parsing

def parse_result(rid: str, host: str, repo: str, outcome: Outcome, *, sha: str | None = None,
                 phase: str | None = None) -> dict:
    """Closed-vocabulary view of one phase result; anything unexpected becomes failed(apply_failed).

    The result must also be about THIS request: rollout id, repo, sha and phase echo the request, and an ok
    apply/health answer must end on the target sha. A `skipped` needs a reason or `not_applicable`."""
    if outcome.timed_out:
        return {"status": "failed", "reason": "timeout"}
    if outcome.rc == SSH_FAILED_RC:
        return {"status": "unreachable", "reason": "ssh_failed"}
    bad = {"status": "failed", "reason": "apply_failed"}
    try:
        data = json.loads(outcome.stdout)
    except ValueError:
        return bad
    if not isinstance(data, dict):
        return bad
    if data.get("rollout_id") != rid or data.get("repo") != short(repo):
        return bad
    if (sha is not None and data.get("sha") != sha) or (phase is not None and data.get("phase") != phase):
        return bad
    keep = {k: data[k] for k in ("status", "reason", "before_sha", "after_sha", "checks", "log", "counts")
            if data.get(k) is not None}
    probe = {"rollout_id": rid, "ts": now().isoformat(), "seq": 0, "host": host, "repo": short(repo), **keep}
    try:
        validate_fleet_rollout(rid, "host.finished", probe)
    except ValueError:
        return bad
    if keep["status"] == "ok" and outcome.rc != 0:
        return bad
    if keep["status"] == "ok" and phase in ("apply", "health") and sha is not None and keep.get("after_sha") != sha:
        # the host may be AT OR PAST the target (it verified that itself) and says so with past_target
        if not (data.get("past_target") is True and isinstance(keep.get("after_sha"), str)
                and re.fullmatch(r"[0-9a-f]{40}", keep["after_sha"])):
            return bad
    keep["no_change"] = data.get("no_change") is True
    keep["not_applicable"] = data.get("not_applicable") is True
    if keep["status"] == "skipped" and not keep.get("reason") and not keep["not_applicable"]:
        return bad
    return keep


def _fields(result: dict, *keys: str) -> dict:
    return {k: result[k] for k in keys if k in result}


# --------------------------------------------------------------------------- runner

@dataclass
class Runner:
    directory: Path
    emitter: events.Emitter
    transport: SshTransport
    rollback_scope: str = "failed_host"
    timeouts: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TIMEOUTS))
    lock_path: Path | None = None             # fleet.lock.yaml moved after a rollout succeeded (spec section 7); None = never

    def __post_init__(self) -> None:
        if self.rollback_scope not in ROLLBACK_SCOPES:
            raise ValueError("rollback_scope must be failed_host or all_updated")

    # -- plan registration ---------------------------------------------------------------------------

    def start(self, saved: dict, plan: dict) -> str:
        """Register a planned rollout (persisted before anything is emitted) and run it."""
        rid = plan["rollout_id"]
        rows = {}
        for host in plan["hosts"]:
            for repo in plan["targets"]:
                rows[f"{host['host']}/{short(repo)}"] = {"state": "pending"}
        saved["rollouts"][rid] = {"status": "running", "announced": False, "targets": plan["targets"],
                                  "before": plan["before"], "hosts": plan["hosts"], "rows": rows}
        state.save(saved, self.directory)
        return self.run(saved, rid)

    def resume(self, saved: dict, rid: str, *, operator: bool = False) -> str:
        """Continue a rollout: after a crash (status running) or on operator request (halted)."""
        rollout = saved["rollouts"][rid]
        if operator:
            if rollout.get("halt") and not rollout.get("halt_emitted"):      # the halt the crash swallowed
                self._emit_halted(saved, rid, rollout)
            for row in rollout["rows"].values():
                if not self._settled(row):
                    row.clear()
                    row["state"] = "pending"
        rollout["status"] = "running"
        rollout.pop("halt", None)
        rollout.pop("halt_emitted", None)
        state.save(saved, self.directory)
        if rollout.get("announced"):
            self._emit(rid, "rollout.resumed", **({"reason": "operator"} if operator else {}))
        return self.run(saved, rid)

    def abandon(self, saved: dict, rid: str) -> None:
        """Operator decision (spec 6.1): a halted rollout is given up. Nothing runs, converged and the lock stay put;
        newer heads are unblocked, and the same heads are not planned again."""
        rollout = saved["rollouts"][rid]
        if rollout.get("halt") and not rollout.get("halt_emitted"):          # the halt a crash swallowed
            self._emit_halted(saved, rid, rollout)
        rollout["status"] = "abandoned"
        state.save(saved, self.directory)
        self._emit(rid, "rollout.abandoned", reason="operator")

    def supersede(self, saved: dict, rid: str) -> None:
        saved["rollouts"][rid]["status"] = "superseded"
        state.save(saved, self.directory)
        self._emit(rid, "rollout.superseded")

    # -- the walk ------------------------------------------------------------------------------------

    def _emit(self, rid: str, event_type: str, **fields) -> None:
        self.emitter.emit(rid, event_type, **fields)

    def _targets(self, rollout: dict) -> list[dict]:
        return [{"repo": short(repo), "sha": sha, "before": rollout["before"].get(repo)}
                for repo, sha in rollout["targets"].items()]

    def _announce(self, rid: str, rollout: dict) -> None:
        if rollout.get("announced"):
            return
        targets = self._targets(rollout)
        trigger = {"repo": targets[0]["repo"], "sha": targets[0]["sha"]}
        self._emit(rid, "rollout.observed", trigger=trigger, targets=targets)
        self._emit(rid, "rollout.planned", trigger=trigger, targets=targets)
        self._emit(rid, "rollout.started", targets=targets)
        rollout["announced"] = True

    @staticmethod
    def _settled(row: dict) -> bool:
        """A finished row that does not stop the rollout: ok, or a skip that is a deliberate non-event."""
        return row.get("state") == "done" and (row.get("status") == "ok" or (
            row.get("status") == "skipped" and (row.get("reason") in SETTLED_SKIPS or row.get("not_applicable") is True)))

    def _emit_halted(self, saved: dict, rid: str, rollout: dict) -> None:
        halt = rollout["halt"]
        self._emit(rid, "rollout.halted", halted_at_host=halt["host"], **({"reason": halt["reason"]}
                                                                         if halt.get("reason") else {}))
        rollout["halt_emitted"] = True
        state.save(saved, self.directory)

    def run(self, saved: dict, rid: str) -> str:
        rollout = saved["rollouts"][rid]
        self._announce(rid, rollout)
        halted: tuple[str, str | None] | None = None
        for host in rollout["hosts"]:
            name = host["host"]
            hold = None
            for repo in rollout["targets"]:
                key = f"{name}/{short(repo)}"
                row = rollout["rows"][key]
                if row.get("state") == "done":
                    # a finished row that is not settled halts the rollout, also after a crash that lost the halt
                    if not self._settled(row) and not halted and row.get("reason") != "halted":
                        halted = (name, row.get("reason"))
                    hold = hold or self._carry(row, name)
                    continue
                if halted or hold == "optional_unreachable":
                    reason = "halted" if halted else "optional_unreachable"
                    self._skip(saved, rid, rollout, name, repo, reason)
                    continue
                status, reason = self._row(saved, rid, rollout, name, repo, bool(host["optional"]))
                if status == "skipped" and reason == "optional_unreachable":
                    hold = reason                  # an unreachable optional host is skipped for every later repo too
                elif not self._settled(row):
                    halted = (name, reason)
        if halted:
            rollout["status"] = "halted"
            rollout.setdefault("halt", {"host": halted[0], "reason": halted[1]})
            state.save(saved, self.directory)
            if not rollout.get("halt_emitted"):
                self._emit_halted(saved, rid, rollout)
            return "halted"
        rollout["status"] = "succeeded"
        rollout["lock"] = "pending"                # saved with the success: a crash before the pin is repaired later
        saved["converged"].update(rollout["targets"])
        state.save(saved, self.directory)
        self._emit(rid, "rollout.succeeded")
        self._pin_lock(saved, rid, rollout)
        return "succeeded"

    # -- fleet.lock.yaml (spec section 7) ------------------------------------------------------------

    def settle_locks(self, saved: dict) -> None:
        """Finish the lock move of a rollout whose success was saved but whose pin outcome was lost to a crash."""
        for rid, rollout in saved["rollouts"].items():
            if rollout.get("status") == "succeeded" and rollout.get("lock") == "pending":
                self._pin_lock(saved, rid, rollout)

    def _pin_lock(self, saved: dict, rid: str, rollout: dict) -> None:
        """Move the lock to the converged targets: only after success, only through `lock.pin_repos` (D3 history
        check, no downgrade). A refusal is recorded and emitted; it never undoes the converged rollout."""
        if self.lock_path is None or not self.lock_path.exists():
            rollout["lock"] = "absent"             # the operator's lock is never invented by a rollout
            state.save(saved, self.directory)
            return
        targets = [{"repo": short(repo), "sha": sha, **({"before": rollout["before"][repo]}
                                                         if rollout["before"].get(repo) else {})}
                   for repo, sha in rollout["targets"].items()]
        try:
            lock.pin_repos(rollout["targets"], self.lock_path, directory=self.directory, base=core.git_base())
        except lock.LockRefused as exc:
            outcome, extra = "refused", {"reason": exc.reason}
        except OSError:
            outcome, extra = "refused", {"reason": "lock_invalid"}
        else:
            outcome, extra = "pinned", {}
        rollout["lock"] = outcome
        state.save(saved, self.directory)
        self._emit(rid, "lock.pinned" if outcome == "pinned" else "lock.refused", targets=targets, **extra)

    @staticmethod
    def _carry(row: dict, host: str) -> str | None:
        return "optional_unreachable" if row.get("reason") == "optional_unreachable" else None

    def _skip(self, saved: dict, rid: str, rollout: dict, host: str, repo: str, reason: str) -> None:
        row = rollout["rows"][f"{host}/{short(repo)}"]
        row.clear()
        row.update(state="done", status="skipped", reason=reason)
        state.save(saved, self.directory)
        self._emit(rid, "host.finished", host=host, repo=short(repo), status="skipped", reason=reason)

    def _call(self, rid: str, host: str, repo: str, sha: str, phase: str, before: str | None) -> dict:
        args = ["--rollout", rid, "--repo", short(repo), "--sha", sha, "--host", host, "--phase", phase]
        if before:
            args += ["--before-sha", before]
        return parse_result(rid, host, repo, self.transport.run(host, args, self.timeouts[phase]),
                            sha=sha, phase=phase)

    def _step(self, rid: str, host: str, repo: str, status: str, result: dict | None = None) -> None:
        extra = {}
        if result:
            extra = _fields(result, "checks")
            if result.get("log", {}).get("host") == host:
                extra["log"] = result["log"]
        self._emit(rid, "host.step", host=host, repo=short(repo), status=status, **extra)

    def _row(self, saved: dict, rid: str, rollout: dict, host: str, repo: str, optional: bool) -> tuple[str, str | None]:
        key = f"{host}/{short(repo)}"
        row = rollout["rows"][key]
        sha = rollout["targets"][repo]
        before = rollout["before"].get(repo)         # the plan-wide baseline; the host's own is learned by precheck
        started = now()
        # A crash mid-row leaves state "running": the host's own previous revision was saved at precheck time and must
        # survive (after a switch, a fresh precheck would report the target as "before" and rollback would do nothing).
        carried = row.get("host_before") if row.get("state") == "running" else None
        row.clear()
        row["state"] = "running"
        if carried:
            row["host_before"] = carried
        state.save(saved, self.directory)
        self._emit(rid, "host.started", host=host, repo=short(repo))
        self._step(rid, host, repo, "precheck")
        result = self._call(rid, host, repo, sha, "precheck", before)
        phase = "precheck"
        own_before = carried or (result.get("before_sha") if result["status"] == "ok" else None)
        if own_before and row.get("host_before") != own_before:
            row["host_before"] = own_before
            state.save(saved, self.directory)
        applied = False
        if result["status"] == "ok":
            self._step(rid, host, repo, "applying", result)
            phase = "apply"
            result = self._call(rid, host, repo, sha, "apply", own_before)
            applied = result["status"] == "ok" and not result.get("no_change")   # the host really changed now
        if result["status"] == "ok":
            self._step(rid, host, repo, "checking", result)
            phase = "health"
            result = self._call(rid, host, repo, sha, "health", own_before)
        status, reason = result["status"], result.get("reason")
        if status == "unreachable" and phase == "precheck" and optional:
            status, reason, result = "skipped", "optional_unreachable", {}
        elif status == "unreachable":
            status = "failed" if phase != "precheck" else "unreachable"
        finish = {"started_at": started.isoformat(), "finished_at": now().isoformat(),
                  "duration_ms": int((now() - started).total_seconds() * 1000)}
        extra = _fields(result, "before_sha", "after_sha", "checks", "counts")
        if result.get("log", {}).get("host") == host:
            extra["log"] = result["log"]
        # A carried host_before that differs from the target means an earlier, crashed attempt already switched this
        # host: the resumed precheck/apply report no_change, but the host did change in this rollout.
        switched = applied or bool(carried and carried != sha)
        row.update(state="done", status=status, updated=switched and status == "ok",
                   **({"reason": reason} if reason else {}),
                   **({"not_applicable": True} if result.get("not_applicable") else {}),
                   **({"host_before": own_before} if own_before else {}))
        if not self._settled(row):                    # the halt is saved with the row that causes it
            rollout["status"] = "halted"
            rollout["halt"] = {"host": host, "reason": reason}
            rollout["halt_emitted"] = False
        state.save(saved, self.directory)
        self._emit(rid, "host.finished", host=host, repo=short(repo), status=status,
                   **({"reason": reason} if reason else {}), **{**extra, **finish})
        if not self._settled(row) and phase != "precheck":
            self._rollback(saved, rid, rollout, host, repo)
        return status, reason

    # -- D8 rollback ---------------------------------------------------------------------------------

    def _rollback(self, saved: dict, rid: str, rollout: dict, host: str, repo: str) -> None:
        """Undo the failed row and, by scope, rows that were updated before it. Only pointer flips and ff-only
        rollbacks exist; hosts answer skipped(not_applicable) for anything else and the row stays as it was."""
        order = list(rollout["rows"])
        failing = f"{host}/{short(repo)}"
        queue = [failing] + [k for k in reversed(order[:order.index(failing)])
                             if rollout["rows"][k].get("updated")
                             and (self.rollback_scope == "all_updated" or k.split("/", 1)[0] == host)]
        by_short = {short(r): r for r in rollout["targets"]}
        for key in queue:
            h, name = key.split("/", 1)
            full = by_short[name]
            row = rollout["rows"][key]
            own = row.get("host_before")          # the host's own previous revision, never the plan baseline
            result = self._call(rid, h, full, rollout["targets"][full], "rollback", own)
            row["rollback"] = result["status"]
            if result["status"] == "rolled_back":
                if key != failing:
                    row["status"] = "rolled_back"
                row["updated"] = False
                self._emit(rid, "host.finished", host=h, repo=name, status="rolled_back",
                           **_fields(result, "before_sha", "after_sha"),
                           **({"log": result["log"]} if result.get("log", {}).get("host") == h else {}))
            state.save(saved, self.directory)
