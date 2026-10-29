import json

from herdr_engineering.control_plane.artifact_bundle import ArtifactBundle


def test_bundle_manifest_is_deterministic_and_pins_exact_versions():
    first = ArtifactBundle.manifest([
        {"artifact_id": "a2", "version_id": "v2"},
        {"artifact_id": "a1", "version_id": "v9"},
    ])
    second = ArtifactBundle.manifest([
        {"artifact_id": "a1", "version_id": "v9"},
        {"artifact_id": "a2", "version_id": "v2"},
    ])
    assert first == second
    assert json.loads(first)["members"][0]["artifact_id"] == "a1"


def test_bundle_zip_is_reproducible():
    left = ArtifactBundle.zip_bytes({"index.html": b"hello", "manifest.json": b"{}"})
    right = ArtifactBundle.zip_bytes({"manifest.json": b"{}", "index.html": b"hello"})
    assert left == right


def test_text_diff_is_bounded_and_binary_diff_is_metadata_only():
    text = ArtifactBundle.diff("a\n", "b\n", media_type="text/plain")
    assert "-a" in text and "+b" in text
    binary = ArtifactBundle.diff(b"a", b"b", media_type="application/pdf")
    assert "binary" in binary.lower()

