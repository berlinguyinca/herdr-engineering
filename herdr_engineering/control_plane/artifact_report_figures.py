"""Read the bytes of the images a report's figures point at, for inline display in its preview.

Only assets named by a descriptor figure are ever read, only when their pinned media type is an image type and
their pinned size fits the per-figure cap; each object is hash- and size-verified like a download. Anything that
cannot be read or does not verify is simply left out, and the figure keeps its caption and download link.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable

from .artifact_report_render import IMAGE_MEDIA_TYPES, MAX_FIGURE_DATA_URI, FigureImages

# base64 grows 4/3 and the data: prefix is at most ~30 characters.
MAX_FIGURE_BYTES = (MAX_FIGURE_DATA_URI - 32) // 4 * 3
MAX_FIGURES_READ = 8
MAX_TOTAL_BYTES = 192 * 1024
_CHUNK = 64 * 1024


def _verified_bytes(rustfs, asset: dict) -> bytes | None:
    digest, parts, size = hashlib.sha256(), [], 0
    for chunk in rustfs.iter_get(asset["storage_key"], asset["sha256"], chunk_size=_CHUNK):
        if not isinstance(chunk, bytes):
            return None
        size += len(chunk)
        if size > asset["size_bytes"] or size > MAX_FIGURE_BYTES:
            return None
        digest.update(chunk)
        parts.append(chunk)
    if size != asset["size_bytes"] or digest.hexdigest() != asset["sha256"]:
        return None
    return b"".join(parts)


def load_figure_images(descriptor: dict, rustfs, get_asset: Callable[[str], dict]) -> FigureImages:
    """``{asset_id: (media_type, bytes)}`` for the embeddable figure images; ``get_asset`` is the permission-checked
    registry lookup the download route uses (it raises for an unknown or unverified asset)."""
    if rustfs is None or not hasattr(rustfs, "iter_get"):
        return {}
    pinned = {asset.get("id"): asset for asset in descriptor.get("assets", []) if isinstance(asset, dict)}
    found: dict[str, tuple[str, bytes]] = {}
    total = 0
    for figure in descriptor.get("figures", []):
        asset_id = figure.get("asset_id")
        listed = pinned.get(asset_id)
        if (asset_id in found or listed is None or listed.get("media_type") not in IMAGE_MEDIA_TYPES
                or not isinstance(listed.get("size_bytes"), int) or not 0 < listed["size_bytes"] <= MAX_FIGURE_BYTES
                or total + listed["size_bytes"] > MAX_TOTAL_BYTES or len(found) >= MAX_FIGURES_READ):
            continue
        try:
            asset = get_asset(asset_id)
            if (asset.get("media_type") not in IMAGE_MEDIA_TYPES or asset.get("sha256") != listed.get("sha256")
                    or asset.get("size_bytes") != listed["size_bytes"]):
                continue
            data = _verified_bytes(rustfs, asset)
        except Exception:  # noqa: BLE001 - any failure to read or verify just means "no inline image"
            continue
        if data:
            found[asset_id] = (asset["media_type"], data)
            total += len(data)
    return found
