"""Small deterministic renderer adapters used by artifact build workers."""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, replace


class ArtifactRendererError(ValueError):
    """Renderer failure. ``code`` is machine-readable; ``to_dict`` is the structured result."""

    def __init__(self, message: str, code: str = "render_failed"):
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict:
        return {"status": "failed", "code": self.code, "message": str(self)}


@dataclass(frozen=True)
class RenderResult:
    profile: str
    source: str
    outputs: list[dict]
    validation: dict
    toolchain: dict
    report_descriptor: dict | None = None

_PAGE_HEAD = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<style>"
    ":root{color-scheme:light dark}"
    "body{margin:0;font:16px/1.6 system-ui,sans-serif;background:Canvas;color:CanvasText}"
    "main{max-width:48rem;margin:0 auto;padding:1.5rem 1rem}"
    "h1,h2,h3{line-height:1.25}"
    "pre{overflow:auto;padding:.75rem;border-radius:6px;background:color-mix(in srgb,CanvasText 8%,Canvas)}"
    "code{font-family:ui-monospace,monospace;font-size:.9em}"
    "table{border-collapse:collapse;display:block;overflow-x:auto}"
    "th,td{border:1px solid color-mix(in srgb,CanvasText 25%,Canvas);padding:.3rem .6rem}"
    "blockquote{margin-left:0;padding-left:1rem;border-left:3px solid color-mix(in srgb,CanvasText 30%,Canvas)}"
    "</style></head><body>\n")

_SAFE_URL = re.compile(r"^(?:https?:|mailto:)", re.I)


def _inline(text: str) -> str:
    """Escape first, then apply a small inline subset. Raw HTML is never passed through."""
    spans: list[str] = []

    def keep_code(match):
        spans.append("<code>" + html.escape(match.group(1)) + "</code>")
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", keep_code, text)
    text = html.escape(text, quote=False)

    def link(match):
        label, url = match.group(1), html.unescape(match.group(2)).strip()
        if ":" in url and not _SAFE_URL.match(url):
            return match.group(0)
        return ('<a href="' + html.escape(url, quote=True)
                + '" rel="noopener noreferrer">' + label + "</a>")

    text = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def render_markdown(source: str) -> str:
    """Render headings, paragraphs, lists, fenced code, tables, quotes and rules."""
    lines = source.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith("```"):
            i += 1
            code = []
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
        elif re.match(r"#{1,6} ", line):
            level = len(line) - len(line.lstrip("#"))
            out.append(f"<h{level}>{_inline(line[level + 1:].strip())}</h{level}>")
            i += 1
        elif re.match(r"(?:-{3,}|\*{3,})\s*$", line):
            out.append("<hr>")
            i += 1
        elif line.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i].lstrip(">").strip())
                i += 1
            out.append("<blockquote><p>" + _inline(" ".join(quote)) + "</p></blockquote>")
        elif re.match(r"(?:[-*+]|\d+\.) ", line):
            ordered = line[0].isdigit()
            marker = r"\d+\. " if ordered else r"[-*+] "
            items = []
            while i < len(lines) and re.match(marker, lines[i]):
                items.append(re.sub("^" + marker, "", lines[i]))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(item)}</li>" for item in items) + f"</{tag}>")
        elif "|" in line and i + 1 < len(lines) and re.match(r"\s*\|?\s*:?-{3,}", lines[i + 1]):
            head = _table_cells(line)
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_table_cells(lines[i]))
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
                                 for row in rows) + "</tbody></table>")
        else:
            para = [line.strip()]
            i += 1
            while (i < len(lines) and lines[i].strip()
                   and not lines[i].startswith(("```", "#", ">"))
                   and not re.match(r"(?:[-*+]|\d+\.) ", lines[i])):
                para.append(lines[i].strip())
                i += 1
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
    return "\n".join(out) + "\n"


def _web_findings(source: str) -> list[dict]:
    """Report active or external content; informational, never a failure."""
    findings = []
    if re.search(r"<script\b", source, re.I):
        findings.append({"code": "active_content", "detail": "contains <script>"})
    external = sorted(set(re.findall(r"""(?:src|href)\s*=\s*["']?(https?://[^"'\s>]+)""", source, re.I)))
    for url in external[:20]:
        findings.append({"code": "external_resource", "detail": url})
    return findings


class RendererRegistry:
    def render(self, profile: str, source: str, request: dict) -> RenderResult:
        if profile == "document":
            return self._markdown(source)
        if profile == "web":
            return self._web(source)
        if profile == "visualization":
            return self._visualization(source)
        if profile == "diagram":
            return self._diagram(source)
        if profile == "report":
            return self._report(source, request)
        raise ArtifactRendererError(f"unsupported artifact profile: {profile}", "unsupported_format")

    def _report(self, source: str, request: dict) -> RenderResult:
        from .artifact_report_descriptor import validate_report_descriptor
        from .artifact_report_render import render_report_preview

        if not isinstance(source, str):
            raise ArtifactRendererError("report source must be text")
        descriptor = request.get("report_descriptor")
        if "report_descriptor" in request:
            descriptor = validate_report_descriptor(descriptor)
            if source.strip():
                try:
                    supplied_source = json.loads(source)
                except ValueError as exc:
                    raise ArtifactRendererError("descriptor reports require matching JSON source") from exc
                if validate_report_descriptor(supplied_source) != descriptor:
                    raise ArtifactRendererError("report source and descriptor disagree")
        elif source.lstrip().startswith(("{", "[")):
            try:
                descriptor = validate_report_descriptor(json.loads(source))
            except ValueError as exc:
                raise ArtifactRendererError(f"invalid report descriptor: {exc}") from exc
        else:
            return replace(self._markdown(source), profile="report")
        canonical = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
        preview = render_report_preview(descriptor)
        return RenderResult(
            "report", canonical,
            [{"name": "preview.html", "media_type": "text/html", "content": preview}],
            {"status": "passed", "checks": ["report-descriptor", "bounded-inert-html"],
             "scope": "format-and-render", "preview_bytes": len(preview.encode())},
            {"renderer": "builtin-report-descriptor", "revision": "1"},
            report_descriptor=descriptor)

    @staticmethod
    def _markdown(source: str) -> RenderResult:
        if not isinstance(source, str):
            raise ArtifactRendererError("document source must be text")
        body = render_markdown(source)
        page = (_PAGE_HEAD + "<main>\n" + body + "</main>\n</body></html>\n")
        return RenderResult("document", source,
                            [{"name": "index.html", "media_type": "text/html",
                              "content": page}],
                            {"status": "passed", "checks": ["markdown-render"]},
                            {"renderer": "builtin-markdown", "revision": "2"})

    @staticmethod
    def _web(source: str) -> RenderResult:
        if not isinstance(source, str) or not re.search(r"<html(?:\s|>)", source, re.I):
            raise ArtifactRendererError("web profile requires HTML source")
        return RenderResult("web", source,
                            [{"name": "index.html", "media_type": "text/html",
                              "content": source}],
                            {"status": "passed", "checks": ["html-envelope"],
                             "findings": _web_findings(source)},
                            {"renderer": "builtin-web", "revision": "2"})

    @staticmethod
    def _visualization(source: str) -> RenderResult:
        try:
            parsed = json.loads(source)
        except (TypeError, ValueError) as exc:
            raise ArtifactRendererError("visualization profile requires valid JSON") from exc
        normalized = json.dumps(parsed, indent=2, sort_keys=True)
        return RenderResult("visualization", source,
                            [{"name": "data.json", "media_type": "application/json",
                              "content": normalized}],
                            {"status": "passed", "checks": ["json-parse"]},
                            {"renderer": "builtin-json", "revision": "1"})

    @staticmethod
    def _diagram(source: str) -> RenderResult:
        if not isinstance(source, str) or not source.strip():
            raise ArtifactRendererError("diagram profile requires Mermaid source")
        # Keep Mermaid as inert text; a later sandboxed renderer may produce SVG.
        content = "<pre data-diagram-format=\"mermaid\">" + html.escape(source) + "</pre>"
        return RenderResult("diagram", source,
                            [{"name": "diagram.html", "media_type": "text/html",
                              "content": content}],
                            {"status": "passed", "checks": ["mermaid-source"]},
                            {"renderer": "builtin-mermaid-source", "revision": "1"})

