"""Enrollment config for the review gate (``control_plane.review_gate``).

The private key is only ever stat()ed here; its contents are never read.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass

from herdr_engineering.control_plane.project_links import canonical_repository

_SECTION_KEYS = {"enabled", "app", "repos", "poll_seconds"}
_APP_KEYS = {"app_id", "installation_id", "private_key_file"}
_REPO_KEYS = {
    "repo", "required_events", "required_checks", "main_pipeline",
    "disabled_tests_file", "review_cap", "ci_fix_cap",
}


@dataclass(frozen=True)
class RepoEnrollment:
    repo: str
    required_events: tuple[str, ...] = ("pull_request",)
    required_checks: tuple[str, ...] = ()
    main_pipeline: str | None = None
    disabled_tests_file: str | None = None
    review_cap: int = 4
    ci_fix_cap: int = 3


@dataclass(frozen=True)
class AppConfig:
    app_id: int
    installation_id: int
    private_key_file: str


@dataclass(frozen=True)
class ReviewGateConfig:
    enabled: bool
    app: AppConfig | None
    repos: tuple[RepoEnrollment, ...]
    poll_seconds: int = 15


def _mapping(raw, where):
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be a mapping")
    return raw


def _no_unknown(raw, allowed, where):
    for key in raw:
        if key not in allowed:
            raise ValueError(f"{where}: unknown key {key!r}")


def _int(raw, key, where, minimum):
    val = raw[key]
    if isinstance(val, bool) or not isinstance(val, int) or val < minimum:
        raise ValueError(f"{where}: {key} must be an integer >= {minimum}")
    return val


def _str_tuple(raw, key, where):
    val = raw[key]
    if not isinstance(val, list) or not all(isinstance(v, str) and v for v in val):
        raise ValueError(f"{where}: {key} must be a list of non-empty strings")
    return tuple(val)


def _opt_str(raw, key, where):
    val = raw.get(key)
    if val is not None and (not isinstance(val, str) or not val):
        raise ValueError(f"{where}: {key} must be a non-empty string")
    return val


def _app(raw, check_key_file):
    raw = _mapping(raw, "review_gate.app")
    _no_unknown(raw, _APP_KEYS, "review_gate.app")
    for key in _APP_KEYS:
        if key not in raw:
            raise ValueError(f"review_gate.app: {key} is required")
    app_id = _int(raw, "app_id", "review_gate.app", 1)
    installation_id = _int(raw, "installation_id", "review_gate.app", 1)
    path = raw["private_key_file"]
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("review_gate.app: private_key_file must be an absolute path")
    if check_key_file:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            raise ValueError("private_key_file must exist and be mode 0600") from None
        if mode != 0o600:
            raise ValueError("private_key_file must be mode 0600")
    return AppConfig(app_id, installation_id, path)


def _repo(raw):
    raw = _mapping(raw, "review_gate.repos entry")
    _no_unknown(raw, _REPO_KEYS, "review_gate.repos entry")
    canon = canonical_repository(raw.get("repo"))
    if not canon or not canon.startswith("github.com/") or canon.count("/") != 2:
        raise ValueError("review_gate.repos: repo must be a github.com/<owner>/<name> repository")
    kw = {"repo": canon}
    for key in ("required_events", "required_checks"):
        if key in raw:
            kw[key] = _str_tuple(raw, key, "review_gate.repos")
    for key in ("main_pipeline", "disabled_tests_file"):
        kw[key] = _opt_str(raw, key, "review_gate.repos")
    for key in ("review_cap", "ci_fix_cap"):
        if key in raw:
            kw[key] = _int(raw, key, "review_gate.repos", 1)
    return RepoEnrollment(**kw)


def parse_review_gate_section(raw, *, check_key_file=True) -> ReviewGateConfig:
    """Validate a ``review_gate`` mapping; raises ValueError naming the problem."""
    raw = _mapping(raw, "review_gate")
    _no_unknown(raw, _SECTION_KEYS, "review_gate")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("review_gate: enabled must be a boolean")
    poll = _int(raw, "poll_seconds", "review_gate", 5) if "poll_seconds" in raw else 15
    app = _app(raw["app"], check_key_file) if raw.get("app") is not None else None
    repos_raw = raw.get("repos", [])
    if not isinstance(repos_raw, list):
        raise ValueError("review_gate: repos must be a list")
    repos = tuple(_repo(r) for r in repos_raw)
    names = [r.repo for r in repos]
    if len(set(names)) != len(names):
        raise ValueError("review_gate.repos: duplicate repo")
    if enabled and app is None:
        raise ValueError("review_gate: enabled requires app")
    return ReviewGateConfig(enabled=enabled, app=app, repos=repos, poll_seconds=poll)


def load_review_gate_config(cfg: dict) -> ReviewGateConfig:
    section = (cfg.get("control_plane") or {}).get("review_gate")
    if section is None:
        return ReviewGateConfig(enabled=False, app=None, repos=())
    return parse_review_gate_section(section)
