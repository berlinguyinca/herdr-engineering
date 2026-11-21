"""Every service on the herdr-engineering image must take its tag from HERDR_CP_IMAGE_TAG, so the
commit-tagged image deploy.sh builds is the one compose runs. A literal tag silently pins a service to a
stale image (the fabric gateway sat on 0.1.0 while every other service rolled)."""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "control-plane" / "compose.yaml"
PARAMETERISED = re.compile(r"^herdr-engineering:\$\{HERDR_CP_IMAGE_TAG(:\?[^}]+|:-unset)\}$")


def _engineering_services() -> dict[str, str]:
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    return {n: s["image"] for n, s in services.items() if s.get("image", "").startswith("herdr-engineering:")}


def test_the_compose_file_has_engineering_image_services():
    assert "fabric-gateway" in _engineering_services()


@pytest.mark.parametrize("name,image", sorted(_engineering_services().items()))
def test_engineering_image_tag_is_parameterised(name, image):
    assert PARAMETERISED.match(image), f"{name}: image {image!r} has a literal tag; use ${{HERDR_CP_IMAGE_TAG:?run deploy.sh}}"


def test_gateway_keeps_its_fd_limit():
    gw = yaml.safe_load(COMPOSE.read_text())["services"]["fabric-gateway"]
    assert gw["ulimits"]["nofile"] == {"soft": 65536, "hard": 524288}
