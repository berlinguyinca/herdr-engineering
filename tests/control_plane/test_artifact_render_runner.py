import asyncio

from herdr_engineering.control_plane.artifact_render_runner import ArtifactRenderRunner


class _RustFS:
    def __init__(self):
        self.objects = {}

    def put(self, data):
        key = f"key-{len(self.objects)}"
        digest = f"sha-{len(self.objects)}"
        self.objects[key] = data
        return key, digest


class _Store:
    def __init__(self):
        self._rf = _RustFS()
        self.calls = []

    async def commit_version(self, **kwargs):
        self.calls.append(kwargs)
        return {"version_id": "version-2"}


class _Renderer:
    def render(self, profile, source, request):
        return type("Result", (), {
            "source": source,
            "outputs": [{"name": "index.html", "media_type": "text/html",
                         "content": "<html>ok</html>"}],
            "validation": {"status": "passed"},
            "toolchain": {"renderer": "test"},
        })()


def test_render_runner_uploads_source_manifest_and_outputs_before_cas_commit():
    store = _Store()
    runner = ArtifactRenderRunner(store, _Renderer())
    result = asyncio.run(runner({
        "artifact_id": "artifact-1", "base_version_id": "version-1",
        "profile": "web", "requested_by": "agent-1", "assigned_host_id": "host-a",
        "request": {"source": "<html>source</html>"},
    }))
    assert result["version_id"] == "version-2"
    assert len(store._rf.objects) == 3
    commit = store.calls[0]
    assert commit["base_version_id"] == "version-1"
    assert commit["output_manifest"][0]["storage_key"] == "key-2"
    assert commit["provenance"]["host_id"] == "host-a"

