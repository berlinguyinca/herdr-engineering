"""The graph merge gate stays OFF by default; its documented config is validated at startup and fails closed."""
import re
from pathlib import Path

import pytest
import yaml

from herdr_engineering import cli
from herdr_engineering.control_plane.graph_merge_gate import config_from_section, validate_section
from herdr_engineering.fleet_config import FleetConfigError
from herdr_engineering.fleet_config_cli import validate_config

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "deploy/control-plane/fleet-config.example.yaml"


def example_block(enabled="true", writers=None):
    """The commented ``graph_merge_gate`` block of the shipped example, uncommented as an operator would."""
    lines, out, on = EXAMPLE.read_text().splitlines(), [], False
    for line in lines:
        if re.match(r"\s*# graph_merge_gate:", line):
            on = True
        elif on and not re.match(r"\s*#   \S", line):
            break
        if on:
            out.append(re.sub(r"^(\s*)# ?", r"\1", line))
    text = "\n".join(out).replace("enabled: false", f"enabled: {enabled}")
    if writers is not None:
        text = re.sub(r"evidence_writers: \[.*?\]", f"evidence_writers: {writers}", text)
    return text


def test_example_file_parses_and_ships_the_gate_disabled_and_commented():
    assert "graph_merge_gate" not in yaml.safe_load(EXAMPLE.read_text())["sections"]["control_plane"]["values"]
    block = yaml.safe_load(example_block())
    assert set(block["graph_merge_gate"]) == {"enabled", "evidence_writers", "required_checks", "require_review_quorum"}
    assert validate_section(yaml.safe_load(example_block(enabled="false"))["graph_merge_gate"]) is None


def test_enabling_the_example_verbatim_fails_closed_because_the_writer_is_a_placeholder():
    with pytest.raises(ValueError, match="explicit principal ids"):
        validate_section(yaml.safe_load(example_block())["graph_merge_gate"])


def test_enabling_with_a_real_writer_works_and_names_it():
    cfg = validate_section(yaml.safe_load(example_block(writers='["key:0123456789ab"]'))["graph_merge_gate"])
    assert cfg.evidence_writers == frozenset({"key:0123456789ab"}) and cfg.required_checks == ("ci",)


@pytest.mark.parametrize("raw,match", [
    ({"enabled": True}, "non-empty list"),
    ({"enabled": True, "evidence_writers": []}, "non-empty list"),
    ({"enabled": True, "evidence_writers": ["*"]}, "explicit principal ids"),
    ({"enabled": True, "evidence_writers": ["key:*"]}, "explicit principal ids"),
    ({"enabled": True, "evidence_writers": ["ci"]}, "explicit principal ids"),
    ({"enabled": True, "evidence_writers": ["key:ab", ""]}, "non-empty list"),
    ({"enabled": "true", "evidence_writers": ["key:ab"]}, "true or false"),
    ({"enabled": True, "evidence_writers": ["key:ab"], "required_check": ["ci"]}, "unknown key"),
    ("on", "mapping"),
])
def test_enabling_without_explicit_writers_or_with_a_bad_shape_is_refused(raw, match):
    with pytest.raises(ValueError, match=match):
        validate_section(raw)
    if isinstance(raw, dict) and raw.get("enabled") is True:
        with pytest.raises(ValueError):
            config_from_section(raw)                                         # the runtime path refuses identically


def test_absent_or_disabled_sections_enable_nothing():
    assert validate_section(None) is None and validate_section({}) is None
    assert validate_section({"enabled": False, "evidence_writers": []}) is None


def test_serve_startup_stops_on_a_bad_section_and_the_validate_command_reports_it(tmp_path):
    with pytest.raises(SystemExit, match="evidence_writers"):
        cli._validate_graph_merge_gate({"graph_merge_gate": {"enabled": True, "evidence_writers": []}})
    cli._validate_graph_merge_gate({"graph_merge_gate": {"enabled": False}})   # no error
    cli._validate_graph_merge_gate({})
    from tests.test_fleet_config_cli import make
    p = make(tmp_path)
    assert validate_config(p)["hosts"] >= 0                                    # a good file still validates
    doc = yaml.safe_load(p.read_text())
    doc["sections"]["control_plane"]["values"]["graph_merge_gate"] = {"enabled": True, "evidence_writers": []}
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(FleetConfigError, match="evidence_writers"):
        validate_config(p)
