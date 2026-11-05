"""Loopback control-plane fixture for the operator Members panel (Playwright).

The browser talks to the production HTTP handlers (``ControlPlaneServer`` + ``artifact_members_api.route_members`` and
``artifact_share_api.revoke_share``), so routing, the operator-only role check, the same-origin check, body validation and
the 404/409 mapping are the real ones. Only persistence is in memory: ``MemberAdminFixture`` mirrors ``MemberAdmin``
(usage rows with effective limits, member-created links, sharing switch, purge answers), ``LinkStore`` the two
``ShareStore`` calls revoke uses, and ``PurgeStore`` ``purge_now``.

API keys: ``op-key`` is an operator, ``view-key`` a viewer (the page sends whichever key the browser context sets in
``X-API-Key``). Stdout carries ``PORT=n``.
"""
from __future__ import annotations

import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.artifact_shares import ShareError  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

HOSTILE = '"><img src=x onerror=window.__x=1>'
MIB = 1048576
_LOCK = threading.RLock()
LIMIT_FIELDS = ("max_artifact_bytes", "max_member_bytes", "max_artifacts")


def _now():
    return datetime.now(UTC)


class State:
    """The shared rows: member usage, artifacts, shares."""

    def __init__(self):
        later = _now() + timedelta(days=20)
        self.members = {
            "member:alice": {"display_name": "Alice", "sharing_disabled": False, "bytes_retracted": 2 * MIB,
                             **{f: None for f in LIMIT_FIELDS}},
            "member:bob": {"display_name": f"Bob {HOSTILE}", "sharing_disabled": False, "bytes_retracted": 0,
                           **{f: None for f in LIMIT_FIELDS}},
        }
        self.artifacts = {
            "art_alice_1": {"created_by": "member:alice", "title": "Alice report", "lifecycle": "published", "bytes": 3 * MIB},
            "art_alice_gone": {"created_by": "member:alice", "title": "Alice old draft", "lifecycle": "retracted", "bytes": 2 * MIB},
            "art_bob_1": {"created_by": "member:bob", "title": f"Bob page {HOSTILE}", "lifecycle": "published", "bytes": MIB},
        }
        self.shares = {
            "share_alicepub": self._share("share_alicepub", "art_alice_1", "member:alice", "public", later),
            "share_alicelab": self._share("share_alicelab", "art_alice_1", "member:alice", "lab", None),
            "share_bobpub": self._share("share_bobpub", "art_bob_1", "member:bob", "public", later),
        }
        self.purged: list[str] = []

    def _share(self, share_id, artifact_id, owner, audience, expires):
        return {"share_id": share_id, "artifact_id": artifact_id, "version_id": "v1", "audience": audience, "state": "live",
                "expires_at": expires.isoformat() if expires else None, "allow_emails": [], "allow_groups": [],
                "created_at": _now().isoformat(), "created_by": owner, "approved_by": owner, "revoked_at": None,
                "title": self.artifacts[artifact_id]["title"], "token_hash": None}


class MemberAdminFixture:
    def __init__(self, state: State):
        self.state = state

    def sync_call(self, method, *args, **kwargs):
        with _LOCK:
            return getattr(self, "_" + method)(*args, **kwargs)

    def _usage(self, principal):
        row, st = self.state.members[principal], self.state
        mine = [a for a in st.artifacts.values() if a["created_by"] == principal]
        live = {"lab": 0, "public": 0}
        for s in st.shares.values():
            if s["created_by"] == principal and s["state"] == "live":
                live[s["audience"]] += 1
        return {"member_principal": principal, **row, "bytes_used": sum(a["bytes"] for a in mine if a["lifecycle"] != "tombstoned"),
                "last_used_at": _now().isoformat(), "limits": {f: row[f] for f in LIMIT_FIELDS},
                "artifact_count": len(mine), "version_count": len(mine), "live_links": live}

    def _list_members(self):
        return [self._usage(p) for p in sorted(self.state.members)]

    def _known(self, principal):
        return principal in self.state.members

    def _links(self, *, principal=None, audience=None, state=None):
        rows = [dict(s) for s in self.state.shares.values()
                if (principal is None or s["created_by"] == principal) and (audience is None or s["audience"] == audience)
                and (state is None or s["state"] == state)
                and self.state.artifacts[s["artifact_id"]]["lifecycle"] != "tombstoned"]
        for r in rows:
            r["recipients"] = [*r.pop("allow_emails"), *r.pop("allow_groups")]
            r.pop("token_hash")
        return sorted(rows, key=lambda r: r["share_id"])

    def _show(self, principal):
        if principal not in self.state.members:
            return None
        arts = [{"artifact_id": k, "title": v["title"], "lifecycle": v["lifecycle"], "bytes": v["bytes"],
                 "created_at": _now().isoformat(), "current_version_id": "v1"}
                for k, v in self.state.artifacts.items() if v["created_by"] == principal and v["lifecycle"] != "tombstoned"]
        return {"member": principal, "usage": self._usage(principal), "artifacts": arts, "links": self._links(principal=principal)}

    def _set_limits(self, principal, changes, actor):
        self.state.members[principal].update(changes)
        return self._usage(principal)

    def _set_sharing(self, principal, enabled, actor):
        self.state.members[principal]["sharing_disabled"] = not enabled
        return self._usage(principal)

    def _open_shares(self, *, principal=None, artifact_id=None):
        return [{"share_id": s["share_id"], "token_hash": None} for s in self.state.shares.values()
                if s["state"] != "revoked" and (principal is None or s["created_by"] == principal)
                and (artifact_id is None or s["artifact_id"] == artifact_id)]

    def _share_of(self, principal, share_id):
        s = self.state.shares.get(share_id)
        return {"share_id": share_id, "token_hash": None} if s and s["created_by"] == principal else None

    def _purgeable(self, principal, artifact_id):
        a = self.state.artifacts.get(artifact_id)
        return {"artifact_id": artifact_id, "lifecycle": a["lifecycle"], "legal_hold": False, "purge_after": _now().isoformat()} if a and a["created_by"] == principal else None

    def _record_purge(self, *args):
        return None


class LinkStore:
    def __init__(self, state: State):
        self.state = state

    def sync_call(self, method, *args, **kwargs):
        with _LOCK:
            return getattr(self, "_" + method)(*args, **kwargs)

    def _get(self, share_id):
        s = self.state.shares.get(share_id)
        return dict(s) if s else None

    def _mark_revoked(self, share_id, *, actor):
        s = self.state.shares.get(share_id)
        if s is None:
            raise ShareError("unknown share")
        s.update(state="revoked", revoked_at=_now().isoformat())
        return {k: v for k, v in s.items() if k != "token_hash"}


class PurgeStore:
    def __init__(self, state: State):
        self.state = state

    def sync_call(self, method, *args, **kwargs):
        with _LOCK:
            return getattr(self, "_" + method)(*args, **kwargs)

    def _list_records(self, **_kwargs):      # the registry list above the Members section: nothing to show
        return []

    def _purge_now(self, artifact_id, *, actor, reason, request_key=None, override_legal_hold=False):
        a = self.state.artifacts[artifact_id]
        if a["lifecycle"] == "tombstoned":
            return {"artifact_id": artifact_id, "result": "already_purged", "deleted": [], "retained_shared": []}
        a["lifecycle"] = "tombstoned"
        self.state.members[a["created_by"]]["bytes_retracted"] = 0
        self.state.purged.append(artifact_id)
        return {"artifact_id": artifact_id, "result": "deleted", "deleted": ["blob"], "retained_shared": []}


if __name__ == "__main__":
    shares_root = Path(tempfile.mkdtemp(prefix="herdr-members-fixture-"))

    def cleanup(*_args):
        shutil.rmtree(shares_root, ignore_errors=True)
        os._exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    state = State()
    server = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, web_root=ROOT / "packages",
                                api_keys={"op-key": "operator", "view-key": "viewer"},
                                artifact_store=PurgeStore(state), share_store=LinkStore(state), shares_root=shares_root,
                                member_admin=MemberAdminFixture(state))
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    print(f"PORT={server.port}", flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        cleanup()
