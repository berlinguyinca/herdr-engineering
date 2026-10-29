"""Opt-in Tailscale Funnel exposure adapter.

This adapter only manages the transport command. Publication authorization and
revocation state remain owned by the publication controller/gateway.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable


class TailscaleFunnelProvider:
    def __init__(self, *, enabled: bool = False,
                 runner: Callable[..., str] | None = None):
        self.enabled = enabled
        self.runner = runner or self._run

    def capabilities(self) -> dict:
        return {"enabled": self.enabled, "https": self.enabled,
                "websocket": self.enabled, "custom_domain": False}

    def prepare(self, publication: dict) -> None:
        self._require_enabled()
        if not publication.get("local_target"):
            raise ValueError("publication local_target is required")

    def expose(self, publication: dict) -> dict:
        self._require_enabled()
        target = publication.get("local_target")
        if not isinstance(target, str) or not target.startswith("http://127.0.0.1:"):
            raise ValueError("Funnel target must be an approved loopback HTTP target")
        self.runner("funnel", "--bg", target)
        return {"status": "observed", "endpoint": "tailscale-funnel://observed"}

    def revoke(self, publication: dict) -> None:
        self._require_enabled()
        self.runner("funnel", "off")

    def _require_enabled(self):
        if not self.enabled:
            raise RuntimeError("Tailscale Funnel provider is disabled")

    @staticmethod
    def _run(*args: str) -> str:
        return subprocess.run(["tailscale", *args], check=True,
                              capture_output=True, text=True).stdout

