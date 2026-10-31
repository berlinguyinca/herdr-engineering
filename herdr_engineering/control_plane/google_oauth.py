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
    MAX_RESPONSE,
    TIMEOUT,
    TOKEN_URL,
    GoogleError,
    _post_form,
    _urlopen,
    error_detail,
    new_pkce,
)


def default_opener(req, timeout):
    return _urlopen(req, timeout)


__all__ = ["SCOPES", "GMAIL_SCOPES", "CALENDAR_SCOPES", "IDENTITY_SCOPES", "normalize_scopes", "REVOKE_URL", "new_pkce", "authorization_url", "exchange_authorization_code", "account_address", "revoke", "default_opener"]

# Requested scopes: who signed in (OpenID email), the calendar, and Gmail read + compose (compose also allows sending, which the
# assistant only does after a person approved the exact message). gmail.* are RESTRICTED scopes: the OAuth client stays
# unverified (up to 100 users, a one-time warning screen) rather than paying for Google's annual security assessment.
IDENTITY_SCOPES = ("openid", "https://www.googleapis.com/auth/userinfo.email")
CALENDAR_SCOPES = ("https://www.googleapis.com/auth/calendar.readonly", "https://www.googleapis.com/auth/calendar.events")
GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose")
SCOPES = IDENTITY_SCOPES + CALENDAR_SCOPES + GMAIL_SCOPES
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def normalize_scopes(granted) -> set[str]:
    """Google may report the short form 'email' for the userinfo.email scope."""
    return {"https://www.googleapis.com/auth/userinfo.email" if x == "email" else x for x in granted}
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


def authorization_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256"})


def exchange_authorization_code(opener: Callable, client: tuple[str, str], code: str, verifier: str, redirect_uri: str) -> dict:
    """Raw token response (``refresh_token``, ``access_token``, ``scope``); the caller judges scopes."""
    data = _post_form(opener, TOKEN_URL, {"client_id": client[0], "client_secret": client[1], "code": code,
                                          "code_verifier": verifier, "redirect_uri": redirect_uri,
                                          "grant_type": "authorization_code"})
    return data if isinstance(data, dict) else {}


def account_address(opener: Callable, access_token: str) -> str:
    req = urllib.request.Request(USERINFO_URL, method="GET",     # noqa: S310
                                 headers={"authorization": f"Bearer {access_token}", "accept": "application/json"})
    try:
        with opener(req, TIMEOUT) as resp:
            data = json.loads(resp.read(MAX_RESPONSE))
    except urllib.error.HTTPError as exc:
        detail = error_detail(exc)
        exc.close()
        raise GoogleError("unavailable", detail) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise GoogleError("unavailable", "network") from None
    address = data.get("email") if isinstance(data, dict) and data.get("email_verified") is not False else None
    if not isinstance(address, str) or not address:
        raise GoogleError("unavailable")
    return address


def revoke(opener: Callable, token: str) -> None:
    """Best effort. Google answers with an empty body, which the form helper counts as unavailable; ignored either way."""
    try:
        _post_form(opener, REVOKE_URL, {"token": token})
    except GoogleError:
        pass
