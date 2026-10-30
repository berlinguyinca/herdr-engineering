"""Telegram adapter (Bot API, long polling, outbound only). Only chats on the operator's allowlist are bridged."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from .relay import Inbound

CHUNK = 4000                    # Telegram caps a message at 4096 characters


class TelegramError(Exception):
    pass


def bot_api(token: str, method: str, params: dict[str, Any], timeout: float) -> Any:
    """One Bot API call. The URL holds the token, so no error ever repeats it."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https host
            data = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise TelegramError(f"{method} failed ({type(exc).__name__})") from None
    if not isinstance(data, dict) or not data.get("ok"):
        raise TelegramError(f"{method} was refused")
    return data["result"]


class Telegram:
    name = "telegram"

    def __init__(self, token: str, chat_ids: list[int], *, api: Callable[..., Any] = bot_api):
        if not token or not chat_ids:
            raise ValueError("telegram needs a token and at least one allowed chat id")
        self._token, self._chats, self._api, self._offset = token, frozenset(chat_ids), api, 0

    def poll(self, timeout: float) -> list[Inbound]:
        updates = self._api(self._token, "getUpdates", {"offset": self._offset, "timeout": int(timeout),
                                                        "allowed_updates": json.dumps(["message"])}, timeout + 10)
        out = []
        for u in updates:
            self._offset = max(self._offset, int(u["update_id"]) + 1)
            m = u.get("message") or {}
            chat, text = (m.get("chat") or {}).get("id"), m.get("text")
            if chat in self._chats and isinstance(text, str) and text.strip():
                who = m.get("from") or {}
                out.append(Inbound(f"{u['update_id']}", str(who.get("username") or who.get("first_name") or "someone")[:40],
                                   text))
        return out

    def send(self, text: str) -> None:
        for chat in self._chats:
            for i in range(0, len(text), CHUNK):
                self._api(self._token, "sendMessage", {"chat_id": chat, "text": text[i:i + CHUNK]}, 15)
