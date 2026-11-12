"""``herdr-eng agent-ops``: a character-cell Agent Operations map for a Herdr pane (standard library only).

``render`` is a pure function from a snapshot (the JSON of ``/api/v1/agent-ops/snapshot``) to lines of text; the
reducer ``handle_key`` is a pure state machine; ``run`` wires them to the control plane over HTTP/SSE. The API is
metadata-only; labels are still stripped of control characters before they reach the terminal.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import select
import signal
import sys
import termios
import threading
import time
import tty
import unicodedata
import urllib.parse
from dataclasses import dataclass, field, replace

MODES = ("activity", "communication", "handoffs", "ownership", "replay")
PANES = ("map", "tasks", "activity", "timeline")
KINDS = ("communication", "delegation", "handoff", "review", "ownership")
PLANNED = {"planned", "proposed", "pending", "queued", "requested"}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
RESET = "\x1b[0m"
_ASCII = str.maketrans({"·": "-", "…": "~", "→": ">", "←": "<", "↑": "^", "↓": "v", "⏎": "E", "▸": ">", "●": "*", "○": "o",
                        "◆": "!", "×": "x", "✗": "x", "◉": "*", "█": "#", "░": "."})

# kind -> (colour, solid h, solid v, dashed h, dashed v, arrow glyph, ascii h, ascii v, ascii arrow)
_EDGE = {
    "communication": ("blue", "─", "│", "┄", "┆", "›", "-", "|", ">"),
    "delegation": ("green", "━", "┃", "╍", "╏", "▶", "=", "!", ">"),
    "handoff": ("yellow", "═", "║", "╌", "╎", "⇒", "#", "H", ">"),
    "review": ("magenta", "─", "│", "┄", "┆", "◇", "~", ";", "o"),
    "ownership": ("cyan", "┈", "┊", "╌", "╎", "●", ".", ":", "*"),
}
_STATE_COLOR = {"attention": "yellow", "working": "green", "idle": "cyan", "blocked": "red", "disconnected": "gray", "ended": "gray",
                "unknown": "gray"}
_STATE_DOT = {"attention": "▲", "working": "●", "idle": "○", "blocked": "◆", "disconnected": "×", "ended": "×", "unknown": "?"}
_COLORS = {"red": (31, 203), "green": (32, 78), "yellow": (33, 215), "blue": (34, 75), "magenta": (35, 141),
           "cyan": (36, 80), "gray": (90, 244), "white": (37, 252), "bold": (1, None), "dim": (2, None),
           "rev": (7, None)}


_DROPPED = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp", "Mn", "Me"})   # controls, bidi/zero-width format chars, combining marks


def _drop(ch: str) -> bool:
    return unicodedata.category(ch) in _DROPPED


def char_width(ch: str) -> int:
    """Terminal cells one (already sanitised) character occupies: 2 for East Asian wide/fullwidth and emoji."""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cell_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def fit(text: str, cells: int) -> str:
    """The longest prefix of ``text`` that fits ``cells`` columns; a wide glyph is dropped whole, never split."""
    out, used = [], 0
    for ch in text:
        used += char_width(ch)
        if used > cells:
            break
        out.append(ch)
    return "".join(out)


def pad(text: str, cells: int) -> str:
    text = fit(text, cells)
    return text + " " * (cells - cell_width(text))


def clean(value: object, limit: int = 40) -> str:
    """Printable text: control, bidi, zero-width and combining characters removed FIRST, then bounded to ``limit``
    terminal cells (wide glyphs count 2) so the cut can never split an escape sequence or a glyph; None becomes ''."""
    text = "".join(ch for ch in ("" if value is None else str(value)) if not _drop(ch))
    return text if cell_width(text) <= limit else fit(text, max(limit - 1, 1)) + "…"


def visible_width(line: str) -> int:
    return cell_width(_ANSI.sub("", line))


@dataclass(frozen=True)
class RenderOpts:
    color: bool = False
    unicode: bool = True
    reduced_motion: bool = True
    filter_kind: str | None = None
    status: str = "live"            # live | degraded | offline | replay
    status_detail: str = ""
    overlay: str | None = None      # help | inspect | handoffs | timeline | filter
    pane: str = "map"
    scroll: int = 0
    replay_pos: tuple[int, int] | None = None   # (index, total) while scrubbing
    replay_paused: bool = False
    error: str = ""


def detect_opts(env: dict[str, str] | None = None, *, isatty: bool = True, no_animation: bool = False,
                ascii_only: bool = False) -> RenderOpts:
    env = os.environ if env is None else env
    term = env.get("TERM", "")
    color = isatty and "NO_COLOR" not in env and term not in ("", "dumb")
    locale = (env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or "").upper()
    unicode_ok = not ascii_only and term not in ("", "dumb", "linux") and "UTF" in locale
    reduced = no_animation or env.get("HERDR_REDUCED_MOTION", "") not in ("", "0")
    return RenderOpts(color=color, unicode=unicode_ok, reduced_motion=reduced)


class Canvas:
    """A fixed grid of cells, each (character, colour name or None); one cell is one terminal column."""

    def __init__(self, width: int, height: int):
        self.w, self.h = max(width, 1), max(height, 1)
        self.cells: list[list[tuple[str, str | None]]] = [[(" ", None)] * self.w for _ in range(self.h)]

    def put(self, x: int, y: int, ch: str, style: str | None = None) -> None:
        if not (0 <= x < self.w and 0 <= y < self.h):
            return
        row, width = self.cells[y], char_width(ch)
        if width == 2 and x + 1 >= self.w:
            ch, width = " ", 1          # a wide glyph that would straddle the right edge is not drawn
        self._blank_partner(row, x)
        if width == 2:
            self._blank_partner(row, x + 1)
        row[x] = (ch, style)
        if width == 2:
            row[x + 1] = ("", style)    # continuation cell: contributes no column of its own

    @staticmethod
    def _blank_partner(row: list[tuple[str, str | None]], x: int) -> None:
        """Overwriting one half of a wide glyph blanks the other half so no orphan half is left."""
        if row[x][0] == "" and x > 0:
            row[x - 1] = (" ", row[x - 1][1])
        elif char_width(row[x][0]) == 2 and x + 1 < len(row):
            row[x + 1] = (" ", row[x + 1][1])

    def text(self, x: int, y: int, s: str, style: str | None = None, limit: int | None = None) -> None:
        col = 0
        for ch in s:
            if _drop(ch):
                continue
            width = char_width(ch)
            if limit is not None and col + width > limit:
                break
            self.put(x + col, y, ch, style)
            col += width

    def at(self, x: int, y: int) -> str:
        if not (0 <= x < self.w and 0 <= y < self.h):
            return " "
        return self.cells[y][x][0] or "\x00"

    def lines(self, color: bool) -> list[str]:
        out = []
        for row in self.cells:
            if not color:
                out.append("".join(c for c, _ in row).rstrip())
                continue
            parts, last = [], None
            for ch, style in row:
                if style != last:
                    parts.append(RESET + sgr(style) if style else RESET)
                    last = style
                parts.append(ch)
            out.append("".join(parts) + RESET)
        return out


def sgr(style: str | None, *, basic: bool = False) -> str:
    codes = []
    for name in (style or "").split("+"):
        if name in _COLORS:
            code, c256 = _COLORS[name]
            codes.append(f"38;5;{c256}" if c256 and not basic else str(code))
    return f"\x1b[{';'.join(codes)}m" if codes else ""


# ----------------------------------------------------------------------------------------------- derived data
def context_bar(context: object, width: int, opts: RenderOpts) -> str:
    """``[████░░] 68%`` for a measured context, ``ctx ?`` when it is unknown (never a guessed percentage)."""
    used = context.get("used") if isinstance(context, dict) else None
    limit = context.get("limit") if isinstance(context, dict) else None
    if not isinstance(used, (int, float)) or not isinstance(limit, (int, float)) or limit <= 0:
        return "ctx ?" if not isinstance(used, (int, float)) else f"ctx {int(used)}"
    pct = max(0, min(100, round(100 * used / limit)))
    cells = max(width - 6, 3)
    full = round(cells * pct / 100)
    on, off = ("█", "░") if opts.unicode else ("#", ".")
    return f"[{on * full}{off * (cells - full)}] {pct}%"


def is_planned(edge: dict) -> bool:
    return str(edge.get("state", "")).lower() in PLANNED


BROKEN = {"stalled", "failed"}      # a handoff interrupted by a disconnect (stalled) or ended session (failed)


def is_broken(edge: dict) -> bool:
    return str(edge.get("state", "")).lower() in BROKEN


def context_fraction(agent: dict) -> float | None:
    """Measured context fraction from the server's ``context_health`` (or, for an older snapshot, from ``context``);
    None means unknown and is never turned into 0."""
    health_ = agent.get("context_health")
    if isinstance(health_, dict):
        f = health_.get("fraction") if health_.get("measured") else None
        return float(f) if isinstance(f, (int, float)) else None
    ctx = agent.get("context")
    if isinstance(ctx, dict) and isinstance(ctx.get("used"), (int, float)) and isinstance(ctx.get("limit"), (int, float)) \
            and ctx["limit"] > 0:
        return ctx["used"] / ctx["limit"]
    return None


def ctx_level_style(fraction: float | None) -> str | None:
    return None if fraction is None else "red" if fraction >= 0.9 else "yellow" if fraction >= 0.7 else "green"


def ctx_badge(agent: dict, width: int, opts: RenderOpts) -> tuple[str, str | None]:
    """The map node's context row: a bar with ``!`` (amber, >=70%) or ``!!`` (red, >=90%) so colour is never the only cue,
    or ``ctx unknown`` when nothing measurable was reported."""
    fraction = context_fraction(agent)
    mark = "" if fraction is None or fraction < 0.7 else "!!" if fraction >= 0.9 else "!"
    bar = context_bar(agent.get("context"), width - len(mark), opts)
    return ("ctx unknown" if bar == "ctx ?" else bar + mark), ctx_level_style(fraction)


def pct_text(fraction: float | None) -> str:
    return "ctx unknown" if fraction is None else f"ctx {round(100 * fraction)}%"


def drift_kinds(item: dict) -> list[str]:
    return [clean(d.get("kind"), 24) for d in item.get("drift") or [] if isinstance(d, dict)]


def hosts_row(snapshot: dict, width: int, opts: RenderOpts) -> str | None:
    """``fleet ctx 69% (3/4)  hosts 3: mac-01 ● ctx 56%  dev-02 × ctx 95%  lab-03 ? ctx unknown``; None for an older snapshot."""
    hosts = snapshot.get("hosts")
    fleet = (snapshot.get("health") or {}).get("context")
    if not isinstance(hosts, list) and not isinstance(fleet, dict):
        return None
    hosts = hosts if isinstance(hosts, list) else []
    glyph = {"connected": ("●", "green"), "disconnected": ("×", "red")}

    def build(short: bool) -> list[tuple[str, str | None]]:
        segs: list[tuple[str, str | None]] = []
        if isinstance(fleet, dict):
            avg = fleet.get("average_fraction")
            avg = avg if isinstance(avg, (int, float)) else None
            measured = f" ({fleet.get('measured_count', '?')}/{fleet.get('total', '?')})" if avg is not None and not short else ""
            segs.append((f"fleet {pct_text(avg)}{measured}  ", ctx_level_style(avg) or "dim"))
        segs.append((f"hosts {(snapshot.get('counts') or {}).get('hosts', len(hosts))}:", "bold"))
        for host in hosts:
            mark, style = glyph.get(str(host.get("state")), ("?", "gray"))     # unknown stays unknown: never "up" or "down"
            avg = (host.get("context_health") or {}).get("average_fraction")
            avg = avg if isinstance(avg, (int, float)) else None
            badge = pct_text(avg) if not short else "?" if avg is None else f"{round(100 * avg)}%"
            segs += [(f" {clean(host.get('host_id'), 14)} ", None), (mark, style), (f" {badge} ", ctx_level_style(avg))]
        return segs

    segs = build(False)
    if sum(cell_width(t) for t, _ in segs) > width:
        segs = build(True)         # narrow pane: drop the "ctx" word and the measured count so every host still fits
    out, used = [], 0
    for text, style in segs:
        text = fit(text, width - used)
        used += cell_width(text)
        out.append(f"{sgr(style)}{text}{RESET}" if opts.color and style and text else text)
    return "".join(out)


def visible_edges(snapshot: dict, mode: str, kind_filter: str | None) -> list[dict]:
    edges = [e for e in snapshot.get("edges") or [] if e.get("kind") in _EDGE]
    allowed = {"communication": {"communication"}, "handoffs": {"handoff", "delegation"},
               "ownership": {"ownership"}}.get(mode)
    if allowed:
        edges = [e for e in edges if e["kind"] in allowed]
    if kind_filter:
        edges = [e for e in edges if e["kind"] == kind_filter]
    return edges


def activity_items(snapshot: dict) -> list[tuple[str, str]]:
    """(timestamp, text) newest first, from edges, model switches and task updates."""
    names = {a["node_id"]: node_name(a) for a in snapshot.get("agents") or []}
    items: list[tuple[str, str]] = []
    for e in snapshot.get("edges") or []:
        who = lambda n: clean(names.get(n, n), 14)  # noqa: E731
        items.append((str(e.get("updated_at") or ""),
                      f"{e.get('kind')} {who(e.get('from'))} → {who(e.get('to'))} {e.get('state', '')}"))
    for a in snapshot.get("agents") or []:
        for ch in a.get("model_changes") or []:
            items.append((str(ch.get("at") or ""), f"{node_name(a)} model {clean(ch.get('from') or '?', 12)} → "
                                                   f"{clean(ch.get('to') or '?', 12)}"))
    items.sort(key=lambda it: it[0], reverse=True)
    return items


def node_name(agent: dict) -> str:
    return clean(agent.get("label") or agent.get("agent_id") or agent.get("node_id") or "?", 18)


def hhmmss(stamp: str) -> str:
    return stamp[11:19] if len(stamp) >= 19 else "--:--:--"


def mode_title(mode: str) -> str:
    return mode.capitalize()


# ------------------------------------------------------------------------------------------------- map layout
_VERB = {"communication": "message", "delegation": "delegate", "handoff": "handoff", "review": "review", "ownership": "owns"}
_ABBR = {"attention": "attn", "disconnected": "disc"}
Box = tuple[int, int, int, int]


def layout_grid(count: int, w: int, h: int, nw: int, nh: int) -> list[tuple[int, int]]:
    """Top-left corners on an even grid spread across (w, h); stacked tightly (and scrolled) when it cannot fit."""
    cols = 1 if count == 1 else 2 if count <= 4 else 3 if count <= 9 else max(1, w // (nw + 2))
    cols = max(1, min(cols, w // (nw + 1)))
    rows = -(-count // cols)
    spread = rows * (nh + 2) <= h
    out = []
    for i in range(count):
        c, r = i % cols, i // cols
        x = (w - nw) // 2 if cols == 1 else round(c * (w - nw) / (cols - 1))
        y = ((h - nh) // 2 if rows == 1 else round(r * (h - nh) / (rows - 1))) if spread else r * (nh + 1)
        out.append((x, y))
    return out


def ring_fits(count: int, w: int, h: int, nw: int, nh: int) -> bool:
    return 3 <= count <= 8 and w >= nw * 2 + 8 and h >= nh * 2 + 1 and (count <= 4 or (w >= nw * 3 + 6 and h >= nh * 3))


def layout_ring(count: int, w: int, h: int, nw: int, nh: int) -> list[tuple[int, int]]:
    """Agents on an ellipse around the central hub: top, right, bottom, left for four (two sit left and right)."""
    import math
    if count == 1:
        return [((w - nw) // 2, 0)]
    cx, cy, rx, ry = (w - nw) / 2, (h - nh) / 2, (w - nw) / 2, (h - nh) / 2
    start = math.pi if count == 2 else -math.pi / 2
    return [(round(cx + rx * math.cos(start + 2 * math.pi * i / count)),
             round(cy + ry * math.sin(start + 2 * math.pi * i / count))) for i in range(count)]


def route(a: Box, b: Box, blocked: set[tuple[int, int]], occupied: dict[tuple[int, int], str],
          w: int, h: int) -> list[tuple[int, int, str]]:
    """Cheapest orthogonal (x, y, 'h'|'v') path from box a to box b over free cells (Dijkstra): few turns, no walking
    through nodes or the hub, and a heavy cost for running along a cell another edge already uses (crossing is cheap).
    Returns [] when no path exists (e.g. the nodes are boxed in)."""
    import heapq

    def ring(box: Box) -> dict[tuple[int, int], int]:
        x, y, bw, bh = box
        cells = {(x + bw, y + r): abs(r - bh // 2) for r in range(1, bh - 1)}
        cells |= {(x - 1, y + r): abs(r - bh // 2) for r in range(1, bh - 1)}
        cells |= {(x + c, y - 1): abs(c - bw // 2) // 3 for c in range(2, bw - 2)}
        cells |= {(x + c, y + bh): abs(c - bw // 2) // 3 for c in range(2, bw - 2)}
        return {c: v for c, v in cells.items() if 0 <= c[0] < w and 0 <= c[1] < h and c not in blocked}

    starts, goals = ring(a), ring(b)
    margin = {(x + dx, y + dy) for x, y in blocked for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))} - blocked
    heap: list[tuple[int, int, int, int, int]] = [(cost, x, y, -1, 0) for (x, y), cost in starts.items()]
    heapq.heapify(heap)
    best: dict[tuple[int, int, int], int] = {}
    parent: dict[tuple[int, int, int], tuple[int, int, int] | None] = {(x, y, -1): None for (x, y) in starts}
    while heap:
        cost, x, y, d, _ = heapq.heappop(heap)
        if best.get((x, y, d), 1 << 30) < cost:
            continue
        if (x, y) in goals and d != -1:
            cost += goals[(x, y)]
            cells = []
            node: tuple[int, int, int] | None = (x, y, d)
            while node:
                cells.append(node)
                node = parent[node]
            cells.reverse()
            out = [(cx, cy, "h" if cd in (0, 1) else "v") for cx, cy, cd in cells]
            if out:
                out[0] = (out[0][0], out[0][1], out[1][2] if len(out) > 1 else out[0][2])
            return out
        for nd, (dx, dy) in enumerate(((1, 0), (-1, 0), (0, 1), (0, -1))):
            nx, ny = x + dx, y + dy
            if not (0 <= nx < w and 0 <= ny < h) or (nx, ny) in blocked:
                continue
            step = 1 + (3 if d not in (-1, nd) else 0) + (5 if (nx, ny) in margin else 0)
            seen = occupied.get((nx, ny))
            if seen:
                step += 30 if seen == ("h" if nd < 2 else "v") else 4
            nc = cost + step
            key = (nx, ny, nd)
            if nc < best.get(key, 1 << 30):
                best[key] = nc
                parent[key] = (x, y, d)
                heapq.heappush(heap, (nc, nx, ny, nd, 0))
    return []


def hub_room(snapshot: dict) -> dict | None:
    rooms = sorted(snapshot.get("rooms") or [], key=lambda r: (-len(r.get("agent_ids") or []), str(r.get("room_id"))))
    return rooms[0] if rooms else None


def hub_text(snapshot: dict, w: int) -> tuple[str, str]:
    r = hub_room(snapshot)
    if r:
        return f"room …{clean(r.get('room_id'), 80)[-6:]}", f"{len(r.get('agent_ids') or [])} members"
    return "ops hub", f"{len(snapshot.get('agents') or [])} agents"


def edge_label(edge: dict, room: int) -> str:
    """'delegate · api-impl': the kind is always spelled out; only the task id is abbreviated to fit ``room`` cells."""
    verb = ("~" if is_planned(edge) else "") + _VERB.get(edge["kind"], edge["kind"])
    verb += f" {str(edge['state']).upper()}" if is_broken(edge) else ""
    task = clean(edge.get("task_id") or "", 40)
    if not task:
        return verb
    left = room - cell_width(verb) - 3
    return f"{verb} · {task if cell_width(task) <= left else fit(task, max(left - 1, 3)) + '…'}" if left >= 4 else verb


def draw_map(snapshot: dict, w: int, h: int, mode: str, focus: int, tick: int, opts: RenderOpts,
             *, compact: bool) -> list[str]:
    cv = Canvas(w, h)
    agents = snapshot.get("agents") or []
    nw, nh = (22, 5) if compact else (26, 5)
    nw = min(nw, max(w - 2, 8))
    if not agents:
        msg = "no agents reported yet" if snapshot.get("counts", {}).get("agents", 0) == 0 else "no agents visible"
        cv.text(max((w - cell_width(msg)) // 2, 0), h // 2, msg, "dim")
        return cv.lines(opts.color)
    ring = ring_fits(len(agents), w, h, 22, nh)
    if ring:
        nw = 22
    pos = layout_ring(len(agents), w, h, nw, nh) if ring else layout_grid(len(agents), w, h, nw, nh)
    shift = 0   # a grid may overflow; scroll rows so the focused node stays in view
    if not ring and pos and max(y for _, y in pos) + nh > h:
        shift = max(0, min(pos[focus % len(pos)][1] + nh - h, max(y for _, y in pos) + nh - h))
    boxes = {a["node_id"]: (x, y - shift, nw, nh) for a, (x, y) in zip(agents, pos)}
    solid: list[Box] = list(boxes.values())
    hub: Box | None = None
    if ring:
        hw = 18
        hub = ((w - hw) // 2, (h - 3) // 2, hw, 3)
        solid.append(hub)

    def free(x: int, y: int, own: str = "") -> bool:
        if not (0 <= x < w and 0 <= y < h) or any(bx <= x < bx + bw and by <= y < by + bh for bx, by, bw, bh in solid):
            return False
        return cv.at(x, y) == " " or cv.at(x, y) in own

    blocked = {(x + i, y + j) for x, y, bw, bh in solid for i in range(bw) for j in range(bh)}
    occupied: dict[tuple[int, int], str] = {}
    drawn: list[tuple[dict, list[tuple[int, int, str]], str]] = []
    for edge in visible_edges(snapshot, mode, opts.filter_kind):
        a, b = boxes.get(edge.get("from")), boxes.get(edge.get("to"))
        if not a or not b or edge.get("from") == edge.get("to"):
            continue        # an edge needs two distinct endpoints (a self ownership is shown on the node instead)
        path = route(a, b, blocked, occupied, w, h)
        occupied.update({(x, y): d for x, y, d in path})
        color, sh, sv, dh, dv, arrow, ah_, av_, aa = _EDGE[edge["kind"]]
        broken = is_broken(edge)
        if broken:
            color = "red"       # stalled = dashed red, failed = solid red ending in a cross; neither looks accepted
            if str(edge.get("state")).lower() == "failed":
                arrow, aa = "✗", "x"
        planned = is_planned(edge) or str(edge.get("state")).lower() == "stalled"
        for i, (x, y, d) in enumerate(path):
            if opts.unicode:
                ch = (dh if d == "h" else dv) if planned else (sh if d == "h" else sv)
            else:
                ch = " " if planned and i % 2 else (ah_ if d == "h" else av_)
            cv.put(x, y, ch, color)
        if path:
            cv.put(path[-1][0], path[-1][1], arrow if opts.unicode else aa, color + "+bold")
        drawn.append((edge, path, color))
    if hub:
        title, sub = hub_text(snapshot, hub[2])
        hx, hy, hw, _ = hub
        tl, tr, bl, br, hz, vt = ("╭", "╮", "╰", "╯", "┈", "┊") if opts.unicode else ("+", "+", "+", "+", ".", ":")
        for row, (l, m, r) in enumerate(((tl, hz, tr), (vt, " ", vt), (bl, hz, br))):
            cv.put(hx, hy + row, l, "dim")
            cv.put(hx + hw - 1, hy + row, r, "dim")
            for c in range(1, hw - 1):
                cv.put(hx + c, hy + row, m, "dim")
        cv.text(hx + 2, hy, fit(f" {title} ", hw - 4), "cyan")
        room_health = (hub_room(snapshot) or {}).get("context_health")
        if isinstance(room_health, dict):
            avg = room_health.get("average_fraction")
            avg = avg if isinstance(avg, (int, float)) else None
            partial = f" {room_health.get('measured_count')}/{room_health.get('total')}" if avg is not None and \
                room_health.get("measured_count") != room_health.get("total") else ""
            cv.text(hx + 2, hy + 2, fit(f" {pct_text(avg)}{partial} ", hw - 4), ctx_level_style(avg) or "dim")
        cv.text(hx + max((hw - cell_width(sub)) // 2, 1), hy + 1, fit(sub, hw - 2), "cyan")
    for i, a in enumerate(agents):
        draw_node(cv, a, boxes[a["node_id"]], i == focus % len(agents), opts, compact, snapshot, mode)
    for edge, path, color in drawn:     # labels go on the longest straight run, on a free row/column beside the line
        runs: list[list[tuple[int, int, str]]] = []
        for cell in path:
            if runs and runs[-1][-1][2] == cell[2]:
                runs[-1].append(cell)
            else:
                runs.append([cell])
        if not runs:
            continue
        placed = False
        for room in (40, 20, 0):      # fullest label first; the task id shrinks before the kind does
            text = edge_label(edge, room)
            for run in sorted(runs, key=len, reverse=True):
                x0, y0, d = run[0]
                if d == "h":
                    xs = min(x0, run[-1][0]) + (len(run) - cell_width(text)) // 2
                    spots = [(xs, y0 - 1), (xs, y0 + 1)]
                else:
                    rows = sorted((c[1] for c in run), key=lambda r: abs(r - run[len(run) // 2][1]))
                    spots = [(sx, r) for r in rows for sx in (x0 + 2, x0 - 1 - cell_width(text))]
                spot = next(((x, y) for x, y in spots if all(free(x + i, y) for i in range(cell_width(text)))), None)
                if spot:
                    cv.text(spot[0], spot[1], text, color)
                    placed = True
                    break
            if placed:
                break
    for edge, path, color in drawn:     # activity pulses: a glyph travelling the edge; fixed mid-edge under reduced motion
        live = not is_planned(edge) and not is_broken(edge) and any(
            next((x for x in agents if x["node_id"] == n), {}).get("state") == "working" for n in (edge.get("from"), edge.get("to")))
        if live and path and mode != "replay":
            px, py, _ = path[len(path) // 2 if opts.reduced_motion else tick % len(path)]
            cv.put(px, py, "◉" if opts.unicode else "*", color + "+bold")
    return cv.lines(opts.color)


def draw_node(cv: Canvas, agent: dict, box: tuple[int, int, int, int], focused: bool, opts: RenderOpts,
              compact: bool, snapshot: dict, mode: str) -> None:
    x, y, w, h = box
    state = str(agent.get("state") or "unknown")
    color = _STATE_COLOR.get(state, "gray")
    tl, tr, bl, br, hz, vt = ("╭", "╮", "╰", "╯", "─", "│") if opts.unicode else ("+", "+", "+", "+", "-", "|")
    style = color + ("+bold" if focused else "")
    for r in range(h):
        for c in range(w):
            cv.put(x + c, y + r, " ")
    cv.put(x, y, tl, style)
    cv.put(x + w - 1, y, tr, style)
    cv.put(x, y + h - 1, bl, style)
    cv.put(x + w - 1, y + h - 1, br, style)
    for c in range(1, w - 1):
        cv.put(x + c, y, hz, style)
        cv.put(x + c, y + h - 1, hz, style)
    for r in range(1, h - 1):
        cv.put(x, y + r, vt, style)
        cv.put(x + w - 1, y + r, vt, style)
    inner = w - 4
    dot = _STATE_DOT.get(state, "?") if opts.unicode else {"working": "*", "idle": "o", "blocked": "!"}.get(state, "?")
    shown_state = _ABBR.get(state, state) if compact else state
    rows = [f"{dot} {'▸' if focused and opts.unicode else '>' if focused else ''}{node_name(agent)}", f"{clean(agent.get('model') or 'model ?', max(inner - cell_width(shown_state) - 1, 4))} {clean(shown_state, 9)}"]
    badge, ctx_style = ctx_badge(agent, inner, opts)
    if not compact or h >= 4:
        rows.append(badge)
    if mode == "ownership":
        owned = [t["task_id"] for t in snapshot.get("tasks") or [] if t.get("accountable_owner") in
                 (agent.get("agent_id"), agent.get("node_id"))]
        rows[1] = ("owns " + ",".join(clean(t, 6) for t in owned)) if owned else rows[1]
    for i, text in enumerate(rows[:h - 2]):
        if i == 0 and focused:      # visible focus: the whole name row in reverse video (marker + bold frame without colour)
            cv.text(x + 1, y + 1, pad(" " + text, w - 2), color + "+rev")
            continue
        cv.text(x + 2, y + 1 + i, text, color if i == 0 else ctx_style if i == 2 else None, limit=inner)
    if drift_kinds(agent):      # heuristic signal, so the tag says so: "?" and never a verdict
        cv.text(x + 2, y + h - 1, f" ~drift?{len(drift_kinds(agent)) if len(drift_kinds(agent)) > 1 else ''} ", "yellow")


# --------------------------------------------------------------------------------------------- side panels
def boxed(title: str, body: list[str], w: int, h: int, opts: RenderOpts, active: bool, scroll: int = 0) -> list[str]:
    """A titled panel exactly w x h cells (plain text, ANSI added only on the frame)."""
    tl, tr, bl, br, hz, vt = ("╭", "╮", "╰", "╯", "─", "│") if opts.unicode else ("+", "+", "+", "+", "-", "|")
    if active:
        tl, tr, bl, br, hz, vt = ("┏", "┓", "┗", "┛", "━", "┃") if opts.unicode else ("#", "#", "#", "#", "=", "#")
    cap = fit(f" {title} ", max(w - 4, 0))
    out = [tl + cap + hz * max(w - 2 - cell_width(cap), 0) + tr]
    window = body[scroll:scroll + max(h - 2, 0)]
    for line in window:
        out.append(vt + pad(line, w - 2) + vt)
    while len(out) < h - 1:
        out.append(vt + " " * (w - 2) + vt)
    out.append(bl + hz * (w - 2) + br)
    return [fit(l, w) for l in out[:h]]


def task_lines(snapshot: dict, w: int) -> list[str]:
    lines = []
    for t in snapshot.get("tasks") or []:
        lines.append(clean(f"{t.get('task_id')} owner:{t.get('accountable_owner') or '?'}", w - 2))
        contrib = ",".join(clean(c, 8) for c in t.get("contributors") or []) or "-"
        lines.append(clean(f"  by:{contrib} rev:{t.get('reviewer') or '-'}", w - 2))
        if drift_kinds(t):
            lines.append(clean(f"  ~drift? {','.join(drift_kinds(t))}", w - 2))
    return lines or ["no tasks"]


def agent_lines(snapshot: dict, w: int, opts: RenderOpts) -> list[str]:
    """name / model / state / context columns sized so ``attention`` and a 9-character model fit untruncated."""
    rows = [clean(f"{pad(node_name(a), 8)} {pad(clean(a.get('model') or 'model ?', 10), 10)} {pad(clean(a.get('state'), 9), 9)} "
                  f"{context_pct(a['context']) if isinstance(a.get('context'), dict) and context_pct(a['context']) else 'ctx ?'}", w - 2)
            for a in snapshot.get("agents") or []]
    return rows or ["no agents"]


def context_pct(context: dict) -> str:
    bar = context_bar(context, 9, RenderOpts())
    return bar.split("] ")[-1] if "]" in bar else bar


def activity_lines(snapshot: dict, w: int) -> list[str]:
    items = activity_items(snapshot)
    return [clean(f"{hhmmss(ts)} {text}", w - 2) for ts, text in items] or ["no activity"]


def timeline_bar(snapshot: dict, w: int, opts: RenderOpts) -> list[str]:
    """Two rows: a time axis with a marker per activity item (or the replay position), and its caption."""
    stamps = sorted(ts for ts, _ in activity_items(snapshot) if ts)
    track = ("─" if opts.unicode else "-") * w
    cells = list(track)
    mark = "●" if opts.unicode else "o"
    if stamps and stamps[-1] != stamps[0]:
        def secs(s: str) -> float:
            return int(s[11:13]) * 3600 + int(s[14:16]) * 60 + float(s[17:23])
        lo, hi = secs(stamps[0]), secs(stamps[-1])
        for s in stamps:
            cells[min(int((secs(s) - lo) / max(hi - lo, 1e-9) * (w - 1)), w - 1)] = mark
    elif stamps:
        cells[w // 2] = mark
    if opts.replay_pos:
        i, n = opts.replay_pos
        cells[min(int(i / max(n - 1, 1) * (w - 1)), w - 1)] = "▼" if opts.unicode else "V"
    caption = (f"replay {opts.replay_pos[0] + 1}/{opts.replay_pos[1]}" + (" paused" if opts.replay_paused else " playing")
               if opts.replay_pos else (f"live · {hhmmss(stamps[-1])}" if stamps else "live · no events"))
    return ["".join(cells), fit(caption, w)]


def legend(opts: RenderOpts) -> str:
    parts = []
    for kind in KINDS:
        _, sh, _, dh, _, arrow, ah_, _, aa = _EDGE[kind]
        parts.append(f"{sh * 2 if opts.unicode else ah_ * 2}{arrow if opts.unicode else aa}{kind[:4]}")
    return " ".join(parts) + ("  ╌╌ planned  ╌╌ stalled  ✗ failed" if opts.unicode else "  -  - planned  -  - stalled  x failed")


def status_text(snapshot: dict, opts: RenderOpts) -> str:
    base = {"live": "LIVE", "degraded": "DEGRADED", "offline": "OFFLINE", "replay": "REPLAY"}.get(opts.status, "?")
    return base + (f" ({clean(opts.status_detail, 30)})" if opts.status_detail else "")


STATUS_COLOR = {"live": "green", "degraded": "yellow", "offline": "red", "replay": "magenta"}
HINTS = "Tab pane  ←↑↓→ nodes  ⏎ inspect  h handoffs  t timeline  r replay  f filter  m modes  ? help  q quit"
HINTS_ASCII = "Tab pane  arrows nodes  Enter inspect  h handoffs  t timeline  r replay  f filter  m modes  ? help  q quit"
HINTS_SHORT = "Tab pane ←↑↓→ ⏎ inspect h handoff t time r replay f filter m mode ? help q quit"
HINTS_SHORT_ASCII = "Tab pane arrows Enter h handoff t time r replay f filter m mode ? help q quit"


def style_line(line: str, style: str, opts: RenderOpts) -> str:
    return f"{sgr(style)}{line}{RESET}" if opts.color else line


def join_columns(left: list[str], right: list[str], lw: int) -> list[str]:
    """Combine two blocks side by side; each left line (which may carry ANSI) is padded to lw visible cells."""
    rows = max(len(left), len(right))
    out = []
    for i in range(rows):
        a = left[i] if i < len(left) else ""
        b = right[i] if i < len(right) else ""
        out.append(a + " " * max(lw - visible_width(a), 0) + b)
    return out


def overlay_lines(snapshot: dict, w: int, h: int, focus: int, opts: RenderOpts) -> list[str]:
    agents = snapshot.get("agents") or []
    if opts.overlay == "help":
        text = ["KEYS", "Tab        next pane", "arrows     move between agents", "Enter      inspect agent / close",
                "h          handoffs and delegations", "t          full timeline", "r          replay (scrub [ ] / { } , )",
                "space      pause or play replay", "l          back to live", "f          cycle edge-kind filter",
                "m          map mode: " + "/".join(MODES), "Esc        close overlay", "q          quit",
                "", "MAP: ━ delegation ═ handoff ─ communication ◇ review ● ownership; dashed = planned" if opts.unicode
                else "MAP: = delegation # handoff - communication ~ review . ownership; spaced = planned"]
    elif opts.overlay == "handoffs":
        names = {a["node_id"]: node_name(a) for a in agents}
        text = ["HANDOFFS AND DELEGATIONS"] + [
            clean(f"{e['kind']:<10} {names.get(e['from'], e['from'])} → {names.get(e['to'], e['to'])}  "
                  f"{e.get('state')}  {e.get('task_id') or e.get('handoff_id') or ''}", w - 2)
            for e in snapshot.get("edges") or [] if e.get("kind") in ("handoff", "delegation")] or ["none"]
    elif opts.overlay == "timeline":
        text = ["TIMELINE (newest first)"] + activity_lines(snapshot, w)
    elif opts.overlay == "filter":
        text = ["FILTER", f"edge kind: {opts.filter_kind or 'all'}", "f cycles: all > " + " > ".join(KINDS)]
    else:
        a = agents[focus % len(agents)] if agents else {}
        ctx = a.get("context")
        text = ["INSPECT " + node_name(a), f"node      {clean(a.get('node_id'))}", f"agent     {clean(a.get('agent_id') or '?')}",
                f"session   {clean(a.get('session_id') or '?')}", f"host      {clean(a.get('host_id') or '?')}",
                f"provider  {clean(a.get('provider') or '?')}", f"model     {clean(a.get('model') or '?')}",
                f"state     {clean(a.get('state'))}  ({clean(a.get('provenance'))})",
                f"context   {context_bar(ctx, 14, opts)}  src:{clean((ctx or {}).get('source') if isinstance(ctx, dict) else None) or '?'}",
                f"compact   {a.get('compactions', 0)}  drift {a.get('drift_warnings', 0)}",
                f"ctx health {pct_text(context_fraction(a))[4:]}",
                f"rooms     {', '.join(clean(r, 12) for r in a.get('room_ids') or []) or '-'}", "edges:"]
        if drift_kinds(a):
            text.append("drift (heuristic, not a proven violation):")
            text += [clean(f"  {d.get('kind')} [{d.get('provenance')}] task:{d.get('task_id') or '-'}", w - 2)
                     for d in a.get("drift") or [] if isinstance(d, dict)]
        for e in snapshot.get("edges") or []:
            if a.get("node_id") in (e.get("from"), e.get("to")):
                text.append(clean(f"  {e['kind']} {e['from']} → {e['to']} {e.get('state')}", w - 2))
        text += ["tasks:"] + [clean(f"  {t['task_id']} owner:{t.get('accountable_owner') or '?'} rev:{t.get('reviewer') or '-'}", w - 2)
                              for t in snapshot.get("tasks") or []
                              if a.get("agent_id") in ([t.get("accountable_owner"), t.get("reviewer")] + (t.get("contributors") or []))]
    return boxed(opts.overlay or "inspect", text, w, h, opts, True, opts.scroll)


# ----------------------------------------------------------------------------------------------------- render
def render(snapshot: dict, width: int, height: int, mode: str = "activity", focus: int = 0, tick: int = 0,
           opts: RenderOpts | None = None) -> list[str]:
    """Exactly ``height`` lines, each at most ``width`` visible cells. Never raises: a failure becomes a message."""
    opts = opts or RenderOpts()
    width, height = max(width, 20), max(height, 5)
    try:
        lines = _render(snapshot or {}, width, height, mode if mode in MODES else "activity", focus, tick, opts)
    except Exception as exc:  # a bad frame must show a message and keep the pane alive
        lines = [f"render error: {clean(type(exc).__name__ + ' ' + str(exc), width - 14)}", "polling continues; q quits"]
    if not opts.unicode:
        lines = [l.translate(_ASCII) for l in lines]
    lines = [l if visible_width(l) <= width else _trim(l, width) for l in lines]
    return (lines + [""] * height)[:height]


def _trim(line: str, width: int) -> str:
    out, n, i = [], 0, 0
    while i < len(line):
        m = _ANSI.match(line, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        n += char_width(line[i])
        if n > width:
            break
        out.append(line[i])
        i += 1
    return "".join(out) + (RESET if "\x1b" in line else "")


def _render(snapshot: dict, width: int, height: int, mode: str, focus: int, tick: int, opts: RenderOpts) -> list[str]:
    counts = snapshot.get("counts") or {}
    agents = snapshot.get("agents") or []
    status = status_text(snapshot, opts)
    if mode == "replay" and opts.status == "live":
        status = "REPLAY"
    hint = (HINTS if opts.unicode else HINTS_ASCII) if width >= 100 else (HINTS_SHORT if opts.unicode else HINTS_SHORT_ASCII)
    footer = fit(hint, width)
    footer = style_line(footer, "dim", opts)
    header_style = STATUS_COLOR.get(opts.status, "white")
    summary = (f"{counts.get('agents', len(agents))} agents  {counts.get('rooms', 0)} rooms  "
               f"{counts.get('edges', 0)} edges  {counts.get('tasks', 0)} tasks"
               + ("  (truncated)" if snapshot.get("truncated") else ""))
    if opts.error:
        summary += "  ! " + clean(opts.error, 30)
    if width < 60:
        return _render_narrow(snapshot, width, height, mode, focus, opts, status, footer)
    title = "HERDR / AGENT OPERATIONS" if width >= 80 else "AGENT OPS"
    head = f"{title}  [{status}]  {mode_title(mode)}" + ("  filter:" + opts.filter_kind if opts.filter_kind else "")
    header = [style_line(fit(head, width), header_style + "+bold", opts), style_line(fit(summary, width), "dim", opts)]
    if opts.overlay:
        return header + overlay_lines(snapshot, width, height - 3, focus, opts) + [footer]
    hrow = hosts_row(snapshot, width, opts)
    header += [hrow] if hrow is not None else []
    extra = len(header) - 2
    full = width >= 100 and height >= 30
    if full:
        rw = 40
        mw = width - rw - 1
        bottom = 4
        mh = height - 2 - extra - bottom - 1
        map_lines = draw_map(snapshot, mw - 2, mh - 3, mode, focus, tick, opts, compact=False)
        map_block = _boxed_ansi("Map · " + mode_title(mode), map_lines, mw, mh, opts, opts.pane == "map", legend(opts))
        t_h = max(mh // 4, 5)
        g_h = max(mh // 3, 6)
        a_h = max(mh - t_h - g_h, 5)
        right = (boxed("Tasks", task_lines(snapshot, rw), rw, t_h, opts, opts.pane == "tasks", opts.scroll if opts.pane == "tasks" else 0)
                 + boxed("Agents", agent_lines(snapshot, rw, opts), rw, g_h, opts, False)
                 + boxed("Recent activity", activity_lines(snapshot, rw), rw, a_h, opts, opts.pane == "activity",
                         opts.scroll if opts.pane == "activity" else 0))
        body = join_columns(map_block, right[:mh], mw + 1)
        tl = timeline_bar(snapshot, width - 2, opts)
        bottom_block = boxed("Timeline", tl, width, bottom, opts, opts.pane == "timeline")
        return header + body + bottom_block + [footer]
    # compact (80x24 and similar): map hero, a two-panel strip below
    strip = 6 if height >= 20 else 3
    mh = height - 2 - extra - strip - 3
    map_lines = draw_map(snapshot, width, mh, mode, focus, tick, opts, compact=True)
    lg = style_line(fit(legend(opts), width), "dim", opts)
    half = width // 2
    left = boxed("Tasks", task_lines(snapshot, half), half, strip, opts, opts.pane == "tasks", opts.scroll if opts.pane == "tasks" else 0)
    right = boxed("Activity", activity_lines(snapshot, width - half), width - half, strip, opts, opts.pane == "activity",
                  opts.scroll if opts.pane == "activity" else 0)
    tl = timeline_bar(snapshot, width, opts)
    return header + map_lines + [lg] + join_columns(left, right, half) + [style_line(fit(tl[0], width), "dim", opts), footer]


def _boxed_ansi(title: str, inner: list[str], w: int, h: int, opts: RenderOpts, active: bool, foot: str) -> list[str]:
    """Frame already-coloured map lines (they carry their own ANSI) inside a w x h border with a legend row."""
    tl, tr, bl, br, hz, vt = ("╭", "╮", "╰", "╯", "─", "│") if opts.unicode else ("+", "+", "+", "+", "-", "|")
    if active:
        tl, tr, bl, br, hz, vt = ("┏", "┓", "┗", "┛", "━", "┃") if opts.unicode else ("#", "#", "#", "#", "=", "#")
    cap = fit(f" {title} ", max(w - 4, 0))
    out = [tl + cap + hz * max(w - 2 - cell_width(cap), 0) + tr]
    for i in range(h - 3):
        line = inner[i] if i < len(inner) else ""
        out.append(vt + line + " " * max(w - 2 - visible_width(line), 0) + vt)
    out.append(vt + style_line(pad(foot, w - 2), "dim", opts) + vt)
    out.append(bl + hz * (w - 2) + br)
    return out


def _render_narrow(snapshot: dict, width: int, height: int, mode: str, focus: int, opts: RenderOpts, status: str,
                   footer: str) -> list[str]:
    agents = snapshot.get("agents") or []
    lines = [style_line(fit(f"AGENT OPS [{status}] {mode_title(mode)}", width), STATUS_COLOR.get(opts.status, "white") + "+bold", opts)]
    if opts.overlay:
        return lines + overlay_lines(snapshot, width, height - 2, focus, opts) + [style_line("q quit Esc close"[:width], "dim", opts)]
    hrow = hosts_row(snapshot, width, opts)
    lines += [hrow] if hrow is not None else []
    for i, a in enumerate(agents):
        state = str(a.get("state") or "unknown")
        mark = ">" if i == focus % max(len(agents), 1) else " "
        row = f"{mark}{_STATE_DOT.get(state, '?') if opts.unicode else state[:1]} {node_name(a)} {clean(a.get('model') or 'model ?', 10)} {ctx_badge(a, 8, opts)[0]}"
        lines.append(style_line(fit(row, width), _STATE_COLOR.get(state, "gray"), opts))
    for e in visible_edges(snapshot, mode, opts.filter_kind):
        lines.append(clean(f" {e['kind'][:5]} {e['from']}→{e['to']} {'~' if is_planned(e) else ''}{e.get('state')}", width))
    lines += [clean(" " + l, width) for l in task_lines(snapshot, width)]
    lines += [clean(" " + l, width) for l in activity_lines(snapshot, width)]
    body = height - 1
    lines = lines[:body] + [""] * max(body - len(lines), 0)
    return lines + [style_line("Tab ?:help q:quit"[:width], "dim", opts)]


# ------------------------------------------------------------------------------------------- state machine
@dataclass(frozen=True)
class UIState:
    mode: str = "activity"
    focus: int = 0
    pane: str = "map"
    overlay: str | None = None
    filter_kind: str | None = None
    scroll: int = 0
    replay_index: int | None = None     # None = live
    replay_paused: bool = True
    previous_mode: str = "activity"
    effects: tuple = field(default=())


def handle_key(state: UIState, key: str, agent_count: int, event_total: int = 0) -> UIState:
    """Pure reducer. ``effects`` on the result lists what the caller must do: ``quit`` | ``("replay", index)`` | ``live``."""
    s = replace(state, effects=())
    n = max(agent_count, 1)
    if key in ("q", "ctrl-c"):
        return replace(s, effects=("quit",))
    if key == "esc":
        return replace(s, overlay=None, scroll=0)
    if key == "?":
        return replace(s, overlay=None if s.overlay == "help" else "help", scroll=0)
    if key == "tab":
        return replace(s, pane=PANES[(PANES.index(s.pane) + 1) % len(PANES)], scroll=0)
    if key in ("left", "up", "right", "down"):
        if s.overlay or s.pane != "map":
            delta = {"up": -1, "left": -1, "down": 1, "right": 1}[key]
            return replace(s, scroll=max(s.scroll + delta, 0))
        step = -1 if key in ("left", "up") else 1
        return replace(s, focus=(s.focus + step) % n)
    if key == "enter":
        return replace(s, overlay=None if s.overlay == "inspect" else "inspect", scroll=0)
    if key == "h":
        return replace(s, overlay=None if s.overlay == "handoffs" else "handoffs", scroll=0)
    if key == "t":
        return replace(s, overlay=None if s.overlay == "timeline" else "timeline", scroll=0)
    if key == "f":
        order = (None,) + KINDS
        return replace(s, filter_kind=order[(order.index(s.filter_kind) + 1) % len(order)])
    if key == "m":
        live_modes = MODES[:-1]
        base = s.mode if s.mode in live_modes else s.previous_mode
        nxt = live_modes[(live_modes.index(base) + 1) % len(live_modes)] if s.mode != "replay" else base
        return replace(s, mode=nxt, previous_mode=nxt, overlay=None)
    if key == "r":
        if event_total <= 0:
            return s
        idx = event_total - 1 if s.replay_index is None else s.replay_index
        return replace(s, mode="replay", replay_index=idx, replay_paused=True, overlay=None,
                       previous_mode=s.mode if s.mode != "replay" else s.previous_mode, effects=(("replay", idx),))
    if key == "l":
        if s.replay_index is None:
            return s
        return replace(s, mode=s.previous_mode, replay_index=None, replay_paused=True, effects=("live",))
    if s.replay_index is not None and event_total > 0:
        if key == " ":
            return replace(s, replay_paused=not s.replay_paused)
        deltas = {"[": -1, "]": 1, "{": -10, "}": 10}
        if key in deltas or key == ",":
            idx = 0 if key == "," else min(max(s.replay_index + deltas[key], 0), event_total - 1)
            return replace(s, replay_index=idx, effects=(("replay", idx),))
    return s


_KEYMAP = {"[A": "up", "OA": "up", "[B": "down", "OB": "down", "[C": "right", "OC": "right", "[D": "left", "OD": "left"}


def parse_keys(data: bytes) -> list[str]:
    text, keys, i = data.decode("utf-8", "ignore"), [], 0
    while i < len(text):
        c = text[i]
        if c == "\x1b":
            m = re.match(r"[\[O][0-9;]*[A-Za-z~]", text[i + 1:])
            if m:
                key = _KEYMAP.get(m.group())
                if key:
                    keys.append(key)
                i += 1 + m.end()
                continue
            keys.append("esc")
        elif c == "\t":
            keys.append("tab")
        elif c in "\r\n":
            keys.append("enter")
        elif c == "\x03":
            keys.append("ctrl-c")
        elif c.isprintable():
            keys.append(c)
        i += 1
    return keys


# ----------------------------------------------------------------------------------------------- network
class Client:
    def __init__(self, base_url: str, api_key: str = "", token: str = "", timeout: float = 5.0):
        self.base = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key} if api_key else {}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout

    def _conn(self, timeout: float | None = None) -> tuple[http.client.HTTPConnection, str]:
        u = urllib.parse.urlparse(self.base)
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        return cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=timeout or self.timeout), u.path

    def get(self, path: str, **query: str) -> dict:
        conn, prefix = self._conn()
        try:
            qs = ("?" + urllib.parse.urlencode(query)) if query else ""
            conn.request("GET", f"{prefix}/api/v1/agent-ops/{path}{qs}", headers=self.headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                raise OSError(f"HTTP {resp.status}")
            return json.loads(raw)
        finally:
            conn.close()

    def all_cursors(self) -> list[str]:
        """Every event cursor, oldest first, for replay scrubbing (capped to keep it bounded)."""
        cursors, cursor = [], ""
        for _ in range(20):
            page = self.get("events", **({"cursor": cursor} if cursor else {}), limit="500")
            cursors += [e["cursor"] for e in page.get("events", []) if isinstance(e.get("cursor"), str)]   # the server's own
            cursor = page.get("next_cursor") or ""
            if not page.get("has_more") or not cursor:
                break
        return [c for c in cursors if c]

    def stream(self, cursor: str, stop: threading.Event, on_event, on_state) -> None:
        """SSE with cursor resume (Last-Event-ID) and exponential backoff; runs until ``stop`` is set."""
        delay = 1.0
        while not stop.is_set():
            conn = None
            try:
                conn, prefix = self._conn(timeout=30)
                headers = dict(self.headers, Accept="text/event-stream")
                if cursor:
                    headers["Last-Event-ID"] = cursor
                conn.request("GET", f"{prefix}/api/v1/agent-ops/stream", headers=headers)
                resp = conn.getresponse()
                if resp.status != 200:
                    if resp.status == 400 and cursor:     # the server refuses our cursor: drop it and re-fetch the snapshot
                        cursor = ""
                        on_event(cursor)
                    raise OSError(f"HTTP {resp.status}")
                on_state("live")
                delay = 1.0
                while not stop.is_set():
                    line = resp.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", "replace").rstrip("\r\n")
                    if text.startswith("id:"):
                        cursor = text[3:].strip()
                        on_event(cursor)
                    elif text.startswith("data:") and "cursor" in text and not cursor:
                        try:
                            cursor = json.loads(text[5:]).get("cursor") or cursor
                        except ValueError:
                            pass
                    elif text.startswith("data:") and '"reset"' in text:
                        try:
                            reset = json.loads(text[5:]).get("reset") is True
                        except ValueError:
                            reset = False
                        if reset and cursor:      # `connected` says our resume cursor was ignored: re-fetch the snapshot
                            cursor = ""
                            on_event(cursor)
            except (OSError, http.client.HTTPException, ValueError) as exc:
                on_state("degraded", clean(exc, 24))
            finally:
                if conn is not None:
                    conn.close()
            if stop.wait(delay):
                return
            delay = min(delay * 2, 30.0)


# --------------------------------------------------------------------------------------------- the pane loop
def resolve_token(args, env) -> tuple[str, bool]:
    """(collab token, whether it came from argv). Order: --token-file, --token, $HERDR_AGENT_OPS_TOKEN, $HERDR_COLLAB_TOKEN."""
    if getattr(args, "token_file", None):
        try:
            with open(args.token_file, encoding="utf-8") as fh:
                return fh.read().strip(), False
        except OSError as exc:
            raise SystemExit(f"agent-ops: cannot read --token-file: {exc.strerror or exc}") from exc
    if args.token:
        return args.token, True
    return env.get("HERDR_AGENT_OPS_TOKEN") or env.get("HERDR_COLLAB_TOKEN", ""), False


def run(args) -> int:
    base = args.url or os.environ.get("HERDR_CP_URL", "http://127.0.0.1:8080")
    token, from_argv = resolve_token(args, os.environ)
    if from_argv:
        print("warning: --token is visible in the process list (ps); prefer --token-file or $HERDR_AGENT_OPS_TOKEN",
              file=sys.stderr)
    client = Client(base, args.api_key or os.environ.get("HERDR_CP_API_KEY", ""), token)
    out = sys.stdout
    opts = detect_opts(isatty=out.isatty(), no_animation=args.no_animation, ascii_only=args.ascii)
    if args.once:
        snap = client.get("snapshot")
        print("\n".join(render(snap, args.width, args.height, opts=opts)))
        return 0
    stop, wake = threading.Event(), threading.Event()
    shared = {"status": "offline", "detail": "connecting", "stream": "degraded"}

    def on_state(status: str, detail: str = "") -> None:
        shared["stream"], shared["detail"] = status, detail
        wake.set()

    threading.Thread(target=client.stream, args=("", stop, lambda c: wake.set(), on_state), daemon=True).start()
    attrs = termios.tcgetattr(0)
    ui, snap, cursors, tick, last_fetch, replay_snap = UIState(), {}, [], 0, 0.0, None
    error = ""
    out.write("\x1b[?1049h\x1b[?25l")
    signal.signal(signal.SIGWINCH, lambda *_: wake.set())
    try:
        tty.setcbreak(0)
        while True:
            now = time.monotonic()
            if ui.replay_index is None and (wake.is_set() or now - last_fetch > 5.0 or not snap):
                try:
                    snap, last_fetch, error = client.get("snapshot"), now, ""
                    shared["status"] = "live" if shared["stream"] == "live" else "degraded"
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    shared["status"], error, last_fetch = "offline", clean(exc, 30), now
                wake.clear()
            shown = replay_snap if ui.replay_index is not None and replay_snap else snap
            try:
                cols, rows = os.get_terminal_size(out.fileno())
            except OSError:
                cols, rows = 80, 24
            status = "replay" if ui.replay_index is not None else shared["status"]
            o = replace(opts, status=status, status_detail=shared["detail"] if status != "live" else "", overlay=ui.overlay,
                        pane=ui.pane, scroll=ui.scroll, filter_kind=ui.filter_kind, error=error,
                        replay_pos=(ui.replay_index, len(cursors)) if ui.replay_index is not None else None,
                        replay_paused=ui.replay_paused)
            frame = render(shown, cols, rows, ui.mode, ui.focus, tick, o)
            out.write("".join(f"\x1b[{i + 1};1H{l}{RESET}\x1b[K" for i, l in enumerate(frame)))
            out.flush()
            tick += 1
            animate = not opts.reduced_motion or (ui.replay_index is not None and not ui.replay_paused)
            ready, _, _ = select.select([0], [], [], 0.25 if animate else 1.0)
            if ui.replay_index is not None and not ui.replay_paused and not ready and ui.replay_index < len(cursors) - 1:
                ui = replace(ui, replay_index=ui.replay_index + 1)
                replay_snap = _safe_replay(client, cursors[ui.replay_index], replay_snap)
            if not ready:
                continue
            data = os.read(0, 64)
            if not data:
                return 0
            for key in parse_keys(data):
                if key == "r" and ui.replay_index is None:
                    try:
                        cursors = client.all_cursors()
                    except (OSError, ValueError, http.client.HTTPException) as exc:
                        error = "replay: " + clean(exc, 24)
                ui = handle_key(ui, key, len(shown.get("agents") or []), len(cursors))
                for effect in ui.effects:
                    if effect == "quit":
                        return 0
                    if effect == "live":
                        replay_snap = None
                        wake.set()
                    elif isinstance(effect, tuple):
                        replay_snap = _safe_replay(client, cursors[effect[1]], replay_snap)
    except KeyboardInterrupt:
        return 0
    finally:
        stop.set()
        out.write("\x1b[?25h\x1b[?1049l")
        out.flush()
        termios.tcsetattr(0, termios.TCSADRAIN, attrs)


def _safe_replay(client: Client, cursor: str, previous: dict | None) -> dict | None:
    try:
        return client.get("replay", until=cursor)
    except (OSError, ValueError, http.client.HTTPException):
        return previous


def register(sub) -> None:
    p = sub.add_parser("agent-ops", help="terminal Agent Operations map (live, replay, inspect) for a Herdr pane")
    p.add_argument("--url", help="control-plane base URL (default $HERDR_CP_URL or http://127.0.0.1:8080)")
    p.add_argument("--api-key", help="viewer API key (default $HERDR_CP_API_KEY)")
    p.add_argument("--token", help="collab bearer token for room visibility. WARNING: visible to every local user in the "
                                   "process list (ps); prefer --token-file or $HERDR_AGENT_OPS_TOKEN (falls back to $HERDR_COLLAB_TOKEN)")
    p.add_argument("--token-file", help="read the collab bearer token from this file (not exposed in ps)")
    p.add_argument("--no-animation", action="store_true", help="static pulses (also HERDR_REDUCED_MOTION=1)")
    p.add_argument("--ascii", action="store_true", help="ASCII glyphs only")
    p.add_argument("--once", action="store_true", help="print one frame and exit (for captures and doctor checks)")
    p.add_argument("--width", type=int, default=120, help="frame width for --once")
    p.add_argument("--height", type=int, default=40, help="frame height for --once")
    p.set_defaults(func=run)
