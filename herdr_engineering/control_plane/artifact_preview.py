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

