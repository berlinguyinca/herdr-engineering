#!/usr/bin/env python3
"""Fleet-update decisions and state for playbooks/fleet-update.yml.

Runs on the main host (the Ansible controller) only. Two subcommands, each
reading one JSON object on stdin and printing one JSON object on stdout:

  decide  busy signal + deferral state -> per host {action: update|defer, ...}.
          Reads the state file, never writes it.
  busy    one host's busy state right before the agent restart.
  record  per host outcome -> per host control-plane ingest body; writes the
          state file (unless dry_run) under an exclusive lock, merging so
          hosts not in this run keep their entries.

State file (keyed by control-plane host id):
  {"<host>": {"last_updated": iso|null, "deferred_since": iso|null, "result": str,
              "restart_pending": bool}}

Outcomes: updated (reason = optional note, e.g. "agent restart deferred"),
deferred, skipped (an earlier host failed), failed. Only "updated" moves
last_updated and clears deferred_since; the others keep the previous values.

Busy rules: a host is busy when its control-plane record reports
latest.agents_running > 0 OR the control plane lists an active session for it.
The sessions list must be readable for a host to count as idle (the fabric
agent does not report agents_running today); otherwise the busy state is
unknown and the host defers. ``force`` overrides both busy and unknown.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

DAY = 86400
RESULT_MAX = 128


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).replace(microsecond=0).isoformat()


def _epoch(value):
    if not isinstance(value, str):
        return None
    try:
        d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return d.timestamp()


def _now(inp) -> float:
    now = inp.get("now")
    return float(now) if isinstance(now, (int, float)) else dt.datetime.now(dt.UTC).timestamp()


def load_state(path) -> dict:
    try:
        data = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _entry(state, host) -> dict:
    e = state.get(host)
    return e if isinstance(e, dict) else {}


def _count(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _active_hosts(sessions_probe):
    """Host ids with an active session, or None when the list is unreadable."""
    sessions_probe = sessions_probe if isinstance(sessions_probe, dict) else {}
    body = sessions_probe.get("json")
    rows = body.get("sessions") if isinstance(body, dict) else None
    if sessions_probe.get("status") != 200 or not isinstance(rows, list):
        return None
    return [r.get("host_id") for r in rows
            if isinstance(r, dict) and r.get("status") == "active"]


def _agents_running(host_probe):
    if "agents_running" in host_probe:
        return _count(host_probe.get("agents_running"))
    if host_probe.get("status") == 200 and isinstance(host_probe.get("json"), dict):
        latest = host_probe["json"].get("latest")
        return _count(latest.get("agents_running")) if isinstance(latest, dict) else None
    return None


def busy_state(host_id, host_probe, sessions_probe=None, *, active_hosts=...) -> str:
    """'yes' | 'no' | 'unknown' (see module docstring).

    ``host_probe``: {"agents_running": n|None, "names": [...]} as the play sends
    it (pre-filtered: no host record reaches this process), or a raw
    {"status", "json"} probe. ``active_hosts``: host ids with an active session,
    None = unreadable; when omitted it is derived from a raw ``sessions_probe``.
    """
    host_probe = host_probe if isinstance(host_probe, dict) else {}
    running = _agents_running(host_probe)
    if running is not None and running > 0:
        return "yes"
    if active_hosts is ...:
        active_hosts = _active_hosts(sessions_probe)
    if not isinstance(active_hosts, list):
        return "unknown"
    names = {host_id, *(n for n in host_probe.get("names") or [] if isinstance(n, str) and n)}
    return "yes" if any(h in names for h in active_hosts if isinstance(h, str)) else "no"


def _busy_kwargs(inp):
    return {"active_hosts": inp["active_hosts"]} if "active_hosts" in inp else {}


def decide(inp: dict) -> dict:
    now = _now(inp)
    state = load_state(inp["state_file"])
    force = inp.get("force") is True
    max_days = float(inp.get("max_defer_days", 7))
    out = {}
    for host, probe in (inp.get("hosts") or {}).items():
        prev = _entry(state, host)
        busy = busy_state(host, probe, inp.get("sessions"), **_busy_kwargs(inp))
        d = {"busy": busy, "forced": False, "last_updated": prev.get("last_updated"),
             "deferred_since": prev.get("deferred_since"),
             "restart_pending": prev.get("restart_pending") is True}
        if busy == "no":
            d.update(action="update", reason="")
        elif force:
            d.update(action="update", reason="", forced=True)
        else:
            since = d["deferred_since"] if _epoch(d["deferred_since"]) is not None else iso(now)
            age = now - _epoch(since)
            reason = "agents running" if busy == "yes" else "busy state unknown"
            if age >= max_days * DAY:
                reason += f" ({int(age // DAY)} d, max {max_days:g} d reached; not forced)"
            d.update(action="defer", reason=reason, deferred_since=since)
        out[host] = d
    return out


def _bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def clean_pins(pins) -> dict:
    """Drop unset pins: the control plane only judges drift for keys present."""
    out = {}
    for k, v in (pins or {}).items():
        if k == "pi_extensions":
            ext = {n: str(x) for n, x in (v or {}).items() if x not in (None, "")}
            if ext:
                out[k] = ext
        elif v not in (None, "") and not isinstance(v, (dict, list)):
            out[k] = str(v)
    return out


def _result(outcome, reason) -> str:
    if outcome == "updated":
        return f"updated ({reason})"[:RESULT_MAX] if reason else "updated"
    text = f"{outcome}: {reason or 'unknown'}"
    return text[:RESULT_MAX]


def _write_state(path: Path, updates: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = path.with_name(path.name + ".lock")
    with open(lock, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        state = load_state(path)
        state.update(updates)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".fleet-update-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def record(inp: dict) -> dict:
    """Per-host ingest bodies; writes the state file unless dry_run.

    ``restart_pending`` (per host, from the play) means the agent depends on
    something that changed but has not been restarted onto it yet (restart
    deferred, failed, or a later step failed after staging). It is kept in the
    state file until a run restarts the agent, so the next run retries even
    when nothing changes then. While pending, an "updated" run does not move
    last_updated (the update is not complete until the agent runs it) and its
    result says so.
    """
    now = _now(inp)
    state = load_state(inp["state_file"])
    bodies, updates = {}, {}
    for host, h in (inp.get("hosts") or {}).items():
        prev = _entry(state, host)
        outcome = h.get("outcome")
        last, since = h.get("last_updated"), h.get("deferred_since")
        reason = h.get("reason")
        if outcome in ("deferred", "skipped"):  # nothing ran: the pending flag stays
            pending = prev.get("restart_pending") is True
        else:
            pending = h.get("restart_pending") is True
        if outcome == "skipped":  # never probed/decided: keep what the state file has
            last, since = prev.get("last_updated"), prev.get("deferred_since")
        if outcome == "updated":
            since = None
            if pending:
                last, reason = prev.get("last_updated"), "agent restart pending"
            else:
                last = iso(now)
        elif outcome not in ("deferred", "skipped"):
            outcome = "failed"
        fu = {"last_updated": last, "deferred_since": since,
              "result": _result(outcome, reason)}
        updates[host] = {**fu, "restart_pending": pending}
        bodies[host] = {"source": "fleet-update", "entity_type": "host",
                        "entity_id": host, "event_type": "host.fleet_update",
                        "payload": {"maintenance": _bool(h.get("maintenance")),
                                    "pins": clean_pins(h.get("pins")),
                                    "fleet_update": fu}}
    if updates and not inp.get("dry_run"):
        _write_state(Path(inp["state_file"]).expanduser(), updates)
    return bodies


def busy(inp: dict) -> dict:
    """Re-check one host right before an agent restart: {"busy": yes|no|unknown}."""
    return {"busy": busy_state(inp["host_id"], inp.get("host"), inp.get("sessions"),
                               **_busy_kwargs(inp))}


COMMANDS = {"decide": decide, "record": record, "busy": busy}


def main(argv) -> int:
    if len(argv) != 2 or argv[1] not in COMMANDS:
        print("usage: fleet-update-state.py decide|record|busy < input.json", file=sys.stderr)
        return 2
    inp = json.load(sys.stdin)
    out = COMMANDS[argv[1]](inp)
    json.dump(out, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
