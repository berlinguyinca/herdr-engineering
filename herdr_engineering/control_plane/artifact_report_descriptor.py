"""Domain-neutral report descriptors and bounded semantic version comparisons.

Validation establishes structure and traceability, not the truth of a finding.
The diff classifies declared fields; a revision can affect multiple categories.
Neither function reads assets or performs domain-specific calculations.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

MAX_DESCRIPTOR_BYTES = 512 * 1024
MAX_COLLECTION_ITEMS = 2000
MAX_TEXT_LENGTH = 16384
MAX_JSON_DEPTH = 16
MAX_DIFF_ENTRIES = 200
MAX_DIFF_VALUE_BYTES = 512

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MEDIA = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")
_COLLECTIONS = ("questions", "evidence", "findings", "figures", "assets")
_ROOT_FIELDS = {"schema_version", "title", "summary", *_COLLECTIONS,
                "analysis", "limitations", "next_steps"}
_ASSET_ROLES = {"data", "source", "figure", "result", "preview"}


class ReportDescriptorError(ValueError):
    """The descriptor cannot be safely interpreted as a version-one report."""


def _fail(path: str, message: str) -> None:
    raise ReportDescriptorError(f"{path}: {message}")


def _object(value: Any, path: str, required: set[str], allowed: set[str]) -> None:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    missing = required - value.keys()
    unknown = value.keys() - allowed
    if missing:
        _fail(path, "missing fields: " + ", ".join(sorted(missing)))
    if unknown:
        _fail(path, "unsupported fields: " + ", ".join(sorted(unknown)))


def _text(value: Any, path: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        _fail(path, "must be a nonempty string" if nonempty else "must be a string")
    if len(value) > MAX_TEXT_LENGTH:
        _fail(path, f"text exceeds {MAX_TEXT_LENGTH} characters")


def _identifier(value: Any, path: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail(path, "must be a safe stable identifier of at most 128 characters")


def _asset_name(value: Any, path: str) -> None:
    if (not isinstance(value, str) or not value or len(value) > 1024
            or ":" in value or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        _fail(path, "must be a safe relative asset name")


def _strings(value: Any, path: str, *, references: bool = False,
             required: bool = False) -> None:
    if not isinstance(value, list) or len(value) > MAX_COLLECTION_ITEMS:
        _fail(path, f"must be an array of at most {MAX_COLLECTION_ITEMS} strings")
    if required and not value:
        _fail(path, "must contain at least one reference")
    for index, item in enumerate(value):
        (_identifier if references else _text)(item, f"{path}[{index}]")
    if references and len(set(value)) != len(value):
        _fail(path, "duplicate references are not allowed")


def _json_tree(value: Any) -> None:
    pending = [(value, "$", 0)]
    while pending:
        current, path, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            _fail(path, f"JSON depth exceeds {MAX_JSON_DEPTH}")
        if isinstance(current, dict):
            if len(current) > MAX_COLLECTION_ITEMS:
                _fail(path, "too many object fields")
            for key, item in current.items():
                if not isinstance(key, str) or len(key) > 256:
                    _fail(path, "object keys must be strings of at most 256 characters")
                pending.append((item, path + "/" + key, depth + 1))
        elif isinstance(current, list):
            if len(current) > MAX_COLLECTION_ITEMS:
                _fail(path, "too many array entries")
            pending.extend((item, f"{path}[{index}]", depth + 1)
                           for index, item in enumerate(current))
        elif isinstance(current, str):
            _text(current, path, nonempty=False)
        elif current is None or isinstance(current, bool):
            continue
        elif isinstance(current, (int, float)):
            if isinstance(current, float) and not math.isfinite(current):
                _fail(path, "numbers must be finite")
        else:
            _fail(path, "value is not JSON")


def _check_links(rows: list[dict], field: str, targets: set[str], path: str) -> None:
    for index, row in enumerate(rows):
        value = row.get(field, [])
        references = value if isinstance(value, list) else [value]
        missing = set(references) - targets
        if missing:
            _fail(f"{path}[{index}].{field}", "unknown references: " + ", ".join(sorted(missing)))


def _check_manifest(assets: list[dict], manifest: Any) -> None:
    if not isinstance(manifest, list):
        _fail("output_manifest", "must be an array")
    for asset in assets:
        matches = [output for output in manifest
                   if isinstance(output, dict) and output.get("name") == asset["name"]]
        if len(matches) != 1:
            _fail("output_manifest", f"asset {asset['id']} must have exactly one matching name")
        output = matches[0]
        digest = output.get("sha256") or output.get("blob_sha256")
        if digest != asset["sha256"]:
            _fail("output_manifest", f"asset {asset['id']} digest differs")
        if output.get("sha256") and output.get("blob_sha256") and output["sha256"] != output["blob_sha256"]:
            _fail("output_manifest", f"asset {asset['id']} has conflicting digest fields")
        if "size_bytes" in output and type(output["size_bytes"]) is not int:
            _fail("output_manifest", f"asset {asset['id']} size_bytes must be an integer")
        for field in ("size_bytes", "media_type"):
            if field in output and output[field] != asset[field]:
                _fail("output_manifest", f"asset {asset['id']} {field} differs")


def validate_report_descriptor(descriptor: Any,
                               output_manifest: list[dict] | None = None) -> dict:
    """Validate v1 shape/references and return a detached, deterministically ordered copy.

    ID collections are unordered semantically; configuration arrays and narrative
    lists retain their order. Optional manifest checks establish matching metadata,
    not blob existence, ownership or byte integrity: the registry checks those.
    """
    try:
        serialized = json.dumps(descriptor, ensure_ascii=False, allow_nan=False,
                                sort_keys=True, separators=(",", ":"))
        size = len(serialized.encode("utf-8"))
    except RecursionError as exc:
        raise ReportDescriptorError("descriptor exceeds maximum JSON depth") from exc
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReportDescriptorError("descriptor must contain finite, UTF-8 JSON values") from exc
    if size > MAX_DESCRIPTOR_BYTES:
        _fail("descriptor", f"size exceeds {MAX_DESCRIPTOR_BYTES} bytes; use separate assets")
    _json_tree(descriptor)
    _object(descriptor, "descriptor", {"schema_version", *_COLLECTIONS}, _ROOT_FIELDS)
    if type(descriptor["schema_version"]) is not int or descriptor["schema_version"] != 1:
        _fail("schema_version", "must be integer 1")
    for field in ("title", "summary"):
        if field in descriptor:
            _text(descriptor[field], field, nonempty=False)
    for field in ("limitations", "next_steps"):
        if field in descriptor:
            _strings(descriptor[field], field)

    ids = {}
    total_items = 0
    for collection in _COLLECTIONS:
        rows = descriptor[collection]
        if not isinstance(rows, list):
            _fail(collection, "must be an array")
        total_items += len(rows)
        identifiers = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or "id" not in row:
                _fail(f"{collection}[{index}]", "must be an object with an id")
            _identifier(row["id"], f"{collection}[{index}].id")
            identifiers.append(row["id"])
        if len(set(identifiers)) != len(identifiers):
            _fail(collection, "duplicate stable IDs are not allowed")
        ids[collection] = set(identifiers)
    if total_items > MAX_COLLECTION_ITEMS:
        _fail("descriptor", f"collections contain more than {MAX_COLLECTION_ITEMS} entries")

    for index, row in enumerate(descriptor["questions"]):
        path = f"questions[{index}]"
        _object(row, path, {"id", "text"}, {"id", "text"})
        _text(row["text"], path + ".text")
    for index, row in enumerate(descriptor["evidence"]):
        path = f"evidence[{index}]"
        _object(row, path, {"id", "title", "description"},
                {"id", "title", "description", "asset_ids"})
        for field in ("title", "description"):
            _text(row[field], path + "." + field)
        _strings(row.get("asset_ids", []), path + ".asset_ids", references=True)
    for index, row in enumerate(descriptor["findings"]):
        path = f"findings[{index}]"
        required = {"id", "title", "statement", "question_ids", "evidence_ids"}
        _object(row, path, required, required | {"figure_ids", "limitations", "next_steps"})
        for field in ("title", "statement"):
            _text(row[field], path + "." + field)
        for field in ("question_ids", "evidence_ids"):
            _strings(row[field], path + "." + field, references=True, required=True)
        _strings(row.get("figure_ids", []), path + ".figure_ids", references=True)
        for field in ("limitations", "next_steps"):
            _strings(row.get(field, []), path + "." + field)
    for index, row in enumerate(descriptor["figures"]):
        path = f"figures[{index}]"
        required = {"id", "title", "asset_id", "caption"}
        _object(row, path, required, required | {"reading_guide"})
        for field in ("title", "caption"):
            _text(row[field], path + "." + field)
        _identifier(row["asset_id"], path + ".asset_id")
        if "reading_guide" in row:
            _text(row["reading_guide"], path + ".reading_guide")
    names = set()
    for index, row in enumerate(descriptor["assets"]):
        path = f"assets[{index}]"
        required = {"id", "name", "role", "media_type", "sha256", "size_bytes"}
        _object(row, path, required, required)
        _asset_name(row["name"], path + ".name")
        if row["name"] in names:
            _fail(path + ".name", "duplicate asset names are not allowed")
        names.add(row["name"])
        if not isinstance(row["role"], str) or row["role"] not in _ASSET_ROLES:
            _fail(path + ".role", "unsupported asset role")
        if not isinstance(row["media_type"], str) or not _MEDIA.fullmatch(row["media_type"]):
            _fail(path + ".media_type", "must be a MIME type without parameters")
        if not isinstance(row["sha256"], str) or not _SHA.fullmatch(row["sha256"]):
            _fail(path + ".sha256", "must be a lowercase SHA-256 digest")
        if type(row["size_bytes"]) is not int or not 0 <= row["size_bytes"] <= 2**63 - 1:
            _fail(path + ".size_bytes", "must be a nonnegative signed-64-bit integer")
    for field, target in (("question_ids", "questions"), ("evidence_ids", "evidence"),
                          ("figure_ids", "figures")):
        _check_links(descriptor["findings"], field, ids[target], "findings")
    _check_links(descriptor["evidence"], "asset_ids", ids["assets"], "evidence")
    _check_links(descriptor["figures"], "asset_id", ids["assets"], "figures")

    if "analysis" in descriptor:
        analysis = descriptor["analysis"]
        _object(analysis, "analysis", set(), {"method", "configuration", "code_revision", "source_hashes"})
        for field in ("method", "code_revision"):
            if field in analysis:
                _text(analysis[field], "analysis." + field, nonempty=False)
        for field in ("configuration", "source_hashes"):
            if field in analysis and not isinstance(analysis[field], dict):
                _fail("analysis." + field, "must be an object")
        for name, digest in analysis.get("source_hashes", {}).items():
            _asset_name(name, "analysis.source_hashes name")
            if not isinstance(digest, str) or not _SHA.fullmatch(digest):
                _fail("analysis.source_hashes." + name, "must be a lowercase SHA-256 digest")
    if output_manifest is not None:
        _check_manifest(descriptor["assets"], output_manifest)
    canonical = json.loads(serialized)
    for collection in _COLLECTIONS:
        canonical[collection].sort(key=lambda row: row["id"])
    return canonical


def _projection(descriptor: dict) -> dict:
    data, analysis, editorial, presentation = {}, {}, {}, {}
    editorial.update({field: descriptor.get(field, "") for field in ("title", "summary")})
    editorial.update({field: descriptor.get(field, []) for field in ("limitations", "next_steps")})
    editorial["questions"] = {row["id"]: {"text": row["text"]} for row in descriptor["questions"]}
    editorial["findings"] = {row["id"]: {key: value for key, value in row.items() if key != "id"}
                             for row in descriptor["findings"]}
    editorial["evidence"] = {row["id"]: {"title": row["title"]} for row in descriptor["evidence"]}
    data["evidence"] = {row["id"]: {"description": row["description"],
                                      "asset_ids": row.get("asset_ids", [])}
                        for row in descriptor["evidence"]}
    analysis["analysis"] = descriptor.get("analysis", {})
    presentation["figures"] = {row["id"]: {key: value for key, value in row.items() if key != "id"}
                                for row in descriptor["figures"]}
    for category in (data, analysis, presentation):
        category["assets"] = {}
    presentation["asset_metadata"] = {}
    for asset in descriptor["assets"]:
        target = data if asset["role"] in {"data", "result"} else analysis if asset["role"] == "source" else presentation
        target["assets"][asset["id"]] = {key: asset[key] for key in ("role", "sha256", "size_bytes")}
        presentation["asset_metadata"][asset["id"]] = {key: asset[key] for key in ("name", "media_type")}
    return {"data": data, "analysis": analysis, "editorial": editorial, "presentation": presentation}


def _bounded_value(value: Any) -> Any:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_DIFF_VALUE_BYTES:
        return value
    return {"truncated": True, "preview": text[:256], "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest()}


def _pointer(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def report_diff(left: dict, right: dict) -> dict:
    """Compare two validated descriptors by stable IDs, with bounded change details.

    Data includes evidence descriptions and data/result asset content. Analysis
    includes method/configuration/code and source asset content. Editorial covers
    questions, findings, labels and narrative. Presentation covers figure content,
    captions and asset names/media types. Counts remain complete if detail is capped.
    """
    before = _projection(validate_report_descriptor(left))
    after = _projection(validate_report_descriptor(right))
    categories = {}
    remaining = MAX_DIFF_ENTRIES
    for name in ("data", "analysis", "editorial", "presentation"):
        result = {"changed": False, "added": [], "removed": [], "modified": [],
                  "counts": {"added": 0, "removed": 0, "modified": 0}, "truncated": False}

        def record(kind, path, old=None, new=None):
            nonlocal remaining
            result["changed"] = True
            result["counts"][kind] += 1
            if remaining <= 0:
                result["truncated"] = True
                return
            entry = {"path": path}
            if kind != "added":
                entry["old"] = _bounded_value(old)
            if kind != "removed":
                entry["new"] = _bounded_value(new)
            result[kind].append(entry)
            remaining -= 1

        def compare(a, b, path):
            if isinstance(a, dict) and isinstance(b, dict):
                for key in sorted(a.keys() | b.keys()):
                    at = path + "/" + _pointer(key)
                    if key not in a:
                        record("added", at, new=b[key])
                    elif key not in b:
                        record("removed", at, old=a[key])
                    else:
                        compare(a[key], b[key], at)
            elif json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True):
                record("modified", path, a, b)

        compare(before[name], after[name], "")
        categories[name] = result
    return {"changed": any(value["changed"] for value in categories.values()),
            "categories": categories,
            "change_count": sum(sum(value["counts"].values()) for value in categories.values()),
            "detail_limit": MAX_DIFF_ENTRIES,
            "truncated": any(value["truncated"] for value in categories.values())}
