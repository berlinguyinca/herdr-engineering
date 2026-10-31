"""Skip enforcement for the full-suite pipeline: a skipped test must be listed.

``herdr-eng review-gate check-skips`` is the last step of the repo's main
pipeline. The disabled-tests file is YAML in the repo::

    disabled:
      - {test: "pkg.mod.TestX::test_y", reason: "flaky on arm, issue 12"}

A testcase counts as skipped when it has a ``<skipped>`` child (pytest xfail
also emits one); its id is ``classname::name``. A skipped test that is not
listed is a failure; a listed test that is not skipped is only reported. Every
unreadable input fails closed: an empty run, a missing path, malformed or
DTD-bearing XML, and a disabled file with a duplicate or reason-less entry.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass

import yaml

MAX_FILE = 64 * 1024 * 1024
_DTD = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)


class SkipsError(ValueError):
    """Bad input (exit 2): the check could not be made, which is never a pass."""


@dataclass(frozen=True)
class SkipReport:
    total: int
    skipped_unlisted: tuple[str, ...]
    unused_disabled: tuple[str, ...]


_ENCODING = re.compile(rb"^\s*<\?xml[^>]*?encoding\s*=\s*[\"']([^\"']+)[\"']")
_BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")


def _utf8_only(raw: bytes) -> bool:
    """Refuse UTF-16/32 (the DTD scan is byte-level) and any non-UTF-8/ASCII declaration."""
    if raw.startswith(_BOMS[1:]) or b"\x00" in raw[:4] or b"\x00" in raw:
        return False
    if raw.startswith(_BOMS[0]):
        raw = raw[3:]
    match = _ENCODING.match(raw)
    return match is None or match.group(1).lower() in (b"utf-8", b"utf8", b"us-ascii", b"ascii")


def _read(path: str, what: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE + 1)
    except OSError:
        raise SkipsError(f"cannot read {what}: {path}") from None
    if len(raw) > MAX_FILE:
        raise SkipsError(f"{what} too large: {path}")
    return raw


def load_disabled(path: str) -> frozenset[str]:
    raw = _read(path, "disabled-tests file")
    try:
        data = yaml.safe_load(raw)
    except (yaml.YAMLError, RecursionError):
        raise SkipsError(f"disabled-tests file is not valid YAML: {path}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict) or set(data) - {"disabled"}:
        raise SkipsError("disabled-tests file must be a mapping with only a 'disabled' list")
    entries = data.get("disabled") or []
    if not isinstance(entries, list):
        raise SkipsError("'disabled' must be a list")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"test", "reason"}:
            raise SkipsError("each disabled entry needs exactly 'test' and 'reason'")
        test, reason = entry["test"], entry["reason"]
        if not isinstance(test, str) or not test.strip() or "::" not in test:
            raise SkipsError("disabled 'test' must be '<classname>::<name>'")
        if not isinstance(reason, str) or not reason.strip():
            raise SkipsError(f"disabled test {test!r} has no reason")
        if test in seen:
            raise SkipsError(f"disabled test listed twice: {test!r}")
        seen.add(test)
    return frozenset(seen)


def _files(paths: Sequence[str]) -> list[str]:
    if not paths:
        raise SkipsError("no junit path given")
    out: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            found = sorted(os.path.join(path, n) for n in os.listdir(path) if n.endswith(".xml"))
            if not found:
                raise SkipsError(f"no junit files in {path}")
            out.extend(found)
        elif os.path.isfile(path):
            out.append(path)
        else:
            raise SkipsError(f"junit path not found: {path}")
    return out


def check_junit(paths: Sequence[str], disabled: frozenset[str]) -> SkipReport:
    total = 0
    skipped: set[str] = set()
    for path in _files(paths):
        raw = _read(path, "junit file")
        if not _utf8_only(raw):
            raise SkipsError(f"junit file is not UTF-8/ASCII: {path}")
        if _DTD.search(raw):
            raise SkipsError(f"junit file carries a DTD or entity declaration: {path}")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise SkipsError(f"junit file is not valid XML: {path}") from None
        for case in root.iter("testcase"):
            total += 1
            if case.find("skipped") is not None:
                skipped.add(f"{case.get('classname', '')}::{case.get('name', '')}")
    if total == 0:
        raise SkipsError("junit input contains no testcases")
    return SkipReport(
        total=total,
        skipped_unlisted=tuple(sorted(skipped - disabled)),
        unused_disabled=tuple(sorted(disabled - skipped)),
    )
