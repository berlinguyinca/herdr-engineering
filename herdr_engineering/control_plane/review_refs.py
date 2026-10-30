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
    """``None`` stays ``None``; otherwise ``owner/name`` or ``host/owner/name`` or ``ValueError``.

    A value with a forge host must be one ``canonical_repository`` understands (the same reading the PR trail uses), so what is stored
    compares the same way later. A value without a host is exactly ``owner/name``: no ``:`` and no extra segments.
    """
    if value is None:
        return None
    ok = isinstance(value, str) and len(value) <= 200 and _REPOSITORY.fullmatch(value) and not ({".", ".."} & set(value.split("/")))
    if ok:
        parts = value.split("/")
        if len(parts) == 2:
            ok = ":" not in parts[0] and "." not in parts[0]   # "x.y/z" would be read as host x.y, repository z
        elif len(parts) == 3:
            from .project_links import canonical_repository
            ok = canonical_repository(value) is not None
        else:
            ok = False
    if not ok:
        raise ValueError("repository must be a string like owner/name or host/owner/name")
    return value


def same_repository(a, b):
    """Whether two repository identities name the same repository, compared exactly as the PR trail compares them."""
    from .pr_trail import same_repository as trail_same
    return trail_same(a, b)


def keep_reference(stored_repository, stored_number, repository_in, number_in):
    """The reference after an update. A stored value is never changed or moved: only ``None`` -> value is allowed, anything else that
    differs raises ``ValueError`` (a review that named a pull request keeps naming that one)."""
    if repository_in is not None and stored_repository is not None and not same_repository(stored_repository, repository_in):
        raise ValueError("review already names another repository")
    if number_in is not None and stored_number is not None and number_in != stored_number:
        raise ValueError("review already names another pull request")
    return (stored_repository if stored_repository is not None else repository_in,
            stored_number if stored_number is not None else number_in)
