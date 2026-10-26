"""Room addressing grammar: pure helpers, no service state.

`@gert` is a person (their handle), `@gert.pi` / `@gert.claude` are gert's agents
(`<owner>.<harness>[-n]`), `@all` / `@agents` / `@here` are groups. Old agent names stay valid as
aliases. The server derives a message's mentions from its text; the text itself is never changed.
"""
from __future__ import annotations

import re
from collections.abc import Iterator

GROUPS = ("all", "agents", "here")
MAX_MENTIONS = 32
HANDLE_MAX = 27        # a person handle, collision suffix included
SLOT_MAX = 20          # the harness part of an address, slot suffix included: 27 + "." + 20 = 48
ADDRESS = re.compile(r"[a-z0-9._-]{1,48}\Z")
# "@" at the start, after whitespace or an opening bracket; never inside a word (mail addresses,
# paths) and never a prefix of a longer name.
_MENTION = re.compile(r"(?<![^\s(\[{])@([A-Za-z0-9._-]{1,48})(?![A-Za-z0-9._-])")
_FENCE = re.compile(r"(```|~~~).*?(?:\1|\Z)", re.S)   # both Markdown fence styles
_INLINE = re.compile(r"`[^`\n]*`")
_HARNESS_ALIASES = {"claude-code": "claude", "claude_code": "claude", "claudecode": "claude"}


def tokens(text: str) -> Iterator[str]:
    """Lower-cased names after `@`, outside code spans, in order (duplicates kept)."""
    plain = _INLINE.sub(" ", _FENCE.sub(" ", str(text)))
    for m in _MENTION.finditer(plain):
        yield m.group(1).lower()


def candidates(token: str) -> list[str]:
    """The token, then the token without trailing punctuation (`@gert.` / `@gert-` end a sentence)."""
    out = [token]
    bare = token.rstrip("._-")
    if bare and bare != token:
        out.append(bare)
    return out


def harness_key(harness: str) -> str:
    """The harness part of an address: `claude-code` -> `claude`; otherwise the family, sanitized."""
    raw = str(harness or "").strip().lower()
    raw = _HARNESS_ALIASES.get(raw, raw)
    key = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")[:20].strip("-")
    return key or "agent"


def handle_base(email: str) -> str:
    """A person's handle from their verified email: the local part, `[a-z0-9-]` only. A `.` becomes
    `-` so a person's handle can never be read as `<owner>.<harness>`."""
    local = str(email or "").split("@", 1)[0].lower()
    base = re.sub(r"[^a-z0-9]+", "-", local).strip("-")[:HANDLE_MAX].strip("-")
    return base or "user"


def first_free(base: str, taken, limit: int | None = None) -> str:
    """`base`, else `base-2`, `base-3`, ... whichever is not in `taken`; never longer than `limit` (the base
    is shortened to make room for the suffix)."""
    def fit(stem: str, suffix: str) -> str:
        return (stem[:limit - len(suffix)].rstrip("-") if limit else stem) + suffix
    if fit(base, "") not in taken:
        return fit(base, "")
    n = 2
    while fit(base, f"-{n}") in taken:
        n += 1
    return fit(base, f"-{n}")
