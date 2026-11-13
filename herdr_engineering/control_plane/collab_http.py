"""The collaboration calls the Rakazo rooms make, answered over HTTP by the collab service's operator listener.

When collaboration runs as its own service (``--collab-operator-url``), the dashboard process has no in-process
``CollabService``. ``HttpCollab`` has the same method names and return shapes the rooms code uses, and sends each call to
``<operator url>/api/v1/collab/...``. That listener acts as ONE user (the operator) with no credential, so this client is
single-tenant: it serves only the principal whose id is the operator's and refuses every other collab user.

Rules: no retries (a lost reply to ``post_message`` is reported, never repeated); a service that cannot be reached is a
503 ``CollabError``; the service's own refusals keep their status (404 stays 404); a route the service does not have is
the service's 404, never guessed around. ``read_tail`` and ``room_people`` use the service's side-effect-free
``events/tail`` and ``people`` routes, so the collab service must be deployed together with this client.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .collab import CollabError, Principal

BASE = "/api/v1/collab"
TIMEOUT = 15.0
MAX_BODY = 4_000_000
OPERATOR_TTL = 60.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                      # the answer is the 3xx itself, which _request refuses


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)   # never via an environment proxy


class HttpCollab:
    def __init__(self, operator_url: str, *, timeout: float = TIMEOUT):
        self.url = operator_url.rstrip("/")
        self.timeout = timeout
        self._operator: tuple[str, float] | None = None
        self.requests = 0                    # calls made, for tests and diagnostics
        self._lock = threading.Lock()

    # transport -----------------------------------------------------------------------------------------------------
    def _request(self, method: str, path: str, query: dict[str, Any] | None = None,
                 body: dict[str, Any] | None = None, *, unknown_outcome: str | None = None) -> tuple[int, Any]:
        self.requests += 1
        url = self.url + BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data is not None else {})
        try:
            try:
                with _OPENER.open(req, timeout=self.timeout) as resp:
                    status, raw = resp.status, resp.read(MAX_BODY)
            except urllib.error.HTTPError as exc:
                status, raw = exc.code, exc.read(MAX_BODY)
        except Exception:                                                        # noqa: BLE001 - refused, timed out, DNS
            raise CollabError(503, "collaboration service unavailable" + (f"; {unknown_outcome}" if unknown_outcome else "")) from None
        if status >= 500 and status not in (502, 503) or 300 <= status < 400:
            raise CollabError(502, "collaboration service answered unexpectedly")
        try:
            out = json.loads(raw) if raw else {}
        except ValueError:
            raise CollabError(502, "collaboration service sent an unreadable answer") from None
        if status >= 400:
            err = out if isinstance(out, dict) else {}
            raise CollabError(status, str(err.get("error") or "request refused"), err.get("reason"), err.get("code"))
        return status, out

    @staticmethod
    def _seg(*parts: str) -> str:
        return "".join("/" + urllib.parse.quote(str(p), safe="") for p in parts)

    # single tenant -------------------------------------------------------------------------------------------------
    def operator_user(self) -> str:
        """The user the service's operator listener acts as (cached briefly). Raises 503 when the service is down."""
        with self._lock:
            if self._operator and time.monotonic() < self._operator[1]:
                return self._operator[0]
        _, me = self._request("GET", "/me")
        user = me.get("user_id") if isinstance(me, dict) else None
        if not isinstance(user, str) or not user:
            raise CollabError(502, "collaboration service did not name its operator")
        with self._lock:
            self._operator = (user, time.monotonic() + OPERATOR_TTL)
        return user

    def acts_for(self, user: str) -> bool:
        """Whether ``user`` may be linked. An operator not yet known (service down) does not unlink; calls still refuse."""
        with self._lock:
            known = self._operator[0] if self._operator else None
        return known is None or known == user

    def _only(self, pr: Principal) -> None:
        if pr.id != self.operator_user():
            raise CollabError(403, "collaboration over HTTP acts only as the service operator")

    def _get(self, pr: Principal, path: str, query: dict[str, Any] | None = None) -> Any:
        self._only(pr)
        return self._request("GET", path, query)[1]

    def _post(self, pr: Principal, path: str, body: dict[str, Any] | None = None) -> Any:
        self._only(pr)
        return self._request("POST", path, None, body)[1]

    # reads ---------------------------------------------------------------------------------------------------------
    def still_valid(self, pr: Principal) -> bool:
        try:
            return pr.id == self.operator_user()
        except CollabError as exc:
            if exc.status in (502, 503):
                raise
            return False

    def list_my_agents(self, pr):
        return self._get(pr, "/agents")

    def list_rooms(self, pr):
        return self._get(pr, "/rooms")

    def list_room_agents(self, pr, room_id):
        return self._get(pr, self._seg("rooms", room_id) + "/agents")

    def read_events(self, pr, room_id, cursor, limit):
        return self._get(pr, self._seg("rooms", room_id) + "/events", {"cursor": cursor, "limit": limit})

    def read_tail(self, pr, room_id, limit):
        return self._get(pr, self._seg("rooms", room_id) + "/events/tail", {"limit": limit})

    def room_people(self, pr, room_id):
        return self._get(pr, self._seg("rooms", room_id) + "/people")["people"]

    def list_steering(self, pr, room_id, query):
        return self._get(pr, self._seg("rooms", room_id) + "/steering", query or None)

    def list_proposals(self, pr, room_id):
        return self._get(pr, self._seg("rooms", room_id) + "/proposals")

    def get_proposal(self, pr, room_id, proposal_id):
        return self._get(pr, self._seg("rooms", room_id, "proposals", proposal_id))

    # writes (never retried) ----------------------------------------------------------------------------------------
    def create_project(self, pr, body):
        return self._post(pr, "/projects", body)

    def create_room(self, pr, project_id, body):
        return self._post(pr, self._seg("projects", project_id) + "/rooms", body)

    def grant_agent(self, pr, room_id, body):
        return self._post(pr, self._seg("rooms", room_id) + "/agents", body)

    def remove_agent_from_room(self, pr, room_id, agent_id):
        return self._post(pr, self._seg("rooms", room_id, "agents", agent_id) + "/remove")

    def revoke_external(self, pr, room_id, agent_id):
        return self._post(pr, self._seg("rooms", room_id, "externals", agent_id) + "/revoke")

    def post_message(self, pr, room_id, body):
        self._only(pr)
        return self._request("POST", self._seg("rooms", room_id) + "/messages", None, body,
                             unknown_outcome="the message may or may not have been posted; check the room before sending again")

    def confirm_steering(self, pr, room_id, steer_id, body):
        return self._post(pr, self._seg("rooms", room_id, "steering", steer_id) + "/confirm", body)

    def reject_steering(self, pr, room_id, steer_id, body):
        return self._post(pr, self._seg("rooms", room_id, "steering", steer_id) + "/reject", body)

    def decide_proposal(self, pr, room_id, proposal_id, body):
        return self._post(pr, self._seg("rooms", room_id, "proposals", proposal_id) + "/decide", body)
