"""Google OAuth web-flow pieces: scopes, authorization URL, code exchange, profile lookup and revoke.

Reuses the PKCE and form-post helpers of ``google_workspace``. Nothing here logs or raises with a token, code or secret;
failures surface as ``GoogleError`` with a fixed reason.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from herdr_engineering.control_plane.google_workspace import (
    AUTH_URL,
    GMAIL,
    MAX_RESPONSE,
    TIMEOUT,
    TOKEN_URL,
    GoogleError,
    _post_form,
    _urlopen,
    new_pkce,
)

__all__ = ["SCOPES", "REVOKE_URL", "new_pkce", "authorization_url", "exchange_code", "account_address", "revoke", "_urlopen"]

SCOPES = ("https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose",
          "https://www.googleapis.com/auth/calendar.readonly", "https://www.googleapis.com/auth/calendar.events")
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


def authorization_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256"})


def exchange_code(opener: Callable, client: tuple[str, str], code: str, verifier: str, redirect_uri: str) -> dict:
    """Raw token response (``refresh_token``, ``access_token``, ``scope``); the caller judges scopes."""
    data = _post_form(opener, TOKEN_URL, {"client_id": client[0], "client_secret": client[1], "code": code,
                                          "code_verifier": verifier, "redirect_uri": redirect_uri,
                                          "grant_type": "authorization_code"})
    return data if isinstance(data, dict) else {}


def account_address(opener: Callable, access_token: str) -> str:
    req = urllib.request.Request(f"{GMAIL}/profile", method="GET",     # noqa: S310
                                 headers={"authorization": f"Bearer {access_token}", "accept": "application/json"})
    try:
        with opener(req, TIMEOUT) as resp:
            data = json.loads(resp.read(MAX_RESPONSE))
    except urllib.error.HTTPError as exc:
        exc.close()
        raise GoogleError("unavailable") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise GoogleError("unavailable") from None
    address = data.get("emailAddress") if isinstance(data, dict) else None
    if not isinstance(address, str) or not address:
        raise GoogleError("unavailable")
    return address


def revoke(opener: Callable, token: str) -> None:
    """Best effort. Google answers with an empty body, which the form helper counts as unavailable; ignored either way."""
    try:
        _post_form(opener, REVOKE_URL, {"token": token})
    except GoogleError:
        pass
