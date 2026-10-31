"""Three-way merge for artifact collaboration (spec 2026-10-30 section B).

Pure functions only: no I/O, no database, stdlib only, no external ``git``.

Text merge algorithm
--------------------
Each input is split into lines on ``\\n`` only (the line terminator, including
any ``\\r`` before it, stays part of the line, so CRLF/LF and a missing final
newline are preserved exactly as each side wrote them). ``str.splitlines`` is
deliberately not used: it also breaks on U+2028, form feeds and other
separators, which would silently rewrite such files.

Each side is diffed against the base and its differing regions become
*hunks* ``(base_start, base_end, replacement_lines)``. Line matching is
patience-anchored: common prefix/suffix are stripped, lines that occur
exactly once on both sides are taken as anchors (longest increasing run),
and the gaps between anchors are matched recursively; a gap with no unique
lines is matched with ``difflib.SequenceMatcher(autojunk=False)``.
``SequenceMatcher`` on its own is quadratic in repeated lines (a 40k-line
Markdown file with a blank line every fifth line took 36 s); anchoring makes
that case ~0.1 s. A gap whose ``SequenceMatcher`` work estimate (sum over base
lines of their occurrences on the other side) exceeds ``_MAX_GAP_COST`` is
reported as a single replace hunk instead: still a correct diff, only coarser,
so pathological all-repeated inputs may conflict where a finer diff would not,
but merge time stays bounded (~1 s for 2 MiB inputs). Hunks from both sides are
sorted by base position and grouped into clusters. A hunk joins the current
cluster when it really touches it:

* its base range overlaps the cluster's base range, or
* it is an insertion strictly inside the cluster's range, or the cluster is an
  insertion point strictly inside the hunk's range, or
* both are insertions at the same base position.

Merely adjacent hunks (one ends where the other starts, or an insertion at the
edge of a replaced range) do not join, so edits to neighbouring lines merge.
A cluster with hunks from one side only takes that side; a cluster whose two
sides rewrite the base span to the same lines takes it once; otherwise it is
a conflict emitted as::

    <<<<<<< {ours_label}
    ...ours...
    =======
    ...theirs...
    >>>>>>> {theirs_label}

Markers use the dominant line ending of the inputs (CRLF if more lines end
in CRLF than in a bare LF). A conflict side whose last line has no newline
gets one so markers always sit on their own lines (conflicted output only).
"""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher

__all__ = ["FileConflict", "ManifestMerge", "TextMerge", "merge3_text", "merge_manifests"]


@dataclass(frozen=True)
class TextMerge:
    text: str
    conflicted: bool
    conflicts: int


@dataclass(frozen=True)
class _Hunk:
    start: int
    end: int
    lines: tuple[str, ...]
    side: int  # 0 = ours, 1 = theirs


def _split(text: str) -> list[str]:
    if not text:
        return []
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


# Above this many candidate comparisons a difflib gap is reported as one
# replace hunk instead of being matched line by line (see module docstring).
_MAX_GAP_COST = 400_000
_MAX_DEPTH = 64


def _match_into(
    a: list[str], b: list[str], alo: int, ahi: int, blo: int, bhi: int,
    out: list[tuple[int, int]], depth: int,
) -> None:
    """Append matched line pairs ``(i, j)`` for ``a[alo:ahi]`` vs ``b[blo:bhi]`` in order."""
    while alo < ahi and blo < bhi and a[alo] == b[blo]:
        out.append((alo, blo))
        alo += 1
        blo += 1
    tail: list[tuple[int, int]] = []
    while alo < ahi and blo < bhi and a[ahi - 1] == b[bhi - 1]:
        ahi -= 1
        bhi -= 1
        tail.append((ahi, bhi))
    if alo < ahi and blo < bhi:
        anchors = _unique_anchors(a, b, alo, ahi, blo, bhi) if depth < _MAX_DEPTH else []
        if anchors:
            i0, j0 = alo, blo
            for i, j in anchors:
                _match_into(a, b, i0, i, j0, j, out, depth + 1)
                out.append((i, j))
                i0, j0 = i + 1, j + 1
            _match_into(a, b, i0, ahi, j0, bhi, out, depth + 1)
        else:
            _difflib_match(a, b, alo, ahi, blo, bhi, out)
    out.extend(reversed(tail))


def _unique_anchors(
    a: list[str], b: list[str], alo: int, ahi: int, blo: int, bhi: int,
) -> list[tuple[int, int]]:
    """Patience anchors: lines occurring exactly once on each side, longest increasing run."""
    seen_a: dict[str, int] = {}
    for i in range(alo, ahi):
        seen_a[a[i]] = -1 if a[i] in seen_a else i
    seen_b: dict[str, int] = {}
    for j in range(blo, bhi):
        line = b[j]
        if seen_a.get(line, -1) >= 0:
            seen_b[line] = -1 if line in seen_b else j
    pairs = sorted((seen_a[line], j) for line, j in seen_b.items() if j >= 0)
    if not pairs:
        return []
    # Longest increasing subsequence on j (pairs are sorted by i).
    tails: list[int] = []  # j value ending the best run of each length
    tail_idx: list[int] = []
    prev: list[int] = [-1] * len(pairs)
    for k, (_, j) in enumerate(pairs):
        pos = bisect_left(tails, j)
        if pos == len(tails):
            tails.append(j)
            tail_idx.append(k)
        else:
            tails[pos] = j
            tail_idx[pos] = k
        prev[k] = tail_idx[pos - 1] if pos else -1
    run: list[tuple[int, int]] = []
    k = tail_idx[-1]
    while k >= 0:
        run.append(pairs[k])
        k = prev[k]
    run.reverse()
    return run


def _difflib_match(
    a: list[str], b: list[str], alo: int, ahi: int, blo: int, bhi: int,
    out: list[tuple[int, int]],
) -> None:
    counts = Counter(b[blo:bhi])
    if sum(counts.get(a[i], 0) for i in range(alo, ahi)) > _MAX_GAP_COST:
        return  # whole gap becomes one replace hunk
    matcher = SequenceMatcher(None, a[alo:ahi], b[blo:bhi], autojunk=False)
    for i, j, size in matcher.get_matching_blocks():
        out.extend((alo + i + k, blo + j + k) for k in range(size))


def _hunks(base: list[str], side: list[str], tag: int) -> list[_Hunk]:
    matches: list[tuple[int, int]] = []
    _match_into(base, side, 0, len(base), 0, len(side), matches, 0)
    matches.append((len(base), len(side)))
    hunks: list[_Hunk] = []
    pi, pj = 0, 0
    for i, j in matches:
        if i > pi or j > pj:
            hunks.append(_Hunk(pi, i, tuple(side[pj:j]), tag))
        pi, pj = i + 1, j + 1
    return hunks


def _touches(hunk: _Hunk, start: int, end: int) -> bool:
    if hunk.start < end and start < hunk.end:
        return True
    if hunk.start == hunk.end:
        p = hunk.start
        return start < p < end or (start == end == p)
    if start == end:
        return hunk.start < start < hunk.end
    return False


def _apply(base: list[str], start: int, end: int, hunks: list[_Hunk]) -> list[str]:
    out: list[str] = []
    pos = start
    for hunk in hunks:
        out.extend(base[pos : hunk.start])
        out.extend(hunk.lines)
        pos = hunk.end
    out.extend(base[pos:end])
    return out


def _dominant_eol(*texts: str) -> str:
    crlf = sum(t.count("\r\n") for t in texts)
    lf = sum(t.count("\n") for t in texts) - crlf
    return "\r\n" if crlf > lf else "\n"


def merge3_text(
    base: str,
    ours: str,
    theirs: str,
    *,
    ours_label: str = "head",
    theirs_label: str = "mine",
) -> TextMerge:
    if any("\r" in label or "\n" in label for label in (ours_label, theirs_label)):
        raise ValueError("merge labels must not contain line breaks")
    if ours == theirs:
        return TextMerge(text=ours, conflicted=False, conflicts=0)
    if ours == base:
        return TextMerge(text=theirs, conflicted=False, conflicts=0)
    if theirs == base:
        return TextMerge(text=ours, conflicted=False, conflicts=0)

    base_l = _split(base)
    hunks = _hunks(base_l, _split(ours), 0) + _hunks(base_l, _split(theirs), 1)
    hunks.sort(key=lambda h: (h.start, h.end, h.side))

    clusters: list[list[_Hunk]] = []
    span = (0, 0)
    open_end = False
    for hunk in hunks:
        # A hunk whose text ends without a newline (it can only end at the
        # base's end) cannot have anything appended after it: an insertion at
        # that point would be glued onto its last line, so it must join.
        if clusters and (_touches(hunk, *span) or (open_end and hunk.start == span[1])):
            clusters[-1].append(hunk)
            span = (span[0], max(span[1], hunk.end))
        else:
            clusters.append([hunk])
            span = (hunk.start, hunk.end)
            open_end = False
        open_end = open_end or _unterminated(hunk.lines)

    eol = _dominant_eol(base, ours, theirs)
    out: list[str] = []
    conflicts = 0
    pos = 0
    for cluster in clusters:
        start = cluster[0].start
        end = max(h.end for h in cluster)
        out.extend(base_l[pos:start])
        pos = end
        mine = [h for h in cluster if h.side == 0]
        other = [h for h in cluster if h.side == 1]
        ours_text = _apply(base_l, start, end, mine)
        if not other:
            out.extend(ours_text)
            continue
        theirs_text = _apply(base_l, start, end, other)
        if not mine or ours_text == theirs_text:
            out.extend(theirs_text)
            continue
        conflicts += 1
        out.append(f"<<<<<<< {ours_label}{eol}")
        out.extend(_terminated(ours_text, eol))
        out.append(f"======={eol}")
        out.extend(_terminated(theirs_text, eol))
        out.append(f">>>>>>> {theirs_label}{eol}")
    out.extend(base_l[pos:])
    return TextMerge(text="".join(out), conflicted=conflicts > 0, conflicts=conflicts)


def _unterminated(lines: tuple[str, ...]) -> bool:
    return bool(lines) and not lines[-1].endswith("\n")


def _terminated(lines: list[str], eol: str) -> list[str]:
    if lines and not lines[-1].endswith("\n"):
        return [*lines[:-1], lines[-1] + eol]
    return lines



# ---------------------------------------------------------------------------
# Manifest merge (spec section B): per file name, three-way over manifests.
#
# A side "changed" a name when its sha256 differs from the base (absent counts
# as None). One side changed -> take it; identical change -> take it; both
# changed differently -> conflict, except modify/modify of a text file, which
# goes through merge3_text (head is "ours", mine is "theirs"). In FileConflict
# ``theirs_sha256`` is the head entry and ``mine_sha256`` the contributor's.
# store_text receives ``{**head_entry, "text": merged_text}`` as template.
# Nothing is stored unless the whole merge is conflict free.
# ---------------------------------------------------------------------------

_TEXT_MEDIA_TYPES = frozenset({
    "application/json", "application/xml", "application/yaml", "application/javascript",
})
_TEXT_EXTENSIONS = (
    ".md", ".html", ".css", ".js", ".json", ".yaml", ".yml", ".xml", ".txt", ".csv",
    ".svg", ".py", ".ts",
)


@dataclass(frozen=True)
class FileConflict:
    name: str
    kind: str  # add_add | delete_modify | modify_modify_text | binary | oversize | not_utf8 | case_collision
    # | validation (set by ArtifactStore.merge_commit when the merged result fails the manifest/blob checks)
    base_sha256: str | None
    theirs_sha256: str | None
    mine_sha256: str | None
    merged_text: str | None


@dataclass(frozen=True)
class ManifestMerge:
    files: list[dict]
    merged_names: list[str]
    clean_names: list[str]
    conflicts: list[FileConflict]


def _is_text_like(entry: dict) -> bool:
    media = str(entry.get("media_type") or "").split(";", 1)[0].strip().lower()
    if media.startswith("text/") or media in _TEXT_MEDIA_TYPES:
        return True
    return str(entry.get("name", "")).lower().endswith(_TEXT_EXTENSIONS)


def merge_manifests(
    base: list[dict],
    head: list[dict],
    mine: list[dict],
    *,
    read_text: Callable[[dict], str],
    store_text: Callable[[str, dict], dict],
    max_text_bytes: int = 2 * 1024 * 1024,
) -> ManifestMerge:
    for side, entries in (("base", base), ("head", head), ("mine", mine)):
        names = [e["name"] for e in entries]
        if len(names) != len(set(names)):
            dup = next(n for n in names if names.count(n) > 1)
            raise ValueError(f"duplicate file name in {side} manifest: {dup}")
    b_map = {e["name"]: e for e in base}
    h_map = {e["name"]: e for e in head}
    m_map = {e["name"]: e for e in mine}
    all_names = sorted(b_map.keys() | h_map.keys() | m_map.keys())

    by_fold: dict[str, list[str]] = {}
    for name in all_names:
        by_fold.setdefault(name.casefold(), []).append(name)

    def sha(entry: dict | None) -> str | None:
        return None if entry is None else entry["sha256"]

    conflicts: list[FileConflict] = []
    chosen: dict[str, dict] = {}
    clean: list[str] = []
    pending: list[tuple[str, dict, str]] = []  # (name, head entry, merged text)

    for name in all_names:
        b, h, m = b_map.get(name), h_map.get(name), m_map.get(name)
        bs, hs, ms = sha(b), sha(h), sha(m)

        def conflict(kind: str, merged_text: str | None = None) -> None:
            conflicts.append(FileConflict(name, kind, bs, hs, ms, merged_text))

        if len(by_fold[name.casefold()]) > 1:
            conflict("case_collision")
            continue
        if hs == ms or ms == bs:
            winner = h
        elif hs == bs:
            winner = m
        elif h is None or m is None:
            conflict("delete_modify")  # base is present here: absent == base was handled above
            continue
        elif b is None:
            conflict("add_add")
            continue
        else:
            kind = _classify_text(b, h, m, max_text_bytes)
            if kind is not None:
                conflict(kind)
                continue
            try:
                texts = [read_text(e) for e in (b, h, m)]
            except UnicodeDecodeError:
                conflict("not_utf8")
                continue
            # the declared size_bytes may lie: the cap applies to what was actually read
            if any(len(t.encode("utf-8")) > max_text_bytes for t in texts):
                conflict("oversize")
                continue
            result = merge3_text(*texts)
            if result.conflicted:
                conflict("modify_modify_text", result.text)
            else:
                pending.append((name, h, result.text))
            continue
        clean.append(name)
        if winner is not None:
            chosen[name] = winner

    if conflicts:
        return ManifestMerge(files=[], merged_names=[], clean_names=clean, conflicts=conflicts)

    merged_names: list[str] = []
    for name, template, text in pending:
        chosen[name] = store_text(name, {**template, "text": text})
        merged_names.append(name)
    return ManifestMerge(
        files=[chosen[n] for n in sorted(chosen)],
        merged_names=merged_names,
        clean_names=clean,
        conflicts=[],
    )


def _classify_text(b: dict, h: dict, m: dict, max_text_bytes: int) -> str | None:
    """Return a conflict kind when a modify/modify file cannot be text-merged."""
    if not (_is_text_like(h) and _is_text_like(m) and _is_text_like(b)):
        return "binary"
    if any(int(e.get("size_bytes") or 0) > max_text_bytes for e in (b, h, m)):
        return "oversize"
    return None
