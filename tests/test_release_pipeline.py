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
    assert "assemble --tag \"$CI_COMMIT_TAG\" --pyproject pyproject.toml" in release_json
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
    for needle in ("owner", "v[0-9]+.[0-9]+.[0-9]+", "release.json", "ghcr_push_token", "github_release_token", "@sha256:"):
        assert needle in text


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


def test_check_tag_against_the_real_pyproject(release):
    pyproject = (ROOT / "pyproject.toml").read_text()
    version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    assert release.check_tag(f"v{version}", pyproject) == version


def test_digest_comes_from_the_buildx_metadata(release):
    metadata = json.loads((FIXTURES / "build-metadata.json").read_text())
    assert release.digest_from_metadata(metadata) == FIXTURE_DIGEST


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
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.1.0"\n')
    assert release.main([
        "assemble", "--tag", "v0.1.0", "--pyproject", str(pyproject), "--repository", REPOSITORY, "--digest-file", str(out / "digest"),
        "--commit", COMMIT, "--out-json", str(out / "release.json"), "--out-notes", str(out / "notes.md"),
    ]) == 0
    doc = json.loads((out / "release.json").read_text())
    jsonschema.validate(doc, json.loads(SCHEMA.read_text()))
    notes = (out / "notes.md").read_text()
    assert f"{REPOSITORY}@{FIXTURE_DIGEST}" in notes and COMMIT in notes
    assert release.main(["verify-index", "--raw", str(out / "notes.md"), "--digest-file", str(out / "digest")]) == 1
    assert "read back" in capsys.readouterr().err


class FakeResponse:
    def __init__(self, status, body):
        self.status, self._body = status, json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_publish_github_creates_the_release_and_uploads_release_json(release, capsys):
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/releases/tags/v0.1.0"):
            raise release.urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        if request.full_url.endswith("/releases"):
            return FakeResponse(201, {"id": 7, "upload_url": "https://uploads.github.com/repos/o/r/releases/7/assets{?name,label}"})
        return FakeResponse(201, {"id": 9})

    release.publish_github("o/r", "v0.1.0", "notes", b'{"x":1}', "s3cr3t-value", opener=opener)
    get, create, upload = calls
    assert create.get_method() == "POST" and json.loads(create.data) == {
        "tag_name": "v0.1.0", "name": "v0.1.0", "body": "notes", "draft": False, "prerelease": False,
    }
    assert upload.full_url == "https://uploads.github.com/repos/o/r/releases/7/assets?name=release.json"
    assert upload.data == b'{"x":1}'
    assert all(c.get_header("Authorization") == "Bearer s3cr3t-value" for c in calls)
    assert "s3cr3t-value" not in "".join(capsys.readouterr())


def test_publish_github_refuses_to_overwrite_an_existing_release(release, capsys):
    def opener(request, timeout):
        return FakeResponse(200, {"id": 7})

    with pytest.raises(release.ReleaseError, match="already exists"):
        release.publish_github("o/r", "v0.1.0", "notes", b"{}", "s3cr3t-value", opener=opener)
    assert "s3cr3t-value" not in "".join(capsys.readouterr())


def test_publish_github_errors_do_not_leak_the_token(release):
    def opener(request, timeout):
        raise release.urllib.error.HTTPError(request.full_url, 401, "Bad credentials", {}, None)

    with pytest.raises(release.ReleaseError) as err:
        release.publish_github("o/r", "v0.1.0", "notes", b"{}", "s3cr3t-value", opener=opener)
    assert "s3cr3t-value" not in str(err.value)
