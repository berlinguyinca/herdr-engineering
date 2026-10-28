"""Small deterministic renderer adapters used by artifact build workers."""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, replace


class ArtifactRendererError(ValueError):
    pass


@dataclass(frozen=True)
class RenderResult:
    profile: str
    source: str
    outputs: list[dict]
    validation: dict
    toolchain: dict
    report_descriptor: dict | None = None


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
        raise ArtifactRendererError(f"unsupported artifact profile: {profile}")

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
        escaped = html.escape(source)
        rendered = re.sub(r"^# (.+)$", r"<h1>\1</h1>", escaped, flags=re.MULTILINE)
        rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rendered)
        rendered = "\n".join(
            f"<p>{line}</p>" if line and not line.startswith("<h1>") else line
            for line in rendered.splitlines())
        return RenderResult("document", source,
                            [{"name": "index.html", "media_type": "text/html",
                              "content": rendered}],
                            {"status": "passed", "checks": ["markdown-render"]},
                            {"renderer": "builtin-markdown", "revision": "1"})

    @staticmethod
    def _web(source: str) -> RenderResult:
        if not isinstance(source, str) or not re.search(r"<html(?:\s|>)", source, re.I):
            raise ArtifactRendererError("web profile requires HTML source")
        return RenderResult("web", source,
                            [{"name": "index.html", "media_type": "text/html",
                              "content": source}],
                            {"status": "passed", "checks": ["html-envelope"]},
                            {"renderer": "builtin-web", "revision": "1"})

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

