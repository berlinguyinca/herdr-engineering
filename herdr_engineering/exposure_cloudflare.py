"""Opt-in Cloudflare Tunnel transport adapter."""
from __future__ import annotations

import subprocess
from collections.abc import Callable


class CloudflareTunnelProvider:
    def __init__(self, *, enabled: bool = False,
                 runner: Callable[..., str] | None = None):
        self.enabled = enabled
        self.runner = runner or self._run

    def capabilities(self) -> dict:
        return {"enabled": self.enabled, "https": self.enabled,
                "websocket": self.enabled, "custom_domain": self.enabled}

    def prepare(self, publication: dict) -> None:
        self._require_enabled()
        self._target(publication)

    def expose(self, publication: dict) -> dict:
        self._require_enabled()
        target = self._target(publication)
        self.runner("tunnel", "--url", target)
        return {"status": "observed", "endpoint": "cloudflare-tunnel://observed"}

    def revoke(self, publication: dict) -> None:
        self._require_enabled()
        self.runner("tunnel", "cleanup")

    def _target(self, publication: dict) -> str:
        target = publication.get("local_target")
        if not isinstance(target, str) or not target.startswith("http://127.0.0.1:"):
            raise ValueError("Cloudflare Tunnel target must be a loopback HTTP URL")
        return target

    def _require_enabled(self):
        if not self.enabled:
            raise RuntimeError("Cloudflare Tunnel provider is disabled")

    @staticmethod
    def _run(*args: str) -> str:
        return subprocess.run(["cloudflared", *args], check=True,
                              capture_output=True, text=True).stdout

