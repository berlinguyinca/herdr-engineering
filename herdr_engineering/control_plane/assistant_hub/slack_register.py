"""Slack token registration: write both token files safely, prove them with a real call, never echo them.

Modeled on ``judge_register``: each token is written to ``PATH.new`` (``O_EXCL``, mode 0600), the bot token is proven
with ``auth.test`` and the app token with ``apps.connections.open`` (plain HTTP, stdlib only), and only then are the files
moved into place; a failure removes both ``.new`` files and leaves existing files untouched.  Errors carry a short code and
never a response body or a token.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .judge_register import (
    MAX_KEY_CHARS,
    RegisterError,
    _check_endpoint,
    _check_target,
    _unlink,
    _write_new,
    fingerprint,
)

__all__ = ["MAX_KEY_CHARS", "SlackRegisterError", "SlackRegistered", "fingerprint", "register_tokens"]

BOT_PREFIX = "xoxb-"
APP_PREFIX = "xapp-"
_MAX_REPLY = 1024 * 1024


class SlackRegisterError(Exception):
    """A refusal with a short code: bad_token, config, bad_path, unsafe_dir, symlink, io, auth_failed (or tmp_exists)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SlackRegistered:
    bot_fingerprint: str
    app_fingerprint: str
    team: str
    bot_user_id: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:     # a bearer token must not follow a redirect
        return None


def _check_token(token: str, prefix: str) -> str:
    token = token.strip()
    if (not token.startswith(prefix) or len(token) > MAX_KEY_CHARS or not token.isascii() or not token.isprintable()
            or any(c.isspace() for c in token)):
        raise SlackRegisterError("bad_token")
    return token


def _call(base_url: str, method: str, token: str, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(base_url.rstrip("/") + "/" + method, method="POST", data=b"", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/x-www-form-urlencoded"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as response:
            body = response.read(_MAX_REPLY)
    except urllib.error.HTTPError as exc:
        body = exc.read(_MAX_REPLY)
    except (OSError, ValueError):
        raise SlackRegisterError("io") from None
    try:
        doc = json.loads(body)
    except ValueError:
        raise SlackRegisterError("auth_failed") from None
    if not isinstance(doc, dict) or doc.get("ok") is not True:
        raise SlackRegisterError("auth_failed")
    return doc


def _target(path: Path) -> bool:
    try:
        return _check_target(path)
    except RegisterError as exc:
        raise SlackRegisterError(exc.code) from None


def _stage(tmp: Path, token: str) -> None:
    try:
        _write_new(tmp, token)
    except RegisterError as exc:
        raise SlackRegisterError(exc.code) from None


def _install(tmp: Path, path: Path, rotating: bool) -> None:
    try:
        if rotating:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)          # fails if a file appeared meanwhile: never clobbers a concurrent create
            os.unlink(tmp)
    except OSError:
        raise SlackRegisterError("io") from None


def register_tokens(base_url: str, bot_file: str, app_file: str, bot_token: str, app_token: str,
                    timeout: float = 10.0) -> SlackRegistered:
    bot_token = _check_token(bot_token, BOT_PREFIX)
    app_token = _check_token(app_token, APP_PREFIX)
    try:
        _check_endpoint(base_url)
    except RegisterError:
        raise SlackRegisterError("config") from None
    bot_path, app_path = Path(bot_file), Path(app_file)
    if bot_path == app_path:
        raise SlackRegisterError("bad_path")
    bot_rot, app_rot = _target(bot_path), _target(app_path)
    bot_tmp = bot_path.with_name(bot_path.name + ".new")
    app_tmp = app_path.with_name(app_path.name + ".new")
    try:
        _stage(bot_tmp, bot_token)
        _stage(app_tmp, app_token)
        who = _call(base_url, "auth.test", bot_token, timeout)
        _call(base_url, "apps.connections.open", app_token, timeout)
        _install(bot_tmp, bot_path, bot_rot)
        _install(app_tmp, app_path, app_rot)
    except BaseException:
        _unlink(bot_tmp)
        _unlink(app_tmp)
        raise
    team = who.get("team") if isinstance(who.get("team"), str) else ""
    user_id = who.get("user_id") if isinstance(who.get("user_id"), str) else ""
    return SlackRegistered(fingerprint(bot_token), fingerprint(app_token), team, user_id)
