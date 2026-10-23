"""Server-side host problems: a pure function over a host record and its history.

``now`` is epoch seconds (what the repo clock returns); host timestamps are
ISO-8601 strings (what ``ControlPlaneRepo._stamp`` writes) and are parsed here.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class Thresholds:
    stale_s: float = 900          # mirrors REPORT_FRESH_MS in packages/record-state.js
    disk_warn: float = 85
    disk_critical: float = 95
    mem_pct: float = 90
    mem_window_s: float = 300
    swap_pct: float = 50
    gpu_temp_c: float = 85
    updates_old_s: float = 14 * 86400
    updates_deferred_s: float = 7 * 86400


_PINNED = ("herdr", "pi", "autospec")
_ORDER = {"critical": 0, "warning": 1}


def _epoch(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.UTC)
        return d.timestamp()
    return None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _p(code, severity, message):
    return {"code": code, "severity": severity, "message": message}


def _days(seconds):
    return int(seconds // 86400)


def _mem_sustained(history, now, t):
    cut = now - t.mem_window_s
    window = [(h.get("ts"), _num(h.get("mem"))) for h in history
              if _num(h.get("ts")) is not None and h["ts"] >= cut]
    if len(window) < 2 or any(m is None or m <= t.mem_pct for _, m in window):
        return False
    return min(ts for ts, _ in window) <= now - t.mem_window_s * 0.9


def _drift(versions, pins):
    out = []
    for k in _PINNED:
        if k in pins and versions.get(k) != pins[k]:
            out.append(k)
    pe, ve = pins.get("pi_extensions"), versions.get("pi_extensions")
    if isinstance(pe, dict):
        ve = ve if isinstance(ve, dict) else {}
        out.extend(f"pi/{n}" for n in pe if ve.get(n) != pe[n])
    return out


def compute_problems(host: dict, history: list[dict], now: float,
                     thresholds: Thresholds = Thresholds()) -> list[dict]:
    t = thresholds
    latest = host.get("latest") if isinstance(host.get("latest"), dict) else None
    seen = [s for s in (_epoch(host.get("last_seen")),
                        _epoch(latest.get("at")) if latest else None) if s is not None]
    if latest is None and not seen:
        return [_p("never_reported", "critical", "Host has never reported")]
    problems = []

    if seen and not host.get("maintenance"):
        age = now - max(seen)
        if age > t.stale_s:
            problems.append(_p("stale", "critical", f"No report for {int(age // 60)} min"))

    if latest:
        mounts = [m for m in latest.get("mounts") or []
                  if isinstance(m, dict) and _num(m.get("pct")) is not None]
        disks = [(m["pct"], m.get("path") or "/") for m in mounts]
        if _num(latest.get("disk")) is not None:
            disks.append((latest["disk"], "/"))
        if disks:
            pct, path = max(disks, key=lambda d: d[0])
            if pct >= t.disk_critical:
                problems.append(_p("disk_critical", "critical", f"Disk {pct:g}% full on {path}"))
            elif pct >= t.disk_warn:
                problems.append(_p("disk_warn", "warning", f"Disk {pct:g}% full on {path}"))

        if _mem_sustained(history or [], now, t):
            msg = f"Memory above {t.mem_pct:g}% for 5 min"
            swap = _num(latest.get("swap"))
            if swap is not None and swap > t.swap_pct:
                msg += f", swap {swap:g}%"
            problems.append(_p("mem_pressure", "warning", msg))

        temps = [g["temp_c"] for g in latest.get("gpus") or []
                 if isinstance(g, dict) and _num(g.get("temp_c")) is not None]
        if temps and max(temps) > t.gpu_temp_c:
            problems.append(_p("gpu_hot", "warning", f"GPU at {max(temps):g} C"))

    updates = host.get("updates")
    if isinstance(updates, dict):
        if updates.get("outdated") is None:
            problems.append(_p("updates_unknown", "warning", "Update status unknown"))
        units = updates.get("failed_units")
        if units:
            n = len(units) if isinstance(units, list) else units
            problems.append(_p("failed_units", "warning", f"{n} failed systemd unit(s)"))
        sec = _num(updates.get("security"))
        if sec:
            problems.append(_p("security_updates", "warning", f"{sec:g} security updates pending"))
        if updates.get("reboot_required"):
            problems.append(_p("reboot_required", "warning", "Reboot required"))

    versions, pins = host.get("versions"), host.get("pins")
    if isinstance(versions, dict) and isinstance(pins, dict):
        drift = _drift(versions, pins)
        if drift:
            problems.append(_p("version_drift", "warning",
                               "Version differs from pin: " + ", ".join(drift)))

    fu = host.get("fleet_update")
    if isinstance(fu, dict):
        deferred = _epoch(fu.get("deferred_since"))
        if deferred is not None and now - deferred > t.updates_deferred_s:
            problems.append(_p("updates_deferred", "warning",
                               f"Updates deferred for {_days(now - deferred)} d"))
        last = _epoch(fu.get("last_updated"))
        if last is not None and now - last > t.updates_old_s:
            problems.append(_p("updates_old", "warning",
                               f"Last updated {_days(now - last)} d ago"))

    problems.sort(key=lambda p: (_ORDER[p["severity"]], p["code"]))
    return problems
