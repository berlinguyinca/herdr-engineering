"""Allowlisted live-service publication policy without arbitrary proxy origins."""
from __future__ import annotations

from dataclasses import dataclass, field


class LiveServicePublicationError(ValueError):
    pass


@dataclass(frozen=True)
class LiveServicePolicy:
    service_id: str
    path_prefix: str
    methods: set[str] = field(default_factory=lambda: {"GET", "HEAD"})
    origin_url: str | None = None
    websocket: bool = False

    def __post_init__(self):
        if not self.service_id:
            raise LiveServicePublicationError("service_id is required")
        if self.origin_url:
            raise LiveServicePublicationError("private origin URLs are not accepted")
        if not self.path_prefix.startswith("/") or ".." in self.path_prefix:
            raise LiveServicePublicationError("unsafe path prefix")
        if not self.methods or not self.methods <= {"GET", "HEAD", "POST", "PUT", "DELETE"}:
            raise LiveServicePublicationError("unsupported service methods")

    def allows(self, *, method: str, path: str) -> bool:
        if ".." in path.split("/"):
            raise LiveServicePublicationError("unsafe request path")
        return method in self.methods and (path == self.path_prefix or
                                           path.startswith(self.path_prefix.rstrip("/") + "/"))

