"""Provisioning of the herdr-build-cache rustfs bucket: pure rules + a read-only live probe.

No fakes: the pure functions take plain data; the live test runs the real MinIO
client against the real rustfs container and ONLY reads (it skips with a reason
when the container or the admin credential is unavailable).
"""
import json
import os
import stat

import pytest

from herdr_engineering import build_cache_rustfs as rf

PW = "ADMIN-PASSWORD-VALUE-xyz"


def _state(**over):
    base = dict(bucket=True, quota_gib=50.0, rw_user=True, ro_user=True, rw_policy=True,
                ro_policy=True, rw_attached=True, ro_attached=True, rw_cred_file=True,
                ro_cred_file=True, registry_file=True)
    return rf.State(**{**base, **over})


def test_policy_is_limited_to_the_bucket_and_read_only_cannot_write():
    rw = rf.policy_document("herdr-build-cache", read_only=False)
    ro = rf.policy_document("herdr-build-cache", read_only=True)
    for doc in (rw, ro):
        text = json.dumps(doc)
        assert "herdr-artifacts" not in text and "arn:aws:s3:::*" not in text
        assert all(r.startswith("arn:aws:s3:::herdr-build-cache") for st in doc["Statement"]
                   for r in st["Resource"])
        assert all(st["Effect"] == "Allow" for st in doc["Statement"])
    ro_actions = {a for st in ro["Statement"] for a in st["Action"]}
    assert "s3:PutObject" not in ro_actions and "s3:DeleteObject" not in ro_actions
    assert "s3:PutObject" in {a for st in rw["Statement"] for a in st["Action"]}


def test_protected_and_foreign_bucket_names_refused():
    for bad in ("herdr-artifacts", "other", "herdr-build-cache/../x", "herdr-build-cache_"):
        with pytest.raises(rf.ProvisionError):
            rf.policy_document(bad, read_only=True)
    rf.policy_document("herdr-build-cache-test", read_only=True)


def test_plan_is_empty_when_everything_exists():
    assert rf.plan_steps(_state(), quota_gib=50) == []


def test_plan_lists_only_missing_pieces_and_never_a_delete():
    steps = rf.plan_steps(_state(bucket=False, quota_gib=None, ro_user=False, ro_policy=False,
                                 ro_attached=False, ro_cred_file=False), quota_gib=50)
    keys = [s.key for s in steps]
    assert keys == ["bucket", "quota", f"policy:{rf.RO_POLICY}", f"user:{rf.RO_USER}",
                    f"attach:{rf.RO_USER}"]
    assert not any("delete" in s.text.lower() or "remove" in s.text.lower() for s in steps)


def test_existing_user_without_local_credential_needs_explicit_rotate():
    steps = rf.plan_steps(_state(rw_cred_file=False), quota_gib=50)
    assert [s.key for s in steps] == [f"blocked:{rf.RW_USER}"]
    steps = rf.plan_steps(_state(rw_cred_file=False), quota_gib=50, rotate=True)
    assert [s.key for s in steps] == [f"user:{rf.RW_USER}", f"user:{rf.RO_USER}"]  # rotate re-issues both


def test_quota_change_is_planned():
    assert [s.key for s in rf.plan_steps(_state(quota_gib=10), quota_gib=50)] == ["quota"]


def test_private_files_are_0600_and_yaml_loadable(tmp_path):
    from herdr_engineering.build_cache_storage import read_credentials
    p = tmp_path / "cfg" / "x.cred"
    rf.write_private(p, rf.credential_yaml("herdr-build-cache-rw", "s3cr3t0123456789"))
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    c = read_credentials(str(p))
    assert c.access_key_id == "herdr-build-cache-rw" and "s3cr3t" not in repr(c)


def test_registry_file_names_endpoint_without_secrets(tmp_path):
    import yaml
    data = yaml.safe_load(rf.registry_yaml("100.104.39.6:9000", "herdr-build-cache", False))
    assert data == {"endpoints": {"build-cache": {"host": "100.104.39.6:9000",
                    "region": "us-east-1", "bucket": "herdr-build-cache", "use_ssl": False}}}


def test_admin_credential_from_fleet_config_is_redacted(tmp_path):
    import yaml
    f = tmp_path / "fleet.yaml"
    f.write_text(yaml.safe_dump({"version": 1, "hosts": {}, "sections": {"control_plane": {
        "roles": ["control-plane"], "values": {"minio_user": "root-u", "minio_password": PW}}}}))
    f.chmod(0o600)
    cred = rf.load_admin_credential(f)
    assert cred.user == "root-u" and PW not in repr(cred) and PW not in str(cred)
    f.chmod(0o644)
    with pytest.raises(rf.ProvisionError) as e:
        rf.load_admin_credential(f)
    assert PW not in str(e.value)


def test_mc_env_carries_secret_not_argv(tmp_path):
    cred = rf.AdminCredential("u", PW)
    with rf.Mc(cred, prefix=["/bin/echo"]) as mc:
        r = mc.run("a", "b", check=False)
        assert PW not in r.stdout and PW not in r.stderr
        cfg = mc._config_dir
        assert os.path.isdir(cfg) and stat.S_IMODE(os.stat(cfg).st_mode) == 0o700
    assert not os.path.exists(cfg)  # private config dir removed


def test_host_mc_that_is_midnight_commander_is_not_used(tmp_path):
    fake = tmp_path / "mc"
    fake.write_text("#!/bin/sh\necho 'GNU Midnight Commander 4.8.30'\n")
    fake.chmod(0o755)
    prefix, in_container = rf.detect_mc_command(
        which=lambda n: str(fake) if n == "mc" else "/usr/bin/docker")
    assert in_container and prefix[:2] == ["/usr/bin/docker", "exec"]
    real = tmp_path / "mc2"
    real.write_text("#!/bin/sh\necho 'mc version RELEASE.2025 MinIO'\n")
    real.chmod(0o755)
    prefix, in_container = rf.detect_mc_command(which=lambda n: str(real))
    assert prefix == [str(real)] and not in_container


def test_live_state_probe_is_read_only_and_idempotent():
    try:
        cred = rf.load_admin_credential()
        prefix, inc = rf.detect_mc_command()
        mc_ctx = rf.Mc(cred, prefix=prefix, container=rf.DEFAULT_CONTAINER if inc else None)
        mc_ctx.__enter__()
    except rf.ProvisionError as exc:
        pytest.skip(f"rustfs admin access unavailable: {exc}")
    try:
        if mc_ctx.run("stat", f"{rf.ALIAS}/herdr-artifacts", check=False).returncode != 0:
            pytest.skip("rustfs container not reachable via mc")
        from pathlib import Path
        rep = rf.provision(mc_ctx, apply=False, config_dir=Path("/nonexistent-hbc-test"))
        assert not rep.applied and not rep.done
        again = rf.provision(mc_ctx, apply=False, config_dir=Path("/nonexistent-hbc-test"))
        assert [s.key for s in rep.steps] == [s.key for s in again.steps]
    finally:
        mc_ctx.__exit__()
