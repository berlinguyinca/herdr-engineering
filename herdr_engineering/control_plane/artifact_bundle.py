"""Deterministic collection manifests, ZIP exports, and bounded diffs."""
from __future__ import annotations

import difflib
import io
import json
import zipfile


class ArtifactBundle:
    @staticmethod
    def manifest(members: list[dict]) -> bytes:
        normalized = []
        for member in members:
            artifact_id = member.get("artifact_id")
            version_id = member.get("version_id")
            if not isinstance(artifact_id, str) or not isinstance(version_id, str):
                raise ValueError("bundle members require artifact_id and version_id")
            normalized.append({"artifact_id": artifact_id, "version_id": version_id})
        normalized.sort(key=lambda item: (item["artifact_id"], item["version_id"]))
        return json.dumps({"schema_version": 1, "members": normalized},
                          sort_keys=True, separators=(",", ":")).encode()

    @staticmethod
    def zip_bytes(files: dict[str, bytes]) -> bytes:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(files):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, files[name])
        return out.getvalue()

    @staticmethod
    def diff(left, right, *, media_type: str, max_lines: int = 2000) -> str:
        if media_type.startswith("text/") or media_type in {"application/json", "text/markdown"}:
            left_text = left.decode("utf-8", "replace") if isinstance(left, bytes) else str(left)
            right_text = right.decode("utf-8", "replace") if isinstance(right, bytes) else str(right)
            lines = list(difflib.unified_diff(left_text.splitlines(), right_text.splitlines(),
                                              fromfile="old", tofile="new", lineterm=""))
            return "\n".join(lines[:max_lines])
        return f"binary diff unavailable for media type {media_type}"

