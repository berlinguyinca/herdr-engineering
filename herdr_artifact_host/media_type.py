"""One strict reader for media types that arrive from uploaders and are later put in a response header.

A stored media type is attacker-controlled text. Browsers split a ``Content-Type`` on commas and take the last
type, so ``application/pdf;a=1,text/html`` is HTML to a browser while a server that looks at the part before the
first ``;`` sees a PDF. The rule is therefore: never send stored parameters. A response carries only the bare
``type/subtype`` this module returns, or ``application/octet-stream``. The send-time check (:func:`safe_media_type`)
does not depend on what was validated at upload time, so artifacts stored before the upload check existed are safe.
"""

from __future__ import annotations

import re

OCTET_STREAM = "application/octet-stream"
MAX_MEDIA_TYPE = 255
_TOKEN = r"[a-z0-9!#$&^_.+-]+"
_ESSENCE = re.compile(_TOKEN + "/" + _TOKEN)
_FORBIDDEN_IN_PARAMS = frozenset(',"\\')


def _has_control(text: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in text)


def media_type_essence(value: object) -> str | None:
    """The lowercase bare ``type/subtype`` of ``value``, or None when it is not a clean token pair.

    Parameters after the first ``;`` are ignored (never returned); everything before it must be exactly two tokens
    around one slash, so a comma, quote, backslash, space or control character in the type part gives None."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_MEDIA_TYPE
        or _has_control(value)
    ):
        return None
    head = value.split(";", 1)[0].strip(" \t").lower()
    return head if _ESSENCE.fullmatch(head) else None


def safe_media_type(value: object) -> str:
    """The bare media type that is safe to send, else ``application/octet-stream``."""
    return media_type_essence(value) or OCTET_STREAM


def canonical_upload_media_type(value: object) -> str | None:
    """The media type to store for an upload, or None when the upload must be refused.

    Refused: a non-string, empty or over-long value, control characters, a type part that is not two tokens, and
    any comma, quote or backslash anywhere (those are what turn parameters into a second type). Harmless parameters
    (``; charset=utf-8``) are accepted and dropped: the canonical value is the bare lowercase ``type/subtype``."""
    essence = media_type_essence(value)
    if essence is None:
        return None
    assert isinstance(value, str)
    if _FORBIDDEN_IN_PARAMS & set(value):
        return None
    return essence
