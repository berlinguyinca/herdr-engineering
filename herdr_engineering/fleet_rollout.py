"""Durable, bounded fleet maintenance reconciliation over Pi's typed control socket.

One apply performs a bounded pass. Repeating it polls or replays durable operation
IDs; it never starts a new mission or sends an instruction to the model.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import fleet

CAPABILITIES = {
    "runtime.describe",
    "runtime.prepare",
    "runtime.reload",
    "mission.resumeAfterMaintenance",
}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
REMOTE_EXEC_PREFIX = 'PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH" exec '


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid target identity")
    return value


def _target(target):
    _identifier(target["host"])
    _identifier(target["sshTarget"])
    if type(target["port"]) is not int or not 1 <= target["port"] <= 65535:
        raise ValueError("invalid SSH port")
    if type(target["local"]) is not bool:
        raise ValueError("invalid local target")
    if target.get("session") is not None:
        _identifier(target["session"])


class RolloutStore:
    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".local/state/herdr-engineering/rollouts"

    def _directory(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if self.root.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("rollout state must be a private, owned directory")

    def _path(self, rollout_id):
        if not re.fullmatch(r"[0-9a-f]{32}", rollout_id):
            raise ValueError("invalid rollout ID")
        return self.root / (rollout_id + ".json")

    @contextmanager
    def lock(self, *, runner_id=None):
        self._directory()
        name = ".lock" if runner_id is None else ".run-" + self._path(runner_id).stem + ".lock"
        fd = os.open(self.root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def read(self, rollout_id):
        path = self._path(rollout_id)
        if path.is_symlink():
            raise ValueError("rollout state must not be a symlink")
        data = json.loads(path.read_text())
        if data.get("id") != rollout_id or not SHA.fullmatch(data.get("revision", "")):
            raise ValueError("invalid rollout state")
        if not data.get("hosts"):
            raise ValueError("empty rollout inventory")
        for host, state in data["hosts"].items():
            _target(state["target"])
            if state["target"]["host"] != host:
                raise ValueError("host identity mismatch")
        return data

    def write(self, data):
        self._directory()
        path = self._path(data["id"])
        if path.exists():
            old = self.read(data["id"])
            if (
                old["revision"] != data["revision"]
                or old["inventory"] != data["inventory"]
                or {h: s["target"] for h, s in old["hosts"].items()}
                != {h: s["target"] for h, s in data["hosts"].items()}
            ):
                raise ValueError("release and inventory are immutable")
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix=".rollout-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def create_rollout(store, revision, targets, *, inventory):
    if not SHA.fullmatch(revision):
        raise ValueError("pi-revision must be a full lowercase 40-character commit SHA")
    if not targets or len({t["host"] for t in targets}) != len(targets):
        raise ValueError("expected hosts must be nonempty and unique")
    for target in targets:
        _target(target)
    plan = {
        "id": uuid.uuid4().hex,
        "schema": 1,
        "revision": revision,
        "inventory": str(Path(inventory).resolve()),
        "createdAt": datetime.now(UTC).isoformat(),
        "status": "planned",
        "hosts": {t["host"]: {"target": t, "status": "pending", "sessions": {}} for t in targets},
    }
    with store.lock():
        store.write(plan)
    return plan


def _stderr_diagnostic(stderr):
    # Extract only known diagnostic categories, never echo arbitrary remote logs.
    # Even unexpected secret-bearing stderr therefore stays out of durable state.
    text = str(stderr or "")[:8192].lower()
    if not text:
        return ""
    for command in ("herdr", "node"):
        if re.search(r"\b" + command + r": (?:command )?not found\b", text):
            return f"; stderr: {command}: not found"
    for diagnostic in (
        "command not found",
        "permission denied",
        "no such file or directory",
        "connection refused",
        "could not resolve hostname",
        "no route to host",
        "connection timed out",
        "operation timed out",
        "host key verification failed",
        "connection closed",
        "connection reset",
    ):
        if diagnostic in text:
            return "; stderr: " + diagnostic
    return "; stderr: unrecognized output withheld"


class PiTransport:
    """Explicit argv locally; one shell-quoted argv through SSH remotely."""

    def __init__(self, runner=None):
        self.runner = runner or subprocess.run

    def native(self, target):
        # Inventory and Pi control must address the same frozen physical host.
        # Controller-side saved machine aliases are not a fleet identity registry.
        argv = ["herdr"]
        if target.get("session") is not None:
            argv.extend(["--session", target["session"]])
        reply = self._json(target, argv + ["api", "snapshot"])
        result = reply.get("result") if isinstance(reply, dict) else None
        snapshot = result.get("snapshot") if isinstance(result, dict) else None
        agents = snapshot.get("agents") if isinstance(snapshot, dict) else None
        if (
            not isinstance(reply, dict)
            or reply.get("ok") is False
            or not isinstance(agents, list)
            or any(not isinstance(agent, dict) for agent in agents)
        ):
            raise ValueError("native host snapshot unavailable or missing agent inventory")
        return [
            {**agent, "machine": target["host"], "session": target.get("session")}
            for agent in agents
        ]

    def _json(self, target, argv, *, allow_failed_receipt=False):
        _target(target)
        command = (
            argv
            if target["local"]
            else [
                "ssh",
                "-oBatchMode=yes",
                "-oConnectTimeout=10",
                "-p",
                str(target["port"]),
                "--",
                target["sshTarget"],
                REMOTE_EXEC_PREFIX + shlex.join(argv),
            ]
        )
        result = self.runner(command, capture_output=True, text=True, timeout=45)
        try:
            reply = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            if result.returncode:
                raise ConnectionError(
                    f"control transport failed (exit {result.returncode}){_stderr_diagnostic(result.stderr)}"
                ) from exc
            raise ValueError("invalid control JSON") from exc
        if result.returncode:
            data = (
                reply.get("data") if isinstance(reply, dict) and reply.get("ok") is True else reply
            )
            receipt = data.get("maintenance") if isinstance(data, dict) else None
            if not (
                allow_failed_receipt
                and result.returncode == 1
                and isinstance(receipt, dict)
                and receipt.get("status") == "failed"
            ):
                raise ConnectionError(
                    f"control transport failed (exit {result.returncode}){_stderr_diagnostic(result.stderr)}"
                )
        return reply

    def sessions(self, target, root):
        return self._json(
            target, ["node", root + "/scripts/pi-engineering.ts", "sessions", "list", "--json"]
        )

    def maintenance(self, target, root, instance, request):
        _identifier(instance)
        reply = self._json(
            target,
            [
                "node",
                root + "/scripts/pi-engineering.ts",
                "sessions",
                "maintenance",
                instance,
                "--request-json",
                json.dumps(request, separators=(",", ":")),
            ],
            allow_failed_receipt=True,
        )
        # CLI may return the wire envelope; accept only a successful typed reply.
        if isinstance(reply, dict) and "ok" in reply:
            if reply["ok"] is not True:
                raise ValueError(
                    "Pi rejected maintenance request: " + str(reply.get("error", "unknown"))[:300]
                )
            reply = reply.get("data")
        return reply


def inventory_targets(inventory, host=None):
    # Use Ansible's resolved fleet group closure, including direct and nested hosts.
    # Resolve connection variables once, then freeze the transport destination.
    result = subprocess.run(
        ["ansible-inventory", "-i", str(inventory), "--list"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    resolved = json.loads(result.stdout)
    visited = set()

    def hosts_in(group):
        if group in visited:
            return set()
        visited.add(group)
        node = resolved.get(group)
        if not isinstance(node, dict):
            raise ValueError("missing resolved fleet inventory group: " + str(group))
        found = set(node.get("hosts") or [])
        for child in node.get("children") or []:
            found.update(hosts_in(child))
        return found

    expected = sorted(hosts_in("herdr_fleet"))
    if host is not None:
        if host not in expected:
            raise ValueError("host is not in expected inventory")
        expected = [host]
    variables = resolved.get("_meta", {}).get("hostvars", {})
    targets = []
    for name in expected:
        _identifier(name)
        var = variables.get(name, {})
        address = var.get("ansible_host", name)
        user = var.get("ansible_user")
        target = {
            "host": name,
            "sshTarget": f"{user}@{address}" if user else address,
            "port": int(var.get("ansible_port", 22)),
            "local": var.get("ansible_connection") == "local",
        }
        if var.get("herdr_session") is not None:
            target["session"] = var["herdr_session"]
        _target(target)
        targets.append(target)
    return targets


def _conversation(agent):
    reference = agent.get("agent_session")
    value = reference.get("value") if isinstance(reference, dict) else None
    if isinstance(value, str) and IDENTIFIER.fullmatch(value):
        return value
    # Native Pi file references contain timestamp_UUID.jsonl; never use cwd/title heuristics.
    if isinstance(value, str):
        match = re.search(
            r"(?:^|[/_])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
            value,
        )
        if match:
            return match[1]
    raise ValueError("native Pi session lacks an exact conversation reference; bootstrap required")


def _rows(transport, target, root):
    rows = transport.sessions(target, root)
    if not isinstance(rows, list) or any(
        not isinstance(r, dict) or not isinstance(r.get("descriptor"), dict) for r in rows
    ):
        raise ValueError("invalid Pi session inventory")
    return rows


def _live(rows, session_id, pid=None):
    matches = [
        r
        for r in rows
        if r["descriptor"].get("sessionId") == session_id and r.get("reachable") is True
    ]
    if len(matches) != 1:
        raise ValueError("session identity missing or ambiguous")
    row = matches[0]
    descriptor = row["descriptor"]
    _identifier(descriptor.get("instanceId"))
    if (
        type(descriptor.get("pid")) is not int
        or descriptor["pid"] <= 0
        or (pid is not None and descriptor["pid"] != pid)
    ):
        raise ValueError("process identity changed; controlled bootstrap required")
    reply = row.get("reply", {})
    if reply.get("ok") is not True or not isinstance(reply.get("data"), dict):
        raise ValueError("invalid session status reply")
    return descriptor, reply["data"]


def _operation(plan, state, key, action, fields, transport, target, root, save):
    operations = state.setdefault("operations", {})
    operation = operations.get(key)
    if operation is None:
        operation = {
            "request": {
                "operationId": uuid.uuid4().hex,
                "action": action,
                "expectedInstanceId": state["instanceId"],
                **fields,
            },
            "attempts": 0,
        }
        operations[key] = operation
    receipt = operation.get("receipt")
    if receipt and receipt["status"] == "succeeded":
        return receipt
    if receipt and receipt["status"] in ("failed", "skipped"):
        return receipt
    request = operation["request"]
    # After an uncertain write, replay the same payload on the same instance.
    # A generation handover may only poll its durable receipt on the new instance.
    if (receipt and receipt["status"] == "running") or request["expectedInstanceId"] != state[
        "instanceId"
    ]:
        request = {
            "operationId": request["operationId"],
            "action": "status",
            "expectedInstanceId": state["instanceId"],
        }
    operation["attempts"] += 1
    save()
    result = transport.maintenance(target, root, state["instanceId"], request)
    receipt = result.get("maintenance") if isinstance(result, dict) else None
    if (
        not isinstance(receipt, dict)
        or receipt.get("operationId") != operation["request"]["operationId"]
        or receipt.get("action") != action
        or receipt.get("status") not in ("running", "succeeded", "failed", "skipped")
    ):
        raise ValueError("invalid maintenance receipt identity or status")
    operation["receipt"] = receipt
    save()
    return receipt


def _retryable_skip(receipt):
    result = receipt.get("result") or {}
    return receipt["status"] == "skipped" and (
        result.get("status") == "busy" or result.get("code") == "STALE_MISSION_FENCE"
    )


def _archive_operation(state, key):
    state.setdefault("retryReceipts", []).append(state["operations"].pop(key))


def _receipt_reason(receipt):
    result = receipt.get("result") or {}
    return str(
        receipt.get("error")
        or result.get("reason")
        or result.get("code")
        or "maintenance skipped without a retryable outcome"
    )[:500]


def _session(plan, state, transport, target, root, save):
    descriptor, data = _live(_rows(transport, target, root), state["sessionId"], state["pid"])
    state["instanceId"] = descriptor["instanceId"]
    runtime = data.get("runtime") or {}
    if not CAPABILITIES.issubset(set(runtime.get("capabilities") or [])):
        state["status"] = "bootstrap_required"
        return
    missions = data.get("missions")
    if not isinstance(missions, list) or any(
        not isinstance(m, dict) or not m.get("id") for m in missions
    ):
        raise ValueError("missing owned mission inventory")
    activation_needed = (
        runtime.get("commit") != plan["revision"] or runtime.get("pending") is not False
    )
    if activation_needed and not state.get("prepared"):
        receipt = _operation(plan, state, "prepare", "prepare", {}, transport, target, root, save)
        if receipt["status"] == "skipped" and receipt.get("result", {}).get("status") == "busy":
            state["status"] = "pending"
            state.setdefault("busyReceipts", []).append(state["operations"].pop("prepare"))
            return
        if receipt["status"] != "succeeded" or receipt.get("result", {}).get("status") != "ready":
            state["status"] = "pending" if receipt["status"] == "running" else "blocked"
            return
        state["prepared"] = True
    if activation_needed:
        receipt = _operation(
            plan,
            state,
            "reload",
            "reload",
            {"expectedCommit": plan["revision"], "sourceRoot": root, "timeoutMs": 30000},
            transport,
            target,
            root,
            save,
        )
        if receipt["status"] != "succeeded":
            if _retryable_skip(receipt):
                _archive_operation(state, "reload")
                state["status"] = "pending"
            else:
                state["status"] = "pending" if receipt["status"] == "running" else "blocked"
                state["error"] = _receipt_reason(receipt)
            return
        descriptor, data = _live(_rows(transport, target, root), state["sessionId"], state["pid"])
        state["instanceId"] = descriptor["instanceId"]
        runtime = data.get("runtime") or {}
        missions = data.get("missions")
        if runtime.get("commit") != plan["revision"] or runtime.get("pending") is not False:
            state["status"] = "activation_unverified"
            return
    state["loaded"] = {k: runtime.get(k) for k in ("commit", "hostCommit", "generation")}
    if runtime.get("hostCommit") != plan["revision"]:
        state["status"] = "bootstrap_required"
        state["error"] = "Runtime activated, but stable Host still needs the selected release"
        return
    if not isinstance(missions, list) or any(not isinstance(m, dict) for m in missions):
        raise ValueError("missing owned mission inventory after activation")
    outcomes = state.setdefault("missions", {})
    for mission in missions:
        mission_id = _identifier(mission["id"])
        outcome = outcomes.setdefault(
            mission_id, {"baselineProgress": mission.get("lastMeaningfulProgressAt")}
        )
        if mission.get("operatorPaused") is True or mission["status"] in (
            "DONE",
            "COMPLETE",
            "COMPLETED",
            "CANCELED",
            "CANCELLED",
        ):
            outcome["status"] = "preserved"
            continue
        if mission.get("resumeEligible") is True or "resume:" + mission_id in state.get(
            "operations", {}
        ):
            if (
                type(mission.get("revision")) is not int
                or type(mission.get("resumptionGeneration")) is not int
            ):
                raise ValueError("missing mission ownership revision")
            receipt = _operation(
                plan,
                state,
                "resume:" + mission_id,
                "resume",
                {
                    "expectedCommit": plan["revision"],
                    "missionId": mission_id,
                    "expectedRevision": mission["revision"],
                    "expectedResumptionGeneration": mission["resumptionGeneration"],
                },
                transport,
                target,
                root,
                save,
            )
            outcome["receipt"] = receipt
            if receipt["status"] == "skipped":
                if _retryable_skip(receipt):
                    _archive_operation(state, "resume:" + mission_id)
                    outcome["status"] = "pending"
                else:
                    outcome["status"] = "blocked"
                    outcome["error"] = _receipt_reason(receipt)
                continue
            if receipt["status"] != "succeeded":
                outcome["status"] = "pending" if receipt["status"] == "running" else "blocked"
                continue
        progress = mission.get("lastMeaningfulProgressAt")
        waiting = mission.get("namedWait") or {}
        valid_wait = False
        try:
            next_action = datetime.fromisoformat(waiting["nextActionAt"].replace("Z", "+00:00"))
            deadline = waiting.get("deadline")
            deadline_at = (
                datetime.fromisoformat(deadline.replace("Z", "+00:00"))
                if deadline is not None
                else None
            )
            now = datetime.now(UTC)
            valid_wait = bool(
                waiting.get("action")
                and next_action > now
                and (deadline_at is None or now < deadline_at and next_action <= deadline_at)
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
        advanced = False
        try:
            current_progress = datetime.fromisoformat(progress.replace("Z", "+00:00"))
            baseline = outcome["baselineProgress"]
            advanced = current_progress.tzinfo is not None and (
                baseline is None
                or current_progress > datetime.fromisoformat(baseline.replace("Z", "+00:00"))
            )
        except (ValueError, TypeError, AttributeError):
            pass
        if advanced:
            outcome.update(status="progress_verified", evidence=progress)
        elif valid_wait:
            outcome.update(status="wait_verified", evidence=waiting)
        elif outcome.get("status") != "progress_verified":
            outcome["status"] = "awaiting_progress"
    if set(outcomes) - {m["id"] for m in missions}:
        state["status"] = "owned_mission_missing"
    elif any(m.get("status") == "blocked" for m in outcomes.values()):
        state["status"] = "blocked"
        state["error"] = "; ".join(
            m.get("error", "mission maintenance failed")
            for m in outcomes.values()
            if m.get("status") == "blocked"
        )
    else:
        state["status"] = (
            "complete"
            if all(
                m.get("status") in ("preserved", "progress_verified", "wait_verified")
                for m in outcomes.values()
            )
            else "awaiting_progress"
        )


def apply_rollout(store, rollout_id, *, transport=None, stage=None):
    transport = transport or PiTransport()
    stage = stage or fleet.stage_pi_runtime
    with store.lock():
        plan = store.read(rollout_id)

        def save():
            store.write(plan)

        for host, host_state in plan["hosts"].items():
            target = host_state["target"]
            host_state.pop("error", None)
            try:
                if not host_state.get("sourceRoot"):
                    if inventory_targets(Path(plan["inventory"]), host) != [target]:
                        raise ValueError(
                            "inventory destination changed since planning; refusing staging"
                        )
                    report = stage(plan["revision"], host=host, inventory=Path(plan["inventory"]))
                    host_state["staging"] = report
                    save()
                    if report.get("ok") is not True:
                        host_state.update(
                            status="pending", error=report.get("error", "staging unavailable")
                        )
                        continue
                    root = report.get("sourceRoot", "")
                    if (
                        not isinstance(root, str)
                        or not root.startswith("/")
                        or not root.endswith(
                            "/.local/share/herdr-engineering/pi/releases/" + plan["revision"]
                        )
                        or ".." in Path(root).parts
                    ):
                        raise ValueError("invalid staged release path")
                    if report.get("revision") != plan["revision"] or report.get("host") != host:
                        raise ValueError("staging attestation mismatch")
                    host_state["sourceRoot"] = root
                    save()
                root = host_state["sourceRoot"]
                native = transport.native(target)
                if not isinstance(native, list):
                    raise ValueError("invalid native inventory")
                agents = [a for a in native if str(a.get("agent", "")).lower() == "pi"]
                identified = []
                coverage_errors = []
                for agent in agents:
                    try:
                        identified.append((agent, _conversation(agent)))
                    except ValueError as exc:
                        coverage_errors.append(
                            {"terminalId": agent.get("terminal_id"), "error": str(exc)}
                        )
                host_state["coverageErrors"] = coverage_errors
                identities = [session_id for _, session_id in identified]
                if len(identities) != len(set(identities)):
                    raise ValueError("ambiguous native conversation identity")
                rows = _rows(transport, target, root)
                if not agents:
                    raise ValueError("no native Pi sessions; expected coverage unverified")
                if set(host_state["sessions"]) - set(identities):
                    coverage_errors.append(
                        {"error": "previously discovered session is missing from native inventory"}
                    )
                session_errors = [error["error"] for error in coverage_errors]
                for agent, session_id in identified:
                    state = host_state["sessions"].setdefault(
                        session_id,
                        {"sessionId": session_id, "native": agent, "status": "discovered"},
                    )
                    state.pop("error", None)
                    try:
                        descriptor, _ = _live(rows, session_id)
                        if "pid" not in state:
                            state.update(pid=descriptor["pid"], instanceId=descriptor["instanceId"])
                        save()
                        _session(plan, state, transport, target, root, save)
                    except (ConnectionError, OSError, subprocess.SubprocessError) as exc:
                        state.update(status="pending", error=str(exc)[:500])
                    except (ValueError, TypeError, KeyError) as exc:
                        state.update(status="blocked", error=str(exc)[:500])
                        session_errors.append(str(exc)[:500])
                    finally:
                        save()
                host_state["status"] = (
                    "blocked"
                    if session_errors
                    or any(s["status"] == "blocked" for s in host_state["sessions"].values())
                    else "complete"
                    if host_state["sessions"]
                    and all(s["status"] == "complete" for s in host_state["sessions"].values())
                    else "pending"
                )
                if session_errors:
                    host_state["error"] = "; ".join(session_errors)[:1000]
            except (ConnectionError, OSError, subprocess.SubprocessError) as exc:
                host_state.update(status="pending", error=str(exc)[:500])
            except (ValueError, TypeError, KeyError) as exc:
                host_state.update(status="blocked", error=str(exc)[:500])
            finally:
                save()
        plan["status"] = (
            "complete"
            if all(h["status"] == "complete" for h in plan["hosts"].values())
            else "pending"
        )
        save()
        return plan


def _offline_failure(state):
    staging = state.get("staging") or {}
    evidence = " ".join(
        str(value)
        for value in (
            state.get("error", ""),
            staging.get("error", ""),
            staging.get("stdout", ""),
            staging.get("stderr", ""),
        )
    ).lower()
    return staging.get("rc") in (4, 124, 255) or any(
        marker in evidence
        for marker in (
            "offline",
            "unreachable",
            "timed out",
            "connection refused",
            "no route to host",
            "could not resolve hostname",
            "control transport failed (exit 255)",
        )
    )


def _automatic_blocker(plan):
    for host, host_state in plan["hosts"].items():
        if host_state["status"] == "blocked":
            return f"{host}: {host_state.get('error', 'host requires operator attention')}"
        if host_state.get("error") and not _offline_failure(host_state):
            return f"{host}: {host_state['error']}"
        for session_id, session in host_state["sessions"].items():
            if session["status"] not in ("complete", "pending", "awaiting_progress", "discovered"):
                return f"{host}/{session_id}: {session['status']}: {session.get('error', 'operator attention required')}"
            if session.get("error") and not _offline_failure(session):
                return f"{host}/{session_id}: {session['error']}"
    return None


def _progress_signature(plan):
    # Attempts, archived busy receipts and fresh operation IDs are not progress.
    evidence = {}
    for host, state in plan["hosts"].items():
        evidence[host] = {
            "status": state["status"],
            "error": state.get("error"),
            "coverage": state.get("coverageErrors"),
            "sessions": {
                session_id: {
                    "status": session["status"],
                    "error": session.get("error"),
                    "loaded": session.get("loaded"),
                    "missions": {
                        mission_id: {
                            key: mission.get(key) for key in ("status", "error", "evidence")
                        }
                        for mission_id, mission in session.get("missions", {}).items()
                    },
                }
                for session_id, session in state["sessions"].items()
            },
        }
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()


def run_rollout(
    store, rollout_id, *, interval_seconds=30, max_passes=None, reconcile=None, sleep=None, now=None
):
    """Reconcile one pinned rollout unattended; never select or create a release.

    The runner lock spans sleeps, while each pass retains apply's short state lock.
    Durable evidence/backoff survives controller restart. Terminal attention stops
    are explicit; unreachable hosts keep retrying with a capped delay.
    """
    if (
        type(interval_seconds) not in (int, float)
        or not math.isfinite(interval_seconds)
        or not 1 <= interval_seconds <= 3600
    ):
        raise ValueError("interval-seconds must be between 1 and 3600")
    if max_passes is not None and (type(max_passes) is not int or max_passes < 1):
        raise ValueError("max-passes must be a positive integer")
    reconcile = reconcile or apply_rollout
    sleep = sleep or time.sleep
    now = now or (lambda: datetime.now(UTC))
    cap = max(300, interval_seconds)

    def record(**fields):
        with store.lock():
            latest = store.read(rollout_id)
            latest.setdefault("runner", {}).update(fields)
            store.write(latest)
            return latest

    with store.lock(runner_id=rollout_id):
        plan = store.read(rollout_id)
        passes = 0
        try:
            previous = plan.get("runner", {})
            if previous.get("nextAttemptAt"):
                remaining = (
                    datetime.fromisoformat(previous["nextAttemptAt"]) - now()
                ).total_seconds()
                if remaining > 0:
                    sleep(min(remaining, cap))
            record(
                status="running",
                reason=None,
                startedAt=now().isoformat(),
                intervalSeconds=interval_seconds,
            )
            while True:
                plan = reconcile(store, rollout_id)
                passes += 1
                previous = plan.get("runner", {})
                signature = _progress_signature(plan)
                unchanged = (
                    previous.get("unchangedPasses", 0) + 1
                    if signature == previous.get("signature")
                    else 0
                )
                delay = min(cap, interval_seconds * 2 ** min(unchanged, 12))
                blocker = _automatic_blocker(plan)
                status = (
                    "complete"
                    if plan["status"] == "complete"
                    else "blocked"
                    if blocker
                    else "max_passes"
                    if max_passes is not None and passes >= max_passes
                    else "running"
                )
                plan = record(
                    status=status,
                    reason=blocker,
                    passes=previous.get("passes", 0) + 1,
                    signature=signature,
                    unchangedPasses=unchanged,
                    lastPassAt=now().isoformat(),
                    nextAttemptAt=(now() + timedelta(seconds=delay)).isoformat()
                    if status in ("running", "max_passes")
                    else None,
                )
                if status != "running":
                    return plan
                sleep(delay)
        except KeyboardInterrupt:
            return record(
                status="interrupted",
                reason="interrupted; existing release and operation IDs retained",
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            return record(status="blocked", reason=str(exc)[:1000], nextAttemptAt=None)
