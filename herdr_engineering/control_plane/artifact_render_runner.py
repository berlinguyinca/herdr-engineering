"""Worker-side renderer-to-RustFS-to-CAS commit pipeline."""
from __future__ import annotations

import json


class ArtifactRenderRunner:
    def __init__(self, artifact_store, renderer):
        self.store = artifact_store
        self.renderer = renderer
        self.rustfs = artifact_store._rf

    async def __call__(self, job: dict) -> dict:
        request = job.get("request") or {}
        if isinstance(request, str):
            request = json.loads(request)
        if not isinstance(request, dict):
            raise ValueError("artifact job request must be an object")
        rendered = self.renderer.render(job["profile"], request.get("source", ""), request)
        source_manifest = {"profile": job["profile"], "toolchain": rendered.toolchain,
                           "source_media_type": "text/plain"}
        source_key, source_sha = self.rustfs.put(rendered.source.encode())
        manifest_key, _ = self.rustfs.put(json.dumps(
            {**source_manifest, "source_blob": source_sha},
            sort_keys=True, separators=(",", ":")).encode())
        outputs = []
        for output in rendered.outputs:
            content = output.get("content")
            if not isinstance(content, str):
                raise ValueError("renderer output content must be text")
            output_key, output_sha = self.rustfs.put(content.encode())
            outputs.append({"name": output["name"],
                            "media_type": output.get("media_type", "application/octet-stream"),
                            "storage_key": output_key, "sha256": output_sha})
        return await self.store.commit_version(
            artifact_id=job["artifact_id"], base_version_id=job.get("base_version_id"),
            source_manifest_blob=manifest_key, output_manifest=outputs,
            provenance={"agent_id": job.get("requested_by"),
                        "host_id": job.get("assigned_host_id"),
                        "generator_skill": "herdr-artifact",
                        "toolchain": rendered.toolchain},
            created_by=job.get("requested_by"),
            host_id=job.get("assigned_host_id"), job_id=job.get("job_id"))

