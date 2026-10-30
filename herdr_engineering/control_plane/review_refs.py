"""The pull request a review names, as an optional reference carried by every kind of review record.

Absent means unknown, never zero: a review that does not name a pull request is attached to one only through the agent
session that wrote it. Nothing here guesses a pull request from a branch or a revision.
"""
from __future__ import annotations

import re

MAX_PULL_REQUEST_NUMBER = 2_147_483_647
_REPOSITORY = re.compile(r"[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.-]+){1,3}")


def pull_request_number(value):
    """``None`` stays ``None``; otherwise a positive integer (not a bool, not a string) or ``ValueError``."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PULL_REQUEST_NUMBER:
        raise ValueError(f"pull_request_number must be an integer from 1 to {MAX_PULL_REQUEST_NUMBER}")
    return value


def repository(value):
    """``None`` stays ``None``; otherwise a bounded ``[host/]owner/name`` string or ``ValueError``."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 200 or not _REPOSITORY.fullmatch(value) or {".", ".."} & set(value.split("/")):
        raise ValueError("repository must be a string like owner/name or host/owner/name")
    return value
