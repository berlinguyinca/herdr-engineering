"""Stable entity IDs (identity is stable, location is dynamic)."""
from __future__ import annotations

import secrets
import time
from typing import Final

_KINDS: Final[frozenset[str]] = frozenset({
    "mission", "session", "service", "artifact",
    "host", "agent", "plan", "worktree",
})
_CROCKFORD: Final[str] = "0123456789abcdefghjkmnpqrstvwxyz"
_SUFFIX_LEN = 26


def new_id(kind: str) -> str:
    if kind not in _KINDS:
        raise ValueError(f"unknown entity kind: {kind!r}")
    ts = int(time.time() * 1000)
    # 48-bit timestamp -> 10 Crockford chars (5 bits each)
    ts_chars = []
    for _ in range(10):
        ts_chars.append(_CROCKFORD[ts & 31])
        ts >>= 5
    ts_chars.reverse()
    suffix = "".join(ts_chars) + _random_suffix(16)
    return f"{kind}_{suffix}"


def is_valid_id(candidate: str, kind: str) -> bool:
    prefix = f"{kind}_"
    if not candidate.startswith(prefix):
        return False
    suffix = candidate[len(prefix):]
    if len(suffix) != _SUFFIX_LEN:
        return False
    return all(c in _CROCKFORD for c in suffix)


def _random_suffix(length: int) -> str:
    return "".join(_CROCKFORD[b & 31] for b in secrets.token_bytes(length))
