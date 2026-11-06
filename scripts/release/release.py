#!/usr/bin/env python3
"""Release helpers for .woodpecker/release.yml (docs/releases.md). Standard library only: the CI step image is a bare python.

    release.py check-tag --tag vX.Y.Z [--pyproject pyproject.toml]   strict semver tag that equals the repo version
    release.py digest --metadata FILE --out FILE                      the image digest from `buildx build --metadata-file`
    release.py verify-index --raw FILE --digest-file FILE             the read-back bytes ARE the multi-arch index digest
    release.py assemble --tag vX.Y.Z [--pyproject F] --repository R --digest-file F --commit SHA --out-json F --out-notes F
    release.py publish-github --repo OWNER/NAME --tag T --notes F --release-json F   (token from GITHUB_RELEASE_TOKEN)

Every failure exits 1 with one line on stderr; nothing here ever prints a token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TAG_PATTERN = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
# A registry repository without tag or digest: host/path, lower case (OCI distribution spec).
REPOSITORY_PATTERN = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+$")
INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
REQUIRED_PLATFORMS = ("linux/amd64", "linux/arm64")
GITHUB_API = "https://api.github.com"


class ReleaseError(Exception):
    pass


def check_tag(tag: str, pyproject_text: str) -> str:
    """The version for `tag`, which must be strict semver `vX.Y.Z` and equal pyproject.toml's [project].version."""
    if not TAG_PATTERN.fullmatch(tag):
        raise ReleaseError(f"tag {tag!r} is not a strict semver release tag (v[0-9]+.[0-9]+.[0-9]+)")
    version = tomllib.loads(pyproject_text).get("project", {}).get("version")
    if tag[1:] != version:
        raise ReleaseError(f"tag {tag} does not match pyproject.toml [project].version {version!r}")
    return version


def require_digest(digest: str, what: str) -> str:
    if not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest):
        raise ReleaseError(f"{what}: {digest!r} is not a sha256 digest")
    return digest


def digest_from_metadata(metadata: dict) -> str:
    """`containerimage.digest` of a multi-platform build is the digest of the pushed image index."""
    return require_digest(metadata.get("containerimage.digest"), "buildx metadata containerimage.digest")


def verify_index(raw: bytes, digest: str) -> None:
    """Fail unless `raw` (the registry's bytes for repository@digest) hashes to `digest` and is a multi-arch index."""
    require_digest(digest, "expected digest")
    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
    if not raw or actual != digest:
        raise ReleaseError(f"the manifest read back from the registry hashes to {actual}, not {digest}")
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise ReleaseError(f"the manifest read back from the registry is not JSON: {exc}") from None
    if manifest.get("mediaType") not in INDEX_MEDIA_TYPES:
        raise ReleaseError(f"{digest} is a {manifest.get('mediaType')!r}, not a multi-arch image index")
    platforms = {
        f"{m.get('platform', {}).get('os')}/{m.get('platform', {}).get('architecture')}" for m in manifest.get("manifests", [])
    }
    missing = [p for p in REQUIRED_PLATFORMS if p not in platforms]
    if missing:
        raise ReleaseError(f"the image index {digest} lacks {', '.join(missing)}")


def assemble(version: str, repository: str, digest: str, commit: str) -> dict:
    """release.json (deploy/release.schema.json): consumers pin repository@digest, never a tag."""
    if not VERSION_PATTERN.fullmatch(version):
        raise ReleaseError(f"version {version!r} is not X.Y.Z")
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ReleaseError(f"repository {repository!r} is not a bare registry repository (no tag, no digest)")
    require_digest(digest, "image digest")
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ReleaseError(f"commit {commit!r} is not a full 40-character git sha")
    return {"version": version, "image": {"repository": repository, "digest": digest}, "commit": commit}


def release_notes(doc: dict) -> str:
    ref = f"{doc['image']['repository']}@{doc['image']['digest']}"
    return (
        f"## herdr-engineering {doc['version']}\n\n"
        f"Image (linux/amd64, linux/arm64), pin by digest:\n\n```\n{ref}\n```\n\n"
        f"- Image index digest: `{doc['image']['digest']}`\n"
        f"- Commit: `{doc['commit']}`\n"
        f"- Machine-readable: the `release.json` asset of this release (schema: deploy/release.schema.json).\n\n"
        f"The `v{doc['version']}` image tag is a convenience for browsing; it is mutable and nothing should deploy by it.\n"
    )


def _request(url: str, token: str, *, method: str = "GET", data: bytes | None = None, content_type: str | None = None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "herdr-engineering-release"}
    if content_type:
        headers["Content-Type"] = content_type
    return urllib.request.Request(url, data=data, method=method, headers=headers)


def _call(opener, request, what: str):
    try:
        with opener(request, timeout=60) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        # The status and reason only: the request (and its Authorization header) is never part of the message.
        raise ReleaseError(f"{what}: GitHub answered {exc.code} {exc.reason}") from None
    except urllib.error.URLError as exc:
        raise ReleaseError(f"{what}: {exc.reason}") from None


def publish_github(repo: str, tag: str, notes: str, release_json: bytes, token: str, *, opener=urllib.request.urlopen) -> str:
    """Create the GitHub release for `tag` with `notes` and attach release.json; refuse if the release exists."""
    if not token:
        raise ReleaseError("GITHUB_RELEASE_TOKEN is empty")
    base = f"{GITHUB_API}/repos/{repo}"
    try:
        with opener(_request(f"{base}/releases/tags/{urllib.parse.quote(tag)}", token), timeout=60):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise ReleaseError(f"looking up release {tag}: GitHub answered {exc.code} {exc.reason}") from None
    except urllib.error.URLError as exc:
        raise ReleaseError(f"looking up release {tag}: {exc.reason}") from None
    else:
        raise ReleaseError(f"a GitHub release for {tag} already exists; it is never overwritten (docs/releases.md)")
    body = {"tag_name": tag, "name": tag, "body": notes, "draft": False, "prerelease": False}
    created = _call(opener, _request(f"{base}/releases", token, method="POST", data=json.dumps(body).encode(),
                                     content_type="application/json"), f"creating release {tag}")
    upload_url = str(created.get("upload_url", "")).split("{", 1)[0]
    if not upload_url.startswith("https://"):
        raise ReleaseError(f"creating release {tag}: no upload_url in GitHub's answer")
    _call(opener, _request(f"{upload_url}?name=release.json", token, method="POST", data=release_json,
                           content_type="application/json"), "uploading release.json")
    return str(created.get("html_url", ""))


def _read_digest_file(path: str) -> str:
    return require_digest(Path(path).read_text().strip(), path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check-tag")
    p.add_argument("--tag", required=True)
    p.add_argument("--pyproject", default="pyproject.toml")
    p = sub.add_parser("digest")
    p.add_argument("--metadata", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("verify-index")
    p.add_argument("--raw", required=True)
    p.add_argument("--digest-file", required=True)
    p = sub.add_parser("assemble")
    p.add_argument("--pyproject", default="pyproject.toml")
    for name in ("--tag", "--repository", "--digest-file", "--commit", "--out-json", "--out-notes"):
        p.add_argument(name, required=True)
    p = sub.add_parser("publish-github")
    for name in ("--repo", "--tag", "--notes", "--release-json"):
        p.add_argument(name, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check-tag":
            print(check_tag(args.tag, Path(args.pyproject).read_text()))
        elif args.command == "digest":
            digest = digest_from_metadata(json.loads(Path(args.metadata).read_text()))
            Path(args.out).write_text(digest + "\n")
            print(digest)
        elif args.command == "verify-index":
            digest = _read_digest_file(args.digest_file)
            verify_index(Path(args.raw).read_bytes(), digest)
            print(f"verified: {digest} is the multi-arch index ({', '.join(REQUIRED_PLATFORMS)})")
        elif args.command == "assemble":
            version = check_tag(args.tag, Path(args.pyproject).read_text())
            doc = assemble(version, args.repository, _read_digest_file(args.digest_file), args.commit)
            Path(args.out_json).write_text(json.dumps(doc, indent=2) + "\n")
            Path(args.out_notes).write_text(release_notes(doc))
            print(json.dumps(doc))
        elif args.command == "publish-github":
            url = publish_github(args.repo, args.tag, Path(args.notes).read_text(), Path(args.release_json).read_bytes(),
                                 os.environ.get("GITHUB_RELEASE_TOKEN", ""))
            print(f"published {url or args.tag}")
    except (ReleaseError, OSError, ValueError) as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
