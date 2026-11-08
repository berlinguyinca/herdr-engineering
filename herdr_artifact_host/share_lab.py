"""Who may open a lab share: verified Cognito tokens checked against the share's allow-list.

The authenticator is a ``CognitoAuthenticator`` built with ``open_signup=True`` (any valid token of the pool and an
allow-listed client); the share's own lists decide. Both tokens are required: the access token (signature, issuer,
expiry, ``token_use=access``, client allow-list) names the user and carries ``cognito:groups``; the id token must be
ours, unexpired, carry a VERIFIED email and name the SAME user. Access is granted when that lower-cased email is in
``allow_emails`` or any token group is in ``allow_groups`` (exact, case-sensitive). Everything else is refused; garbage
input never raises.
"""

from __future__ import annotations

MAX_TOKEN_CHARS = 8192


def _token_ok(token) -> bool:
    return isinstance(token, str) and 0 < len(token) <= MAX_TOKEN_CHARS and token.isascii()


def authorise(state, access_token, id_token, authenticator) -> tuple[dict | None, str, str | None]:
    """``(grant, reason, identity)``: grant is ``{identity, email, exp, basis}`` or None with a fixed reason code; identity is
    the verified Cognito user once the access token checked out (for the audit actor), else None."""
    try:
        if not isinstance(state, dict) or state.get("audience") != "lab" or authenticator is None:
            return None, "audience", None
        emails, groups = state.get("allow_emails") or [], state.get("allow_groups") or []
        if not isinstance(emails, list) or not isinstance(groups, list):
            return None, "invalid_state", None
        # ASCII only: str.lower folds U+212A KELVIN SIGN to "k". create refuses non-ASCII entries, and
        # verify_id_token returns no email for a non-ASCII claim (that user is denied, groups included)
        allow_emails = {
            e.strip().lower() for e in emails if isinstance(e, str) and e.strip() and e.isascii()
        }
        allow_groups = {g for g in groups if isinstance(g, str) and g}
        if not _token_ok(access_token) or not _token_ok(id_token):
            return None, "bad_token", None
        claims = authenticator.verify_claims(access_token)
        if not claims:
            return None, "bad_access_token", None
        user, exp, token_groups = claims.get("user"), claims.get("exp"), claims.get("groups") or []
        if not isinstance(user, str) or not user or not isinstance(exp, float):
            return None, "bad_access_token", None
        email = authenticator.verify_id_token(id_token, user=user)
        if not isinstance(email, str) or not email:
            return None, "bad_id_token", user
        if email in allow_emails or any(
            isinstance(g, str) and g in allow_groups for g in token_groups
        ):
            basis = (
                "e" if email in allow_emails else "g"
            )  # the email list wins when both would admit
            return {"identity": user, "email": email, "exp": exp, "basis": basis}, "", user
        return None, "not_on_list", user
    except Exception:
        return None, "auth_error", None


def still_admitted(state: object, email: object, basis: object) -> bool:
    """Whether a session may still WRITE against the share's CURRENT allow-list, by how it was admitted.

    ``basis`` is the session's MAC'd admission basis. ``e`` (email): the lower-cased email must still be on
    ``allow_emails`` -- removing it stops the writes at once, whatever groups the share has. ``g`` (group claim): the
    session holds no group names and the tokens are gone, so the best check is that the share still has a non-empty
    ``allow_groups``; a member removed from a group keeps writing until the session expires (documented limit; the
    session is capped at the access token's ``exp``). Anything else, a malformed list or a non-lab state refuses.
    Never raises."""
    try:
        if (
            not isinstance(state, dict)
            or state.get("audience") != "lab"
            or not isinstance(email, str)
            or not email
        ):
            return False
        emails, groups = state.get("allow_emails") or [], state.get("allow_groups") or []
        if not isinstance(emails, list) or not isinstance(groups, list):
            return False
        if basis == "e":
            allowed = {
                e.strip().lower()
                for e in emails
                if isinstance(e, str) and e.strip() and e.isascii()
            }
            return email.strip().lower() in allowed
        if basis == "g":
            return any(isinstance(g, str) and g for g in groups)
        return False
    except Exception:
        return False


def authorise_lab(state: dict, access_token: str, id_token: str, authenticator) -> dict | None:
    """``{identity, email, exp, basis}`` when the verified tokens may open this lab share, else None (never raises)."""
    return authorise(state, access_token, id_token, authenticator)[0]
