"""Dependency-free SVG/HTML renderers: charts, Mermaid flowcharts and slide decks.

Output is inert: all text is escaped, nothing is fetched and no script is emitted,
so results can be served through the same protected preview as other artifacts.
"""
from __future__ import annotations

import html
import math
import re

MAX_NODES = 60
MAX_EDGES = 120
PALETTE = ("#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed")
_FONT = "font-family=\"system-ui,sans-serif\" font-size=\"12\" fill=\"#1f2937\""


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _svg(width: float, height: float, body: str, title: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:g} {height:g}" '
            f'width="{width:g}" height="{height:g}" role="img" aria-label="{_esc(title)}">'
            f'<title>{_esc(title)}</title><rect width="100%" height="100%" fill="#ffffff"/>'
            f"{body}</svg>")


# ---------------------------------------------------------------- charts

def _chart_points(spec: dict) -> list[tuple[str, float]]:
    data = spec.get("data")
    if not isinstance(data, list) or not data or len(data) > 500:
        raise ValueError("chart data must be a non-empty list of at most 500 rows")
    points = []
    for row in data:
        if not isinstance(row, dict):
            raise ValueError("chart rows must be objects")
        xkey = spec.get("x") or next((k for k, v in row.items() if not _is_number(v)), None)
        ykey = spec.get("y") or next((k for k, v in row.items() if _is_number(v)), None)
        if xkey is None or ykey is None or not _is_number(row.get(ykey)):
            raise ValueError("each row needs a label and a numeric value")
        points.append((str(row[xkey]), float(row[ykey])))
    return points


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _nice_step(span: float) -> float:
    raw = span / 4 or 1
    magnitude = 10 ** math.floor(math.log10(raw))
    return next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw)


def render_chart_svg(spec: dict) -> str:
    kind = spec.get("type")
    if kind not in {"bar", "line"}:
        raise ValueError("chart type must be 'bar' or 'line'")
    points = _chart_points(spec)
    title = str(spec.get("title") or "Chart")
    left, right, top, bottom = 56, 16, 40, 56
    width = max(480, min(1200, left + right + 48 * len(points)))
    height = 360
    lo = min(0.0, min(v for _, v in points))
    hi = max(0.0, max(v for _, v in points))
    step = _nice_step(hi - lo)
    lo = math.floor(lo / step) * step
    hi = math.ceil(hi / step) * step or step
    plot_w, plot_h = width - left - right, height - top - bottom

    def y_of(value):
        return top + plot_h * (hi - value) / (hi - lo)

    parts = [f'<text x="{left}" y="24" {_FONT.replace('font-size="12"', 'font-size="15" font-weight="600"')}>{_esc(title)}</text>']
    tick = lo
    while tick <= hi + step / 2:
        y = y_of(tick)
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e5e7eb"/>'
                     f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" {_FONT}>{tick:g}</text>')
        tick += step
    slot = plot_w / len(points)
    coords = []
    for index, (label, value) in enumerate(points):
        cx = left + slot * (index + 0.5)
        coords.append((cx, y_of(value)))
        if kind == "bar":
            bar_w = slot * 0.6
            top_y, base_y = sorted((y_of(value), y_of(0)))
            parts.append(f'<rect x="{cx - bar_w / 2:.1f}" y="{top_y:.1f}" width="{bar_w:.1f}" '
                         f'height="{max(base_y - top_y, 1):.1f}" fill="{PALETTE[0]}"><title>{_esc(label)}: {value:g}</title></rect>')
        parts.append(f'<text x="{cx:.1f}" y="{height - bottom + 16}" text-anchor="middle" {_FONT}>{_esc(_clip(label, 12))}</text>')
        parts.append(f'<text x="{cx:.1f}" y="{y_of(value) + (14 if value < 0 else -6):.1f}" text-anchor="middle" {_FONT}>{value:g}</text>')
    if kind == "line":
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        parts.append(f'<polyline points="{path}" fill="none" stroke="{PALETTE[0]}" stroke-width="2"/>')
        parts.extend(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{PALETTE[0]}"/>' for x, y in coords)
    parts.append(f'<line x1="{left}" x2="{left}" y1="{top}" y2="{top + plot_h}" stroke="#9ca3af"/>')
    return _svg(width, height, "".join(parts), title)


# ------------------------------------------------------------- flowcharts

_HEADER = re.compile(r"^\s*(?:graph|flowchart)\s*(TD|TB|LR|RL|BT)?\s*$", re.I)
_NODE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*(?:(\(\()(.+?)\)\)|(\(\[)(.+?)\]\)|(\[)(.+?)\]|(\()(.+?)\)|(\{)(.+?)\}|(>)(.+?)\])?\s*$")
_EDGE = re.compile(r"\s*(-->|---|-\.->|-\.-|==>|===)\s*(?:\|([^|]*)\|)?\s*")


def parse_flowchart(source: str) -> dict:
    """Parse the common Mermaid flowchart subset; raise ValueError for anything else."""
    statements = [part.strip() for line in source.splitlines()
                  for part in line.split(";") if part.strip() and not part.strip().startswith("%%")]
    if not statements:
        raise ValueError("empty diagram")
    header = _HEADER.match(statements[0])
    if not header:
        raise ValueError("only graph/flowchart diagrams are rendered")
    direction = (header.group(1) or "TD").upper().replace("TB", "TD")
    nodes: dict[str, dict] = {}
    edges: list[tuple[str, str, str, str]] = []

    def node(token: str) -> str:
        match = _NODE.match(token)
        if not match:
            raise ValueError(f"unsupported node syntax: {token.strip()[:40]}")
        ident, groups = match.group(1), match.groups()[1:]
        text, shape = ident, "rect"
        for i in range(0, len(groups), 2):
            if groups[i]:
                text = groups[i + 1].strip().strip('"')
                shape = {"((": "circle", "([": "round", "[": "rect", "(": "round",
                         "{": "diamond", ">": "rect"}[groups[i]]
        if ident not in nodes or text != ident:
            nodes[ident] = {"text": text, "shape": shape}
        return ident

    for statement in statements[1:]:
        if re.match(r"(?:subgraph|end|style|classDef|class|linkStyle|click)\b", statement):
            raise ValueError("subgraphs and styling are not rendered")
        pieces = _EDGE.split(statement)
        # split() yields: node, op, label, node, op, label, node ...
        idents = [node(pieces[i]) for i in range(0, len(pieces), 3)]
        for i in range(1, len(pieces), 3):
            edges.append((idents[i // 3], idents[i // 3 + 1], (pieces[i + 1] or "").strip(), pieces[i]))
    if not nodes or len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
        raise ValueError("diagram is empty or too large to draw")
    return {"direction": direction, "nodes": nodes, "edges": edges}


def _layers(nodes: dict, edges: list) -> dict[str, int]:
    forward: dict[str, list[str]] = {n: [] for n in nodes}
    state: dict[str, int] = {}

    def visit(start):
        stack = [(start, iter([dst for src, dst, *_ in edges if src == start]))]
        state[start] = 1
        while stack:
            current, children = stack[-1]
            for child in children:
                if state.get(child) == 1:
                    continue  # back edge: cycles are drawn but do not add depth
                forward[current].append(child)
                if child not in state:
                    state[child] = 1
                    stack.append((child, iter([dst for src, dst, *_ in edges if src == child])))
                    break
            else:
                state[current] = 2
                stack.pop()

    for name in nodes:
        if name not in state:
            visit(name)
    rank = {n: 0 for n in nodes}
    for _ in range(len(nodes)):
        changed = False
        for src, dsts in forward.items():
            for dst in dsts:
                if rank[dst] < rank[src] + 1:
                    rank[dst] = rank[src] + 1
                    changed = True
        if not changed:
            break
    return rank


def _boundary(cx, cy, w, h, tx, ty):
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return cx, cy
    scale = min((w / 2) / abs(dx) if dx else math.inf, (h / 2) / abs(dy) if dy else math.inf)
    return cx + dx * scale, cy + dy * scale


def render_flowchart_svg(diagram: dict) -> str:
    nodes, edges, direction = diagram["nodes"], diagram["edges"], diagram["direction"]
    horizontal = direction in {"LR", "RL"}
    rank = _layers(nodes, edges)
    size = {}
    for name, info in nodes.items():
        text_w = 7.2 * len(_clip(info["text"], 28)) + 28
        w, h = (max(72, text_w), 40)
        if info["shape"] == "diamond":
            w, h = w * 1.3, 56
        if info["shape"] == "circle":
            w = h = max(48, text_w * 0.9)
        size[name] = (w, h)
    by_layer: dict[int, list[str]] = {}
    for name in nodes:
        by_layer.setdefault(rank[name], []).append(name)
    gap_cross, gap_flow = 40, 70
    cross_of = lambda n: size[n][1] if horizontal else size[n][0]  # noqa: E731
    flow_of = lambda n: size[n][0] if horizontal else size[n][1]  # noqa: E731
    layer_cross = {k: sum(cross_of(n) for n in v) + gap_cross * (len(v) - 1) for k, v in by_layer.items()}
    widest = max(layer_cross.values())
    flow_pos, cursor = {}, 0.0
    for layer in sorted(by_layer):
        thickness = max(flow_of(n) for n in by_layer[layer])
        flow_pos[layer] = cursor + thickness / 2
        cursor += thickness + gap_flow
    total_flow = cursor - gap_flow
    centers = {}
    for layer, members in by_layer.items():
        start = (widest - layer_cross[layer]) / 2
        for name in members:
            cross = start + cross_of(name) / 2
            start += cross_of(name) + gap_cross
            flow = flow_pos[layer]
            if direction in {"BT", "RL"}:
                flow = total_flow - flow
            centers[name] = (flow, cross) if horizontal else (cross, flow)
    margin = 24
    width = (total_flow if horizontal else widest) + 2 * margin
    height = (widest if horizontal else total_flow) + 2 * margin
    body = ['<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#4b5563"/></marker></defs>']
    for src, dst, label, op in edges:
        (sx, sy), (dx, dy) = centers[src], centers[dst]
        if src == dst:
            continue
        x1, y1 = _boundary(sx, sy, *size[src], dx, dy)
        x2, y2 = _boundary(dx, dy, *size[dst], sx, sy)
        if any(a == dst and b == src for a, b, *_ in edges):
            length = math.hypot(dx - sx, dy - sy) or 1.0
            ox, oy = -(dy - sy) / length * 7, (dx - sx) / length * 7
            x1, y1, x2, y2 = x1 + ox, y1 + oy, x2 + ox, y2 + oy
        dash = ' stroke-dasharray="5 4"' if "." in op else ""
        thick = "3" if "=" in op else "1.6"
        arrow = ' marker-end="url(#arrow)"' if ">" in op else ""
        body.append(f'<line x1="{x1 + margin:.1f}" y1="{y1 + margin:.1f}" x2="{x2 + margin:.1f}" '
                    f'y2="{y2 + margin:.1f}" stroke="#4b5563" stroke-width="{thick}"{dash}{arrow}/>')
        if label:
            mx, my = (x1 + x2) / 2 + margin, (y1 + y2) / 2 + margin
            lw = 6.6 * len(_clip(label, 24)) + 8
            body.append(f'<rect x="{mx - lw / 2:.1f}" y="{my - 9:.1f}" width="{lw:.1f}" height="18" fill="#fff"/>'
                        f'<text x="{mx:.1f}" y="{my + 4:.1f}" text-anchor="middle" {_FONT}>{_esc(_clip(label, 24))}</text>')
    for name, info in nodes.items():
        cx, cy = centers[name][0] + margin, centers[name][1] + margin
        w, h = size[name]
        attrs = 'fill="#eff6ff" stroke="#2563eb" stroke-width="1.5"'
        if info["shape"] == "diamond":
            shape = (f'<polygon points="{cx:.1f},{cy - h / 2:.1f} {cx + w / 2:.1f},{cy:.1f} '
                     f'{cx:.1f},{cy + h / 2:.1f} {cx - w / 2:.1f},{cy:.1f}" {attrs}/>')
        elif info["shape"] == "circle":
            shape = f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{w / 2:.1f}" ry="{h / 2:.1f}" {attrs}/>'
        else:
            radius = 18 if info["shape"] == "round" else 4
            shape = (f'<rect x="{cx - w / 2:.1f}" y="{cy - h / 2:.1f}" width="{w:.1f}" height="{h:.1f}" '
                     f'rx="{radius}" {attrs}/>')
        body.append(f'<g>{shape}<title>{_esc(info["text"])}</title>'
                    f'<text x="{cx:.1f}" y="{cy + 4:.1f}" text-anchor="middle" {_FONT}>{_esc(_clip(info["text"], 28))}</text></g>')
    return _svg(width, height, "".join(body), "Diagram")


# ------------------------------------------------------------------ slides

_DECK_CSS = (
    ":root{color-scheme:light dark}"
    "body{margin:0;font:20px/1.5 system-ui,sans-serif;background:Canvas;color:CanvasText}"
    "section{min-height:100vh;box-sizing:border-box;padding:4rem min(8vw,6rem);display:flex;"
    "flex-direction:column;justify-content:center;border-bottom:1px solid color-mix(in srgb,CanvasText 20%,Canvas)}"
    "html{scroll-snap-type:y proximity}section{scroll-snap-align:start}"
    "h1{font-size:2.4rem}h2{font-size:1.8rem}"
    "pre{overflow:auto;padding:.75rem;background:color-mix(in srgb,CanvasText 8%,Canvas)}"
    "table{border-collapse:collapse}th,td{border:1px solid color-mix(in srgb,CanvasText 25%,Canvas);padding:.3rem .6rem}"
    ".n{position:absolute;right:1rem;margin-top:-3rem;opacity:.5;font-size:.8rem}")


def split_slides(source: str) -> list[str]:
    slides, current, fenced = [], [], False
    for line in source.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and re.fullmatch(r"-{3,}\s*", line):
            slides.append("\n".join(current))
            current = []
        else:
            current.append(line)
    slides.append("\n".join(current))
    return [slide for slide in slides if slide.strip()]
