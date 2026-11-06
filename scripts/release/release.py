#!/usr/bin/env python3
"""Release helpers for .woodpecker/release.yml (docs/releases.md). Standard library only: the CI step image is a bare python.

    release.py check-tag --tag vX.Y.Z [--root .]                      strict semver tag equal to every version source
    release.py digest --metadata FILE --out FILE                      the image digest from `buildx build --metadata-file`
    release.py verify-index --raw FILE --digest-file FILE             the read-back bytes ARE the multi-arch index digest
    release.py assemble --tag vX.Y.Z [--root .] --repository R --digest-file F --commit SHA --out-json F --out-notes F
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
# Besides pyproject.toml, the version the CLI and /api/v1/health report: all must equal the tag.
VERSION_FILES = ("herdr_engineering/version.py", "herdr_engineering/control_plane/__init__.py")
PY_VERSION = re.compile(r'^__version__ = "([^"]*)"$', re.M)


class ReleaseError(Exception):
    pass


def check_tag(tag: str, pyproject_text: str, python_sources: dict[str, str] | None = None) -> str:
    """The version for `tag`: strict semver `vX.Y.Z`, equal to pyproject.toml's [project].version and to the
    `__version__` of every file in `python_sources` ({path: text})."""
    if not TAG_PATTERN.fullmatch(tag):
        raise ReleaseError(f"tag {tag!r} is not a strict semver release tag (v[0-9]+.[0-9]+.[0-9]+)")
    project = tomllib.loads(pyproject_text).get("project", {})
    version = project.get("version") if isinstance(project, dict) else None
    if tag[1:] != version:
        raise ReleaseError(f"tag {tag} does not match pyproject.toml [project].version {version!r}")
    for path, text in (python_sources or {}).items():
        found = PY_VERSION.findall(text)
        if found != [version]:
            raise ReleaseError(f"tag {tag} does not match {path} __version__ {found[0] if len(found) == 1 else found!r}")
    return version


def check_tag_in(tag: str, root: str) -> str:
    base = Path(root)
    return check_tag(tag, (base / "pyproject.toml").read_text(), {p: (base / p).read_text() for p in VERSION_FILES})


def require_digest(digest: str, what: str) -> str:
    if not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest):
        raise ReleaseError(f"{what}: {digest!r} is not a sha256 digest")
    return digest


def digest_from_metadata(metadata: dict) -> str:
    """`containerimage.digest` of a multi-platform build is the digest of the pushed image index."""
    if not isinstance(metadata, dict):
        raise ReleaseError("buildx metadata is not a JSON object, so it has no containerimage.digest")
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
    if not isinstance(manifest, dict) or manifest.get("mediaType") not in INDEX_MEDIA_TYPES:
        kind = manifest.get("mediaType") if isinstance(manifest, dict) else type(manifest).__name__
        raise ReleaseError(f"{digest} is a {kind!r}, not a multi-arch image index")
    entries = manifest.get("manifests")
    if not isinstance(entries, list) or not all(isinstance(m, dict) and isinstance(m.get("platform"), dict) for m in entries):
        raise ReleaseError(f"the image index {digest} has a malformed manifests list")
    platforms = {f"{m['platform'].get('os')}/{m['platform'].get('architecture')}" for m in entries}
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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib re-sends the Authorization header on a redirect, to any host: never follow one with the token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"refusing to follow a redirect ({msg})", headers, fp)


OPENER = urllib.request.build_opener(NoRedirect())


def _call(opener, request, what: str):
    try:
        with opener(request, timeout=60) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        # The status and reason only: the request (and its Authorization header) is never part of the message.
        raise ReleaseError(f"{what}: GitHub answered {exc.code} {exc.reason}") from None
    except urllib.error.URLError as exc:
        raise ReleaseError(f"{what}: {exc.reason}") from None


def publish_github(repo: str, tag: str, notes: str, release_json: bytes, token: str, *, opener=OPENER.open) -> str:
    """Publish the GitHub release for `tag` with `notes` and release.json, never public without the asset.

    A draft is created (or the existing draft for `tag`, left by a failed run, is reused), its release.json is
    replaced by `release_json`, the asset is read back, and only then is the draft published. A release that is
    already published is never touched.
    """
    if not token:
        raise ReleaseError("GITHUB_RELEASE_TOKEN is empty")
    base = f"{GITHUB_API}/repos/{repo}"

    def call(url, what, **kw):
        if "data" in kw and not isinstance(kw["data"], bytes):
            kw["data"] = json.dumps(kw["data"]).encode()
            kw.setdefault("content_type", "application/json")
        return _call(opener, _request(url, token, **kw), what)

    # The by-tag endpoint does not return drafts; the list does (to a token with push access). Drafts are newest-first.
    releases = call(f"{base}/releases?per_page=100", f"listing releases for {tag}")
    mine = [r for r in releases if isinstance(r, dict) and r.get("tag_name") == tag] if isinstance(releases, list) else []
    if any(not r.get("draft") for r in mine):
        raise ReleaseError(f"a GitHub release for {tag} is already published; it is never overwritten (docs/releases.md)")
    if mine:
        draft = mine[0]
    else:
        draft = call(f"{base}/releases", f"creating draft release {tag}", method="POST",
                     data={"tag_name": tag, "name": tag, "body": notes, "draft": True, "prerelease": False})
    rid = draft.get("id")
    upload_url = str(draft.get("upload_url", "")).split("{", 1)[0]
    if not isinstance(rid, int) or not upload_url.startswith("https://"):
        raise ReleaseError(f"draft release {tag}: no id or upload_url in GitHub's answer")
    for asset in draft.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") == "release.json":
            call(f"{base}/releases/assets/{asset.get('id')}", "removing the stale release.json", method="DELETE")
    call(f"{upload_url}?name=release.json", "uploading release.json", method="POST", data=release_json,
         content_type="application/json")
    assets = call(f"{base}/releases/{rid}/assets", "reading back release.json")
    if not any(isinstance(a, dict) and a.get("name") == "release.json" and a.get("size") == len(release_json)
               for a in (assets if isinstance(assets, list) else [])):
        raise ReleaseError(f"release.json is not on draft release {tag} after the upload; the draft stays unpublished")
    published = call(f"{base}/releases/{rid}", f"publishing release {tag}", method="PATCH",
                     data={"draft": False, "body": notes, "name": tag})
    if not isinstance(published, dict) or published.get("draft") is not False:
        raise ReleaseError(f"release {tag} is still a draft after publishing")
    return str(published.get("html_url", ""))


def _read_digest_file(path: str) -> str:
    return require_digest(Path(path).read_text().strip(), path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check-tag")
    p.add_argument("--tag", required=True)
    p.add_argument("--root", default=".")
    p = sub.add_parser("digest")
    p.add_argument("--metadata", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("verify-index")
    p.add_argument("--raw", required=True)
    p.add_argument("--digest-file", required=True)
    p = sub.add_parser("assemble")
    p.add_argument("--root", default=".")
    for name in ("--tag", "--repository", "--digest-file", "--commit", "--out-json", "--out-notes"):
        p.add_argument(name, required=True)
    p = sub.add_parser("publish-github")
    for name in ("--repo", "--tag", "--notes", "--release-json"):
        p.add_argument(name, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check-tag":
            print(check_tag_in(args.tag, args.root))
        elif args.command == "digest":
            digest = digest_from_metadata(json.loads(Path(args.metadata).read_text()))
            Path(args.out).write_text(digest + "\n")
            print(digest)
        elif args.command == "verify-index":
            digest = _read_digest_file(args.digest_file)
            verify_index(Path(args.raw).read_bytes(), digest)
            print(f"verified: {digest} is the multi-arch index ({', '.join(REQUIRED_PLATFORMS)})")
        elif args.command == "assemble":
            version = check_tag_in(args.tag, args.root)
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
