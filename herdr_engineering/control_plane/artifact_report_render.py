"""Bounded, inert report previews built only from descriptor metadata."""
from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit

DEFAULT_PREVIEW_BYTES = 256 * 1024


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


def render_report_preview(descriptor: dict, *, asset_url: Callable[[dict], str] | None = None,
                          max_bytes: int = DEFAULT_PREVIEW_BYTES) -> str:
    """Render a preview without opening evidence blobs or executing report source.

    Every section has a bounded share of the byte budget so an oversized evidence
    inventory cannot suppress all findings. Whole entries are omitted rather than
    cutting HTML, and counts plus the full-descriptor link disclose omissions.
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

    def references(kind: str, identifiers: list[str]) -> str:
        return ", ".join(f'<a href="#{kind}-{quote(identifier, safe="")}">{text(identifier, 128)}</a>'
                         for identifier in identifiers)

    def bullets(values: list[str]) -> str:
        return "<ul>" + "".join(f"<li>{text(value)}</li>" for value in values) + "</ul>" if values else ""

    questions = [f'<article id="question-{html.escape(row["id"], quote=True)}"><strong>{text(row["id"], 128)}</strong>'
                 f'<p>{text(row["text"])}</p></article>' for row in report["questions"]]
    findings = []
    for row in report["findings"]:
        findings.append(
            f'<article id="finding-{html.escape(row["id"], quote=True)}"><h3>{text(row["title"], 256)}</h3>'
            f'<p>{text(row["statement"])}</p>'
            f'<p>Question: {references("question", row["question_ids"])}. '
            f'Evidence: {references("evidence", row["evidence_ids"])}.</p>'
            + (f'<p>Figures: {references("figure", row["figure_ids"])}.</p>' if row.get("figure_ids") else "")
            + ("<h4>Limits</h4>" + bullets(row["limitations"]) if row.get("limitations") else "")
            + ("<h4>Next checks</h4>" + bullets(row["next_steps"]) if row.get("next_steps") else "")
            + "</article>")
    figures = [f'<article id="figure-{html.escape(row["id"], quote=True)}"><h3>{text(row["title"], 256)}</h3>'
               f'<p>{text(row["caption"])}</p>'
               + (f'<p><strong>How to read:</strong> {text(row["reading_guide"])}</p>' if row.get("reading_guide") else "")
               + f'<a href="{links[row["asset_id"]]}">Download figure</a></article>'
               for row in report["figures"]]
    evidence = [f'<article id="evidence-{html.escape(row["id"], quote=True)}"><h3>{text(row["title"], 256)}</h3>'
                f'<p>{text(row["description"])}</p>'
                + " ".join(f'<a href="{links[asset_id]}">Download {text(asset_id, 128)}</a>'
                           for asset_id in row.get("asset_ids", [])) + "</article>"
                for row in report["evidence"]]
    assets = [f'<li><a href="{links[row["id"]]}">{text(row["name"], 256)}</a> '
              f'({text(row["role"])}; {text(row["media_type"])}; {row["size_bytes"]} bytes) '
              f'<code>{row["sha256"]}</code></li>' for row in report["assets"]]
    overview = [f'<h2>{text(report.get("title", "Report"), 256)}</h2>'
                + (f'<p>{text(report["summary"])}</p>' if report.get("summary") else "")]
    analysis = [f'<pre>{text(json.dumps(report["analysis"], sort_keys=True, indent=2), 4096)}</pre>'] \
        if report.get("analysis") else []
    sections = [
        ("Overview", "overview entries", overview, 1),
        ("Questions", "questions", questions, 1),
        ("Findings", "findings", findings, 4),
        ("Figures", "figures", figures, 2),
        ("Evidence", "evidence entries", evidence, 2),
        ("Separate downloads", "assets", assets, 2),
        ("Analysis configuration", "analysis entries", analysis, 1),
        ("Limitations", "limitations", [f"<li>{text(value)}</li>" for value in report.get("limitations", [])], 1),
        ("Next checks", "next checks", [f"<li>{text(value)}</li>" for value in report.get("next_steps", [])], 1),
    ]
    prefix = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
              '<meta name="viewport" content="width=device-width,initial-scale=1">'
              '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
              'style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
              '<title>Artifact report preview</title><style>'
              ':root{color-scheme:light dark;--fg:#213447;--bg:#fff;--rule:#537b9b;--link:#174f7c}'
              '@media (prefers-color-scheme:dark){:root{--fg:#dbe5ee;--bg:#111826;--rule:#6f9cc0;--link:#8cc0ea}}'
              'body{font:16px/1.5 system-ui,sans-serif;color:var(--fg);background:var(--bg);margin:24px;max-width:1000px}'
              'article{border-left:3px solid var(--rule);padding:0 14px;margin:16px 0}'
              'pre,code{white-space:pre-wrap;overflow-wrap:anywhere}a{color:var(--link)}'
              '</style></head><body><main><h1>Artifact report preview</h1>'
              '<p>Evidence files are separate downloads. Rendering checks format; '
              'evidence checking and review are separate recorded stages.</p>')
    footer_start = '<section aria-label="Preview coverage"><h2>Preview coverage</h2>'
    footer_end = ('<p>Entries or excerpts omitted here remain in the '
                  '<a href="report">Full descriptor</a> and authorized evidence downloads. '
                  'Preview coverage is not evidence quality or a review outcome.</p></section></main></body></html>')
    # Each count is no wider than its total. Reserving all sections' headings and
    # coverage records guarantees valid closing tags within the requested cap.
    coverage_reserve = sum(len(f'<p>{len(items)} of {len(items)} {label}; {len(items)} omitted.</p>'.encode())
                           for _, label, items, _ in sections) + 100
    wrappers = [f'<section><h2>{title}</h2><ul></ul></section>' for title, _, _, _ in sections]
    remaining = max_bytes - len((prefix + footer_start + footer_end + "".join(wrappers)).encode()) - coverage_reserve
    if remaining < 0:
        raise ValueError("max_bytes is too small for the preview envelope")
    total_weight = sum(weight for _, _, items, weight in sections if items)
    bodies, coverage = [], []
    for title, label, items, weight in sections:
        quota = remaining * weight // total_weight if items else 0
        selected, used = [], 0
        for item in items:
            size = len(item.encode())
            if used + size <= quota:
                selected.append(item)
                used += size
        is_list = title in {"Separate downloads", "Limitations", "Next checks"}
        body = "".join(selected)
        bodies.append(f'<section><h2>{title}</h2>' + (f'<ul>{body}</ul>' if is_list else body) + '</section>')
        coverage.append(f'<p>{len(selected)} of {len(items)} {label}; {len(items) - len(selected)} omitted.</p>')
    notice = '<p>Some text excerpts were shortened.</p>' if shortened else ""
    result = prefix + "".join(bodies) + footer_start + "".join(coverage) + notice + footer_end
    present = set(re.findall(r' id="([^"]+)"', result))
    result = re.sub(r'href="#([^"]+)"',
                    lambda match: match.group() if unquote(match.group(1)) in present else 'href="report"',
                    result)
    if len(result.encode()) > max_bytes:
        raise ValueError("max_bytes exceeded while building preview")
    return result
