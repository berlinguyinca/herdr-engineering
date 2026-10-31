"""Loopback control-plane fixture for the artifact Share card and version history (Playwright).

The browser talks to the production HTTP handlers (``ControlPlaneServer`` + ``artifact_share_api.route_share``), so
response shapes and request checks (application/json bodies, ``{"confirm": true}``, one approve per share, the Origin
check, 400/404/409 mapping) are the real ones. Only persistence is in memory: ``ShareFixture`` mirrors ``ShareStore``'s
rules that matter to the UI (audiences, public expiry 1-90 days, lab allow-lists, the email pattern, pending -> live ->
revoked) and ``ArtifactFixture`` mirrors the registry calls the detail page and restore make. Bundles are built for
real under a temporary ``shares_root`` removed on exit. Share links are issued under this server's own address.

Environment: ``RESTORE_CONFLICT=1`` makes every version commit fail with a stale-head 409;
``APPROVE_DELAY=<seconds>`` slows approve down so a second click lands while the first is in flight.
``COLLAB_DISABLED=1`` makes every collaborator/activity/diff route answer 503 (the real route's "not enabled" answer).
The collaborator routes themselves are the production ``artifact_collab_api.route_collab``; ``CollabFixture`` is the
in-memory grant store behind them, and ``FakeCollabOperator`` stands in for the collab service the dashboard proxies
``/api/v1/collab/*`` to (agents, rooms, one-time invite creation).
Stdout carries ``PORT=n`` and one ``MARK_LIVE <share_id>`` line per share made live (never a token or URL).
"""
import asyncio
import copy
import hashlib
import http.server
import json
import os
import re
import secrets
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
from herdr_engineering.control_plane.artifact_bundle import ArtifactBundle  # noqa: E402
from herdr_engineering.control_plane.artifact_collab_store import InviteGrantConflict  # noqa: E402
from herdr_engineering.control_plane.artifacts import ArtifactConflict  # noqa: E402
from herdr_engineering.control_plane.collab_mirror import REPORT_READ_ONLY  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

HOSTILE = '"><img src=x onerror=window.__x=1>'
HOSTILE_TAG = '<img src=x onerror=window.__x=1>'
INVITE_CODE = "hcn_invite_ONE-TIME-CODE-7f3a9c2e51b84d60"
_EMAIL = re.compile(r"[!-?A-~]+@[!-?A-~]+\Z", re.ASCII)   # artifact_shares._EMAIL
_LOCK = threading.RLock()


def _now():
    return datetime.now(UTC)


def _blob(data: bytes) -> dict:
    digest = hashlib.sha256(data).hexdigest()
    return {"storage_key": f"sha256/{digest[:2]}/{digest[2:4]}/{digest}", "sha256": digest, "size_bytes": len(data)}


class ArtifactFixture:
    """The registry calls the artifact detail page, the report reader, restore and share bundling make."""

    def __init__(self):
        self.blobs = {}
        self.records = {
            "web_1": {"artifact_id": "web_1", "artifact_type": "web", "display_name": f"Lab notes {HOSTILE}",
                      "original_filename": "notes.zip", "mime_type": "application/zip", "status": "available",
                      "current_version_id": "version_3"},
            "report_1": {"artifact_id": "report_1", "artifact_type": "report", "display_name": "Latency report",
                         "original_filename": "Latency report", "mime_type": "text/html", "status": "available",
                         "current_version_id": "report_v1"},
        }
        self.records["collab_1"] = {
            "artifact_id": "collab_1", "artifact_type": "web", "display_name": "Collaboration notes",
            "original_filename": "collab.zip", "mime_type": "application/zip", "status": "available",
            "current_version_id": "c1_v4"}
        self.versions = {"web_1": [], "report_1": [], "collab_1": []}
        for number in (1, 2, 3):
            self.versions["web_1"].append(self._web_version("web_1", number,
                                                            f"version_{number - 1}" if number > 1 else None))
        self._seed_collab_history()
        csv = b"measurement,value\nsample,1\n"
        descriptor = {   # the shape artifact-reports-server.py uses (a valid descriptor for the preview renderer)
            "schema_version": 1, "title": "Latency investigation", "summary": "Explanation of the timing study.",
            "questions": [{"id": "q1", "text": "Does the candidate reduce latency?"}],
            "evidence": [{"id": "timing", "title": "Timing observations", "description": "Measured milliseconds.",
                          "asset_ids": ["observations"]}],
            "findings": [{"id": "f1", "title": "A descriptive difference", "statement": "Finding one.",
                          "question_ids": ["q1"], "evidence_ids": ["timing"], "figure_ids": ["figure_latency"],
                          "limitations": ["No independent validation yet."], "next_steps": ["Collect more."]}],
            "figures": [{"id": "figure_latency", "title": "Timing chart", "asset_id": "observations",
                         "caption": "Source observations.", "reading_guide": "Compare like-for-like groups."}],
            "assets": [{"id": "observations", "name": "evidence/timing.csv", "role": "data", "media_type": "text/csv",
                        "sha256": hashlib.sha256(csv).hexdigest(), "size_bytes": len(csv)}],
            "analysis": {"method": "descriptive comparison"}, "limitations": ["Exploratory."], "next_steps": ["Verify."],
        }
        self.versions["report_1"].append({
            "version_id": "report_v1", "artifact_id": "report_1", "version_number": 1, "parent_version_id": None,
            "created_by": "api-key:fixture:local", "state": "ready", "created_at": "2026-10-30T10:00:00+00:00",
            "committed_at": "2026-10-30T10:00:00+00:00", "report_descriptor": descriptor, "output_manifest": [],
            "source_manifest_blob": None, "validation": {"status": "passed"}})
        self.commits = 0

    def _version(self, artifact_id, number, parents, files, *, message=None, author="api-key:author:local"):
        outputs = []
        for name, data, media in files:
            ref = _blob(data)
            self.blobs[ref["storage_key"]] = data
            outputs.append({"name": name, "media_type": media, **ref})
        prefix = "c1" if artifact_id == "collab_1" else artifact_id
        return {"version_id": f"{prefix}_v{number}", "artifact_id": artifact_id, "version_number": number,
                "parent_version_id": parents[0] if parents else None, "parent_version_ids": list(parents),
                "created_by": author, "state": "ready", "created_at": f"2026-10-30T1{number}:00:00+00:00",
                "committed_at": f"2026-10-30T1{number}:00:00+00:00", "report_descriptor": None,
                "output_manifest": outputs, "source_manifest_blob": None, "provenance": {}, "message": message}

    def _seed_collab_history(self):
        """v1 root; v2 and v3 both branch from it; v4 merges them. v4 against its first parent (v2) changes a text file,
        adds a binary file and adds a text file whose name is hostile."""
        text = "text/html"
        v1 = self._version("collab_1", 1, [], [("index.html", b"<p>one</p>\n", text)], message="first draft")
        v2 = self._version("collab_1", 2, ["c1_v1"], [("index.html", b"<p>one</p>\n<p>two</p>\n", text)])
        v3 = self._version("collab_1", 3, ["c1_v1"], [("index.html", b"<p>one</p>\n<p>side</p>\n", text)],
                           author="collab:agent-codex")
        v4 = self._version("collab_1", 4, ["c1_v2", "c1_v3"], [
            ("index.html", b"<p>one</p>\n<p>two</p>\n<p>side</p>\n", text),
            ("logo.png", b"\x89PNG\r\n\x1a\n\x00\x00", "image/png"),
            (f"{HOSTILE_TAG}.txt", b"hostile file body\n", "text/plain")],
            message=f"merge {HOSTILE_TAG}", author="collab:agent-codex")
        self.versions["collab_1"] = [v1, v2, v3, v4]

    def _web_version(self, artifact_id, number, parent):
        page = f"<!doctype html><title>Notes v{number}</title><p>Revision {number}</p>".encode()
        style = f"body {{ color: #{number}{number}{number}; }}".encode()
        outputs = []
        for name, data, media in (("index.html", page, "text/html"), ("style.css", style, "text/css")):
            ref = _blob(data)
            self.blobs[ref["storage_key"]] = data
            outputs.append({"name": name, "media_type": media, **ref})
        return {"version_id": f"version_{number}", "artifact_id": artifact_id, "version_number": number,
                "parent_version_id": parent, "created_by": f"api-key:author{number}:local", "state": "ready",
                "created_at": f"2026-10-30T1{number}:00:00+00:00", "committed_at": f"2026-10-30T1{number}:00:00+00:00",
                "report_descriptor": None, "output_manifest": outputs,
                "source_manifest_blob": f"sha256:{hashlib.sha256(page).hexdigest()}", "provenance": {}}

    def sync_call(self, method, *args, **kwargs):
        return asyncio.run(getattr(self, method)(*args, **kwargs))

    # ---------------------------------------------------------------- reads
    async def get_record(self, artifact_id, **_kwargs):
        with _LOCK:
            return copy.deepcopy(self.records.get(artifact_id))

    async def list_records(self, **_kwargs):
        with _LOCK:
            return [copy.deepcopy(record) for record in self.records.values()]

    async def get_version(self, version_id, artifact_id=None):
        with _LOCK:
            for owner, rows in self.versions.items():
                for row in rows:
                    if row["version_id"] == version_id and artifact_id in (None, owner):
                        return copy.deepcopy(row)
        return None

    async def list_versions(self, artifact_id):
        with _LOCK:
            return copy.deepcopy(sorted(self.versions.get(artifact_id, []), key=lambda r: -r["version_number"]))

    async def list_version_summaries(self, artifact_id):   # ArtifactStore.list_version_summaries columns
        keys = ("version_id", "artifact_id", "version_number", "parent_version_id", "created_by", "state",
                "created_at", "committed_at")
        return [{**{k: row[k] for k in keys}, "is_report": row["report_descriptor"] is not None,
                 **({"parent_version_ids": row["parent_version_ids"]} if "parent_version_ids" in row else {}),
                 **({"message": row["message"]} if row.get("message") else {})}
                for row in await self.list_versions(artifact_id)]

    async def collab_state(self, artifact_id):   # ArtifactStore.collab_state: owner and writability
        if artifact_id not in self.records:
            return None
        reason = REPORT_READ_ONLY if self.records[artifact_id]["artifact_type"] == "report" else None
        return {"created_by": "api-key:owner:local", "writable": reason is None, "writable_reason": reason}

    async def version_text_diff(self, artifact_id, version_id):   # ArtifactStore.version_text_diff, same item shape
        version = await self.get_version(version_id, artifact_id)
        if version is None:
            return None
        parents = version.get("parent_version_ids") or ([version["parent_version_id"]] if version["parent_version_id"] else [])
        parent = await self.get_version(parents[0], artifact_id) if parents else None
        new_files = {e["name"]: e for e in version["output_manifest"]}
        old_files = {e["name"]: e for e in (parent["output_manifest"] if parent else [])}
        out = []
        for name in sorted(set(new_files) | set(old_files)):
            old, new = old_files.get(name), new_files.get(name)
            change = ("added" if old is None else "deleted" if new is None
                      else "unchanged" if old["sha256"] == new["sha256"] else "modified")
            item = {"name": name, "change": change, "diff": None}
            out.append(item)
            if change == "unchanged":
                continue
            media = (new or old)["media_type"]
            if not (media.startswith("text/") or media == "application/json"):
                item["reason"] = "binary"
                continue
            left = self.blobs[old["storage_key"]] if old else b""
            right = self.blobs[new["storage_key"]] if new else b""
            item["diff"] = ArtifactBundle.diff(left, right, media_type=media)
        return {"version_id": version_id, "against": parents[0] if parents else None, "files": out}

    async def report_permission(self, artifact_id, principal_id, permission):
        return artifact_id in self.records

    async def verify_share_outputs(self, artifact_id, version_id):
        version = await self.get_version(version_id, artifact_id)
        return len(version["output_manifest"]) if version else 0

    async def mark_linked(self, version_id, error=None):
        return None

    # ---------------------------------------------------------------- restore
    async def commit_version(self, artifact_id, *, base_version_id, source_manifest_blob, output_manifest,
                             provenance=None, created_by=None, idempotency_key=None, **_rest):
        with _LOCK:
            record = self.records.get(artifact_id)
            if record is None:
                raise ArtifactConflict(artifact_id, None)
            if os.environ.get("RESTORE_CONFLICT") or base_version_id != record["current_version_id"]:
                raise ArtifactConflict(artifact_id, record["current_version_id"])
            rows = self.versions[artifact_id]
            number = max(row["version_number"] for row in rows) + 1
            row = {"version_id": f"version_{number}", "artifact_id": artifact_id, "version_number": number,
                   "parent_version_id": base_version_id, "created_by": created_by, "state": "ready",
                   "created_at": _now().isoformat(), "committed_at": _now().isoformat(), "report_descriptor": None,
                   "output_manifest": copy.deepcopy(output_manifest), "source_manifest_blob": source_manifest_blob,
                   "provenance": dict(provenance or {}), "idempotency_key": idempotency_key}
            rows.append(row)
            record["current_version_id"] = row["version_id"]
            self.commits += 1
            return copy.deepcopy(row)

    # ---------------------------------------------------------------- report reader
    async def get_report_descriptor(self, artifact_id, version_id):
        version = await self.get_version(version_id, artifact_id)
        return version["report_descriptor"] if version else None

    async def get_report(self, artifact_id, version_id):
        version = await self.get_version(version_id, artifact_id)
        if not version or not version["report_descriptor"]:
            return None
        checks = {"rendered": {"status": "passed", "actor_id": "worker", "notes": "Rendered preview."},
                  "evidence_checked": {"status": "not_checked"}, "reviewed": {"status": "not_checked"}}
        return {"artifact_id": artifact_id, "version_id": version_id, "report_descriptor": version["report_descriptor"],
                "checks": checks, "state": "rendered", "feedback": [], "feedback_count": 0,
                "feedback_has_more": False, "assets": [],
                "preview_url": f"/api/v1/artifacts/{artifact_id}/versions/{version_id}/preview"}

    async def list_report_feedback(self, artifact_id, version_id, *, limit=10, offset=0):
        return []

    async def count_report_feedback(self, artifact_id, version_id):
        return 0

    async def get_report_diff(self, artifact_id, version_id, base_version_id):
        return {"artifact_id": artifact_id, "version_id": version_id, "base_version_id": base_version_id,
                "changed": False, "categories": {}}

    # ---------------------------------------------------------------- blobs (rustfs)
    def head(self, key):
        return key in self.blobs

    def iter_get(self, key, digest, chunk_size=1024 * 1024):
        data = self.blobs[key]
        assert hashlib.sha256(data).hexdigest() == digest
        for offset in range(0, len(data), chunk_size):
            yield data[offset:offset + chunk_size]


class ShareFixture:
    """In-memory ``ShareStore``: same methods, row columns and validation rules; never holds a token."""

    COLUMNS = ("share_id", "artifact_id", "version_id", "audience", "allow_emails", "allow_groups", "state", "entry",
               "file_count", "total_bytes", "title", "expires_at", "created_by", "created_at", "approved_by",
               "approved_at", "revoked_at")

    def __init__(self, artifacts: ArtifactFixture):
        self.artifacts = artifacts
        self.rows: dict[str, dict] = {}
        self.hashes: dict[str, str] = {}
        self.log: list[dict] = []

    def sync_call(self, method, *args, **kwargs):
        return asyncio.run(getattr(self, method)(*args, **kwargs))

    def _public(self, share_id, keep_hash=False):
        row = {k: copy.deepcopy(self.rows[share_id][k]) for k in self.COLUMNS}
        if keep_hash:
            row["token_hash"] = self.hashes.get(share_id)
        return row

    def _event(self, share_id, kind, actor, detail):
        self.log.append({"share_id": share_id, "at": _now(), "kind": kind, "actor": actor, "detail": detail or {}})

    @staticmethod
    def _clean(values, what, pattern=None, lower=False):
        if values is None:
            return []
        if isinstance(values, str) or not isinstance(values, (list, tuple)):
            raise ShareError(f"{what} must be a list")
        out = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ShareError(f"{what} contains an empty entry")
            value = value.strip()
            if pattern is not None and not pattern.match(value):
                raise ShareError(f"{what}: {value!r} is not valid")
            value = value.lower() if lower else value
            if value not in out:
                out.append(value)
        return out

    async def create(self, *, artifact_id, version_id, audience, allow_emails=None, allow_groups=None,
                     expires_at=None, entry=None, title=None, created_by=None):
        if audience not in ("lab", "public"):
            raise ShareError("audience must be 'lab' or 'public'")
        emails = self._clean(allow_emails, "allow_emails", _EMAIL, lower=True)
        groups = self._clean(allow_groups, "allow_groups")
        now = _now()
        if audience == "lab":
            if not emails and not groups:
                raise ShareError("a lab share needs at least one allowed email or group")
        else:
            if emails or groups:
                raise ShareError("a public share cannot carry an allow-list")
            if expires_at is None:
                raise ShareError("a public share must expire")
            if not now + timedelta(days=1) - timedelta(minutes=5) <= expires_at <= now + timedelta(days=90):
                raise ShareError("a public share must expire within 1-90 days")
        if await self.artifacts.get_version(version_id, artifact_id) is None:
            raise ShareError("version does not belong to that artifact")
        with _LOCK:
            share_id = "share_" + secrets.token_hex(12)
            self.rows[share_id] = {"share_id": share_id, "artifact_id": artifact_id, "version_id": version_id,
                                   "audience": audience, "allow_emails": emails, "allow_groups": groups,
                                   "state": "pending", "entry": entry, "file_count": None, "total_bytes": None,
                                   "title": title, "expires_at": expires_at, "created_by": created_by,
                                   "created_at": now, "approved_by": None, "approved_at": None, "revoked_at": None}
            self._event(share_id, "created", created_by or "unknown", {"audience": audience})
            return self._public(share_id)

    async def get(self, share_id):
        with _LOCK:
            return self._public(share_id, keep_hash=True) if share_id in self.rows else None

    async def list_for_artifact(self, artifact_id):
        with _LOCK:
            ids = sorted((r for r in self.rows.values() if r["artifact_id"] == artifact_id),
                         key=lambda r: (-r["created_at"].timestamp(), r["share_id"]))
            return [self._public(r["share_id"]) for r in ids]

    async def mark_live(self, share_id, *, token_hash, index, approved_by):
        delay = float(os.environ.get("APPROVE_DELAY") or 0)
        if delay:
            time.sleep(delay)
        with _LOCK:
            row = self.rows.get(share_id)
            if row is None:
                raise ShareError("unknown share")
            if row["state"] != "pending":
                raise ShareError(f"share is {row['state']}, not pending")
            if token_hash in self.hashes.values():
                raise ShareError("token hash already in use")
            row.update(state="live", entry=index.entry, file_count=len(index.files), total_bytes=index.total_bytes,
                       approved_by=approved_by, approved_at=_now())
            self.hashes[share_id] = token_hash
            self._event(share_id, "approved", approved_by,
                        {"file_count": len(index.files), "total_bytes": index.total_bytes})
            print(f"MARK_LIVE {share_id}", flush=True)
            return self._public(share_id)

    async def mark_revoked(self, share_id, *, actor):
        with _LOCK:
            row = self.rows.get(share_id)
            if row is None:
                raise ShareError("unknown share")
            if row["state"] != "revoked":
                was = row["state"]
                row.update(state="revoked", revoked_at=_now())
                self._event(share_id, "revoked", actor, {"was": was})
            return self._public(share_id)

    async def live_total_bytes(self):
        with _LOCK:
            return sum(r["total_bytes"] or 0 for r in self.rows.values() if r["state"] == "live")

    async def events(self, share_id, limit=200):
        with _LOCK:
            return copy.deepcopy([e for e in self.log if e["share_id"] == share_id][:limit])


class CollabFixture:
    """In-memory ``CollabGrantStore`` behind the adapter call path: ``store("grant", artifact_id, ...)``."""

    def __init__(self):
        self.grants: dict[tuple[str, str], dict] = {}
        self.invites: dict[str, dict] = {}
        self.attempts = [
            {"ticket_id": "t2", "artifact_id": "collab_1", "outcome": "rejected", "actor_id": "agent-evil",
             "received_at": "2026-10-30T14:05:00+00:00", "conflict_names": [],
             "reason": f"stale base {HOSTILE_TAG}"},
            {"ticket_id": "t1", "artifact_id": "collab_1", "outcome": "conflict", "actor_id": "agent-codex",
             "received_at": "2026-10-30T14:00:00+00:00", "conflict_names": ["index.html", f"{HOSTILE_TAG}.txt"],
             "reason": None},
        ]

    def __call__(self, method, *args, **kwargs):
        with _LOCK:
            return getattr(self, "_" + method)(*args, **kwargs)

    def _collaborators(self, artifact_id):
        return [{**row, "last_used_at": None, "commits": 0, "conflicts": 0, "rejected": 0}
                for (art, _agent), row in sorted(self.grants.items()) if art == artifact_id]

    def _pending_invite_grants(self, artifact_id):
        return [dict(row) for row in self.invites.values() if row["artifact_id"] == artifact_id]

    def _grant(self, artifact_id, agent_id, permission, *, expires_at, granted_by):
        row = {"artifact_id": artifact_id, "agent_id": agent_id, "permission": permission, "expires_at": expires_at}
        self.grants[(artifact_id, agent_id)] = row
        return dict(row)

    def _revoke(self, artifact_id, agent_id):
        return self.grants.pop((artifact_id, agent_id), None) is not None

    def _revoke_grants(self, artifact_id, *, write_only):
        gone = [k for k, v in self.grants.items() if k[0] == artifact_id and (not write_only or v["permission"] == "write")]
        for key in gone:
            del self.grants[key]
        pend = [k for k, v in self.invites.items() if v["artifact_id"] == artifact_id]
        for key in pend:
            del self.invites[key]
        return {"grants": len(gone), "pending_invites": len(pend)}

    def _add_invite_grant(self, invite_id, artifact_id, permission, *, expires_at, granted_by):
        if invite_id in self.invites:
            raise InviteGrantConflict("invite already registered")
        row = {"invite_id": invite_id, "artifact_id": artifact_id, "permission": permission,
               "expires_at": expires_at, "granted_by": granted_by}
        self.invites[invite_id] = row
        return dict(row)

    def _attempts(self, artifact_id, *, limit=100):
        return [dict(a) for a in self.attempts if a["artifact_id"] == artifact_id]


class FakeCollabOperator:
    """The collab service's operator listener, as far as the Collaborators tab uses it (the dashboard proxies to it)."""

    AGENTS = [{"agent_id": "agent-codex", "name": "Codex"},
              {"agent_id": "agent-evil", "name": f"Evil {HOSTILE_TAG}"}]
    ROOMS = [{"room_id": "room-1", "name": "Lab room"}]

    def __init__(self):
        self.invites_made = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return None

            def _send(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/v1/collab/agents":
                    self._send(200, {"agents": outer.AGENTS})
                elif self.path == "/api/v1/collab/rooms":
                    self._send(200, {"rooms": outer.ROOMS})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if re.fullmatch(r"/api/v1/collab/rooms/[^/]+/externals/invites", self.path):
                    outer.invites_made += 1
                    self._send(200, {"invite_id": f"inv-{outer.invites_made}", "code": INVITE_CODE,
                                     "expires_at": (_now() + timedelta(hours=1)).isoformat()})
                else:
                    self._send(404, {"error": "not found"})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


if __name__ == "__main__":
    shares_root = Path(tempfile.mkdtemp(prefix="herdr-share-fixture-"))

    def cleanup(*_args):
        shutil.rmtree(shares_root, ignore_errors=True)
        os._exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    artifacts = ArtifactFixture()
    operator = FakeCollabOperator()
    server = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, web_root=ROOT / "packages",
                                artifact_store=artifacts, rustfs=artifacts,
                                share_store=ShareFixture(artifacts), shares_root=shares_root,
                                collab_grant_store=CollabFixture(),
                                collab_artifacts_enabled=not os.environ.get("COLLAB_DISABLED"),
                                collab_operator_url=operator.url)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    # share_settings_config insists on https; this loopback fixture issues links under its own http address.
    server.share_settings = {"public_url": f"http://127.0.0.1:{server.port}", "max_share_bytes": 512 * 1024 * 1024,
                             "max_files": 20000, "max_total_bytes": 2 * 1024 * 1024 * 1024, "default_public_days": 30}
    print(f"PORT={server.port}", flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        cleanup()
