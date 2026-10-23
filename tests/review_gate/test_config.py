import pytest

from herdr_engineering.fleet_config import FleetConfigError, load_fleet_config
from herdr_engineering.review_gate.config import (
    AppConfig,
    RepoEnrollment,
    ReviewGateConfig,
    load_review_gate_config,
)
from tests.test_fleet_config import base, write


def key_file(tmp_path, mode=0o600):
    p = tmp_path / "app.pem"
    p.write_text("not-a-real-key")
    p.chmod(mode)
    return str(p)


def section(tmp_path, **over):
    sec = {
        "enabled": True,
        "app": {"app_id": 1, "installation_id": 2, "private_key_file": key_file(tmp_path)},
        "repos": [{"repo": "https://github.com/Acme/Widget.git"}],
    }
    sec.update(over)
    return {"control_plane": {"review_gate": sec}}


def test_absent_section_is_disabled():
    for cfg in ({}, {"control_plane": {}}):
        got = load_review_gate_config(cfg)
        assert got == ReviewGateConfig(enabled=False, app=None, repos=())


def test_repo_defaults(tmp_path):
    got = load_review_gate_config(section(tmp_path))
    assert got.enabled and got.poll_seconds == 15
    assert isinstance(got.app, AppConfig) and got.app.app_id == 1
    assert got.repos == (RepoEnrollment(repo="github.com/acme/widget"),)
    r = got.repos[0]
    assert r.required_events == ("pull_request",) and r.required_checks == ()
    assert (r.main_pipeline, r.disabled_tests_file, r.review_cap, r.ci_fix_cap) == (None, None, 4, 3)


def test_present_section_defaults_to_disabled(tmp_path):
    cfg = section(tmp_path)
    del cfg["control_plane"]["review_gate"]["enabled"]
    assert load_review_gate_config(cfg).enabled is False


@pytest.mark.parametrize("repo", ["gitlab.com/a/b", "nonsense", "github.com/onlyowner", ""])
def test_bad_repo_rejected(tmp_path, repo):
    with pytest.raises(ValueError, match="repo"):
        load_review_gate_config(section(tmp_path, repos=[{"repo": repo}]))


def test_duplicate_repos_rejected(tmp_path):
    repos = [{"repo": "github.com/a/b"}, {"repo": "https://github.com/A/B.git"}]
    with pytest.raises(ValueError, match="duplicate"):
        load_review_gate_config(section(tmp_path, repos=repos))


def test_loose_key_permissions_rejected(tmp_path):
    cfg = section(tmp_path)
    cfg["control_plane"]["review_gate"]["app"]["private_key_file"] = key_file(tmp_path, 0o644)
    with pytest.raises(ValueError, match="private_key_file must be mode 0600"):
        load_review_gate_config(cfg)


def test_relative_key_path_rejected(tmp_path):
    cfg = section(tmp_path)
    cfg["control_plane"]["review_gate"]["app"]["private_key_file"] = "app.pem"
    with pytest.raises(ValueError, match="absolute"):
        load_review_gate_config(cfg)


@pytest.mark.parametrize("where", ["section", "app", "repo"])
def test_unknown_key_rejected(tmp_path, where):
    cfg = section(tmp_path)
    sec = cfg["control_plane"]["review_gate"]
    target = {"section": sec, "app": sec["app"], "repo": sec["repos"][0]}[where]
    target["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        load_review_gate_config(cfg)


def test_caps_and_poll_bounds(tmp_path):
    with pytest.raises(ValueError, match="poll_seconds"):
        load_review_gate_config(section(tmp_path, poll_seconds=4))
    with pytest.raises(ValueError, match="review_cap"):
        load_review_gate_config(section(tmp_path, repos=[{"repo": "github.com/a/b", "review_cap": 0}]))


def test_fleet_config_accepts_section(tmp_path):
    d = base()
    d["sections"]["control_plane"]["values"]["review_gate"] = section(tmp_path)["control_plane"]["review_gate"]
    cfg = load_fleet_config(write(tmp_path, d))
    assert load_review_gate_config({"control_plane": cfg.section("control_plane")}).enabled


def test_fleet_config_rejects_unknown_review_gate_key(tmp_path):
    d = base()
    d["sections"]["control_plane"]["values"]["review_gate"] = {"bogus": 1}
    with pytest.raises(FleetConfigError, match="bogus"):
        load_fleet_config(write(tmp_path, d))
