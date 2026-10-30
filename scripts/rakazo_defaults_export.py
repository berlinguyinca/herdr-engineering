#!/usr/bin/env python3
"""Write herdr_engineering/control_plane/rakazo_defaults.json: every upstream procedure name, and for each read-only Rakazo procedure the bridge answers with
"nothing configured here", the smallest value that satisfies the upstream output schema (fixtures/rakazo_contract.json).
Run after the contract fixture is refreshed; test_rakazo_rest checks the file is current and valid."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from herdr_engineering.control_plane.rakazo_rest import READS  # noqa: E402

CONTRACT = ROOT / "tests/control_plane/fixtures/rakazo_contract.json"
OUT = ROOT / "herdr_engineering/control_plane/rakazo_defaults.json"


def minimal(schema):
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = schema[key]
            nulls = [o for o in options if o.get("type") == "null"]
            return None if nulls else minimal(options[0])
    kind = schema.get("type")
    if isinstance(kind, list):
        return None if "null" in kind else minimal({**schema, "type": kind[0]})
    if kind == "object":
        return {k: minimal(schema["properties"][k]) for k in schema.get("required", [])}
    if kind == "array":
        return [minimal(schema["items"]) for _ in range(schema.get("minItems", 0))]
    if kind == "string":
        if schema.get("format") == "date-time":
            return "1970-01-01T00:00:00.000Z"
        return "x" * schema.get("minLength", 0)
    if kind in ("number", "integer"):
        return max(schema.get("minimum", 0), 0) if "exclusiveMinimum" not in schema else schema["exclusiveMinimum"] + 1
    if kind == "boolean":
        return False
    return None


def main():
    procs = json.loads(CONTRACT.read_text())["procedures"]
    out = {"procedures": sorted(procs), "defaults": {name: minimal(procs[name]["output"]) for name in sorted(READS)}}
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"{len(out['defaults'])} defaults, {len(out['procedures'])} procedure names written")


if __name__ == "__main__":
    main()
