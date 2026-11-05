"""One rule for "a bare https origin", shared by the deploy-time config check and the share server.

Dependency free on purpose: ``fleet_config_cli`` runs on the deploy host, where the control plane's drivers are not
installed, and ``control_plane.share_public`` must agree with it exactly (a value this rejects must never reach
runtime and silently degrade)."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

HOST_PORT = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?")


def https_origin(value: object) -> str | None:
    """``https://host[:port]`` (lowercased) for a bare https origin (a trailing ``/`` allowed), else None.

    Refused: non-strings and non-ASCII (IDN), userinfo, query, fragment, any path beyond ``/``, a host that is not
    lowercase-ASCII letters/digits/dots/hyphens (so no trailing dot, no brackets, no spaces), a non-https scheme."""
    if not isinstance(value, str) or not value.isascii():
        return None
    try:
        parts = urlsplit(value.strip())
        netloc = parts.netloc.lower()
    except ValueError:
        return None
    if (parts.scheme != "https" or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in ("", "/") or not HOST_PORT.fullmatch(netloc)
            or netloc.split(":")[0].endswith(".")):
        return None
    return "https://" + netloc


def origin_hostname(origin: str) -> str:
    """The lowercase hostname of an origin ``https_origin`` returned (port dropped)."""
    return origin.removeprefix("https://").split(":")[0]
