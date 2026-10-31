"""Safe, explicit artifact preview policy for dashboard and public delivery."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PreviewPolicy:
    mode: str
    sandboxed: bool = False
    content_disposition: str = "inline"

    @classmethod
    def for_media_type(cls, media_type: str) -> PreviewPolicy:
        media = (media_type or "").lower().split(";", 1)[0].strip()
        if media == "application/pdf":
            return cls("pdf")
        if media.startswith("image/") and media not in {"image/svg+xml"}:
            return cls("image")
        if media in {"text/markdown", "text/x-markdown"}:
            return cls("markdown")
        if media == "application/json":
            return cls("json")
        if media in {"text/html", "application/xhtml+xml", "image/svg+xml"}:
            return cls("html", sandboxed=True)
        return cls("download", content_disposition="attachment")

    def headers(self) -> dict[str, str]:
        headers = {"X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer",
                   "Content-Disposition": self.content_disposition}
        if self.sandboxed:
            headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return headers



_KIND_BY_MEDIA = {
    "application/pdf": "pdf", "text/markdown": "markdown", "text/x-markdown": "markdown",
    "application/json": "json", "text/html": "html", "application/xhtml+xml": "html", "image/svg+xml": "html",
    "application/msword": "document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    "application/vnd.oasis.opendocument.text": "document",
    "application/vnd.ms-excel": "spreadsheet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "spreadsheet",
    "application/vnd.oasis.opendocument.spreadsheet": "spreadsheet",
    "application/vnd.ms-powerpoint": "presentation",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "presentation",
    "application/vnd.oasis.opendocument.presentation": "presentation",
    "text/csv": "table", "text/tab-separated-values": "table", "application/x-ndjson": "table",
    "text/plain": "text", "text/x-log": "log", "text/uri-list": "url",
}
_KIND_BY_EXT = {
    ".pdf": "pdf", ".md": "markdown", ".markdown": "markdown", ".json": "json", ".html": "html", ".htm": "html",
    ".svg": "html", ".doc": "document", ".docx": "document", ".odt": "document", ".xls": "spreadsheet",
    ".xlsx": "spreadsheet", ".ods": "spreadsheet", ".ppt": "presentation", ".pptx": "presentation",
    ".odp": "presentation", ".csv": "table", ".tsv": "table", ".ndjson": "table", ".txt": "text", ".log": "log",
    ".url": "url", ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
}
# Kinds a dashboard may render itself (sandboxed or inert); everything else is offered as a download.
INLINE_KINDS = frozenset({"pdf", "image", "markdown", "json", "html", "table", "text", "log", "url"})


def preview_kind(media_type: str | None, filename: str | None = None) -> str:
    """Typed renderer class for an artifact output. ``html`` is always sandboxed (see ``PreviewPolicy``)."""
    media = (media_type or "").lower().split(";", 1)[0].strip()
    if media in _KIND_BY_MEDIA:
        return _KIND_BY_MEDIA[media]
    if media.startswith("image/"):
        return "image"
    if media.endswith("+json"):
        return "json"
    name = (filename or "").lower()
    dot = name.rfind(".")
    if dot >= 0 and name[dot:] in _KIND_BY_EXT and media in {"", "application/octet-stream"}:
        return _KIND_BY_EXT[name[dot:]]
    return "download"
