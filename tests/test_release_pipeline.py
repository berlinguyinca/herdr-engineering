"""The release pipeline (.woodpecker/release.yml, docs/releases.md) and its release.json assembly.

Offline: the pipeline is parsed, not run; scripts/release/release.py is exercised on the fixtures in
tests/fixtures/release/ with no network and no docker. The GitHub release upload goes through a fake opener.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / ".woodpecker" / "release.yml"
SCRIPTS = ROOT / "scripts" / "release"
SCHEMA = ROOT / "deploy" / "release.schema.json"
FIXTURES = ROOT / "tests" / "fixtures" / "release"

PINNED_IMAGE = re.compile(r"^[a-z0-9./_-]+(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
COMMIT = "0123456789abcdef0123456789abcdef01234567"
REPOSITORY = "ghcr.io/berlinguyinca/herdr-engineering"
FIXTURE_DIGEST = "sha256:fef44e51eea0c6533d611b828ed36160aa2966a5dfc787b5683d12e8150c7221"


def load_release_module():
    spec = importlib.util.spec_from_file_location("herdr_release_script", SCRIPTS / "release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def release():
    return load_release_module()


@pytest.fixture(scope="module")
def pipeline():
    return yaml.safe_load(PIPELINE.read_text())


def steps(pipeline):
    return {step["name"]: step for step in pipeline["steps"]}


def step_text(step):
    """A step's commands plus the text of every repository script those commands run."""
    commands = "\n".join(step.get("commands", []))
    text = [commands]
    for script in re.findall(r"scripts/release/[A-Za-z0-9_.-]+", commands):
        text.append((ROOT / script).read_text())
    return "\n".join(text)


def all_release_text():
    files = [PIPELINE, *sorted(SCRIPTS.iterdir())]
    return {path: path.read_text() for path in files if path.is_file()}


# --- the pipeline definition ---------------------------------------------------------------------------------------------


def test_runs_only_on_a_version_tag_never_on_pull_requests_or_pushes(pipeline):
    assert pipeline["when"] == [{"event": "tag", "ref": "refs/tags/v*"}]
    for step in pipeline["steps"]:
        assert "when" not in step, f"{step['name']} must not widen or narrow the pipeline trigger"


def test_strict_semver_gate_is_the_first_step_after_checkout(pipeline, release):
    names = [step["name"] for step in pipeline["steps"]]
    assert names[:2] == ["checkout", "verify-tag"]
    assert "release.py check-tag" in step_text(steps(pipeline)["verify-tag"])
    assert release.TAG_PATTERN.pattern == r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"


def test_every_image_and_the_build_tooling_is_pinned_by_digest(pipeline):
    assert pipeline.get("skip_clone") is True, "the default clone image is not pinned; checkout is an explicit step"
    for step in pipeline["steps"]:
        assert PINNED_IMAGE.match(step["image"]), f"{step['name']}: {step['image']} is not pinned by digest"
    env = {k: v for step in pipeline["steps"] for k, v in step.get("environment", {}).items() if isinstance(v, str)}
    assert PINNED_IMAGE.match(env["HERDR_BUILDKIT_IMAGE"])
    assert PINNED_IMAGE.match(env["HERDR_RUNTIME_BASE"])


def test_checkout_proves_the_commit_and_that_it_is_on_main(pipeline):
    text = step_text(steps(pipeline)["checkout"])
    assert '"$CI_COMMIT_SHA"' in text and "git rev-parse HEAD" in text
    assert "merge-base --is-ancestor" in text


def test_build_is_multi_arch_and_pushes_by_digest(pipeline):
    text = step_text(steps(pipeline)["build-push"])
    assert "docker buildx build" in text
    assert "--platform linux/amd64,linux/arm64" in text
    assert "push-by-digest=true" in text and "push=true" in text
    assert "-f deploy/fabric-stack/Dockerfile" in text
    assert "--metadata-file" in text
    assert "--build-arg \"GIT_COMMIT=$CI_COMMIT_SHA\"" in text
    assert "--build-arg \"RUNTIME_BASE=$HERDR_RUNTIME_BASE\"" in text


def test_digest_is_read_back_and_verified_before_anything_is_published(pipeline):
    names = [step["name"] for step in pipeline["steps"]]
    order = ["build-push", "digest", "read-back", "release-json", "tag-image", "github-release"]
    assert [n for n in names if n in order] == order
    assert "release.py digest" in step_text(steps(pipeline)["digest"])
    read_back = step_text(steps(pipeline)["read-back"])
    assert "imagetools inspect --raw" in read_back
    release_json = step_text(steps(pipeline)["release-json"])
    assert "release.py verify-index" in release_json and "release.py assemble" in release_json
    assert "--out-json .release/release.json" in release_json
    assert "--commit \"$CI_COMMIT_SHA\"" in release_json
    assert "assemble --tag \"$CI_COMMIT_TAG\" --repository" in release_json
    assert "release.py publish-github" in step_text(steps(pipeline)["github-release"])


def test_secrets_are_referenced_by_name_only(pipeline):
    secret_steps = {}
    for step in pipeline["steps"]:
        for key, value in step.get("environment", {}).items():
            if isinstance(value, dict):
                assert set(value) == {"from_secret"}, f"{step['name']}.{key}"
                assert re.fullmatch(r"[a-z0-9_]+", value["from_secret"])
                secret_steps.setdefault(value["from_secret"], []).append(step["name"])
            elif re.search(r"TOKEN|PASSWORD|SECRET", key):
                pytest.fail(f"{step['name']}.{key} holds a literal where a from_secret is required")
    assert set(secret_steps) == {"ghcr_push_token", "github_release_token"}
    assert sorted(secret_steps["ghcr_push_token"]) == ["build-push", "read-back", "tag-image"]
    assert secret_steps["github_release_token"] == ["github-release"]


def test_no_literal_credentials_and_no_secret_echo():
    token_shapes = re.compile(r"(ghp_|gho_|ghs_|ghu_|github_pat_)[A-Za-z0-9_]{10,}|login[^\n]*\s-p\s|--password[\s=]")
    for path, text in all_release_text().items():
        assert not token_shapes.search(text), f"{path} contains a credential or a password argument"
        assert "set -x" not in text, f"{path} traces commands (would print secrets)"
        for line in text.splitlines():
            if re.search(r"\b(echo|printf)\b", line) and re.search(r"TOKEN", line):
                assert "--password-stdin" in line, f"{path}: {line.strip()} may print a secret"


def test_shell_scripts_are_posix_and_shellcheck_clean():
    scripts = sorted(SCRIPTS.glob("*.sh"))
    assert scripts
    for script in scripts:
        assert script.read_text().startswith("#!/bin/sh\n")
        subprocess.run(["sh", "-n", str(script)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", "-s", "sh", *map(str, scripts)], check=True)


def test_docs_describe_the_manual_gate():
    text = (ROOT / "docs" / "releases.md").read_text()
    for needle in ("owner", "v[0-9]+.[0-9]+.[0-9]+", "release.json", "ghcr_push_token", "github_release_token", "@sha256:",
                   "Tag ruleset", "required before the first release", "not a security boundary", "## Rollback",
                   "never re-tag", "## Known gap", "base image only", "keep deploying with `deploy.sh`", "classic",
                   "fine-grained", "draft", "## Builder cleanup"):
        assert needle.lower() in text.lower(), needle
    binfmt = re.search(r"tonistiigi/binfmt\S*", text).group(0)
    assert PINNED_IMAGE.match(binfmt), binfmt


# --- scripts/release/release.py ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tag", ["v0.1.0", "v10.20.30"])
def test_check_tag_accepts_the_matching_strict_semver_tag(release, tag):
    version = tag[1:]
    assert release.check_tag(tag, f'[project]\nname = "x"\nversion = "{version}"\n') == version


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1", "v0.1.0-rc1", "v0.1.0+meta", "v01.1.0", "release/v0.1.0", "v0.1.0\n", ""])
def test_check_tag_rejects_anything_but_strict_semver(release, tag):
    with pytest.raises(release.ReleaseError, match="semver"):
        release.check_tag(tag, '[project]\nversion = "0.1.0"\n')


def test_check_tag_rejects_a_tag_that_does_not_match_the_repo_version(release):
    with pytest.raises(release.ReleaseError, match="pyproject.toml"):
        release.check_tag("v0.2.0", '[project]\nversion = "0.1.0"\n')


def test_check_tag_against_the_real_version_sources(release):
    pyproject = (ROOT / "pyproject.toml").read_text()
    version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    sources = {path: (ROOT / path).read_text() for path in release.VERSION_FILES}
    assert set(release.VERSION_FILES) == {"herdr_engineering/version.py", "herdr_engineering/control_plane/__init__.py"}
    assert release.check_tag(f"v{version}", pyproject, sources) == version


@pytest.mark.parametrize("source", ['__version__ = "0.0.9"\n', "VERSION = '0.1.0'\n", ""])
def test_check_tag_rejects_a_python_version_source_that_disagrees(release, source):
    with pytest.raises(release.ReleaseError, match="herdr_engineering/version.py"):
        release.check_tag("v0.1.0", '[project]\nversion = "0.1.0"\n', {"herdr_engineering/version.py": source})


def test_check_tag_cli_reads_every_version_source(release, tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    for path in release.VERSION_FILES:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text('__version__ = "0.1.0"\n')
    args = ["check-tag", "--tag", "v0.1.0", "--root", str(tmp_path)]
    assert release.main(args) == 0
    (tmp_path / "herdr_engineering/control_plane/__init__.py").write_text('__version__ = "0.0.1"\n')
    assert release.main(args) == 1
    assert "control_plane/__init__.py" in capsys.readouterr().err


def test_digest_comes_from_the_buildx_metadata(release):
    metadata = json.loads((FIXTURES / "build-metadata.json").read_text())
    assert metadata["containerimage.config.digest"] != FIXTURE_DIGEST  # the config digest is never the image's digest
    assert release.digest_from_metadata(metadata) == FIXTURE_DIGEST


@pytest.mark.parametrize("raw", [b"[]", b'"sha256:x"', b'{"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": {}}',
                                 b'{"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": ["x"]}',
                                 b'{"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [{"platform": "linux"}]}'])
def test_non_object_json_fails_cleanly(release, raw):
    with pytest.raises(release.ReleaseError):
        release.verify_index(raw, "sha256:" + hashlib.sha256(raw).hexdigest())


def test_non_object_metadata_is_one_line_on_stderr_not_a_traceback(release, tmp_path, capsys):
    (tmp_path / "m.json").write_text("[1, 2]")
    assert release.main(["digest", "--metadata", str(tmp_path / "m.json"), "--out", str(tmp_path / "d")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("release: ") and "Traceback" not in err and err.count("\n") == 1


@pytest.mark.parametrize("metadata", [{}, {"containerimage.digest": ""}, {"containerimage.digest": "sha256:abc"}])
def test_missing_or_malformed_digest_fails(release, metadata):
    with pytest.raises(release.ReleaseError, match="digest"):
        release.digest_from_metadata(metadata)


def test_fixture_digest_is_the_sha256_of_the_index_bytes():
    raw = (FIXTURES / "index.json").read_bytes()
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == FIXTURE_DIGEST


def test_verify_index_accepts_the_multi_arch_index(release):
    release.verify_index((FIXTURES / "index.json").read_bytes(), FIXTURE_DIGEST)


def test_verify_index_rejects_bytes_that_do_not_hash_to_the_digest(release):
    with pytest.raises(release.ReleaseError, match="read back"):
        release.verify_index((FIXTURES / "index.json").read_bytes(), "sha256:" + "0" * 64)


def test_verify_index_rejects_an_empty_read_back(release):
    with pytest.raises(release.ReleaseError, match="read back"):
        release.verify_index(b"", FIXTURE_DIGEST)


def test_verify_index_rejects_a_per_arch_manifest(release):
    raw = (FIXTURES / "single-manifest.json").read_bytes()
    with pytest.raises(release.ReleaseError, match="index"):
        release.verify_index(raw, "sha256:" + hashlib.sha256(raw).hexdigest())


def test_verify_index_rejects_a_missing_platform(release):
    index = json.loads((FIXTURES / "index.json").read_text())
    index["manifests"] = index["manifests"][:1]
    raw = json.dumps(index).encode()
    with pytest.raises(release.ReleaseError, match="linux/arm64"):
        release.verify_index(raw, "sha256:" + hashlib.sha256(raw).hexdigest())


def test_assembled_release_json_validates_against_the_schema(release):
    doc = release.assemble("0.1.0", REPOSITORY, FIXTURE_DIGEST, COMMIT)
    assert doc == {"version": "0.1.0", "image": {"repository": REPOSITORY, "digest": FIXTURE_DIGEST}, "commit": COMMIT}
    jsonschema.Draft202012Validator.check_schema(json.loads(SCHEMA.read_text()))
    jsonschema.validate(doc, json.loads(SCHEMA.read_text()))


def test_sample_release_json_validates_and_the_schema_rejects_drift():
    schema = json.loads(SCHEMA.read_text())
    sample = json.loads((FIXTURES / "release.json").read_text())
    jsonschema.validate(sample, schema)
    for broken in (
        {**sample, "image": {**sample["image"], "digest": "latest"}},
        {**sample, "image": {**sample["image"], "tag": "v0.1.0"}},
        {**sample, "commit": "abc123"},
        {**sample, "version": "v0.1.0"},
        {k: v for k, v in sample.items() if k != "commit"},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(broken, schema)


@pytest.mark.parametrize(
    "args",
    [
        ("v0.1.0", REPOSITORY, FIXTURE_DIGEST, COMMIT),
        ("0.1.0", REPOSITORY + ":latest", FIXTURE_DIGEST, COMMIT),
        ("0.1.0", REPOSITORY, "sha256:abc", COMMIT),
        ("0.1.0", REPOSITORY, FIXTURE_DIGEST, "abc1234"),
    ],
)
def test_assemble_refuses_invalid_inputs(release, args):
    with pytest.raises(release.ReleaseError):
        release.assemble(*args)


def test_cli_end_to_end_on_fixtures(release, tmp_path, capsys):
    out = tmp_path / ".release"
    out.mkdir()
    (out / "build-metadata.json").write_bytes((FIXTURES / "build-metadata.json").read_bytes())
    (out / "index.json").write_bytes((FIXTURES / "index.json").read_bytes())
    assert release.main(["digest", "--metadata", str(out / "build-metadata.json"), "--out", str(out / "digest")]) == 0
    assert (out / "digest").read_text() == FIXTURE_DIGEST + "\n"
    assert release.main(["verify-index", "--raw", str(out / "index.json"), "--digest-file", str(out / "digest")]) == 0
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    for path in release.VERSION_FILES:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text('__version__ = "0.1.0"\n')
    assert release.main([
        "assemble", "--tag", "v0.1.0", "--root", str(tmp_path), "--repository", REPOSITORY, "--digest-file", str(out / "digest"),
        "--commit", COMMIT, "--out-json", str(out / "release.json"), "--out-notes", str(out / "notes.md"),
    ]) == 0
    doc = json.loads((out / "release.json").read_text())
    jsonschema.validate(doc, json.loads(SCHEMA.read_text()))
    notes = (out / "notes.md").read_text()
    assert f"{REPOSITORY}@{FIXTURE_DIGEST}" in notes and COMMIT in notes
    assert release.main(["verify-index", "--raw", str(out / "notes.md"), "--digest-file", str(out / "digest")]) == 1
    assert "read back" in capsys.readouterr().err


class FakeResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGitHub:
    """Enough of the releases API for publish_github: list, create, assets, upload, delete asset, patch."""

    API = "https://api.github.com/repos/o/r"

    def __init__(self, releases=None, fail_uploads=0, drop_uploads=False):
        self.releases = releases or []
        self.fail_uploads, self.drop_uploads = fail_uploads, drop_uploads
        self.calls = []

    def release(self, rid):
        return next(r for r in self.releases if r["id"] == rid)

    def __call__(self, request, timeout):
        url, method = request.full_url, request.get_method()
        self.calls.append((method, url, request))
        if method == "GET" and url == f"{self.API}/releases?per_page=100":
            return FakeResponse(self.releases)
        if method == "POST" and url == f"{self.API}/releases":
            body = json.loads(request.data)
            rid = 100 + len(self.releases)
            self.releases.append({**body, "id": rid, "assets": [], "html_url": f"https://github.com/o/r/releases/{rid}",
                                  "upload_url": f"https://uploads.github.com/repos/o/r/releases/{rid}/assets{{?name,label}}"})
            return FakeResponse(self.release(rid))
        m = re.fullmatch(r"https://uploads\.github\.com/repos/o/r/releases/(\d+)/assets\?name=release\.json", url)
        if method == "POST" and m:
            if self.fail_uploads:
                self.fail_uploads -= 1
                raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, None)
            if not self.drop_uploads:
                assets = self.release(int(m.group(1)))["assets"]
                assets.append({"id": 900 + len(assets), "name": "release.json", "size": len(request.data)})
            return FakeResponse({"id": 1})
        m = re.fullmatch(rf"{self.API}/releases/(\d+)/assets", url)
        if method == "GET" and m:
            return FakeResponse(self.release(int(m.group(1)))["assets"])
        m = re.fullmatch(rf"{self.API}/releases/assets/(\d+)", url)
        if method == "DELETE" and m:
            for r in self.releases:
                r["assets"] = [a for a in r["assets"] if a["id"] != int(m.group(1))]
            return FakeResponse({})
        m = re.fullmatch(rf"{self.API}/releases/(\d+)", url)
        if method == "PATCH" and m:
            self.release(int(m.group(1))).update(json.loads(request.data))
            return FakeResponse(self.release(int(m.group(1))))
        raise AssertionError(f"unexpected {method} {url}")

    def methods(self):
        return [(method, url.split("?")[0].rsplit("/", 1)[-1]) for method, url, _ in self.calls]


import urllib.error  # noqa: E402  (FakeGitHub raises the same HTTPError the script catches)


def publish(release, api, payload=b'{"x":1}'):
    return release.publish_github("o/r", "v0.1.0", "notes", payload, "s3cr3t-value", opener=api)


def test_publish_github_drafts_uploads_verifies_then_publishes(release, capsys):
    api = FakeGitHub()
    publish(release, api)
    (created,) = api.releases
    assert created["draft"] is False and created["tag_name"] == "v0.1.0" and created["body"] == "notes"
    assert [a["name"] for a in created["assets"]] == ["release.json"]
    create = next(req for method, url, req in api.calls if method == "POST" and url.endswith("/releases"))
    assert json.loads(create.data)["draft"] is True, "a release is never public before release.json is attached"
    assert [m for m, _ in api.methods()] == ["GET", "POST", "POST", "GET", "PATCH"]
    assert all(req.get_header("Authorization") == "Bearer s3cr3t-value" for _, _, req in api.calls)
    assert "s3cr3t-value" not in "".join(capsys.readouterr())


def test_a_failed_upload_leaves_a_draft_that_a_rerun_completes(release):
    api = FakeGitHub(fail_uploads=1)
    with pytest.raises(release.ReleaseError, match="502"):
        publish(release, api)
    (draft,) = api.releases
    assert draft["draft"] is True and not any(m == "PATCH" for m, _ in api.methods())
    publish(release, api)
    assert len(api.releases) == 1 and api.releases[0]["draft"] is False
    assert [a["name"] for a in api.releases[0]["assets"]] == ["release.json"]


def test_a_rerun_replaces_a_stale_release_json_on_the_draft(release):
    api = FakeGitHub(releases=[{"id": 5, "tag_name": "v0.1.0", "draft": True, "assets": [{"id": 77, "name": "release.json", "size": 3}],
                                "upload_url": "https://uploads.github.com/repos/o/r/releases/5/assets{?name,label}"}])
    publish(release, api, payload=b'{"new":true}')
    assert ("DELETE", "77") in api.methods()
    assert api.releases[0]["assets"] == [{"id": 900, "name": "release.json", "size": len(b'{"new":true}')}]
    assert api.releases[0]["draft"] is False


def test_an_asset_missing_after_upload_is_not_published(release):
    api = FakeGitHub(drop_uploads=True)
    with pytest.raises(release.ReleaseError, match="release.json"):
        publish(release, api)
    assert api.releases[0]["draft"] is True


def test_publish_github_refuses_to_overwrite_a_published_release(release, capsys):
    api = FakeGitHub(releases=[{"id": 5, "tag_name": "v0.1.0", "draft": False, "assets": []}])
    with pytest.raises(release.ReleaseError, match="already published"):
        publish(release, api)
    assert [m for m, _ in api.methods()] == ["GET"]
    assert "s3cr3t-value" not in "".join(capsys.readouterr())


def test_publish_github_errors_do_not_leak_the_token(release):
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Bad credentials", {}, None)

    with pytest.raises(release.ReleaseError) as err:
        publish(release, opener)
    assert "s3cr3t-value" not in str(err.value)


def test_the_default_opener_never_follows_a_redirect(release):
    """urllib keeps the Authorization header across redirects; the bearer token must not reach another host."""
    handler = release.NoRedirect()
    request = urllib.request.Request("https://api.github.com/x", headers={"Authorization": "Bearer s3cr3t-value"})
    with pytest.raises(urllib.error.HTTPError) as err:
        handler.redirect_request(request, None, 302, "Found", {}, "https://elsewhere.example/x")
    assert "s3cr3t-value" not in str(err.value)
    assert any(isinstance(h, release.NoRedirect) for h in release.OPENER.handlers)


# --- the docker steps, run against a fake docker -------------------------------------------------------------------------

FAKE_DOCKER = r"""#!/bin/sh
printf '%s\n' "$*" >>"$FAKE_LOG"
case "$*" in
  login*) cat >"$FAKE_STDIN"; exit 0 ;;
  "buildx inspect --bootstrap"*) printf 'Platforms: %s\n' "$FAKE_PLATFORMS"; exit 0 ;;
  "buildx build"*) if [ -n "${FAKE_TERM:-}" ]; then kill -TERM "$PPID"; fi; exit 0 ;;
  "buildx imagetools inspect --raw "*)
    ref=$(printf '%s' "$5" | tr '/:@' '___')
    [ -f "$FAKE_REFS/$ref" ] || exit 1
    cat "$FAKE_REFS/$ref"; exit 0 ;;
esac
exit 0
"""


@pytest.fixture
def docker_env(tmp_path):
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    shutil.copytree(SCRIPTS, work / "scripts" / "release")
    (work / ".release").mkdir()
    (work / ".release" / "digest").write_text(FIXTURE_DIGEST + "\n")
    bin_dir, refs = tmp_path / "bin", tmp_path / "refs"
    bin_dir.mkdir()
    refs.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin", "FAKE_LOG": str(tmp_path / "log"), "FAKE_STDIN": str(tmp_path / "stdin"),
        "FAKE_REFS": str(refs), "FAKE_PLATFORMS": "linux/amd64, linux/arm64", "HERDR_RELEASE_REPOSITORY": REPOSITORY,
        "GHCR_PUSH_TOKEN": "s3cr3t-value", "CI_REPO_OWNER": "berlinguyinca", "CI_REPO": "berlinguyinca/herdr-engineering",
        "CI_COMMIT_SHA": COMMIT, "CI_COMMIT_TAG": "v0.1.0", "CI_PIPELINE_NUMBER": "7",
        "HERDR_BUILDKIT_IMAGE": "moby/buildkit:x@sha256:" + "a" * 64, "HERDR_RUNTIME_BASE": "python:x@sha256:" + "b" * 64,
    }

    class Env:
        def __init__(self):
            self.work, self.env, self.refs = work, env, refs

        def ref(self, ref, content: bytes):
            (refs / ref.replace("/", "_").replace(":", "_").replace("@", "_")).write_bytes(content)

        def run(self, script, **extra):
            return subprocess.run(["sh", f"scripts/release/{script}"], cwd=work, env={**env, **extra}, capture_output=True, text=True)

        def log(self):
            path = tmp_path / "log"
            return path.read_text() if path.exists() else ""

        def stdin(self):
            return (tmp_path / "stdin").read_text()

    return Env()


def test_read_back_fetches_by_digest_and_logs_in_over_stdin(docker_env):
    docker_env.ref(f"{REPOSITORY}@{FIXTURE_DIGEST}", (FIXTURES / "index.json").read_bytes())
    result = docker_env.run("read-back.sh")
    assert result.returncode == 0, result.stderr
    assert (docker_env.work / ".release" / "index.json").read_bytes() == (FIXTURES / "index.json").read_bytes()
    assert f"imagetools inspect --raw {REPOSITORY}@{FIXTURE_DIGEST}" in docker_env.log()
    assert docker_env.stdin() == "s3cr3t-value" and "s3cr3t-value" not in docker_env.log() + result.stdout + result.stderr


def test_read_back_fails_when_the_registry_has_no_such_digest(docker_env):
    docker_env.ref(f"{REPOSITORY}:latest", (FIXTURES / "index.json").read_bytes())
    result = docker_env.run("read-back.sh")
    assert result.returncode != 0 and "could not read" in result.stderr


def test_read_back_fails_on_an_empty_manifest(docker_env):
    docker_env.ref(f"{REPOSITORY}@{FIXTURE_DIGEST}", b"")
    result = docker_env.run("read-back.sh")
    assert result.returncode != 0 and "empty" in result.stderr


@pytest.mark.parametrize("digest", ["sha256:" + "a" * 63, "sha256:" + "A" * 64, "sha512:" + "a" * 64, "sha256:" + "a" * 64 + "x", ""])
def test_a_malformed_digest_file_stops_before_any_registry_call(docker_env, digest):
    (docker_env.work / ".release" / "digest").write_text(digest + "\n")
    result = docker_env.run("read-back.sh")
    assert result.returncode != 0 and "not a sha256 digest" in result.stderr
    assert "imagetools" not in docker_env.log()


def test_tag_image_points_the_tag_at_the_digest_and_checks_it(docker_env):
    docker_env.ref(f"{REPOSITORY}:v0.1.0", (FIXTURES / "index.json").read_bytes())
    result = docker_env.run("tag-image.sh")
    assert result.returncode == 0, result.stderr
    assert f"imagetools create --tag {REPOSITORY}:v0.1.0 {REPOSITORY}@{FIXTURE_DIGEST}" in docker_env.log()


def test_tag_image_fails_when_the_tag_resolves_elsewhere(docker_env):
    docker_env.ref(f"{REPOSITORY}:v0.1.0", (FIXTURES / "single-manifest.json").read_bytes())
    result = docker_env.run("tag-image.sh")
    assert result.returncode != 0 and "not " + FIXTURE_DIGEST in result.stderr


def test_build_push_builds_multi_arch_by_digest_and_removes_its_builder(docker_env):
    result = docker_env.run("build-push.sh")
    assert result.returncode == 0, result.stderr
    build = next(line for line in docker_env.log().splitlines() if line.startswith("buildx build"))
    for arg in ("--builder herdr-release-7", "--platform linux/amd64,linux/arm64", "-f deploy/fabric-stack/Dockerfile",
                f"--build-arg GIT_COMMIT={COMMIT}", "--build-arg RUNTIME_BASE=python:x@sha256:", "--provenance=false", "--sbom=false",
                f"type=image,name={REPOSITORY},push-by-digest=true,name-canonical=true,push=true", "--metadata-file .release/build-metadata.json"):
        assert arg in build
    assert "driver-opt image=moby/buildkit:x@sha256:" in docker_env.log()
    assert docker_env.log().rstrip().splitlines()[-2:][0] == "buildx rm --force herdr-release-7"


def test_build_push_refuses_a_builder_without_arm64(docker_env):
    result = docker_env.run("build-push.sh", FAKE_PLATFORMS="linux/amd64")
    assert result.returncode != 0 and "linux/arm64" in result.stderr
    assert "buildx build" not in docker_env.log() and "buildx rm --force herdr-release-7" in docker_env.log()


def test_build_push_removes_its_builder_when_cancelled(docker_env):
    result = docker_env.run("build-push.sh", FAKE_TERM="1")
    assert result.returncode == 143
    assert "buildx rm --force herdr-release-7" in docker_env.log()
