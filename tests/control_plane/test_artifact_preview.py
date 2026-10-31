from herdr_engineering.control_plane.artifact_preview import PreviewPolicy


def test_preview_policy_maps_supported_formats_to_safe_modes():
    assert PreviewPolicy.for_media_type("application/pdf").mode == "pdf"
    assert PreviewPolicy.for_media_type("image/png").mode == "image"
    assert PreviewPolicy.for_media_type("text/markdown").mode == "markdown"
    assert PreviewPolicy.for_media_type("application/json").mode == "json"
    assert PreviewPolicy.for_media_type("text/html").sandboxed is True


def test_unsupported_documents_are_download_only_with_safe_headers():
    policy = PreviewPolicy.for_media_type(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert policy.mode == "download"
    assert policy.content_disposition == "attachment"
    assert policy.headers()["X-Content-Type-Options"] == "nosniff"

