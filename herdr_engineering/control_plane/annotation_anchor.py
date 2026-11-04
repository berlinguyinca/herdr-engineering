"""Pure validation of annotation anchors and comments (no I/O, no database).

Used identically by ``share-public`` and the worker. Everything refused raises ``AnnotationInputError``; nothing else escapes.
"""

from __future__ import annotations

import json
import math
import re
from typing import Final

from .share_bundle import ShareBundleError, normalise_name

MAX_BODY_BYTES: Final = 32768
MAX_COMMENT_CHARS: Final = 4000
MAX_LIMIT: Final = 10**9
MAX_EXACT_CHARS: Final = 1000
MAX_CONTEXT_CHARS: Final = 64
MAX_PAGE: Final = 100000
ANCHOR_KINDS: Final = ("document", "text_quote", "pdf_page", "image_region")
ANNOTATION_ID_RE: Final = re.compile(r"ann_[A-Za-z0-9_-]{22}")
ANNOTATION_DEFAULTS: Final[dict[str, bool | int]] = {
    "enabled": True,
    "per_user_per_minute": 60,
    "per_share_per_minute": 600,
    "global_per_minute": 3000,
    "per_user_per_day": 2000,
    "max_per_share": 20000,
    "max_comment_chars": 4000,
}

_CODES: Final = frozenset({"bad_anchor", "bad_file", "empty", "too_long", "bad_chars", "bad_id"})
_BOX = ("x", "y", "w", "h")
_ALLOWED_KEYS: Final[dict[str, frozenset[str]]] = {
    "document": frozenset({"kind", "file"}),
    "text_quote": frozenset({"kind", "file", "exact", "prefix", "suffix"}),
    "pdf_page": frozenset({"kind", "file", "page", "exact", "region"}),
    "image_region": frozenset({"kind", "file", *_BOX}),
}


class AnnotationInputError(ValueError):
    """A refused annotation input; ``code`` is one of the stable codes."""

    def __init__(self, code: str, message: str = "") -> None:
        if code not in _CODES:
            raise ValueError(f"unknown annotation error code: {code}")
        super().__init__(message or code)
        self.code = code


_OWNER_EMAIL: Final = re.compile(
    r"[!-?A-~]+@[!-?A-~]+\Z", re.ASCII
)  # the share store's rule: printable ASCII


def clean_owner_emails(raw: object) -> list[str]:
    """``control_plane.share.owner_emails`` as a sorted, de-duplicated list of lowercase ASCII emails; ``ValueError``
    when it is not a list of valid emails (None means none)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("control_plane.share.owner_emails must be a list of emails")
    out: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or len(item) > 254 or not _OWNER_EMAIL.match(item):
            raise ValueError("control_plane.share.owner_emails must be a list of ASCII emails")
        out.add(item.lower())
    return sorted(out)


def annotation_settings(raw: dict[str, object] | None) -> dict[str, bool | int]:
    """``control_plane.share.annotations`` merged over ``ANNOTATION_DEFAULTS``; raises ``ValueError`` when refused.

    Plain ``ValueError`` on purpose: this module imports no ``fleet_config``; the fleet-config layer wraps it."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("control_plane.share.annotations must be a mapping")
    unknown = sorted(str(k) for k in set(raw) - set(ANNOTATION_DEFAULTS))
    if unknown:
        raise ValueError("control_plane.share.annotations: unknown keys: " + ", ".join(unknown))
    out: dict[str, bool | int] = {**ANNOTATION_DEFAULTS, **raw}  # type: ignore[arg-type]
    if not isinstance(out["enabled"], bool):
        raise ValueError("control_plane.share.annotations.enabled must be true or false")
    for key, value in out.items():
        if key == "enabled":
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_LIMIT:
            raise ValueError(
                f"control_plane.share.annotations.{key} must be an integer from 1 to {MAX_LIMIT}"
            )
    if not 1 <= out["max_comment_chars"] <= MAX_COMMENT_CHARS:
        raise ValueError(
            f"control_plane.share.annotations.max_comment_chars must be 1-{MAX_COMMENT_CHARS}"
        )
    return out


def _bad_chars(text: str, *, allow_whitespace: bool) -> bool:
    for ch in text:
        o = ord(ch)
        if 0xD800 <= o <= 0xDFFF or o == 0 or o == 0x7F:
            return True
        if o < 0x20 and not (allow_whitespace and ch in "\n\t"):
            return True
    return False


def clean_comment(value: object, *, max_chars: int = MAX_COMMENT_CHARS) -> str:
    if not isinstance(value, str):
        raise AnnotationInputError("bad_anchor", "comment must be a string")
    if _bad_chars(value, allow_whitespace=True):
        raise AnnotationInputError("bad_chars", "comment has a control character or surrogate")
    text = value.strip()
    if not text:
        raise AnnotationInputError("empty", "comment is empty")
    if len(text) > min(max_chars, MAX_COMMENT_CHARS):
        raise AnnotationInputError("too_long", "comment is too long")
    return text


def _text_field(raw: dict[str, object], key: str, limit: int, *, required: bool) -> str | None:
    if key not in raw:
        if required:
            raise AnnotationInputError("bad_anchor", f"{key} is required")
        return None
    value = raw[key]
    if not isinstance(value, str):
        raise AnnotationInputError("bad_anchor", f"{key} must be a string")
    if _bad_chars(value, allow_whitespace=True):
        raise AnnotationInputError("bad_chars", f"{key} has a control character or surrogate")
    if required and not value:
        raise AnnotationInputError("bad_anchor", f"{key} is empty")
    if len(value) > limit:
        raise AnnotationInputError("too_long", f"{key} is too long")
    return value


def _unit(raw: dict[str, object], key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnnotationInputError("bad_anchor", f"{key} must be a number")
    if isinstance(value, int) and not 0 <= value <= 1:
        raise AnnotationInputError("bad_anchor", f"{key} must be within [0, 1]")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise AnnotationInputError("bad_anchor", f"{key} must be within [0, 1]")
    return number


def _box(raw: object) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise AnnotationInputError("bad_anchor", "region must be an object")
    out = {key: _unit(raw, key) for key in _BOX}
    if out["x"] + out["w"] > 1.0 or out["y"] + out["h"] > 1.0:
        raise AnnotationInputError("bad_anchor", "region extends beyond the page")
    return out


def clean_anchor(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AnnotationInputError("bad_anchor", "anchor must be an object")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in ANCHOR_KINDS:
        raise AnnotationInputError("bad_anchor", "unknown anchor kind")
    file = value.get("file")
    if not isinstance(file, str):
        raise AnnotationInputError("bad_file", "file must be a string")
    try:
        name = normalise_name(file)
    except ShareBundleError as exc:
        raise AnnotationInputError("bad_file", str(exc)) from exc
    raw = {k: v for k, v in value.items() if k in _ALLOWED_KEYS[kind]}
    out: dict[str, object] = {"kind": kind, "file": name}
    if kind == "text_quote":
        out["exact"] = _text_field(raw, "exact", MAX_EXACT_CHARS, required=True)
        out["prefix"] = _text_field(raw, "prefix", MAX_CONTEXT_CHARS, required=False) or ""
        out["suffix"] = _text_field(raw, "suffix", MAX_CONTEXT_CHARS, required=False) or ""
    elif kind == "pdf_page":
        page = raw.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= MAX_PAGE:
            raise AnnotationInputError(
                "bad_anchor", f"page must be an integer from 1 to {MAX_PAGE}"
            )
        out["page"] = page
        exact = (
            _text_field(raw, "exact", MAX_EXACT_CHARS, required=True) if "exact" in raw else None
        )
        if exact is not None:
            out["exact"] = exact
        if "region" in raw:
            out["region"] = _box(raw["region"])
    elif kind == "image_region":
        out.update(_box(raw))
    return out


def _load_object(raw: bytes) -> dict[str, object]:
    if not isinstance(raw, (bytes, bytearray)):
        raise AnnotationInputError("bad_anchor", "body must be bytes")
    if len(raw) > MAX_BODY_BYTES:
        raise AnnotationInputError("too_long", "request body is too large")
    try:
        doc = json.loads(bytes(raw).decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise AnnotationInputError("bad_anchor", "body is not valid JSON") from exc
    if not isinstance(doc, dict):
        raise AnnotationInputError("bad_anchor", "body must be a JSON object")
    return doc


def _only_keys(doc: dict[str, object], allowed: set[str]) -> None:
    if set(doc) - allowed:
        raise AnnotationInputError("bad_anchor", "unknown field in request")


def parse_annotation_request(
    raw: bytes, *, max_chars: int = MAX_COMMENT_CHARS
) -> tuple[dict[str, object], str]:
    doc = _load_object(raw)
    _only_keys(doc, {"anchor", "comment"})
    if "anchor" not in doc or "comment" not in doc:
        raise AnnotationInputError("bad_anchor", "anchor and comment are required")
    return clean_anchor(doc["anchor"]), clean_comment(doc["comment"], max_chars=max_chars)


def parse_edit_request(raw: bytes, *, max_chars: int = MAX_COMMENT_CHARS) -> str:
    doc = _load_object(raw)
    _only_keys(doc, {"comment"})
    if "comment" not in doc:
        raise AnnotationInputError("bad_anchor", "comment is required")
    return clean_comment(doc["comment"], max_chars=max_chars)


def parse_delete_request(raw: bytes) -> None:
    _only_keys(_load_object(raw), set())
