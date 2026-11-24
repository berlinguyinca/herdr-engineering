"""Spec 11 (self-contained part): default-off, non-executing Slurm adapter. No Slurm binary,
no network; pure functions, a real Registry and tmp dirs."""
import copy
import hashlib
import json
import os

import pytest

from herdr_engineering import build_registry as br
from herdr_engineering import build_slurm_adapter as sa
from herdr_engineering.build_cache_config import validate
from herdr_engineering.config import DEFAULTS
from herdr_engineering.errors import InvalidError


def cfg(**slurm):
    s = copy.deepcopy(DEFAULTS["fabric_build_cache"])
    s["enabled"] = True
    s["slurm"].update(slurm)
    return validate(s)


def on(**kw):
    base = {"enabled": True, "account": "buildgrp", "allowed_partitions": ["cpu-build"]}
    base.update(kw)
    return cfg(**base)


def tree(tmp_path):
    root = tmp_path / "src"
    (root / "a").mkdir(parents=True, exist_ok=True)
    (root / "a" / "lib.rs").write_text("fn main(){}\n")
    (root / "Cargo.toml").write_text("[package]\n")
    return root


def binding(epoch=3):
    return sa.LeaseBinding(build_id="b" * 32, attempt_id="a" * 32, scope="/repo", epoch=epoch)


def request(tmp_path, **kw):
    m = sa.stage_manifest(tree(tmp_path), "rustc 1.97.0")
    d = dict(binding=binding(), manifest=m, partition="cpu-build", account="buildgrp",
             cpus=8, mem_gb=16, time_limit_minutes=60,
             argv=("cargo", "check", "--workspace"), staging_dir="/stage/b1")
    d.update(kw)
    return sa.SubmitRequest(**d)


# --- config -----------------------------------------------------------------
def test_slurm_section_default_off():
    c = validate(None)
    assert c.slurm.enabled is False and c.slurm.allowed_partitions == ()
    assert c.slurm_runner_requested is False


def test_slurm_config_fails_closed():
    for bad in ({"enabled": 1}, {"nope": 1}, {"password": "x"}, {"cpus": 0},
                {"account": "a b"}, {"allowed_partitions": ["p;rm"]},
                {"enabled": True}, {"enabled": True, "account": "g"},
                {"enabled": True, "allowed_partitions": ["p"]}):
        with pytest.raises(InvalidError):
            cfg(**bad)


def test_slurm_enabled_validates():
    c = on()
    assert c.slurm.account == "buildgrp" and c.slurm_runner_requested


# --- staging ----------------------------------------------------------------
def test_manifest_deterministic_and_content_sensitive(tmp_path):
    root = tree(tmp_path)
    a = sa.stage_manifest(root, "tc1")
    assert a == sa.stage_manifest(root, "tc1")
    assert a.file_count == 2
    assert sa.stage_manifest(root, "tc2").manifest_digest != a.manifest_digest
    (root / "a" / "lib.rs").write_text("changed")
    assert sa.stage_manifest(root, "tc1").source_digest != a.source_digest


def test_manifest_refuses_symlink_in_source(tmp_path):
    root = tree(tmp_path)
    os.symlink("/etc/passwd", root / "evil")
    with pytest.raises(sa.StagingError):
        sa.stage_manifest(root, "tc")


# --- placement decisions / fallback ------------------------------------------
def test_disabled_by_default_falls_back_local(tmp_path):
    d = sa.decide_placement(validate(None), sa.NullRunner(), request(tmp_path), current_epoch=3)
    assert not d.use_slurm and d.reason is sa.FallbackReason.DISABLED
    assert d.fallback == "local"


def test_null_runner_reports_unavailable(tmp_path):
    r = sa.NullRunner()
    assert r.verified is False
    resp = r.submit(request(tmp_path))
    assert not resp.accepted and resp.reason is sa.FallbackReason.RUNNER_UNAVAILABLE
    d = sa.decide_placement(on(), r, request(tmp_path), current_epoch=3)
    assert not d.use_slurm and d.reason is sa.FallbackReason.RUNNER_UNAVAILABLE


def test_partition_not_allow_listed(tmp_path):
    d = sa.decide_placement(on(), sa.NullRunner(), request(tmp_path, partition="gpu"),
                            current_epoch=3)
    assert d.reason is sa.FallbackReason.PARTITION_NOT_ALLOWED


def test_fail_policy_is_carried(tmp_path):
    s = copy.deepcopy(DEFAULTS["fabric_build_cache"])
    s["enabled"] = True
    s["execution"]["remote_fallback"] = "fail"
    d = sa.decide_placement(validate(s), sa.NullRunner(), request(tmp_path), current_epoch=3)
    assert d.fallback == "fail"


class VerifiedRunner(sa.NullRunner):
    """A real subclass, not a mock: claims verification so the decision logic can be reached."""
    verified = True


def test_verified_runner_allows_slurm(tmp_path):
    d = sa.decide_placement(on(), VerifiedRunner(), request(tmp_path), current_epoch=3)
    assert d.use_slurm and d.reason is None


def test_stale_epoch_rejected(tmp_path):
    d = sa.decide_placement(on(), VerifiedRunner(), request(tmp_path), current_epoch=4)
    assert not d.use_slurm and d.reason is sa.FallbackReason.STALE_EPOCH


def test_fence_against_real_registry(tmp_path):
    reg = br.Registry(tmp_path / "state")
    r = reg.register(repo_id="r", input_digest="d", cmd_kind="cargo build")
    lease = reg.acquire_lease("/repo", r.attempt_id)
    b = sa.LeaseBinding(r.build_id, r.attempt_id, lease.scope, lease.epoch)
    assert sa.fence_ok(b, reg.get_lease("/repo"))
    reg.release(lease)
    assert not sa.fence_ok(b, reg.get_lease("/repo"))
    assert not sa.fence_ok(b, None)
    r2 = reg.register(repo_id="r2", input_digest="d2", cmd_kind="cargo build")
    reg.acquire_lease("/repo", r2.attempt_id)
    assert not sa.fence_ok(b, reg.get_lease("/repo"))


# --- rendering --------------------------------------------------------------
def test_sbatch_plan_contains_fence_and_is_data(tmp_path):
    p = sa.build_sbatch(request(tmp_path))
    joined = " ".join(p.argv)
    assert p.argv[0] == "sbatch" and "--partition=cpu-build" in p.argv
    assert "--account=buildgrp" in p.argv and "--no-requeue" in p.argv
    assert "--export=NONE" in p.argv
    assert any(a.startswith("--job-name=") and "-e3" in a for a in p.argv)
    assert any(a.startswith("--comment=") and "epoch=3" in a for a in p.argv)
    assert "gres" not in joined and "gpu" not in joined
    assert "cargo check --workspace" in p.script


def test_script_has_no_secrets_or_destructive_commands(tmp_path):
    p = sa.build_sbatch(request(tmp_path))
    text = (p.script + " ".join(p.argv)).lower()
    for w in ("secret", "token", "password", "access_key", "aws_", "credential", "sudo",
              "rm ", "rm\t", "mkfs", "dd if", "curl", "wget", "chmod", "> /dev", "scancel"):
        assert w not in text, w


@pytest.mark.parametrize("argv", [("rm", "-rf", "/"), ("cargo", "install", "x"),
                                  ("cargo", "check;rm", "-rf"), ("sh", "-c", "x"), ()])
def test_unsafe_build_argv_rejected(tmp_path, argv):
    with pytest.raises(sa.PlanError):
        sa.build_sbatch(request(tmp_path, argv=argv))


@pytest.mark.parametrize("kw", [{"partition": "a;b"}, {"account": "$(x)"}, {"cpus": 0},
                                {"staging_dir": "relative/x"}, {"staging_dir": "/a/../b"}])
def test_unsafe_fields_rejected(tmp_path, kw):
    with pytest.raises(sa.PlanError):
        sa.build_sbatch(request(tmp_path, **kw))


# --- cancellation contract ---------------------------------------------------
def test_cancel_only_matching_comment():
    b = binding()
    ok = sa.cancel_target(b, sa.render_comment(b), job_id="123")
    assert ok.allowed and ok.job_id == "123"
    other = sa.cancel_target(b, sa.render_comment(binding(epoch=9)), job_id="123")
    assert not other.allowed
    assert not sa.cancel_target(b, "somebody elses job", job_id="123").allowed
    assert not sa.cancel_target(b, sa.render_comment(b), job_id="12 3").allowed


def test_null_runner_cancel_is_noop(tmp_path):
    res = sa.NullRunner().cancel(sa.CancelRequest("1", binding(), "test"))
    assert res.cancelled is False and res.reason is sa.FallbackReason.RUNNER_UNAVAILABLE


# --- output validation -------------------------------------------------------
def make_out(tmp_path, content=b"artifact"):
    out = tmp_path / "out"
    out.mkdir()
    (out / "bin").write_bytes(content)
    exp = sa.ExpectedArtifact("bin", hashlib.sha256(content).hexdigest(), len(content))
    return out, exp


def test_outputs_ok(tmp_path):
    out, exp = make_out(tmp_path)
    assert sa.validate_outputs(out, [exp]) == []


def test_output_digest_mismatch(tmp_path):
    out, exp = make_out(tmp_path)
    (out / "bin").write_bytes(b"tampered!")
    assert sa.validate_outputs(out, [exp])[0].code == "size_mismatch"
    (out / "bin").write_bytes(b"x" * len(b"artifact"))
    assert sa.validate_outputs(out, [exp])[0].code == "digest_mismatch"


@pytest.mark.parametrize("rel", ["../escape", "/etc/passwd", "a/../../x", ""])
def test_output_path_traversal(tmp_path, rel):
    out, _ = make_out(tmp_path)
    probs = sa.validate_outputs(out, [sa.ExpectedArtifact(rel, "0" * 64, 1)])
    assert probs and probs[0].code == "path_escape"


def test_output_symlink_rejected(tmp_path):
    out, exp = make_out(tmp_path)
    secret = tmp_path / "secret"
    secret.write_bytes(b"artifact")
    (out / "link").symlink_to(secret)
    e = sa.ExpectedArtifact("link", exp.sha256, exp.size)
    assert sa.validate_outputs(out, [e])[0].code == "symlink"
    (out / "d").symlink_to(out.parent)
    e2 = sa.ExpectedArtifact("d/secret", exp.sha256, exp.size)
    assert sa.validate_outputs(out, [e2])[0].code == "symlink"


def test_output_missing_and_unexpected(tmp_path):
    out, exp = make_out(tmp_path)
    (out / "extra").write_bytes(b"x")
    codes = {p.code for p in sa.validate_outputs(out, [exp])}
    assert codes == {"unexpected_file"}
    codes = {p.code for p in sa.validate_outputs(out, [sa.ExpectedArtifact("nope", "0" * 64, 1)])}
    assert "missing" in codes


# --- CLI ---------------------------------------------------------------------
def test_cli_plan_states_not_submitted(tmp_path, monkeypatch, capsys):
    from herdr_engineering.cli import main
    src = tree(tmp_path)
    monkeypatch.setenv("HERDR_ENGINEERING_CONFIG", str(tmp_path / "none.yaml"))
    rc = main(["build-cache-slurm-plan", "--json", "--source-dir", str(src),
               "--partition", "cpu-build"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["submitted"] is False and out["runner_verified"] is False
    assert out["message"] == "not submitted; no runner verified"
    assert out["decision"]["use_slurm"] is False
    assert out["decision"]["reason"] == "disabled"


# --- review fixes (specs 06-13) ----------------------------------------------
@pytest.mark.parametrize("field", ["build_id", "attempt_id"])
@pytest.mark.parametrize("bad", ["abc\nrm -rf / #", "x" * 100, "a b", "$(id)", "", "A;B"])
def test_ids_rendered_into_script_are_validated(tmp_path, field, bad):
    b = sa.LeaseBinding(**{**binding().__dict__, field: bad})
    with pytest.raises(sa.PlanError):
        sa.build_sbatch(request(tmp_path, binding=b))
    with pytest.raises(sa.PlanError):
        sa.render_comment(b)


def test_epoch_and_digests_are_validated(tmp_path):
    with pytest.raises(sa.PlanError):
        sa.build_sbatch(request(tmp_path, binding=sa.LeaseBinding("b" * 32, "a" * 32, "/r", "1\nx")))
    m = request(tmp_path).manifest
    bad = sa.StagingManifest("x\nrm -rf /", m.toolchain_digest, m.manifest_digest, 1, 1)
    with pytest.raises(sa.PlanError):
        sa.build_sbatch(request(tmp_path, manifest=bad))


def test_registry_style_dashed_uuid_ids_are_accepted(tmp_path):
    import uuid
    b = sa.LeaseBinding(str(uuid.uuid4()), str(uuid.uuid4()), "/r", 1)
    assert sa.build_sbatch(request(tmp_path, binding=b)).script


def test_cancel_with_malformed_binding_is_refused_not_raised():
    b = sa.LeaseBinding("x\ny", "a" * 32, "/r", 1)
    assert sa.cancel_target(b, "anything", job_id="123").allowed is False


def test_output_symlinked_directory_anywhere_is_flagged(tmp_path):
    out, exp = make_out(tmp_path)
    (out / "extra_dir_link").symlink_to("/etc")
    (out / "sub").mkdir()
    (out / "sub" / "deep").symlink_to("/etc")
    codes = {(p.relpath, p.code) for p in sa.validate_outputs(out, [exp])}
    assert ("extra_dir_link", "symlink") in codes and ("sub/deep", "symlink") in codes


def test_manifest_refuses_fifo_without_hanging(tmp_path):
    src = tree(tmp_path)
    os.mkfifo(src / "pipe")
    with pytest.raises(sa.StagingError):
        sa.stage_manifest(src, "tc")


def test_preview_does_not_stage_when_slurm_not_requested(tmp_path):
    src = tree(tmp_path)
    os.mkfifo(src / "pipe")  # would raise/hang if the tree were read
    from herdr_engineering.build_cache_config import validate
    out = sa.preview(validate(None), str(src), None, "tc")
    assert out["submitted"] is False and out["decision"]["reason"] == "disabled"
    assert out.get("manifest") is None
