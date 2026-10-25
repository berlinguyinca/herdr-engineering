"""`#` issue references in room messages (Task C4): pure grammar, no service state.

`#binview-44` names issue 44 of the in-scope repository called `binview`; `acme/binview#44` names
it with its owner (needed when two in-scope owners share the repository name). A reference opens
only at the start of the text, after whitespace or after an opening bracket/quote, and never inside
a code span or fence (backtick or `~~~`), a URL, a word (`abc#12`) or an HTML entity (`&#39;`). A
Markdown heading (`# Title`) has a space after `#` and names nothing.

The server resolves references from the stored text against the GitHub cache (clients cannot
supply them) and keeps only coordinates: `[{kind: issue|pr, owner, repo, number}]`, at most
`MAX_REFS`. Title and state are read from the cache when a message is read, never stored.

Per-viewer rule: a guest or an external agent sees title and state only for issues of public
repositories; a private repository's reference reaches them as coordinates only (they are in the
text already), and their `#` menu lists public repositories' issues only.
"""
from __future__ import annotations

import re
from collections.abc import Iterator

from .collab_mentions import _FENCE, _INLINE

MAX_REFS = 20
_URL = re.compile(r"(?:[a-z][a-z0-9+.-]{1,15}://|www\.)[^\s<>]{0,2048}", re.I)
_OPEN = r"(?<![^\s(\[{\"'])"                 # start, whitespace, or an opening bracket / quote
_END = r"(?![A-Za-z0-9_#/-]|\.[A-Za-z0-9_])"  # not a longer token (`#a-1x`, `#a-1.5`); a full stop may end it
_SHORT = re.compile(_OPEN + r"#([A-Za-z0-9_.-]{1,100}?)-([0-9]{1,10})" + _END)
_LONG = re.compile(_OPEN + r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})#([0-9]{1,10})" + _END)


def _plain(text: str) -> str:
    """The text with code and URLs blanked (same length, so match order is text order)."""
    blank = lambda m: " " * len(m.group(0))  # noqa: E731
    return _URL.sub(blank, _INLINE.sub(blank, _FENCE.sub(blank, str(text))))


def tokens(text: str) -> Iterator[tuple[str | None, str, int]]:
    """``(owner | None, repo, number)`` for each reference outside code and URLs, in text order."""
    plain = _plain(text)
    found = [(m.start(), None, m.group(1), m.group(2)) for m in _SHORT.finditer(plain)]
    found += [(m.start(), m.group(1), m.group(2), m.group(3)) for m in _LONG.finditer(plain)]
    for _, owner, repo, number in sorted(found, key=lambda f: f[0]):
        if repo.strip(".") and repo not in {".", ".."}:
            yield (owner.lower() if owner else None), repo.lower(), int(number)


def for_viewer(refs: list[dict], details: list[dict | None], restricted: bool) -> list[dict]:
    """Stored refs as one reader sees them: coordinates plus, when the cache knows it and the reader
    may see it, ``title`` and ``state``. ``restricted`` (a guest or an external agent) hides both
    for private repositories."""
    out = []
    for ref, info in zip(refs, details, strict=True):
        view = {k: ref[k] for k in ("kind", "owner", "repo", "number")}
        if info is not None and not (restricted and info.get("private")):
            view.update(title=info.get("title"), state=info.get("state"))
            if isinstance(info.get("url"), str) and info["url"].startswith("https://github.com/"):
                view["url"] = info["url"]
        out.append(view)
    return out
