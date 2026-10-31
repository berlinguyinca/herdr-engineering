"""Worker-side renderer-to-RustFS-to-CAS commit pipeline."""
from __future__ import annotations

import json
import logging
import re
from pathlib import PurePosixPath


def _safe_name(name):
    if (not isinstance(name, str) or not name or len(name) > 1024
            or "\\" in name or ":" in name or any(ord(char) < 32 for char in name)
            or PurePosixPath(name).is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise ValueError("renderer and asset names must be safe relative paths")
    return name


def _asset_references(value):
    if not isinstance(value, list) or len(value) > 2000:
        raise ValueError("assets must be a bounded list of uploaded blob references")
    allowed = {"name", "storage_key", "sha256", "size_bytes", "media_type"}
    assets = []
    for item in value:
        if not isinstance(item, dict) or set(item) != allowed:
            raise ValueError("assets must contain name, storage_key, sha256, size_bytes and media_type")
        _safe_name(item["name"])
        if not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]):
            raise ValueError("assets require a SHA-256 digest")
        if not isinstance(item["storage_key"], str) or not item["storage_key"] or len(item["storage_key"]) > 1024:
            raise ValueError("assets require an uploaded storage reference")
        if isinstance(item["size_bytes"], bool) or not isinstance(item["size_bytes"], int) or item["size_bytes"] < 0:
            raise ValueError("asset size_bytes must be a nonnegative integer")
        if (not isinstance(item["media_type"], str) or not item["media_type"]
                or len(item["media_type"]) > 255
                or any(ord(char) < 32 for char in item["media_type"])):
            raise ValueError("asset media_type must be a safe MIME value")
        assets.append(dict(item))
    return assets


class ArtifactRenderRunner:
    def __init__(self, artifact_store, renderer, lineage=None):
        self.store = artifact_store
        self.renderer = renderer
        # optional ``fn(version: dict) -> Any`` called after a durable commit (see artifact_lineage.lineage_emitter);
        # the worker process has no ingest channel of its own, so the caller supplies one or leaves it None
        self.lineage = lineage
        self.rustfs = artifact_store._rf

    async def __call__(self, job: dict) -> dict:
        request = job.get("request") or {}
        if isinstance(request, str):
            request = json.loads(request)
        if not isinstance(request, dict):
            raise ValueError("artifact job request must be an object")
        rendered = self.renderer.render(job["profile"], request.get("source", ""), request)
        assets = _asset_references(request.get("assets", []))
        descriptor = getattr(rendered, "report_descriptor", None)
        if descriptor is not None:
            from .artifact_report_descriptor import validate_report_descriptor

            descriptor = validate_report_descriptor(descriptor, output_manifest=assets)
            if {asset["name"] for asset in assets} != {asset["name"] for asset in descriptor["assets"]}:
                raise ValueError("report assets must exactly match the descriptor asset inventory")
        if not isinstance(rendered.source, str) or not isinstance(rendered.validation, dict):
            raise ValueError("renderer must return textual source and validation evidence")
        if not isinstance(rendered.outputs, list):
            raise ValueError("renderer outputs must be a list")
        names = [asset["name"] for asset in assets]
        for output in rendered.outputs:
            if not isinstance(output, dict) or not isinstance(output.get("content"), str):
                raise ValueError("renderer output content must be text")
            names.append(_safe_name(output.get("name")))
        if len(names) != len(set(names)):
            raise ValueError("renderer outputs and asset references have colliding names")
        source_manifest = {"profile": getattr(rendered, "profile", job["profile"]),
                           "toolchain": rendered.toolchain,
                           "source_media_type": "application/json" if descriptor is not None else "text/plain",
                           "assets": assets}

        async def put_generated(data, media_type):
            key, digest = self.rustfs.put(data)
            if descriptor is not None:
                await self.store.register_report_blob(
                    job["artifact_id"], storage_key=key, sha256=digest,
                    size_bytes=len(data), media_type=media_type)
            return key, digest

        source_key, source_sha = await put_generated(
            rendered.source.encode(), source_manifest["source_media_type"])
        manifest_key, _ = await put_generated(json.dumps(
            {**source_manifest, "source_blob": source_sha, "source_storage_key": source_key},
            sort_keys=True, separators=(",", ":")).encode(), "application/json")
        outputs = []
        for output in rendered.outputs:
            content = output.get("content")
            data = content.encode()
            output_key, output_sha = await put_generated(
                data, output.get("media_type", "application/octet-stream"))
            outputs.append({"name": output["name"],
                            "media_type": output.get("media_type", "application/octet-stream"),
                            "storage_key": output_key, "sha256": output_sha,
                            "size_bytes": len(data)})
        outputs.extend(assets)
        context = {field: job[field] for field in ("agent_id", "session_id", "mission_id")
                   if job.get(field) is not None}
        if job.get("job_id"):
            context["idempotency_key"] = "artifact-job:" + job["job_id"]
        provenance = {**{key: value for key, value in context.items() if key != "idempotency_key"},
                      "requested_by": job.get("requested_by"),
                      "host_id": job.get("assigned_host_id"),
                      "job_id": job.get("job_id"),
                      "generator_skill": "herdr-artifact", "toolchain": rendered.toolchain}
        committed = await self.store.commit_version(
            artifact_id=job["artifact_id"], base_version_id=job.get("base_version_id"),
            source_manifest_blob=manifest_key, output_manifest=outputs,
            provenance=provenance, validation=rendered.validation,
            report_descriptor=descriptor,
            created_by=job.get("requested_by"),
            host_id=job.get("assigned_host_id"), job_id=job.get("job_id"), **context)
        if self.lineage is not None:
            # the version is already durable: a lineage failure must never fail or retry the job
            try:
                self.lineage({"artifact_id": job["artifact_id"], "output_manifest": outputs,
                              "parent_version_id": job.get("base_version_id"),
                              "host_id": job.get("assigned_host_id"), "job_id": job.get("job_id"),
                              **context, **(committed if isinstance(committed, dict) else {})})
            except Exception:                              # noqa: BLE001
                logging.getLogger(__name__).warning("artifact lineage emission failed", exc_info=True)
        return committed

