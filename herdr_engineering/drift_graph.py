"""The ``drift-graph`` block of a /summary report: pulled out of the text and checked against the real prompts.

The summarizing model proposes labelled segments (``from_prompt``/``to_prompt``) and, if it sees one, where the
topic changed. Everything measurable (counts, start times, agent badges) is computed here from the history, never
taken from the model, and any proposal that does not check out is dropped whole: a wrong graph is worse than none.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime

from .control_plane.redact import redact_text

MIN_PROMPTS = 5
MIN_SEGMENTS = 2
MAX_SEGMENTS = 8
MAX_LABEL = 40
MAX_NOTE = 120
IDLE_GAP_SECONDS = 1800          # a pause longer than this between two events is not time worked

_FENCE = re.compile(r"^```drift-graph[ \t]*\n(.*?)^```[ \t]*$\n?", re.DOTALL | re.MULTILINE)


def extract_block(report: str) -> tuple[str, str | None]:
    """The report without any ``drift-graph`` fence, and the raw JSON of the block (None unless there is exactly one).

    Two blocks, or one that never closes, give no graph: which one the model meant is a guess. Every trace of a
    block is removed either way, so nobody sees raw JSON.
    """
    if "```drift-graph" not in report:
        return report, None
    text = report.replace("\r\n", "\n")
    found = _FENCE.findall(text)
    clean = _FENCE.sub("", text)
    open_fence = re.search(r"^```drift-graph", clean, re.MULTILINE)
    if open_fence:                                   # never closed (or cut off by the report size limit)
        clean = clean[: open_fence.start()].rstrip("\n") + "\n"
        found = []
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean, found[0] if len(found) == 1 else None


def _clip(text: str, limit: int) -> str:
    text = redact_text(str(text))
    text = "".join(" " if c.isspace() else c for c in text if unicodedata.category(c) not in ("Cc", "Cf") or c.isspace())
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _when(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
    except ValueError:
        return None


def _bounds(lo, hi, activity: list | None) -> tuple[datetime | None, datetime | None, list[datetime]]:
    """The window's start and end as times, and the activity inside it; the end of an open window is the last activity."""
    start, end = _when(lo), _when(hi)
    seen = sorted(t for t in map(_when, activity or []) if t)
    if start and end is None and hi is None and seen:
        end = max(seen[-1], start)
    inside = [t for t in seen if start and start <= t and (hi is None or (end and t < end))]
    return start, end, inside


def worked_seconds(activity: list[str | None], lo: str | None, hi: str | None, idle: int = IDLE_GAP_SECONDS) -> int | None:
    """Seconds of work between ``lo`` and ``hi`` (None: until the last activity): the gaps between consecutive events,
    leaving out any gap longer than ``idle``. None when the window cannot be placed in time."""
    start, end, inside = _bounds(lo, hi, activity)
    if not start or not end:
        return None
    points = [start, *inside, end]
    return int(sum(g for a, b in zip(points, points[1:]) if (g := (b - a).total_seconds()) <= idle))


def _span_seconds(activity, lo, hi) -> int | None:
    start, end, _ = _bounds(lo, hi, activity)
    return int((end - start).total_seconds()) if start and end else None


def _tokens(usage: list[dict] | None, lo: str | None, hi: str | None) -> tuple[int | None, int | None]:
    """(fresh, cached) summed over the usage rows timed inside the window; (None, None) when no row lies in it."""
    start, end = _when(lo), _when(hi)
    if not usage or not start or (hi is not None and not end):
        return None, None
    rows = [u for u in usage if (t := _when(u.get("ts"))) and start <= t and (end is None or t < end)]
    if not rows:
        return None, None
    return sum(u.get("fresh", 0) for u in rows), sum(u.get("cached", 0) for u in rows)


def _metrics(lo, hi, activity, usage) -> dict:
    fresh, cached = _tokens(usage, lo, hi)
    return {"worked_s": worked_seconds(activity, lo, hi) if activity else None,
            "span_s": _span_seconds(activity, lo, hi) if activity else None, "fresh": fresh, "cached": cached}


def build_graph(raw: str | None, prompts: list[dict], total: int,
                dispatch_ts: list[str | None] | None = None, activity_ts: list[str | None] | None = None,
                usage: list[dict] | None = None, main_only: bool = False) -> dict | None:
    """The validated graph, or None. ``prompts`` are the history's ``{n, ts}`` entries and ``total`` their true count.

    ``activity_ts`` (every event's time) gives time worked and ``usage`` (``{ts, fresh, cached}`` rows) gives tokens;
    a figure that cannot be known from what was recorded is None, never zero.
    """
    try:
        data = json.loads(raw) if raw else None
        segments = data.get("segments") if isinstance(data, dict) else None
        if not isinstance(segments, list) or not MIN_SEGMENTS <= len(segments) <= MAX_SEGMENTS:
            return None
        if not _is_int(total) or total < MIN_PROMPTS:
            return None
        by_n = {p["n"]: p for p in prompts if _is_int(p.get("n"))}
        out, expect = [], 1
        for seg in segments:
            first, last = seg.get("from_prompt"), seg.get("to_prompt")
            if not isinstance(seg.get("label"), str):
                return None
            label = _clip(seg["label"], MAX_LABEL)
            if not (_is_int(first) and _is_int(last) and label):
                return None
            if first != expect or last < first or last > total or first not in by_n:
                return None
            out.append({"label": label, "from_prompt": first, "to_prompt": last, "count": last - first + 1,
                        "start": by_n[first].get("ts"), "agents": None})
            expect = last + 1
        if expect != total + 1:
            return None
    except (AttributeError, TypeError, ValueError):
        return None

    if dispatch_ts:
        starts = [s["start"] for s in out]
        for i, seg in enumerate(out):
            last = i + 1 == len(out)
            lo, hi = starts[i], (None if last else starts[i + 1])
            if not lo or (not last and not hi):          # a boundary with no recorded time: count nothing rather than guess
                continue
            seg["agents"] = sum(1 for t in dispatch_ts if t and t >= lo and (hi is None or t < hi))

    for i, seg in enumerate(out):
        end = None if i + 1 == len(out) else (out[i + 1]["start"] or "")     # "" = a next segment with no time: unknown, not open
        seg.update(_metrics(seg["start"], end, activity_ts, usage))

    drift = None
    at = data.get("drift_at")
    if _is_int(at) and at in {s["from_prompt"] for s in out[1:]}:
        note = data.get("drift_note")
        drift = {"at": at, "note": _clip(note, MAX_NOTE) if isinstance(note, str) else ""}
        after = next(s for s in out if s["from_prompt"] == at)["start"]
        cost = _metrics(after, None, activity_ts, usage)
        drift.update({k: cost[k] for k in ("worked_s", "fresh", "cached")})
    fresh, cached = _tokens(usage, "0001-01-01T00:00:00Z", None) if usage else (None, None)
    if usage and any(u.get("ts") is None for u in usage):        # untimed rows still belong in the session total
        fresh = sum(u.get("fresh", 0) for u in usage)
        cached = sum(u.get("cached", 0) for u in usage)
    totals = {**_metrics(out[0]["start"], None, activity_ts, None), "fresh": fresh, "cached": cached,
              "main_only": bool(main_only)}
    return {"total_prompts": total, "segments": out, "drift": drift, "totals": totals}
