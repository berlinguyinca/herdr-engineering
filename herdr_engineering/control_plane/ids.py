"""Stable entity IDs (identity is stable, location is dynamic)."""
from __future__ import annotations

import secrets
import time
from typing import Final

_KINDS: Final[frozenset[str]] = frozenset({
    "mission", "session", "service", "artifact",
    "host", "agent", "plan", "worktree", "event", "lease", "llmkey",
    "project", "room", "grant", "collab", "cifix", "file",
})
# Crockford base32 in ASCII order (0-9 then a-z, omitting i/l/o/u) so the
# integer encoding is lexicographically sortable.
_CROCKFORD: Final[str] = "0123456789abcdefghjkmnpqrstvwxyz"
_SUFFIX_LEN = 26

# 48-bit millisecond timestamp occupies the high bits, then a per-process counter, then
# random bits. The counter guarantees strict monotonicity even
# for two ids created within the same millisecond, so string comparison (which
# is lexicographic) matches creation order. Identity is stable; the counter
# only affects sortability, not the entity's identity.
_counter = 0


def new_id(kind: str) -> str:
    global _counter
    if kind not in _KINDS:
        raise ValueError(f"unknown entity kind: {kind!r}")
    _counter = (_counter + 1) & 0xFFFFFFFF
    # ms timestamp | 32-bit counter (strict order within a millisecond) | 48 random bits, so
    # an observed id does not reveal the neighbouring ones.
    value = (int(time.time() * 1000) << 80) | (_counter << 48) | secrets.randbits(48)
    suffix = _encode(value, _SUFFIX_LEN)
    return f"{kind}_{suffix}"


def is_valid_id(candidate: str, kind: str) -> bool:
    prefix = f"{kind}_"
    if not candidate.startswith(prefix):
        return False
    suffix = candidate[len(prefix):]
    if len(suffix) != _SUFFIX_LEN:
        return False
    return all(c in _CROCKFORD for c in suffix)


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    chars.reverse()
    return "".join(chars)
