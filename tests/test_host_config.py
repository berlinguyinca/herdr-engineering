import json
import stat

import pytest

from herdr_engineering.host_config import (
    CACHE_REL,
    HOST_JSON_REL,
    HostConfig,
    HostConfigUnavailable,
    load_host_config,
)
from herdr_engineering.host_config import (
    host_config as _REAL_HOST_CONFIG,  # noqa: E402  (before the conftest stub)
)

TOKEN = "placeholder-value-one"
VALUES = {"ingest": {"api_key": "placeholder-ing-upper"}, "ci": {"url": "https://ci.test", "token": "placeholder-ci-upper"}}


def _home(tmp_path):
    hj = tmp_path / HOST_JSON_REL
    hj.parent.mkdir(parents=True)
    hj.write_text(json.dumps({"control_plane_url": "http://cp.test/", "host_id": "h1", "token": TOKEN}))
    return tmp_path


class Fetch:
    def __init__(self, get=(200, {"revision": "r1", "values": VALUES}), applied=(204, None), exc=None):
        self.get, self.applied, self.exc, self.calls = get, applied, exc, []

    def __call__(self, url, *, token, timeout, method="GET", body=None):
        self.calls.append((method, url, token, body))
        if self.exc:
            raise self.exc
        return self.applied if method == "POST" else self.get


def test_fetch_success_writes_cache_and_reports_applied(tmp_path):
    home = _home(tmp_path)
    f = Fetch()
    cfg = load_host_config(home=home, fetch=f)
    assert cfg.revision == "r1" and cfg.control_plane_url == "http://cp.test" and cfg.host_id == "h1"
    cache = home / CACHE_REL
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert stat.S_IMODE(cache.parent.stat().st_mode) == 0o700
    assert json.loads(cache.read_text())["revision"] == "r1"
    assert f.calls[0][:3] == ("GET", "http://cp.test/api/v1/fleet/config", TOKEN)
    assert f.calls[1] == ("POST", "http://cp.test/api/v1/fleet/config/applied", TOKEN,
                          {"host_id": "h1", "revision": "r1"})


def test_applied_failure_is_ignored(tmp_path):
    class F(Fetch):
        def __call__(self, url, **kw):
            if kw.get("method") == "POST":
                raise OSError("down")
            return super().__call__(url, **kw)
    assert load_host_config(home=_home(tmp_path), fetch=F()).revision == "r1"


@pytest.mark.parametrize("failure", [dict(exc=OSError("nope")), dict(exc=TimeoutError()),
                                     dict(get=(503, None))])
def test_unreachable_uses_cache(tmp_path, failure):
    home = _home(tmp_path)
    load_host_config(home=home, fetch=Fetch())
    cfg = load_host_config(home=home, fetch=Fetch(**failure))
    assert cfg.revision == "r1" and cfg.get("ci.url") == "https://ci.test"


def test_unreachable_no_cache_raises(tmp_path):
    with pytest.raises(HostConfigUnavailable):
        load_host_config(home=_home(tmp_path), fetch=Fetch(exc=OSError("nope")))


def test_401_discards_cache_and_raises(tmp_path):
    home = _home(tmp_path)
    load_host_config(home=home, fetch=Fetch())
    assert (home / CACHE_REL).exists()
    with pytest.raises(HostConfigUnavailable):
        load_host_config(home=home, fetch=Fetch(get=(401, None)))
    assert not (home / CACHE_REL).exists()
    with pytest.raises(HostConfigUnavailable):  # and it stays gone
        load_host_config(home=home, fetch=Fetch(exc=OSError("down")))


def test_token_and_values_never_in_exception_text(tmp_path):
    home = _home(tmp_path)
    load_host_config(home=home, fetch=Fetch())
    msgs = []
    for f in (Fetch(get=(401, None)), Fetch(exc=OSError(TOKEN)), Fetch(get=(403, None))):
        try:
            load_host_config(home=home, fetch=f)
        except HostConfigUnavailable as exc:
            msgs.append(str(exc) + repr(exc))
    (home / CACHE_REL).unlink(missing_ok=True)
    try:
        load_host_config(home=home, fetch=Fetch(exc=OSError(TOKEN)))
    except HostConfigUnavailable as exc:
        msgs.append(str(exc))
    assert msgs
    for m in msgs:
        assert TOKEN not in m and "SECRET" not in m


def test_dotted_get_and_api_key():
    cfg = HostConfig("http://cp", "h", "r", VALUES)
    assert cfg.get("ci.url") == "https://ci.test"
    assert cfg.get("ci.missing", "d") == "d" and cfg.get("nosuch.x") is None
    assert cfg.api_key == "placeholder-ing-upper"
    assert "SECRET" not in repr(cfg)


def test_missing_host_json_message(tmp_path):
    with pytest.raises(HostConfigUnavailable) as e:
        load_host_config(home=tmp_path, fetch=Fetch())
    assert "herdr-eng fleet join" in str(e.value)


def test_cache_for_other_host_is_ignored(tmp_path):
    home = _home(tmp_path)
    load_host_config(home=home, fetch=Fetch())
    hj = home / HOST_JSON_REL
    hj.write_text(json.dumps({"control_plane_url": "http://cp.test", "host_id": "other", "token": TOKEN}))
    with pytest.raises(HostConfigUnavailable):
        load_host_config(home=home, fetch=Fetch(exc=OSError()))


def test_feeds_exit_2_with_message_when_unavailable(capsys):
    from herdr_engineering import service_metadata_feed
    from herdr_engineering.cli import main as cli_main
    assert service_metadata_feed.main(["--once", "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "no host configuration" in err and "Traceback" not in err
    assert cli_main(["control-plane", "mission-feed", "--once"]) == 2
    assert "no host configuration" in capsys.readouterr().err


def test_explicit_control_plane_override_still_works(fake_host_config):
    from herdr_engineering.host_config import resolve_control_plane
    fake_host_config(control_plane_url="http://from-host-json")
    assert resolve_control_plane("http://explicit/") == "http://explicit"
    assert resolve_control_plane(None) == "http://from-host-json"


def test_memoised_accessor_loads_once(tmp_path, monkeypatch):
    from herdr_engineering import host_config as hc
    monkeypatch.undo()  # drop the conftest stub to exercise the real accessor
    hc.reset_host_config()
    calls = []
    monkeypatch.setattr(hc, "load_host_config", lambda: calls.append(1) or HostConfig("u", "h", "r", {}))
    assert hc.host_config() is hc.host_config()
    assert len(calls) == 1
    hc.reset_host_config()


def test_redirect_is_not_followed_and_keeps_cache(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    hits = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    target = HTTPServer(("127.0.0.1", 0), Target)

    class Redirector(BaseHTTPRequestHandler):
        def _go(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/elsewhere")
            self.end_headers()
        do_GET = do_POST = _go  # noqa: N815

        def log_message(self, *a):
            pass

    redir = HTTPServer(("127.0.0.1", 0), Redirector)
    for s in (target, redir):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        home = _home(tmp_path)
        load_host_config(home=home, fetch=Fetch())  # populate the cache
        (home / HOST_JSON_REL).write_text(json.dumps({
            "control_plane_url": f"http://127.0.0.1:{redir.server_port}",
            "host_id": "h1", "token": TOKEN}))
        cfg = load_host_config(home=home)  # real default fetch
        assert cfg.revision == "r1"  # fell back to the cache
        assert (home / CACHE_REL).exists()  # a 3xx is not a revocation
        assert hits == []  # the redirect target was never contacted
        (home / CACHE_REL).unlink()
        with pytest.raises(HostConfigUnavailable) as e:
            load_host_config(home=home)
        assert TOKEN not in str(e.value) and hits == []
    finally:
        redir.shutdown()
        target.shutdown()


def test_dev_agent_exits_2_without_host_config(capsys):
    from herdr_engineering.cli import main as cli_main
    assert cli_main(["devfabric", "agent", "--once"]) == 2
    err = capsys.readouterr().err
    assert "no host configuration" in err and "Traceback" not in err


def test_control_plane_source_serves_fetched_or_cached_values(tmp_path, monkeypatch):
    """Providers read ci/github/fabric/llm from a real HostConfig installed as the
    source (home= + fetch= injection, not the conftest stub)."""
    from herdr_engineering import host_config as hc
    from herdr_engineering.ci import WoodpeckerProvider
    from herdr_engineering.fabric import gateway_url_from_config, read_fabric_token
    from herdr_engineering.github import GitHubProvider

    values = {"ci": {"url": "https://ci.src.test", "token": "placeholder-ci-short"},
              "github": {"token": "placeholder-gh-short"},
              "fabric": {"url": "http://fab.src.test:29999", "token": "placeholder-fab-short"},
              "llm": {"upstream_url": "https://llm.src.test"}}
    home = _home(tmp_path)
    load_host_config(home=home, fetch=Fetch(get=(200, {"revision": "r9", "values": values})))
    # second load comes from the cache (control plane unreachable)
    cfg = load_host_config(home=home, fetch=Fetch(exc=OSError("down")))
    monkeypatch.setattr(hc, "host_config", _REAL_HOST_CONFIG)
    hc.install_source(lambda: cfg)
    try:
        p = WoodpeckerProvider()
        assert (p.base_url, p.token) == ("https://ci.src.test", "placeholder-ci-short")
        assert GitHubProvider().available
        assert gateway_url_from_config({}) == "http://fab.src.test:29999"
        assert read_fabric_token() == "placeholder-fab-short"
        assert hc.host_value("llm.upstream_url") == "https://llm.src.test"
    finally:
        hc.reset_host_config()


def test_install_fleet_host_config_reads_the_fleet_file(tmp_path, monkeypatch):
    """The control-plane web process answers host_config() from the fleet file."""
    from herdr_engineering import fleet_config_cli as fcc
    from herdr_engineering import host_config as hc
    from herdr_engineering.cli import _install_fleet_host_config
    from herdr_engineering.fleet_config import FleetConfigStore
    from herdr_engineering.github import GitHubProvider

    path = fcc.init_config(tmp_path / "fleet.yaml", {
        "public_url": "https://cp.test", "llm_upstream_url": "https://llm.test",
        "llm_upstream_key": "k", "ci_url": "https://ci.test", "ci_token": "ci-t",
        "github_token": "gh-t", "fabric_url": "http://fab:29999", "hosts": {"h": ["ci"]}},
        prompt=lambda *_: "", secret_prompt=lambda *_: "")
    monkeypatch.setattr(hc, "host_config", _REAL_HOST_CONFIG)
    _install_fleet_host_config(FleetConfigStore(path))
    try:
        cfg = hc.host_config()
        assert cfg.get("ci.token") == "ci-t" and cfg.get("fabric.url") == "http://fab:29999"
        assert cfg.get("github.token") == "gh-t" and cfg.get("llm.upstream_url") == "https://llm.test"
        assert cfg.get("agent.api_key") and cfg.get("ingest.api_key")
        assert GitHubProvider().available
    finally:
        hc.reset_host_config()
