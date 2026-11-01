"""Rakazo sets a specialist up for the dashboard owner: no YAML, no token, no credential file.

The dashboard has one owner, and the collaboration service already knows them as ``operator_user`` (the same identity the
dashboard uses for rooms). Setup acts as that user, in-process, through the same room authorization every other caller
meets, and does three things: finds the agent the owner has enrolled for the specialist's type, finds or creates one private
room for it, and grants that agent only ``read`` and ``execute`` there. It binds nothing else and reads no personal source.
What it found is the binding: nothing is stored, so there is nothing to drift from the collaboration service.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from .assistant_cli import AssistantAPIError, AssistantClient, AssistantConfig
from .assistant_pi_delegate import HcnPiTaskTransport, PiSecretaryBinding, PiSecretaryDelegationAdapter

PROJECT_NAME = "Assistants"
ROOM_PREFIX = "Assistant: "
GRANT = ["execute", "read"]
PI_TYPE = "pi-secretary"


class SelfSetupError(RuntimeError):
    pass


class InProcessAssistantClient(AssistantClient):
    """An ``AssistantClient`` whose requests go straight to the collaboration service as its operator user."""

    def __init__(self, collab, operator: str):
        self.collab, self.operator = collab, operator
        self.config = AssistantConfig(base_url="http://127.0.0.1", token="in-process", auth_kind="user")

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        prefix = "/api/v1/collab/"
        if not path.startswith(prefix):
            raise ValueError("assistant API path must remain inside /api/v1/collab")
        parts = [p for p in path[len(prefix):].split("/") if p]
        status, payload = self.collab.dispatch(method, parts, {}, json.loads(json.dumps(body or {})), "", None,
                                               operator=self.operator)
        if not isinstance(payload, dict):
            raise AssistantAPIError(502, "unexpected collaboration response")
        if not 200 <= status < 300:
            raise AssistantAPIError(status, str(payload.get("error") or "request failed"))
        return payload


class SelfSetup:
    def __init__(self, collab, operator: str):
        self.client = InProcessAssistantClient(collab, operator)
        self._lock = threading.Lock()          # two setups at once must not both create the room

    @staticmethod
    def supports(type_name: str) -> bool:
        return type_name == PI_TYPE

    @staticmethod
    def room_name(type_name: str) -> str:
        return ROOM_PREFIX + type_name

    def _agent(self, type_name: str, *, strict: bool = False) -> dict | None:
        rows = self.client._request("GET", "/api/v1/collab/agents").get("agents", [])
        live = [a for a in rows if isinstance(a, dict) and a.get("name") == type_name and a.get("status") == "active"]
        if len(live) > 1 and strict:
            raise SelfSetupError(f"{len(live)} active agents are named {type_name}; keep one so setup can tell which to use")
        return live[0] if len(live) == 1 else None

    def _room(self, type_name: str) -> str | None:
        rows = self.client._request("GET", "/api/v1/collab/rooms").get("rooms", [])
        named = [r for r in rows if isinstance(r, dict) and r.get("name") == self.room_name(type_name)
                 and r.get("role") == "owner"]
        # Ids sort by creation time, so a duplicate (left by an earlier race) resolves to the first room, never to none.
        return sorted(r["room_id"] for r in named)[0] if named else None

    def find(self, type_name: str) -> dict[str, str] | None:
        """The binding that already exists: the owner's room and their agent, with the agent granted ``execute`` there."""
        try:
            agent, room = self._agent(type_name), self._room(type_name)
            if agent is None or room is None:
                return None
            granted = next((r for r in agent.get("rooms", []) if r.get("room_id") == room), None)
            if granted is None or "execute" not in granted.get("actions", []):
                return None
            return {"room_id": room, "agent_id": agent["agent_id"]}
        except AssistantAPIError:
            return None

    def provision(self, type_name: str) -> dict[str, Any]:
        """Idempotent. Raises SelfSetupError with a plain reason when something the owner must provide is missing."""
        if type_name != PI_TYPE:
            raise SelfSetupError(f"no setup is defined for {type_name}")
        with self._lock:
            return self._provision(type_name)

    def _provision(self, type_name: str) -> dict[str, Any]:
        try:
            agent = self._agent(type_name, strict=True)
            if agent is None:
                raise SelfSetupError(f"no active agent named {type_name} is enrolled for you; enroll it first")
            room = self._room(type_name)
            created = room is None
            if room is None:
                # collaboration lists no projects, so reuse the one holding the owner's other assistant rooms
                rooms = self.client._request("GET", "/api/v1/collab/rooms").get("rooms", [])
                project = next((r["project_id"] for r in rooms if isinstance(r, dict) and r.get("role") == "owner"
                                and str(r.get("name", "")).startswith(ROOM_PREFIX) and r.get("project_id")), None)
                if project is None:
                    project = self.client._request("POST", "/api/v1/collab/projects", {"name": PROJECT_NAME})["project_id"]
                room = self.client._request("POST", f"/api/v1/collab/projects/{project}/rooms",
                                            {"name": self.room_name(type_name)})["room_id"]
            self.client._request("POST", f"/api/v1/collab/rooms/{room}/agents",
                                 {"agent_id": agent["agent_id"], "actions": GRANT})
        except AssistantAPIError as exc:
            raise SelfSetupError(f"collaboration refused: {exc.message}") from None
        return {"room_id": room, "agent_id": agent["agent_id"], "created_room": created, "actions": GRANT}

    def resolver(self, is_owner, fallback=None):
        """``delegation_adapter_resolver``: the owner's setup for ``pi-secretary``; anything else goes to ``fallback``."""
        def resolve(tenant_id: str, user_id: str, target: str):
            if target == PI_TYPE and is_owner(tenant_id, user_id):
                found = self.find(PI_TYPE)
                if found is not None:
                    return PiSecretaryDelegationAdapter(
                        PiSecretaryBinding(room_id=found["room_id"], agent_id=found["agent_id"]),
                        HcnPiTaskTransport(self.client, found["agent_id"]))
            return fallback(tenant_id, user_id, target) if fallback else None
        return resolve
