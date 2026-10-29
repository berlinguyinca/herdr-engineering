"""Dependency-free SVG/HTML renderers: charts, Mermaid diagrams and slide decks.

Output is inert: all text is escaped, nothing is fetched and no script is emitted,
so results can be served through the same protected preview as other artifacts.
Every SVG carries an embedded stylesheet (CSS variables + a dark-scheme media query)
so it follows the viewer's colour scheme both standalone and inlined into HTML.
"""
from __future__ import annotations

import html
import math
import re

MAX_NODES = 60
MAX_EDGES = 120
MAX_SERIES = 8
MAX_PARTICIPANTS = 20
MAX_MESSAGES = 200
PALETTE = ("#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#65a30d")
_DARK_PALETTE = ("#60a5fa", "#f87171", "#34d399", "#fbbf24", "#a78bfa", "#22d3ee", "#f472b6", "#a3e635")
# Text colour comes from the embedded stylesheet (``.hz-svg text``), not from attributes.
_FONT = 'font-family="system-ui,sans-serif" font-size="12"'


def _style() -> str:
    """Embedded stylesheet: CSS variables with a dark-scheme override, scoped to ``.hz-svg``."""
    series = "".join(f"--c{i}:{c};" for i, c in enumerate(PALETTE))
    dark_series = "".join(f"--c{i}:{c};" for i, c in enumerate(_DARK_PALETTE))
    rules = "".join(f".hz-svg .f{i}{{fill:var(--c{i})}}.hz-svg .k{i}{{stroke:var(--c{i})}}"
                    for i in range(len(PALETTE)))
    return ("<style>"
            ".hz-svg{--bg:#ffffff;--fg:#1f2937;--axis:#9ca3af;--grid:#e5e7eb;--edge:#4b5563;"
            "--nfill:#eff6ff;--nstroke:#2563eb;--lbg:#ffffff;--note:#fef9c3;--nnote:#ca8a04;"
            + series + "background:var(--bg)}"
            "@media (prefers-color-scheme: dark){.hz-svg{--bg:#111827;--fg:#e5e7eb;--axis:#6b7280;"
            "--grid:#374151;--edge:#9ca3af;--nfill:#1e3a5f;--nstroke:#60a5fa;--lbg:#111827;"
            "--note:#422006;--nnote:#d97706;" + dark_series + "}}"
            ".hz-svg text{fill:var(--fg)}.hz-svg .grid{stroke:var(--grid)}.hz-svg .axis{stroke:var(--axis)}"
            ".hz-svg .edge{stroke:var(--edge)}.hz-svg .edgefill{fill:var(--edge)}"
            ".hz-svg .node{fill:var(--nfill);stroke:var(--nstroke)}.hz-svg .lbg{fill:var(--lbg)}"
            ".hz-svg .note{fill:var(--note);stroke:var(--nnote)}"
            + rules + "</style>")


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _svg(width: float, height: float, body: str, title: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" class="hz-svg" viewBox="0 0 {width:g} {height:g}" '
            f'width="{width:g}" height="{height:g}" role="img" aria-label="{_esc(title)}">'
            f'<title>{_esc(title)}</title><defs>{_style()}</defs>{body}</svg>')


# ---------------------------------------------------------------- charts

def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _rows_to_points(rows, spec: dict) -> list[tuple[str, float]]:
    if not isinstance(rows, list) or not rows or len(rows) > 500:
        raise ValueError("chart data must be a non-empty list of at most 500 rows")
    points = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("chart rows must be objects")
        xkey = spec.get("x") or next((k for k, v in row.items() if not _is_number(v)), None)
        ykey = spec.get("y") or next((k for k, v in row.items() if _is_number(v)), None)
        if xkey is None or ykey is None or not _is_number(row.get(ykey)):
            raise ValueError("each row needs a label and a numeric value")
        points.append((str(row[xkey]), float(row[ykey])))
    return points


def _chart_series(spec: dict) -> tuple[list[str], list[tuple[str, list[float | None]]]]:
    """Return (labels, [(series name, values aligned to labels; None where missing)])."""
    raw = spec.get("series")
    if raw is None:
        points = _rows_to_points(spec.get("data"), spec)
        return [label for label, _ in points], [("", [value for _, value in points])]
    if not isinstance(raw, list) or not raw:
        raise ValueError("series must be a non-empty list")
    if len(raw) > MAX_SERIES:
        raise ValueError(f"at most {MAX_SERIES} series are supported")
    labels: list[str] = []
    parsed = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("each series must be an object")
        values: dict[str, float] = {}
        for label, value in _rows_to_points(item.get("data"), spec):
            if label not in labels:
                labels.append(label)
            values[label] = value  # a repeated label within one series: last row wins
        parsed.append((str(item.get("name") or f"Series {index + 1}"), values))
    if len(labels) > 500:
        raise ValueError("chart has too many distinct labels")
    return labels, [(name, [values.get(label) for label in labels]) for name, values in parsed]


def _nice_step(span: float) -> float:
    raw = span / 4 or 1
    magnitude = 10 ** math.floor(math.log10(raw))
    return next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw)


def render_chart_svg(spec: dict) -> str:
    kind = spec.get("type")
    if kind not in {"bar", "line"}:
        raise ValueError("chart type must be 'bar' or 'line'")
    labels, series = _chart_series(spec)
    count = len(series)
    multi = count > 1 or bool(series[0][0])
    title = str(spec.get("title") or "Chart")
    left, right, bottom = 56, 16, 56
    per_label = 48 if kind == "line" else max(48, 18 * count + 16)
    width = max(480, min(1200, left + right + per_label * len(labels)))
    # Legend rows wrap inside the plot width, below the title.
    legend, lx, ly = [], left, 40
    if multi:
        for i, (name, _) in enumerate(series):
            text = _clip(name, 24)
            item_w = 7 * len(text) + 32
            if lx + item_w > width - right and lx > left:
                lx, ly = left, ly + 18
            colour = PALETTE[i % len(PALETTE)]
            legend.append(f'<rect x="{lx}" y="{ly - 9}" width="12" height="12" rx="2" '
                          f'class="f{i % len(PALETTE)}" fill="{colour}"/>'
                          f'<text x="{lx + 18}" y="{ly + 1}" {_FONT}>{_esc(text)}</text>')
            lx += item_w
    top = ly + 16 if multi else 40
    height = top + 264 + bottom
    flat = [v for _, vals in series for v in vals if v is not None]
    lo, hi = min(0.0, min(flat)), max(0.0, max(flat))
    step = _nice_step(hi - lo)
    lo = math.floor(lo / step) * step
    hi = math.ceil(hi / step) * step or step
    plot_w, plot_h = width - left - right, height - top - bottom

    def y_of(value):
        return top + plot_h * (hi - value) / (hi - lo)

    parts = [f'<text x="{left}" y="24" font-family="system-ui,sans-serif" font-size="15" '
             f'font-weight="600">{_esc(title)}</text>', *legend]
    tick = lo
    while tick <= hi + step / 2:
        y = y_of(tick)
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                     f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" {_FONT}>{tick:g}</text>')
        tick += step
    slot = plot_w / len(labels)
    group_w = slot * 0.7
    bar_w = group_w / count
    for index, label in enumerate(labels):
        cx = left + slot * (index + 0.5)
        parts.append(f'<text x="{cx:.1f}" y="{height - bottom + 16}" text-anchor="middle" {_FONT}>{_esc(_clip(label, 12))}</text>')
        if kind != "bar":
            continue
        for s, (name, values) in enumerate(series):
            value = values[index]
            if value is None:
                continue
            ci = s % len(PALETTE)
            x = cx - group_w / 2 + bar_w * s
            top_y, base_y = sorted((y_of(value), y_of(0)))
            tip = f"{name}: " if multi else ""
            parts.append(f'<rect x="{x:.1f}" y="{top_y:.1f}" width="{bar_w * 0.92:.1f}" '
                         f'height="{max(base_y - top_y, 1):.1f}" class="f{ci}" fill="{PALETTE[ci]}">'
                         f'<title>{_esc(tip + label)}: {value:g}</title></rect>')
            if not multi or bar_w >= 30:
                parts.append(f'<text x="{x + bar_w * 0.46:.1f}" y="{y_of(value) + (14 if value < 0 else -6):.1f}" '
                             f'text-anchor="middle" font-family="system-ui,sans-serif" '
                             f'font-size="{10 if multi else 12}">{value:g}</text>')
    if kind == "line":
        for s, (name, values) in enumerate(series):
            ci = s % len(PALETTE)
            runs: list[list[str]] = []
            run: list[str] = []
            dots = []
            for i, v in enumerate(values):  # a missing value breaks the line; it is never plotted as zero
                if v is None:
                    if run:
                        runs.append(run)
                    run = []
                    continue
                x, y = left + slot * (i + 0.5), y_of(v)
                run.append(f"{x:.1f},{y:.1f}")
                dots.append((x, y, labels[i], v))
            if run:
                runs.append(run)
            for pts in runs:
                if len(pts) > 1:
                    parts.append(f'<polyline points="{" ".join(pts)}" fill="none" class="k{ci}" '
                                 f'stroke="{PALETTE[ci]}" stroke-width="2"/>')
            for x, y, label, v in dots:
                tip = f"{name}: " if multi else ""
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" class="f{ci}" fill="{PALETTE[ci]}">'
                             f'<title>{_esc(tip + label)}: {v:g}</title></circle>')
                if not multi:
                    parts.append(f'<text x="{x:.1f}" y="{y + (14 if v < 0 else -8):.1f}" '
                                 f'text-anchor="middle" {_FONT}>{v:g}</text>')
    parts.append(f'<line x1="{left}" x2="{left}" y1="{top}" y2="{top + plot_h}" class="axis"/>')
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
    body = ['<defs><marker id="hz-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="edgefill"/></marker></defs>']
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
        arrow = ' marker-end="url(#hz-arrow)"' if ">" in op else ""
        body.append(f'<line x1="{x1 + margin:.1f}" y1="{y1 + margin:.1f}" x2="{x2 + margin:.1f}" '
                    f'y2="{y2 + margin:.1f}" class="edge" stroke-width="{thick}"{dash}{arrow}/>')
        if label:
            mx, my = (x1 + x2) / 2 + margin, (y1 + y2) / 2 + margin
            lw = 6.6 * len(_clip(label, 24)) + 8
            body.append(f'<rect x="{mx - lw / 2:.1f}" y="{my - 9:.1f}" width="{lw:.1f}" height="18" class="lbg"/>'
                        f'<text x="{mx:.1f}" y="{my + 4:.1f}" text-anchor="middle" {_FONT}>{_esc(_clip(label, 24))}</text>')
    for name, info in nodes.items():
        cx, cy = centers[name][0] + margin, centers[name][1] + margin
        w, h = size[name]
        attrs = 'class="node" stroke-width="1.5"'
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


# ------------------------------------------------------- sequence diagrams

_SEQ_HEADER = re.compile(r"^sequenceDiagram\s*$", re.I)
_SEQ_PARTICIPANT = re.compile(r"^(participant|actor)\s+([A-Za-z0-9_]+)(?:\s+as\s+(.+))?$")
_SEQ_MESSAGE = re.compile(r"^([A-Za-z0-9_]+)\s*(-->>|->>|-->|->|--x|-x)\s*[+-]?\s*([A-Za-z0-9_]+)\s*:\s*(.*)$")
_SEQ_NOTE = re.compile(r"^note\s+(over|left of|right of)\s+([A-Za-z0-9_]+)(?:\s*,\s*([A-Za-z0-9_]+))?\s*:\s*(.*)$", re.I)
_SEQ_IGNORED = re.compile(r"^(?:activate|deactivate)\s+[A-Za-z0-9_]+$|^autonumber(?:\s.*)?$", re.I)


def _significant_lines(source: str) -> list[str]:
    return [line.strip() for line in source.splitlines()
            if line.strip() and not line.strip().startswith("%%")]


def mermaid_kind(source: str) -> str:
    """Classify Mermaid source by its first non-comment line: 'sequence', 'flowchart' or 'other'."""
    lines = _significant_lines(source)
    if not lines:
        return "other"
    first = lines[0].split(";")[0].strip()
    if _SEQ_HEADER.match(first):
        return "sequence"
    if _HEADER.match(first):
        return "flowchart"
    return "other"


def parse_sequence(source: str) -> dict:
    """Parse the supported sequenceDiagram subset; raise ValueError naming anything else."""
    lines = _significant_lines(source)
    if not lines or not _SEQ_HEADER.match(lines[0]):
        raise ValueError("not a sequenceDiagram")
    participants: dict[str, dict] = {}
    events: list[dict] = []

    def ensure(ident: str) -> None:
        if ident not in participants:
            participants[ident] = {"label": ident, "actor": False}
            if len(participants) > MAX_PARTICIPANTS:
                raise ValueError(f"too many participants (more than {MAX_PARTICIPANTS}) to draw")

    for line in lines[1:]:
        if _SEQ_IGNORED.match(line):
            continue
        declared = _SEQ_PARTICIPANT.match(line)
        if declared:
            ident = declared.group(2)
            ensure(ident)
            participants[ident]["actor"] = declared.group(1) == "actor"
            if declared.group(3):
                participants[ident]["label"] = declared.group(3).strip()
            continue
        message = _SEQ_MESSAGE.match(line)
        if message:
            src, op, dst, text = message.groups()
            ensure(src)
            ensure(dst)
            events.append({"kind": "message", "src": src, "dst": dst, "op": op, "text": text.strip()})
            if sum(e["kind"] == "message" for e in events) > MAX_MESSAGES:
                raise ValueError(f"too many messages (more than {MAX_MESSAGES}) to draw")
            continue
        note = _SEQ_NOTE.match(line)
        if note:
            where, first, second, text = note.groups()
            ensure(first)
            if second:
                ensure(second)
            events.append({"kind": "note", "where": where.lower(), "a": first, "b": second, "text": text.strip()})
            continue
        word = line.split()[0]
        raise ValueError(f"unsupported sequenceDiagram construct: {word}")
    if not participants or not events:
        raise ValueError("sequenceDiagram has no participants or messages to draw")
    if len(events) > MAX_MESSAGES + 100:
        raise ValueError("diagram is too large to draw")
    return {"participants": participants, "events": events}


def render_sequence_svg(diagram: dict) -> str:
    participants, events = diagram["participants"], diagram["events"]
    names = list(participants)
    index = {n: i for i, n in enumerate(names)}
    box_w = {n: max(90.0, 7.2 * len(_clip(participants[n]["label"], 24)) + 24) for n in names}
    # Gaps between neighbouring lifelines grow until every message/note text fits its span.
    gap = [max(110.0, (box_w[names[i]] + box_w[names[i + 1]]) / 2 + 24) for i in range(len(names) - 1)]
    needs = []
    for event in events:
        text_w = 7.0 * len(_clip(event["text"], 48)) + 24
        if event["kind"] == "message":
            a, b = sorted((index[event["src"]], index[event["dst"]]))
        elif event["b"]:
            a, b = sorted((index[event["a"]], index[event["b"]]))
        else:
            continue
        if b > a:
            needs.append((a, b, text_w))
    for a, b, text_w in sorted(needs, key=lambda item: item[1] - item[0]):
        short = text_w - sum(gap[a:b])
        if short > 0:
            for k in range(a, b):
                gap[k] += short / (b - a)
    xs = [0.0]
    for g in gap:
        xs.append(xs[-1] + g)
    # Horizontal extents (self-messages and notes can stick out past the outer lifelines).
    lo, hi = min(x - box_w[n] / 2 for x, n in zip(xs, names)), max(x + box_w[n] / 2 for x, n in zip(xs, names))
    rows = []  # (event, height)
    for event in events:
        text_w = 7.0 * len(_clip(event["text"], 48)) + 24
        if event["kind"] == "message":
            self_msg = event["src"] == event["dst"]
            if self_msg:
                hi = max(hi, xs[index[event["src"]]] + 34 + text_w)
            rows.append((event, 52 if self_msg else 38))
        else:
            x_a = xs[index[event["a"]]]
            if event["where"] == "left of":
                x0, x1 = x_a - 12 - max(text_w, 80), x_a - 12
            elif event["where"] == "right of":
                x0, x1 = x_a + 12, x_a + 12 + max(text_w, 80)
            elif event["b"]:
                x_b = xs[index[event["b"]]]
                x0, x1 = min(x_a, x_b) - 16, max(x_a, x_b) + 16
            else:
                x0, x1 = x_a - max(text_w, 80) / 2, x_a + max(text_w, 80) / 2
            event["_box"] = (x0, x1)
            lo, hi = min(lo, x0), max(hi, x1)
            rows.append((event, 44))
    margin = 24
    shift = margin - lo
    head_h, top_y = 36, 16
    y = top_y + head_h + 14
    body, drawn = [], []
    for event, height in rows:
        if event["kind"] == "message":
            sx, dx = xs[index[event["src"]]] + shift, xs[index[event["dst"]]] + shift
            dashed = event["op"].startswith("--")
            cross = event["op"].endswith("x")
            head = event["op"].endswith(">>") and not cross
            cls = ' stroke-dasharray="6 4"' if dashed else ""
            marker = ' marker-end="url(#hz-arrow)"' if head else ""
            ay = y + height - 14
            if sx == dx:
                ay = y + 14
                body.append(f'<path d="M{sx:.1f},{ay:.1f} h30 v20 h-30" fill="none" class="edge" '
                            f'stroke-width="1.6"{cls}{marker}/>')
                tx, anchor, ty = sx + 36, "start", ay + 3
                end_x, end_y = sx, ay + 20
            else:
                body.append(f'<line x1="{sx:.1f}" y1="{ay:.1f}" x2="{dx:.1f}" y2="{ay:.1f}" class="edge" '
                            f'stroke-width="1.6"{cls}{marker}/>')
                tx, anchor, ty = (sx + dx) / 2, "middle", ay - 6
                end_x, end_y = dx, ay
            if cross:
                body.append(f'<path d="M{end_x - 5:.1f},{end_y - 5:.1f} l10,10 m-10,0 l10,-10" class="edge" '
                            f'stroke-width="1.8" fill="none"/>')
            if event["text"]:
                body.append(f'<text x="{tx:.1f}" y="{ty:.1f}" text-anchor="{anchor}" {_FONT}>'
                            f'{_esc(_clip(event["text"], 48))}</text>')
        else:
            x0, x1 = event["_box"]
            x0, x1 = x0 + shift, x1 + shift
            body.append(f'<rect x="{x0:.1f}" y="{y + 4:.1f}" width="{x1 - x0:.1f}" height="{height - 12}" '
                        f'class="note" stroke-width="1.2"/>'
                        f'<text x="{(x0 + x1) / 2:.1f}" y="{y + height / 2 + 3:.1f}" text-anchor="middle" {_FONT}>'
                        f'{_esc(_clip(event["text"], 48))}</text>')
        drawn.append(y)
        y += height
    bottom_y = y + 6
    lifelines, boxes = [], []
    for name in names:
        cx = xs[index[name]] + shift
        info = participants[name]
        w = box_w[name]
        radius = 18 if info["actor"] else 4
        for by in (top_y, bottom_y):
            boxes.append(f'<g><rect x="{cx - w / 2:.1f}" y="{by}" width="{w:.1f}" height="{head_h}" rx="{radius}" '
                         f'class="node" stroke-width="1.5"/><title>{_esc(info["label"])}</title>'
                         f'<text x="{cx:.1f}" y="{by + head_h / 2 + 4}" text-anchor="middle" {_FONT}>'
                         f'{_esc(_clip(info["label"], 24))}</text></g>')
        lifelines.append(f'<line x1="{cx:.1f}" y1="{top_y + head_h}" x2="{cx:.1f}" y2="{bottom_y}" '
                         f'class="axis" stroke-dasharray="3 4"/>')
    width = hi - lo + 2 * margin
    height = bottom_y + head_h + 16
    marker = ('<defs><marker id="hz-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
              'orient="auto"><path d="M0,0 L10,5 L0,10 z" class="edgefill"/></marker></defs>')
    return _svg(width, height, marker + "".join(lifelines) + "".join(body) + "".join(boxes), "Sequence diagram")


def render_mermaid(source: str) -> tuple[str, str]:
    """Render supported Mermaid source to SVG, returning (svg, check name); else raise ValueError."""
    kind = mermaid_kind(source)
    if kind == "sequence":
        return render_sequence_svg(parse_sequence(source)), "sequence-svg"
    if kind == "flowchart":
        return render_flowchart_svg(parse_flowchart(source)), "flowchart-svg"
    raise ValueError("only graph/flowchart and sequenceDiagram diagrams are rendered")


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
