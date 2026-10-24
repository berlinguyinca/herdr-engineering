"""Stable identity for review findings across pushes."""

from __future__ import annotations

import hashlib
import re

from .models import Finding

_WS = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")


def _normalise_message(message: str) -> str:
    text = _WS.sub(" ", message.strip().lower())
    return _DIGITS.sub("#", text)


def _normalise_path(path: str) -> str:
    text = path.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def finding_fingerprint(f: Finding) -> str:
    """Hex sha256 over path, category and normalised message (line excluded)."""
    parts = (_normalise_path(f.path), f.category.strip().lower(), _normalise_message(f.message))
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()
