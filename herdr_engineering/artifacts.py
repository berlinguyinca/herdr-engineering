"""Shared artifact / handoff workspace (spec 0080).

Implements an abstract ArtifactWorkspaceProvider with a filesystem provider.
Artifacts are referenced by ArtifactRef with provenance, content hashing and
atomic publish. Concurrent writers are safe (no last-writer corruption).
Local work continues when shared storage is down; publication degrades to
queued with visible status. Retention/quota are configurable.

Secret exclusions: never sync .git, SSH keys, cloud credentials, auth tokens
or browser profiles by default.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import ArtifactRef

_SECRET_MARKERS = {
    ".git", ".ssh", ".aws", ".config/gh", ".kube", ".netrc", "id_rsa",
    "id_ed25519", ".npmrc", ".pypirc", "credentials", "token", "secret",
    "browser profile", ".vscode-server",
}


@dataclass
class PublishResult:
    ok: bool
    artifact: ArtifactRef | None = None
    status: str = "published"   # published | queued | failed
    error: str | None = None
    queued_path: str | None = None


@dataclass
class ArtifactWorkspaceProvider(ABC):
    """Abstract provider contract. Concrete providers must implement the
    primitives; the base class implements safe publish/read on top."""

    retention_days: int = 30
    quota_bytes: int | None = None

    @abstractmethod
    def _write_bytes(self, rel_path: str, data: bytes) -> None: ...

    @abstractmethod
    def _read_bytes(self, rel_path: str) -> bytes | None: ...

    @abstractmethod
    def _exists(self, rel_path: str) -> bool: ...

    @abstractmethod
    def _list(self, rel_path: str) -> list[str]: ...

    @abstractmethod
    def _delete(self, rel_path: str) -> None: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    def _is_secret(self, rel_path: str) -> bool:
        parts = rel_path.lower().split("/")
        for marker in _SECRET_MARKERS:
            if marker in rel_path.lower() or marker in parts:
                return True
        return False

    def publish_bytes(
        self,
        *,
        title: str,
        kind: str,
        data: bytes,
        project: str,
        mission_or_session: str,
        producer_system: str = "herdr-engineering",
        producer_session_id: str | None = None,
        machine_id: str | None = None,
        repository: str | None = None,
        worktree_id: str | None = None,
        revision: str | None = None,
        media_type: str | None = None,
        rel_name: str | None = None,
    ) -> PublishResult:
        if self._is_secret(rel_name or title):
            return PublishResult(ok=False, status="failed",
                                 error="refusing to publish secret-like path")
        art_id = f"art_{uuid.uuid4().hex[:20]}"
        sha256 = hashlib.sha256(data).hexdigest()
        safe_rel = _safe_rel(rel_name or f"{art_id}.bin")
        rel = f"{_safe_rel(project)}/{_safe_rel(mission_or_session)}/{art_id}/{safe_rel}"
        if self.quota_bytes and len(data) > self.quota_bytes:
            return PublishResult(ok=False, status="failed", error="exceeds quota")
        try:
            if not self.is_available():
                raise OSError("storage unavailable")
            self._write_bytes(rel, data)
        except OSError as exc:
            return PublishResult(ok=False, status="queued", error=str(exc),
                                 queued_path=rel)
        ref = ArtifactRef(
            id=art_id, kind=kind, title=title, uri=f"herdr-artifact://{rel}",
            machine_id=machine_id, repository=repository, worktree_id=worktree_id,
            revision=revision, media_type=media_type, size_bytes=len(data),
            producer_system=producer_system, producer_session_id=producer_session_id,
            sha256=sha256)
        return PublishResult(ok=True, artifact=ref, status="published")

    def publish_file(self, path: Path, **kw) -> PublishResult:
        data = path.read_bytes()
        return self.publish_bytes(data=data, rel_name=path.name, **kw)

    def read(self, ref: ArtifactRef) -> bytes | None:
        if not ref.uri.startswith("herdr-artifact://"):
            return None
        rel = ref.uri[len("herdr-artifact://"):]
        try:
            data = self._read_bytes(rel)
        except OSError:
            return None
        if data is None:
            return None
        if ref.sha256 and hashlib.sha256(data).hexdigest() != ref.sha256:
            return None  # content mismatch
        return data

    def resolve(self, ref: ArtifactRef) -> str | None:
        return f"herdr-artifact://{ref.uri[len('herdr-artifact://'):]}"


def _safe_rel(name: str) -> str:
    import re
    s = re.sub(r"[^A-Za-z0-9._/-]", "-", name)
    s = s.replace("..", "-").strip("/")
    return s or "artifact"


@dataclass
class FilesystemArtifactWorkspace(ArtifactWorkspaceProvider):
    root: Path = field(default_factory=lambda: Path(
        os.environ.get("HERDR_ENGINEERING_ARTIFACT_ROOT",
                       str(Path.home() / ".local/share/herdr-engineering/artifacts"))))
    retention_days: int = 30
    quota_bytes: int | None = None
    _available: bool = True

    def _full(self, rel: str) -> Path:
        return (self.root / rel).resolve()

    def is_available(self) -> bool:
        return self._available

    def _write_bytes(self, rel_path: str, data: bytes) -> None:
        full = self._full(rel_path)
        full.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(full.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, full)   # atomic publish, safe for concurrent writers
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _read_bytes(self, rel_path: str) -> bytes | None:
        full = self._full(rel_path)
        if not full.exists():
            return None
        return full.read_bytes()

    def _exists(self, rel_path: str) -> bool:
        return self._full(rel_path).exists()

    def _list(self, rel_path: str) -> list[str]:
        full = self._full(rel_path)
        if not full.exists():
            return []
        return [str(p.relative_to(self.root)) for p in full.rglob("*") if p.is_file()]

    def _delete(self, rel_path: str) -> None:
        full = self._full(rel_path)
        if full.exists():
            full.unlink()

    def enforce_retention(self, now_ts: float | None = None) -> int:
        """Delete artifacts older than retention_days. Returns count removed."""
        now = now_ts or time.time()
        removed = 0
        for rel in self._list(""):
            full = self._full(rel)
            try:
                if now - full.stat().st_mtime > self.retention_days * 86400:
                    full.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def mark_unavailable(self) -> None:
        self._available = False

    def mark_available(self) -> None:
        self._available = True
