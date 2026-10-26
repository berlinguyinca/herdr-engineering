"""Private, checksum-verified checkpoint storage for certified recovery only."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from .recovery import ExecutionLease


class CheckpointError(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckpointManifest:
    checkpoint_id: str
    owner_id: str
    agent_id: str
    host_uid: str
    generation: int
    created_at: str
    sha256: str
    size_bytes: int
    private: bool = True


class ProtectedCheckpointStore:
    """Filesystem-backed private store; it never returns a public URL."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def put(self, *, checkpoint_id: str, owner_id: str, agent_id: str,
            lease: ExecutionLease, payload: bytes) -> CheckpointManifest:
        if not lease.checkpoint_capable:
            raise CheckpointError("lease is not checkpoint-capable")
        if not checkpoint_id or not owner_id or not agent_id or not isinstance(payload, bytes):
            raise CheckpointError("checkpoint identity and bytes are required")
        digest = hashlib.sha256(payload).hexdigest()
        manifest = CheckpointManifest(
            checkpoint_id=checkpoint_id, owner_id=owner_id, agent_id=agent_id,
            host_uid=lease.host_uid, generation=lease.generation,
            created_at=datetime.now(UTC).isoformat(), sha256=digest,
            size_bytes=len(payload),
        )
        self._atomic_write(self.root / f"{checkpoint_id}.bin", payload, binary=True)
        self._atomic_write(self.root / f"{checkpoint_id}.json",
                           json.dumps(asdict(manifest), sort_keys=True).encode(), binary=True)
        return manifest

    def get(self, checkpoint_id: str, *, owner_id: str) -> bytes:
        manifest_path = self.root / f"{checkpoint_id}.json"
        payload_path = self.root / f"{checkpoint_id}.bin"
        try:
            manifest = json.loads(manifest_path.read_text())
            payload = payload_path.read_bytes()
        except (OSError, ValueError) as exc:
            raise CheckpointError("checkpoint unavailable") from exc
        if manifest.get("owner_id") != owner_id:
            raise CheckpointError("checkpoint owner mismatch")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != manifest.get("sha256") or len(payload) != manifest.get("size_bytes"):
            raise CheckpointError("checkpoint digest mismatch")
        return payload

    @staticmethod
    def _atomic_write(path: Path, data: bytes, *, binary: bool) -> None:
        with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)

