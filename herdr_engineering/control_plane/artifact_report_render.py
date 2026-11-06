"""Bounded, inert report previews built only from descriptor metadata."""
from __future__ import annotations

import base64
import html
import json
import re
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit

DEFAULT_PREVIEW_BYTES = 256 * 1024
# Figures are embedded as opaque data: URIs on <img> (a browser never runs script or loads anything from an SVG
# shown that way). Only these types qualify, and one URI may not exceed the cap (the sanitiser's own limit is 262144).
IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"})
MAX_FIGURE_DATA_URI = 150_000
FigureImages = Mapping[str, tuple[str, bytes]]


def figure_data_uri(media_type: str, data: bytes) -> str | None:
    """The data: URI for one figure image, or None when its type is not an image type or it is over the cap."""
    if media_type not in IMAGE_MEDIA_TYPES or not isinstance(data, bytes) or not data:
        return None
    if (len(data) + 2) // 3 * 4 + len(f"data:{media_type};base64,") > MAX_FIGURE_DATA_URI:
        return None
    return f"data:{media_type};base64," + base64.b64encode(data).decode("ascii")


def _asset_link(value: str) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("asset URL must be a local protected path")
    parsed = urlsplit(value)
    decoded = unquote(parsed.path)
    if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "\\" in decoded
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
            or decoded.startswith("//") or ".." in PurePosixPath(decoded).parts):
        raise ValueError("asset URL must be a local protected path")
    return html.escape(value, quote=True)


_LIGHT = ("--bg:#f7f7f8;--surface:#fff;--surface-2:#ececf1;--border:#d9d9e3;"
          "--text:#353740;--muted:#666678;--accent:#10a37f;--accent-weak:#d2f0e8;--link:#0a7058;--warn:#9b5d00;"
          "--warn-bg:#fff4e5;--btn:#0b7a60;--on-accent:#fff;--shadow:0 1px 2px rgba(16,24,40,.05);")
# WCAG AA (4.5:1) in light needed three deviations from theme.css: --muted #6e6e80 -> #666678 (4.2 on --surface-2),
# --link #0d8f6f -> #0a7058 (3.4 on --accent-weak), and white-on-#10a37f (3.2) -> a darker --btn fill; --accent stays.
_DARK = ("color-scheme:dark;--bg:#17181c;--surface:#1f2026;--surface-2:#2a2b33;--border:#3a3b45;--text:#e6e6ea;"
         "--muted:#a4a4b3;--accent:#2fc59c;--accent-weak:#153d33;--link:#4ad6b0;--warn:#fbbf24;"
         "--warn-bg:#2b2310;--btn:#2fc59c;--on-accent:#07211a;--shadow:0 1px 3px rgba(0,0,0,.5);")
# Light is the base; dark applies when the OS asks for it unless the host forces light, or whenever the host forces
# dark. The host sets data-theme on :root (standalone page) or on the shadow host (dashboard), never in the markup.
_CORE_CSS = (
    ":root,:host{color-scheme:light dark;" + _LIGHT + "--radius:8px;--sans:\"Inter\",-apple-system,BlinkMacSystemFont,\"Segoe UI\","
    "Roboto,Helvetica,Arial,sans-serif;--mono:\"JetBrains Mono\",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}"
    "@media (prefers-color-scheme:dark){:root:not([data-theme=\"light\"]),:host(:not([data-theme=\"light\"])){"
    + _DARK + "}}"
    ":root[data-theme=\"light\"],:host([data-theme=\"light\"]){color-scheme:light}"
    ":root[data-theme=\"dark\"],:host([data-theme=\"dark\"]){" + _DARK + "}"
    "body{margin:0;background:var(--bg)}"
    ".rp{max-width:860px;margin:0 auto;padding:32px 16px 64px;background:var(--bg);color:var(--text);"
    "font:15px/1.6 var(--sans);overflow-wrap:break-word}a{color:var(--link)}.fig-img{max-width:100%;height:auto}"
)
_FULL_CSS = _CORE_CSS + (
    ".rp *{box-sizing:border-box}.rp section{margin:40px 0 0;min-width:0}"
    ".hero{padding-bottom:20px;border-bottom:1px solid var(--border)}"
    "h1{font:700 30px/1.2 var(--sans);letter-spacing:-.02em;margin:0 0 12px;text-wrap:balance}"
    "h2{font:600 20px/1.3 var(--sans);margin:0 0 16px;text-wrap:balance}"
    "h3{font:600 16px/1.4 var(--sans);margin:0 0 8px}"
    ".lede{font-size:17px;color:var(--muted);margin:0;max-width:68ch}"
    "p{margin:0 0 12px;max-width:68ch}"
    ".toc{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0 0;font-size:13px}"
    ".toc a{padding:3px 10px;border:1px solid var(--border);border-radius:999px;background:var(--surface);"
    "text-decoration:none;color:var(--muted)}.toc a:hover{border-color:var(--accent);color:var(--link)}"
    ".card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px;"
    "margin:0 0 14px;box-shadow:var(--shadow)}.finding{border-left:4px solid var(--accent)}"
    ".chips{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 0;align-items:center}"
    ".chips b{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}"
    ".chip{font:500 12px/1.5 var(--mono);padding:1px 8px;border:1px solid var(--border);border-radius:999px;"
    "background:var(--surface-2);color:var(--text);text-decoration:none}.chip:hover{border-color:var(--accent)}"
    ".sub{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:14px 0 4px}"
    "ul,ol{margin:0 0 12px;padding-left:20px;max-width:68ch}li{margin-bottom:6px}"
    ".qlist{list-style:none;padding:0}.qlist li{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:4px 12px;align-items:baseline}"
    ".qlist .chip,.chips .chip{white-space:nowrap;overflow-wrap:normal;justify-self:start}"
    ".tablewrap{overflow-x:auto;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface)}"
    "table{border-collapse:collapse;width:100%;min-width:560px;font-size:13.5px}"
    "th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--border);vertical-align:top}"
    "th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);background:var(--surface-2)}"
    "tr:last-child td{border-bottom:0}td.num{white-space:nowrap}td:first-child{min-width:12rem}td .chip{white-space:nowrap}"
    "code,.mono{font:12.5px var(--mono);background:var(--surface-2);padding:1px 5px;border-radius:4px}"
    ".callout{background:var(--accent-weak);border-radius:var(--radius);padding:10px 14px;margin:10px 0;font-size:14px}"
    ".callout b{display:block;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--link)}"
    ".fig-img{display:block;margin:4px 0 12px;padding:8px;background:#fff;border:1px solid var(--border);"
    "border-radius:var(--radius)}"
    ".btn{display:inline-block;padding:5px 14px;border-radius:4px;background:var(--btn);color:var(--on-accent);"
    "font-weight:600;font-size:13px;text-decoration:none}.btn:hover{filter:brightness(1.08)}"
    "details{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 16px}"
    "summary{cursor:pointer;font-weight:600}dl{display:grid;grid-template-columns:minmax(0,max-content) 1fr;"
    "gap:6px 16px;margin:12px 0 0;font-size:13.5px}dt{font-family:var(--mono);color:var(--muted)}dd{margin:0}"
    ".notice{margin-top:40px;padding:10px 14px;border:1px dashed var(--warn);border-radius:var(--radius);"
    "background:var(--warn-bg);font-size:13px;color:var(--text)}"
    "@media (max-width:640px){h1{font-size:25px}.rp{padding-top:20px}}"
    "@media print{body,.rp{background:#fff;color:#000}.rp{max-width:none;padding:0}.toc{display:none}"
    ".card,.tablewrap{break-inside:avoid;box-shadow:none}a{color:#000}.btn{border:1px solid #000;background:none;color:#000}}"
)
_FULL_CSS_MARGIN = 1200  # a cap below css + this much falls back to the core styles
_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'")
_ANALYSIS_PAIR_LIMIT = 40


def _size(value: int) -> str:
    size = float(value)
    for unit in ("bytes", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _flatten(value, prefix: str = ""):
    """Yield (dotted key, scalar text) pairs; lists and scalars are rendered compactly."""
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _flatten(value[key], f"{prefix}.{key}" if prefix else str(key))
    else:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        yield prefix, text


def render_report_preview(descriptor: dict, *, asset_url: Callable[[dict], str] | None = None,
                          max_bytes: int = DEFAULT_PREVIEW_BYTES,
                          figure_images: FigureImages | None = None) -> str:
    """Render a styled preview without opening evidence blobs or executing report source.

    Every section has a bounded share of the byte budget so an oversized evidence
    inventory cannot suppress all findings. Whole entries are omitted rather than
    cutting HTML; one small note with counts and the full-descriptor link discloses
    omissions or shortened text, and appears only when something was left out.

    ``figure_images`` maps an asset id to ``(media_type, bytes)``; a figure whose asset is present, is an image
    type and fits the per-figure cap is shown inline. Images are the first thing given up when the budget is
    tight: they only use what the text entries left over, and a figure without one keeps its caption and link.
    """
    from .artifact_report_descriptor import validate_report_descriptor

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 4096:
        raise ValueError("max_bytes must be an integer of at least 4096")
    report = validate_report_descriptor(descriptor)
    make_url = asset_url or (lambda asset: "assets/" + quote(asset["id"], safe=""))
    links = {asset["id"]: _asset_link(make_url(asset)) for asset in report["assets"]}
    shortened = False

    def text(value: str, limit: int = 2048) -> str:
        nonlocal shortened
        if len(value) > limit:
            shortened = True
            value = value[:limit] + " … [text shortened; see full descriptor]"
        return html.escape(value, quote=True)

    def tracked(build: Callable, *args) -> tuple[str, bool]:
        """Build one entry and report whether its own text was cut (so dropped entries never count)."""
        nonlocal shortened
        shortened = False
        built = build(*args)
        cut, shortened = shortened, False
        return built, cut

    def esc_id(identifier: str) -> str:
        return html.escape(identifier, quote=True)

    def chips(label: str, kind: str, identifiers: list[str]) -> str:
        if not identifiers:
            return ""
        return (f'<p class="chips"><b>{label}</b>'
                + "".join(f'<a class="chip" href="#{kind}-{quote(identifier, safe="")}">'
                          f'{text(identifier, 128)}</a>' for identifier in identifiers) + "</p>")

    def bullets(values: list[str]) -> str:
        return "<ul>" + "".join(f"<li>{text(value)}</li>" for value in values) + "</ul>" if values else ""

    def finding(row: dict) -> str:
        return (f'<article class="card finding" id="finding-{esc_id(row["id"])}"><h3>{text(row["title"], 256)}</h3>'
                f'<p>{text(row["statement"])}</p>'
                + chips("Question", "question", row["question_ids"])
                + chips("Evidence", "evidence", row["evidence_ids"])
                + chips("Figure", "figure", row.get("figure_ids", []))
                + ('<p class="sub">Limits</p>' + bullets(row["limitations"]) if row.get("limitations") else "")
                + ('<p class="sub">Next checks</p>' + bullets(row["next_steps"]) if row.get("next_steps") else "")
                + "</article>")

    supplied = figure_images or {}

    def figure_uri(row: dict) -> str | None:
        found = supplied.get(row["asset_id"])
        return figure_data_uri(*found) if found else None

    def figure(row: dict, uri: str | None = None) -> str:
        image = (f'<img class="fig-img" alt="{html.escape(row["title"][:256], quote=True)}" src="{uri}">' if uri else "")
        return (f'<article class="card figure" id="figure-{esc_id(row["id"])}"><h3>{text(row["title"], 256)}</h3>'
                + image + f'<p>{text(row["caption"])}</p>'
                + (f'<div class="callout"><b>How to read</b>{text(row["reading_guide"])}</div>'
                   if row.get("reading_guide") else "")
                + f'<a class="btn" href="{links[row["asset_id"]]}" download>Download figure</a></article>')

    def evidence_row(row: dict) -> str:
        files = " ".join(f'<a class="chip" href="{links[asset_id]}">{text(asset_id, 128)}</a>'
                         for asset_id in row.get("asset_ids", [])) or "&mdash;"
        return (f'<tr id="evidence-{esc_id(row["id"])}"><td><strong>{text(row["title"], 256)}</strong>'
                f'<br><span class="mono">{text(row["id"], 128)}</span></td>'
                f'<td>{text(row["description"])}</td><td>{files}</td></tr>')

    def asset_row(row: dict) -> str:
        return (f'<tr><td><a href="{links[row["id"]]}">{text(row["name"], 256)}</a></td>'
                f'<td>{text(row["role"])}</td><td>{text(row["media_type"])}</td>'
                f'<td class="num" title="{int(row["size_bytes"])} bytes">{_size(int(row["size_bytes"]))}'
                f' ({int(row["size_bytes"])} bytes)</td>'
                f'<td><code>{text(row["sha256"][:12], 12)}</code></td></tr>')

    pairs = []
    analysis = report.get("analysis") or {}
    for key in ("method", "code_revision"):
        if analysis.get(key):
            pairs.append((key, analysis[key]))
    for key in ("configuration", "source_hashes"):
        pairs.extend(_flatten(analysis.get(key, {}), key))
    def pair(key: str, value: str) -> str:
        return f'<dt>{text(key, 256)}</dt><dd>{text(value, 512)}</dd>'

    def question(row: dict) -> str:
        return (f'<li id="question-{esc_id(row["id"])}"><span class="chip">{text(row["id"], 128)}</span>'
                f'<span>{text(row["text"])}</span></li>')

    def list_item(value: str) -> str:
        return f"<li>{text(value)}</li>"

    pair_items = [tracked(pair, key, value) for key, value in pairs]

    # (anchor, title, label, items, weight, kind, item limit)
    sections = [
        ("questions", "Questions", "questions", [tracked(question, row) for row in report["questions"]], 1,
         "qlist", None),
        ("findings", "Findings", "findings", [tracked(finding, row) for row in report["findings"]], 4, "div", None),
        ("figures", "Figures", "figures", [tracked(figure, row) for row in report["figures"]], 2, "div", None),
        ("evidence", "Evidence", "evidence entries", [tracked(evidence_row, row) for row in report["evidence"]], 2,
         "table:Evidence|Description|Files", None),
        ("downloads", "Separate downloads", "downloads", [tracked(asset_row, row) for row in report["assets"]], 2,
         "table:Name|Role|Type|Size|SHA-256", None),
        ("analysis", "Analysis", "analysis entries", pair_items, 1, "dl", _ANALYSIS_PAIR_LIMIT),
        ("limitations", "Limitations", "limitations",
         [tracked(list_item, value) for value in report.get("limitations", [])], 1, "ul", None),
        ("next-checks", "Next checks", "next checks",
         [tracked(list_item, value) for value in report.get("next_steps", [])], 1, "ul", None),
    ]

    def wrap(anchor: str, title: str, kind: str, body: str) -> str:
        if kind == "qlist":
            inner = f'<ul class="qlist">{body}</ul>'
        elif kind == "ul":
            inner = f"<ul>{body}</ul>"
        elif kind == "dl":
            inner = f"<details><summary>Method and configuration</summary><dl>{body}</dl></details>"
        elif kind.startswith("table:"):
            heads = "".join(f"<th>{name}</th>" for name in kind[6:].split("|"))
            inner = (f'<div class="tablewrap"><table><thead><tr>{heads}</tr></thead>'
                     f"<tbody>{body}</tbody></table></div>")
        else:
            inner = body
        return f'<section id="sec-{anchor}"><h2>{title}</h2>{inner}</section>'

    def toc(anchors: list[tuple[str, str]]) -> str:
        return ('<nav class="toc" aria-label="Contents">'
                + "".join(f'<a href="#sec-{anchor}">{title}</a>' for anchor, title in anchors) + "</nav>")

    def note(counts: list[tuple[str, int, int]]) -> str:
        shown = ", ".join(f"{kept} of {total} {label}" for label, kept, total in counts if kept < total)
        parts = ["Some content was shortened or omitted; see the <a href=\"report\">full descriptor</a>."]
        if shown:
            parts.append(f"Shown: {shown}.")
        return f'<aside class="notice" role="note">{" ".join(parts)}</aside>'

    present_sections = [(a, t, label, items) for a, t, label, items, *_ in sections if items]
    title_source = report.get("title", "Report")
    title = ""
    title_shortened = False
    summary_html = f'<p class="lede">{text(report["summary"])}</p>' if report.get("summary") else ""
    summary_shortened, shortened = shortened, False

    def document(css: str, hero_summary: str, nav: str, body: str, footer: str) -> str:
        return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
                f'<title>{title}</title><style>{css}</style></head><body><main class="rp">'
                f'<header class="hero"><h1>{title}</h1>{hero_summary}{nav}</header>{body}{footer}'
                "</main></body></html>")

    # Reserve the largest possible contents list and omission note (every count is no wider than its
    # total). The title appears twice, so it is cut until the envelope fits even the smallest cap.
    worst_note = note([(label, 0, len(items)) for _, _, label, items in present_sections])
    nav_html = toc([(a, t) for a, t, *_ in present_sections])
    wrappers = sum(len(wrap(a, t, k, "").encode()) for a, t, _, _, _, k, _ in sections)
    for title_limit in (256, 128, 64, 32, 16, 8, 0):
        title_shortened = len(title_source) > title_limit
        title = html.escape(title_source[:title_limit] + ("\u2026" if title_shortened else ""), quote=True)
        css = _FULL_CSS if len(document(_FULL_CSS, "", "", "", "").encode()) + _FULL_CSS_MARGIN <= max_bytes \
            else _CORE_CSS
        fixed = len(document(css, "", nav_html, "", worst_note).encode())
        remaining = max_bytes - fixed - wrappers
        if remaining >= 0:
            break
    else:
        raise ValueError("max_bytes is too small for the preview envelope")
    summary_omitted = bool(summary_html) and len(summary_html.encode()) > remaining // 4
    if summary_omitted:
        summary_html = ""
    else:
        remaining -= len(summary_html.encode())
    total_weight = sum(weight for _, _, _, items, weight, _, _ in sections if items)
    chosen, kept_shortened = [], False
    for anchor, title_text, label, items, weight, kind, limit in sections:
        if not items:
            continue
        quota = remaining * weight // total_weight
        selected, used = [], 0
        for position, (item, cut) in enumerate(items):
            size = len(item.encode())
            if used + size <= quota and (limit is None or len(selected) < limit):
                selected.append(position)
                used += size
                kept_shortened = kept_shortened or cut
        chosen.append([anchor, title_text, label, items, kind, selected, used])
    # Images come last: they only spend what every text entry left over, in report order.
    spare = remaining - sum(entry[6] for entry in chosen)
    upgraded: dict[int, str] = {}
    image_typed = {asset["id"]: asset["media_type"] in IMAGE_MEDIA_TYPES for asset in report["assets"]}
    image_dropped = False
    for entry in chosen:
        if entry[0] != "figures":
            continue
        for position in entry[5]:
            uri = figure_uri(report["figures"][position])
            if uri is None:
                # a caller that tried to supply images and has none for an image-type figure: it is not shown
                image_dropped = image_dropped or (figure_images is not None and image_typed.get(
                    report["figures"][position]["asset_id"], False))
                continue
            richer = figure(report["figures"][position], uri)
            extra = len(richer.encode()) - len(entry[3][position][0].encode())
            if extra <= spare:
                spare -= extra
                upgraded[position] = richer
            else:
                image_dropped = True
    bodies, counts, kept_anchors = [], [], []
    for anchor, title_text, label, items, kind, selected, _ in chosen:
        pieces = [upgraded.get(position, items[position][0]) if anchor == "figures" else items[position][0]
                  for position in selected]
        bodies.append(wrap(anchor, title_text, kind, "".join(pieces)))
        counts.append((label, len(selected), len(items)))
        kept_anchors.append((anchor, title_text))
    left_out = (summary_omitted or summary_shortened or title_shortened or kept_shortened or image_dropped
                or any(kept < total for _, kept, total in counts))
    footer = note(counts) if left_out else ""
    result = document(css, summary_html, toc(kept_anchors), "".join(bodies), footer)
    present = set(re.findall(r' id="([^"]+)"', result))
    result = re.sub(r'href="#([^"]+)"',
                    lambda match: match.group() if unquote(match.group(1)) in present else 'href="report"',
                    result)
    if len(result.encode()) > max_bytes:
        raise ValueError("max_bytes exceeded while building preview")
    return result
