"""HTTP helpers for the member API tests: sign in through the real exchange, call the real routes."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote

from herdr_artifact_host.member_api import CHUNK_BYTES
from tests.artifact_host.seed import PUBLIC_URL, Host, Resp, access, ident

ORIGIN = {"Origin": PUBLIC_URL}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def exchange(
    host: Host,
    user: str = "alice",
    *,
    groups: tuple[str, ...] = ("local.dev",),
    nonce: str | None = None,
    sent_nonce: str | None = None,
    forwarded: str | None = None,
) -> Resp:
    """One real session exchange.  ``nonce`` is in the ID token, ``sent_nonce`` is what the page sends in X-Nonce."""
    n = nonce if nonce is not None else f"n-{user}"
    headers = {
        **ORIGIN,
        "Authorization": f"Bearer {access(user, groups=groups)}",
        "X-Id-Token": ident(user, email=f"{user}@lab.example", nonce=n),
        "X-Nonce": sent_nonce if sent_nonce is not None else n,
    }
    if forwarded:
        headers["X-Forwarded-For"] = forwarded
    return host.request("POST", "/api/member/session", headers, b"")


def sign_in(host: Host, user: str = "alice", *, groups: tuple[str, ...] = ("local.dev",)) -> str:
    r = exchange(host, user, groups=groups)
    assert r.status == 200, r.body
    return r.json()["session"]


def call(
    host: Host,
    session: str | None,
    method: str,
    path: str,
    body: Any = None,
    *,
    origin: bool = True,
    headers: dict[str, str] | None = None,
) -> Resp:
    h: dict[str, str] = dict(headers or {})
    if session is not None:
        h["Authorization"] = f"Member {session}"
    if origin and method not in ("GET", "HEAD"):
        h.update(ORIGIN)
    raw: bytes | None = None
    if isinstance(body, (bytes, bytearray)):
        raw = bytes(body)
    elif body is not None:
        raw = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    elif method not in ("GET", "HEAD"):
        raw = b""
    return host.request(method, "/api/member" + path, h, raw)


def create(host: Host, session: str, title: str = "Report") -> str:
    r = call(host, session, "POST", "/artifacts", {"title": title, "filename": "index.html"})
    assert r.status == 200, r.body
    return r.json()["artifact_id"]


def upload(host: Host, session: str, aid: str, name: str, data: bytes) -> dict[str, Any]:
    spec = {"name": name, "size_bytes": len(data), "file_sha256": sha(data)}
    st = call(host, session, "POST", f"/artifacts/{aid}/uploads/status", spec)
    assert st.status == 200, st.body
    uid = st.json()["upload_id"]
    for first in range(0, max(1, -(-len(data) // CHUNK_BYTES))):
        part = data[first * CHUNK_BYTES : (first + 1) * CHUNK_BYTES]
        r = call(
            host,
            session,
            "PUT",
            f"/artifacts/{aid}/uploads/{uid}/runs/{first}",
            part,
            headers={
                "X-Name": quote(name),
                "X-Size-Bytes": str(len(data)),
                "X-File-SHA256": spec["file_sha256"],
                "X-Run-SHA256": sha(part),
                "Content-Type": "application/octet-stream",
            },
        )
        assert r.status == 200, r.body
    return spec


def publish_files(
    host: Host, session: str, aid: str, files: dict[str, bytes], base: str | None = None
) -> Resp:
    specs = [upload(host, session, aid, n, d) for n, d in files.items()]
    return call(
        host,
        session,
        "POST",
        f"/artifacts/{aid}/publish",
        {"base_version_id": base, "files": specs},
    )


def published(host: Host, session: str, files: dict[str, bytes] | None = None) -> tuple[str, str]:
    aid = create(host, session)
    r = publish_files(host, session, aid, files or {"index.html": b"<html><body>hi</body></html>"})
    assert r.status == 200, r.body
    return aid, r.json()["version_id"]
