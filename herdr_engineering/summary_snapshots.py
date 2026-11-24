"""The private, bounded history of /summary snapshots of one session.

Each ``herdr-eng summary publish`` used to overwrite the one report. It now also appends a *snapshot*: a handful
of numbers computed by code from the session history (never written by the model) plus the redacted report text
of the newest few. The series is drawn at the bottom of the summary view (``snapshot_plot.py``) and in the
dashboard. Only ``publish`` writes it; the history helper and the viewers only read.
"""
from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from .control_plane.redact import redact_text

VERSION = 1
MAX_SNAPSHOTS = 200            # the first is kept; after that the oldest go
KEEP_REPORTS = 5               # only the newest snapshots keep their report text
MAX_REPORT_CHARS = 60_000
CARRY_CHARS = 6_000            # how much of the previous report the summarizer is shown
MAX_FILE_BYTES = 2_000_000     # a larger file is not trusted
COVERAGE = ("complete", "partial", "limited")
COUNTS = ("prompts", "events", "tool_calls", "tool_failures", "compactions", "agents",
          "worked_s", "fresh", "cached", "segments")
_LIMIT = 10**12
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def cache_dir(home) -> Path:
    return Path(home) / ".cache/herdr-engineering/summary"


def store_path(directory, agent: str, session_id: str) -> Path:
    return Path(directory) / f"{agent}-{session_id}.snapshots.json"


def snapshots_file(report_file) -> Path:
    """The store that sits beside a cached report file (``<agent>-<session>.md``)."""
    path = Path(report_file)
    return path.with_name(path.stem + ".snapshots.json")


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < _LIMIT else None


def clean_point(raw) -> dict | None:
    """Only the known numeric fields, each a non-negative integer or None; None for anything that is not a point.

    This is the one gate for what the plot may show, including a point posted by a host.
    """
    if not isinstance(raw, dict):
        return None
    point = {key: _count(raw.get(key)) for key in COUNTS}
    if point["prompts"] is None or point["events"] is None:
        return None
    drift = raw.get("drift")
    point["drift"] = drift if isinstance(drift, bool) else None
    point["coverage"] = raw.get("coverage") if raw.get("coverage") in COVERAGE else None
    taken = raw.get("taken_at")
    point["taken_at"] = taken if isinstance(taken, str) and _TS.match(taken) else None
    return point


def snapshot_point(history: dict, graph: dict | None = None) -> dict | None:
    """The numbers of one snapshot from a history (``read_history`` output) and its validated drift graph."""
    if not isinstance(history, dict):
        return None
    counts = history.get("counts") or {}
    usage = history.get("usage") or []
    timed = history.get("activity_ts") or []
    from .drift_graph import worked_seconds
    point = {
        "prompts": history.get("prompts_total"),
        "events": history.get("events_total"),
        "tool_calls": counts.get("tool_calls"),
        "tool_failures": counts.get("tool_failures"),
        "compactions": history.get("compactions"),
        "agents": len(history.get("dispatch_ts") or []),
        "worked_s": worked_seconds(timed, next((t for t in timed if t), None), None) if timed else None,
        "fresh": sum(u.get("fresh", 0) for u in usage) if usage else None,
        "cached": sum(u.get("cached", 0) for u in usage) if usage else None,
        "segments": len(graph["segments"]) if graph else None,
        "drift": bool(graph.get("drift")) if graph else None,
        "coverage": (history.get("coverage") or {}).get("verdict"),
    }
    return clean_point(point)


def load(path) -> list[dict]:
    """The stored snapshots, oldest first; an unreadable, oversized or foreign file is an empty history."""
    try:
        path = Path(path)
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    rows = data.get("snapshots") if isinstance(data, dict) and data.get("version") == VERSION else None
    out = []
    for row in rows if isinstance(rows, list) else []:
        point = clean_point(row)
        n = row.get("n") if isinstance(row, dict) else None
        if point and _count(n):
            point["n"] = n
            report = row.get("report")
            point["report"] = report if isinstance(report, str) else None
            out.append(point)
    return out


def _write(path: Path, rows: list[dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"version": VERSION, "snapshots": rows}, handle)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def record(directory, agent: str, session_id: str, point: dict, report: str | None = None) -> dict:
    """Append one snapshot (``point`` from ``snapshot_point``) and return it as stored. Mode 0600, bounded."""
    clean = clean_point(point)
    if clean is None:
        raise ValueError("not a snapshot point")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = store_path(directory, agent, session_id)
    rows = load(path)
    clean["n"] = (rows[-1]["n"] if rows else 0) + 1
    clean["taken_at"] = clean["taken_at"] or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    clean["report"] = redact_text(report)[:MAX_REPORT_CHARS] if isinstance(report, str) else None
    rows.append(clean)
    if len(rows) > MAX_SNAPSHOTS:
        rows = rows[:1] + rows[-(MAX_SNAPSHOTS - 1):]
    for row in rows[:-KEEP_REPORTS]:
        row["report"] = None
    _write(path, rows)
    return clean


def previous(directory, agent: str, session_id: str) -> dict | None:
    """What the summarizer is shown of the newest snapshot (read only): its numbers and a clipped report."""
    rows = load(store_path(directory, agent, session_id))
    if not rows:
        return None
    last = rows[-1]
    report = last.get("report")
    return {"n": last["n"], "count": len(rows), "taken_at": last["taken_at"], "prompts": last["prompts"],
            "events": last["events"], "coverage": last["coverage"],
            "report": report[:CARRY_CHARS] if report else None}


def series(rows: list[dict]) -> list[dict]:
    """The snapshots without report text: what the plot and the dashboard need."""
    return [{k: v for k, v in row.items() if k != "report"} for row in rows]
