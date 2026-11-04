"""Generic private report client; metadata stays separate from supporting bytes."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from .control_plane.artifact_report_descriptor import validate_report_descriptor


class ReportClientError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        # An authenticated artifact client must never forward its key elsewhere.
        return None


class ReportClient:
    def __init__(self, base, *, api_key=None):
        parsed = urlsplit(base)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ReportClientError("a trusted HTTP(S) control-plane URL without credentials is required")
        self.base = base.rstrip("/")
        self.headers = {"X-API-Key": api_key} if api_key else {}
        self.opener = urllib.request.build_opener(NoRedirect())

    def open(self, path, *, body=None, stream=None, size=None, mime="application/json"):
        headers = {**self.headers, "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body, allow_nan=False).encode()
        if stream is not None:
            payload = stream
            headers["Content-Length"] = str(size)
        if payload is not None:
            headers["Content-Type"] = mime
        request = urllib.request.Request(self.base + path, data=payload, headers=headers,
                                         method="POST" if payload is not None else "GET")
        try:
            return self.opener.open(request, timeout=120)
        except urllib.error.HTTPError as error:
            raise ReportClientError(f"report request failed (HTTP {error.code})") from None
        except (OSError, ValueError):
            raise ReportClientError("report transport is unavailable") from None

    def json(self, path, **kwargs):
        with self.open(path, **kwargs) as response:
            data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ReportClientError("report metadata response exceeds its bound")
        try:
            return json.loads(data)
        except ValueError:
            raise ReportClientError("report endpoint did not return structured JSON") from None


def _path(aid, vid=None):
    result = "/api/v1/artifacts/" + quote(aid, safe="")
    return result + "/versions/" + quote(vid, safe="") if vid is not None else result


def _json_file(path):
    source = Path(path)
    if source.stat().st_size > 4 * 1024 * 1024:
        raise ReportClientError("metadata files must contain references, not large inline evidence")
    return json.loads(source.read_text())


def configure_parser(parser):
    commands = parser.add_subparsers(dest="report_action", required=True)
    for action in ("create", "upload-asset", "build", "status", "inspect", "versions", "diff",
                   "feedback", "feedback-list", "check", "preview", "download", "validate"):
        command = commands.add_parser(action)
        command.add_argument("--control-plane")
        command.add_argument("--api-key-env", default="HERDR_API_KEY", help="Environment variable name, never the key itself")
        if action == "create":
            command.add_argument("--title", required=True)
        elif action == "validate":
            command.add_argument("descriptor", type=Path)
        else:
            command.add_argument("artifact_id")
        if action in {"inspect", "diff", "feedback", "feedback-list", "check", "preview", "download"}:
            command.add_argument("version_id")
        if action == "upload-asset":
            command.add_argument("data", type=Path)
            command.add_argument("--name")
            command.add_argument("--mime")
        if action == "build":
            command.add_argument("--base-version", required=True, help="Exact version, or 'none' for the initial version")
            command.add_argument("--descriptor", required=True, type=Path)
            command.add_argument("--assets", type=Path, help="JSON array of scoped upload results")
        if action == "status":
            command.add_argument("job_id")
        if action == "diff":
            command.add_argument("--base-version", required=True)
        if action == "feedback":
            command.add_argument("--target-kind", choices=("report", "finding", "figure"), required=True)
            command.add_argument("--target-id")
            command.add_argument("--body", required=True)
        if action == "feedback-list":
            command.add_argument("--limit", type=int, choices=range(1, 21), default=10,
                                  metavar="1..20", help="Bounded feedback page size (default: 10)")
            command.add_argument("--offset", type=int, default=0)
        if action == "check":
            command.add_argument("--stage", choices=("rendered", "evidence_checked", "reviewed"), required=True)
            command.add_argument("--status", choices=("passed", "failed"), required=True)
            command.add_argument("--notes", required=True)
            command.add_argument("--evidence-ref", action="append", default=[])
        if action == "download":
            command.add_argument("asset_id")
        if action in {"download", "preview"}:
            command.add_argument("--output", type=Path, required=True)
        command.set_defaults(func=run_command)
    return parser


def add_report_subparser(subparsers):
    return configure_parser(subparsers.add_parser("reports", help="versioned, private investigation reports"))


def _save_response(client, path, destination, *, sha256=None, size=None, maximum=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        digest, received = hashlib.sha256(), 0
        with client.open(path) as response, tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, prefix=".report-", delete=False) as stream:
            temporary = Path(stream.name)
            while block := response.read(1024 * 1024):
                received += len(block)
                if maximum is not None and received > maximum or size is not None and received > size:
                    raise ReportClientError("report response exceeds its pinned size")
                digest.update(block)
                stream.write(block)
        if size is not None and received != size or sha256 is not None and digest.hexdigest() != sha256:
            raise ReportClientError("report asset failed pinned integrity validation")
        temporary.replace(destination)
        return {"path": str(destination), "sha256": digest.hexdigest(), "size_bytes": received}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_command(args):
    try:
        action = args.report_action
        if action == "validate":
            descriptor = validate_report_descriptor(_json_file(args.descriptor))
            result = {"status": "schema_valid", "checks": ["descriptor_shape", "reference_integrity"],
                      "evidence_review": "not_checked", "title": descriptor.get("title"),
                      "asset_count": len(descriptor["assets"])}
        else:
            from .host_config import resolve_control_plane
            client = ReportClient(resolve_control_plane(args.control_plane),
                                  api_key=os.environ.get(args.api_key_env))
            aid = getattr(args, "artifact_id", None)
            vid = getattr(args, "version_id", None)
            path = _path(aid, vid) if aid else ""
            if action == "create":
                result = client.json("/api/v1/artifact-reports", body={"title": args.title})
            elif action == "upload-asset":
                name = args.name or args.data.name
                mime = args.mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
                size = args.data.stat().st_size
                if size > 1024 * 1024 * 1024:
                    raise ReportClientError("report assets are limited to 1 GiB per upload")
                with args.data.open("rb") as stream:
                    result = client.json(path + "/blobs?" + urlencode({"filename": name}),
                                         stream=stream, size=size, mime=mime)
            elif action == "build":
                descriptor = validate_report_descriptor(_json_file(args.descriptor))
                assets = _json_file(args.assets) if args.assets else []
                validate_report_descriptor(descriptor, assets)
                result = client.json("/api/v1/artifact-reports/" + quote(aid, safe="") + "/jobs",
                    body={"base_version_id": None if args.base_version == "none" else args.base_version,
                          "request": {"report_descriptor": descriptor, "assets": assets,
                                      "source": json.dumps(descriptor, sort_keys=True)}})
            elif action == "status":
                result = client.json("/api/v1/artifact-reports/" + quote(aid, safe="") +
                                     "/jobs/" + quote(args.job_id, safe=""))
            elif action == "inspect":
                result = client.json(path + "/report")
            elif action == "versions":
                result = client.json(path + "/versions?summary=true")
            elif action == "diff":
                result = client.json(path + "/diff?" + urlencode({"base_version_id": args.base_version}))
            elif action == "feedback":
                if (args.target_kind == "report") != (args.target_id is None):
                    raise ReportClientError("finding/figure feedback requires its stable target ID; report feedback has no target ID")
                result = client.json(path + "/feedback", body={"target_kind": args.target_kind,
                    "target_id": args.target_id, "body": args.body})
            elif action == "feedback-list":
                result = client.json(path + "/feedback?" + urlencode({"limit": args.limit, "offset": args.offset}))
            elif action == "check":
                result = client.json(path + "/checks", body={"stage": args.stage, "status": args.status,
                    "notes": args.notes, "evidence_refs": args.evidence_ref})
            elif action == "preview":
                result = _save_response(client, path + "/preview", args.output, maximum=256 * 1024)
            elif action == "download":
                report = client.json(path + "/report")
                asset = next((item for item in report.get("assets", []) if item["id"] == args.asset_id), None)
                if asset is None:
                    raise ReportClientError("asset is absent from this pinned report version")
                result = _save_response(client, path + "/assets/" + quote(args.asset_id, safe=""),
                                        args.output, sha256=asset["sha256"], size=asset["size_bytes"])
            else:
                raise ReportClientError("unsupported report command")
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        # Host configuration errors are already value-redacted. Transport errors
        # report status only, and credentials never appear in URLs or request JSON.
        message = str(error)
        key = os.environ.get(getattr(args, "api_key_env", "HERDR_API_KEY"))
        if key:
            message = message.replace(key, "[redacted]")
        print(json.dumps({"error": message, "status": "failed"}))
        return 1


def main(argv=None):
    parser = configure_parser(argparse.ArgumentParser(prog="herdr-eng artifacts reports"))
    args = parser.parse_args(argv)
    return run_command(args)
