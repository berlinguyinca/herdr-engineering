"""Event contact research records and local form proposals. No network or submission.

A trusted adapter attests identity and provenance; model/page-provided booleans are
not attestations. These domain checks supplement that adapter, never replace it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from .event_coordinator import Coordinator, Venue, _email, _line, _time


def _https(value: str) -> str:
    _line(value)
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(c.isspace() for c in value)):
        raise ValueError("query-free HTTPS contact/source URL required")
    # Force validation of malformed ports while parsing, without resolving a host.
    _ = parsed.port
    return value


def _origin(value: str) -> tuple[str, int]:
    parsed = urlsplit(_https(value))
    return parsed.hostname.lower(), parsed.port or 443


@dataclass(frozen=True)
class ContactRoute:
    channel: str
    target: str

    def __post_init__(self):
        if self.channel == "email":
            object.__setattr__(self, "target", _email(self.target))
        elif self.channel == "form":
            _https(self.target)
        else:
            raise ValueError("contact route must be email or form")


@dataclass(frozen=True)
class ContactSnapshot:
    user_id: str
    task_id: str
    venue_id: str
    resource_ref: str
    official_origin: str
    source_url: str
    observed_at: float
    valid_until: float
    identity_verified: bool
    routes: tuple[ContactRoute, ...]

    def __post_init__(self):
        for value in (self.user_id, self.task_id, self.venue_id, self.resource_ref):
            _line(value)
        _https(self.official_origin)
        parsed = urlsplit(self.official_origin)
        if parsed.path not in {"", "/"} or parsed.query:
            raise ValueError("official origin must not contain path or query")
        _https(self.source_url)
        _time(self.observed_at)
        _time(self.valid_until)
        if self.valid_until <= self.observed_at or type(self.identity_verified) is not bool:
            raise ValueError("explicit identity and verification window required")
        if not isinstance(self.routes, tuple) or any(not isinstance(r, ContactRoute) for r in self.routes):
            raise ValueError("immutable structured contact routes required")


@dataclass(frozen=True)
class RouteSelection:
    route: ContactRoute | None
    blockers: tuple[str, ...]


def select_route(coordinator: Coordinator, venue: Venue, snapshot: ContactSnapshot,
                 now: float) -> RouteSelection:
    """Choose only among scoped, fresh routes from a trusted verified observation."""
    _time(now)
    if ((snapshot.user_id, snapshot.task_id, snapshot.venue_id)
            != (coordinator.user_id, coordinator.task_id, venue.id)
            or venue.id not in coordinator.venue_ids or snapshot.resource_ref not in coordinator.resource_refs):
        raise ValueError("contact observation outside selected delegation scope")
    blockers = []
    history = [r for r in coordinator.receipts if r.venue_id == venue.id]
    if venue.id in coordinator.stopped_venues or any(r.status == "bounced" for r in history):
        blockers.append("venue_contact_stopped")
    if venue.id in coordinator.replied_venues:
        blockers.append("reply_received")
    if any(r.status == "uncertain" for r in history):
        blockers.append("delivery_uncertain")
    if any(r.status == "sent" for r in history):
        blockers.append("venue_already_contacted")
    if venue.identity != "verified" or not snapshot.identity_verified:
        blockers.append("contact_identity_unverified")
    if not snapshot.observed_at <= now < snapshot.valid_until:
        blockers.append("contact_observation_stale")
    if _origin(snapshot.source_url) != _origin(snapshot.official_origin):
        blockers.append("contact_source_cross_origin")
    if blockers:
        return RouteSelection(None, tuple(blockers))
    emails = tuple(dict.fromkeys(r for r in snapshot.routes if r.channel == "email"))
    if len(emails) == 1:
        return RouteSelection(emails[0], ())
    if len(emails) > 1:
        return RouteSelection(None, ("contact_route_ambiguous",))
    forms = tuple(dict.fromkeys(r for r in snapshot.routes if r.channel == "form"))
    if any(_origin(r.target) != _origin(snapshot.official_origin) for r in forms):
        return RouteSelection(None, ("contact_form_cross_origin",))
    if len(forms) != 1:
        return RouteSelection(None, ("contact_route_ambiguous" if forms else "contact_route_missing",))
    return RouteSelection(forms[0], ())


@dataclass(frozen=True)
class FormField:
    name: str
    semantic: str
    required: bool
    visible: bool = True

    def __post_init__(self):
        _line(self.name)
        _line(self.semantic)
        if type(self.required) is not bool or type(self.visible) is not bool:
            raise ValueError("explicit form field flags required")


@dataclass(frozen=True)
class FormProposal:
    user_id: str
    task_id: str
    venue_id: str
    resource_ref: str
    source_url: str
    observed_at: float
    target: str | None
    values: tuple[tuple[str, str], ...]
    blockers: tuple[str, ...]

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, allow_nan=False).encode()).hexdigest()


def prepare_form(coordinator: Coordinator, venue: Venue, snapshot: ContactSnapshot,
                 fields: tuple[FormField, ...], now: float, *, requester_email: str,
                 requester_name: str | None = None) -> FormProposal:
    """Prepare a local semantic field map, never interact with a real form."""
    email = _email(requester_email)
    if requester_name is not None:
        _line(requester_name)
    if not isinstance(fields, tuple) or any(not isinstance(f, FormField) for f in fields):
        raise ValueError("immutable semantic form fields required")
    if len({f.name for f in fields}) != len(fields):
        raise ValueError("duplicate form field name")
    selection = select_route(coordinator, venue, snapshot, now)
    blockers = list(selection.blockers)
    values = []
    route = selection.route
    if route and route.channel != "form":
        blockers.append("email_route_preferred")
    if route and route.channel == "form":
        draft = coordinator.draft(venue, email)
        allowed = {**asdict(coordinator.requirements), "email": email, "name": requester_name,
                   "subject": draft.subject,
                   "message": draft.body if not coordinator.requirements.missing() else None}
        for field in fields:
            if not field.visible:
                if field.required:
                    blockers.append(f"hidden_required_field:{field.name}")
            elif field.semantic not in allowed:
                if field.required:
                    blockers.append(f"unsupported_required_field:{field.name}")
            elif allowed[field.semantic] is None:
                if field.required:
                    blockers.append(f"missing_required_field:{field.name}")
            else:
                values.append((field.name, str(allowed[field.semantic])))
    blockers.extend(("external_write_capability_unbound", "outreach_approval_required"))
    return FormProposal(coordinator.user_id, coordinator.task_id, venue.id, snapshot.resource_ref,
                        snapshot.source_url, snapshot.observed_at,
                        route.target if route and route.channel == "form" else None,
                        tuple(values), tuple(blockers))
