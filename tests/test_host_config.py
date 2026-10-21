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

TOKEN = "tok-SECRET-123"
VALUES = {"ingest": {"api_key": "ing-SECRET"}, "ci": {"url": "https://ci.test", "token": "ci-SECRET"}}


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
    assert cfg.api_key == "ing-SECRET"
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

    values = {"ci": {"url": "https://ci.src.test", "token": "ci-tok"},
              "github": {"token": "gh-tok"},
              "fabric": {"url": "http://fab.src.test:29999", "token": "fab-tok"},
              "llm": {"upstream_url": "https://llm.src.test"}}
    home = _home(tmp_path)
    load_host_config(home=home, fetch=Fetch(get=(200, {"revision": "r9", "values": values})))
    # second load comes from the cache (control plane unreachable)
    cfg = load_host_config(home=home, fetch=Fetch(exc=OSError("down")))
    monkeypatch.setattr(hc, "host_config", _REAL_HOST_CONFIG)
    hc.install_source(lambda: cfg)
    try:
        p = WoodpeckerProvider()
        assert (p.base_url, p.token) == ("https://ci.src.test", "ci-tok")
        assert GitHubProvider().available
        assert gateway_url_from_config({}) == "http://fab.src.test:29999"
        assert read_fabric_token() == "fab-tok"
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


# ---------------------------------------------------------------- refresher
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeClient:
    def __init__(self):
        self.url, self.key, self.auth_rejected = "http://old", "old-key", False

    def retarget(self, url, api_key):
        if url:
            self.url = url
        if api_key is not None:
            self.key = api_key


def _cfg(key, source="fetch", url="http://cp"):
    return HostConfig(url, "h1", "r", {"ingest": {"api_key": key}}, source)


def _refresher(loader, clock=None, err=None, initial=None):
    from herdr_engineering.host_config import ConfigRefresher
    return ConfigRefresher(initial or _cfg("old-key"), loader=loader, interval=300,
                           clock=clock or Clock(), err=err)


def test_rotated_key_used_after_interval_and_not_before(capsys):
    clock, calls = Clock(), []
    r = _refresher(lambda: calls.append(1) or _cfg("new-key"), clock)
    c = FakeClient()
    clock.t = 299
    r.sync(c)
    assert c.key == "old-key" and not calls
    clock.t = 300
    r.sync(c)
    assert c.key == "new-key" and len(calls) == 1
    clock.t = 400
    r.sync(c)
    assert len(calls) == 1  # the timer advanced


def test_auth_rejection_forces_refresh_before_interval():
    clock, calls = Clock(), []
    r = _refresher(lambda: calls.append(1) or _cfg("new-key"), clock)
    c = FakeClient()
    c.auth_rejected = True
    r.sync(c)
    assert c.key == "new-key" and c.auth_rejected is False


def test_revoked_ends_with_exit_2_and_cache_deleted(tmp_path, monkeypatch, capsys):
    from herdr_engineering import host_config as hc
    from herdr_engineering import service_metadata_feed as smf
    home = _home(tmp_path)
    load_host_config(home=home, fetch=Fetch())
    assert (home / CACHE_REL).exists()
    monkeypatch.setattr(hc.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(hc, "host_config", lambda: _cfg("old-key"))
    monkeypatch.setattr(hc, "_default_fetch", lambda *a, **k: (401, None))
    monkeypatch.setattr(smf.ServiceMetadataFeed, "run_once",
                        lambda self, dry_run=False: {"errors": 0})
    import herdr_engineering.host_config as hcm
    real = hcm.ConfigRefresher.__init__

    def tiny(self, initial, **kw):  # refresh on the first pass
        real(self, initial, **{**kw, "interval": 0.0})
    monkeypatch.setattr(hcm.ConfigRefresher, "__init__", tiny)
    monkeypatch.setattr(hcm, "resolve_control_plane", lambda x=None: "http://cp")
    monkeypatch.setattr(smf, "resolve_control_plane", lambda x=None: "http://cp")
    assert smf.main(["--loop", "30"]) == 2
    err = capsys.readouterr().err
    assert "rejected this host's token" in err and TOKEN not in err
    assert not (home / CACHE_REL).exists()


def test_transient_failure_without_cache_keeps_old_key_and_signals_once():
    import io
    clock, err = Clock(), io.StringIO()

    def down():
        raise HostConfigUnavailable("control plane unreachable and no cached configuration exists")
    r = _refresher(down, clock, err)
    c = FakeClient()
    for t in (300, 600, 900):
        clock.t = t
        r.sync(c)
    assert c.key == "old-key"
    assert err.getvalue().count("\n") == 1  # one line per outage
    assert "old-key" not in err.getvalue() and TOKEN not in err.getvalue()


def test_cache_fallback_does_not_overwrite_a_fresh_key_and_signal_resets():
    import io
    clock, err = Clock(), io.StringIO()
    answers = [_cfg("rotated"), _cfg("stale-cached", "cache"), _cfg("stale-cached", "cache"),
               _cfg("rotated-2")]
    r = _refresher(lambda: answers.pop(0), clock, err)
    c = FakeClient()
    for t in (300, 600, 900, 1200):
        clock.t = t
        r.sync(c)
        if t == 300:
            assert c.key == "rotated"
        if t in (600, 900):
            assert c.key == "rotated"
    assert c.key == "rotated-2"
    assert err.getvalue().count("\n") == 1  # outage 600-900 only, once
    assert "stale-cached" not in err.getvalue()


def test_auth_rejected_stale_refresh_does_not_advance_timer():
    clock, calls = Clock(), []
    r = _refresher(lambda: calls.append(1) or _cfg("cached", "cache"), clock)
    c = FakeClient()
    clock.t = 100
    c.auth_rejected = True
    r.sync(c)  # forced, stale: timer must not advance
    assert len(calls) == 1 and c.key == "old-key"
    clock.t = 300  # 300 since the original load -> periodic refresh is due
    r.sync(c)
    assert len(calls) == 2
    clock.t = 301  # periodic stale refresh DID advance the timer
    r.sync(c)
    assert len(calls) == 2


def test_revoked_propagates_from_refresher():
    clock = Clock()

    def revoked():
        raise HostConfigUnavailable("revoked", revoked=True)
    r = _refresher(revoked, clock)
    clock.t = 300
    with pytest.raises(HostConfigUnavailable) as e:
        r.sync(FakeClient())
    assert e.value.revoked


def test_401_sets_revoked_flag_and_other_failures_do_not(tmp_path):
    home = _home(tmp_path)
    with pytest.raises(HostConfigUnavailable) as e:
        load_host_config(home=home, fetch=Fetch(get=(401, None)))
    assert e.value.revoked is True
    with pytest.raises(HostConfigUnavailable) as e:
        load_host_config(home=home, fetch=Fetch(exc=OSError()))
    assert e.value.revoked is False


def test_load_marks_source_and_non_dict_cache_values_are_no_cache(tmp_path):
    home = _home(tmp_path)
    assert load_host_config(home=home, fetch=Fetch()).source == "fetch"
    assert load_host_config(home=home, fetch=Fetch(exc=OSError())).source == "cache"
    cache = home / CACHE_REL
    cache.write_text(json.dumps({"host_id": "h1", "revision": "r", "values": ["x"]}))
    with pytest.raises(HostConfigUnavailable):
        load_host_config(home=home, fetch=Fetch(exc=OSError()))


def test_mission_feed_sees_401_403_as_auth_rejection():
    import urllib.error

    from herdr_engineering.mission_activity_feed import MissionActivityFeed
    feed = MissionActivityFeed("http://127.0.0.1:9", host_id="h")

    class Op:
        def open(self, req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 403, "no", {}, None)
    feed._http = Op()
    with pytest.raises(OSError):
        feed._http_json("/api/v1/ingest", {"a": 1})
    assert feed.auth_rejected is True
    feed.retarget("http://new/", "k2")
    assert feed.url == "http://new" and feed.api_key == "k2"


def test_mission_feed_loop_exits_2_when_revoked(tmp_path, monkeypatch, capsys):
    from herdr_engineering import host_config as hc
    from herdr_engineering import mission_activity_feed as maf
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def revoked():
        raise HostConfigUnavailable("token rejected", revoked=True)
    r = hc.ConfigRefresher(_cfg("k"), loader=revoked, interval=0.0)
    monkeypatch.setattr(maf.MissionActivityFeed, "run_once",
                        lambda self: {"capable": True, "errors": 0})
    assert maf.run_feed("http://cp", host_id="h", control_dir=tmp_path, interval=1,
                        api_key=None, refresher=r) == 2
    assert "token rejected" in capsys.readouterr().err


# ------------------------------------------------------------- F1 review fixes
def test_auth_rejection_only_from_the_ingest_post():
    import io
    import urllib.error

    from herdr_engineering.mission_activity_feed import MissionActivityFeed
    from herdr_engineering.service_metadata_feed import MetadataClient

    def boom(code):
        class Op:
            def open(self, req, timeout=0):
                raise urllib.error.HTTPError(req.full_url, code, "no", {}, io.BytesIO(b""))
        return Op()

    feed = MissionActivityFeed("http://127.0.0.1:9", host_id="h")
    feed._http = boom(401)
    with pytest.raises(OSError):
        feed._http_json("/api/v1/health")
    assert feed.auth_rejected is False
    with pytest.raises(OSError):
        feed._http_json("/api/v1/ingest", {"a": 1})
    assert feed.auth_rejected is True

    client = MetadataClient("http://127.0.0.1:9", "k")
    for code in (401, 403):
        client.http = boom(code)
        with pytest.raises(OSError):
            client.request("/api/v1/services")
        assert client.auth_rejected is False
        with pytest.raises(OSError):
            client.request("/api/v1/ingest", {"a": 1})
        assert client.auth_rejected is True
        client.auth_rejected = False
    client.retarget("http://new.test/", "k2")
    assert client.url == "http://new.test" and client.key == "k2"


def test_forced_refresh_floor_prevents_a_storm():
    clock, calls = Clock(), []
    r = _refresher(lambda: calls.append(1) or _cfg("cached", "cache"), clock)
    c = FakeClient()
    for t in (1, 2, 10, 29):
        clock.t = t
        c.auth_rejected = True
        r.sync(c)
    assert len(calls) == 1  # one forced refresh inside the 30 s floor
    clock.t = 31
    c.auth_rejected = True
    r.sync(c)
    assert len(calls) == 2  # the pending rejection retried once the floor passed


def test_fully_explicit_service_metadata_runs_without_host_config(tmp_path, monkeypatch, capsys):
    from herdr_engineering import service_metadata_feed as smf
    keyfile = tmp_path / "key"
    keyfile.write_text("file-key\n")
    assert smf.main(["--control-plane", "http://127.0.0.1:9", "--host-id", "h",
                     "--api-key-file", str(keyfile), "--once", "--dry-run",
                     "--state-file", str(tmp_path / "s.json")]) in (0, 1)
    assert "no host configuration" not in capsys.readouterr().err


def test_explicit_values_are_not_followed_after_refresh():
    clock = Clock()
    r = _refresher(lambda: _cfg("rotated", url="http://central"), clock)
    c = FakeClient()
    clock.t = 300
    r.sync(c, follow_url=False, follow_key=False)
    assert (c.url, c.key) == ("http://old", "old-key")
    clock.t = 600
    r.sync(c, follow_url=False, follow_key=True)
    assert (c.url, c.key) == ("http://old", "rotated")


def test_fully_explicit_mission_feed_runs_without_host_config(monkeypatch, capsys):
    from herdr_engineering import mission_activity_feed as maf
    from herdr_engineering.cli import main as cli_main
    seen = {}
    monkeypatch.setattr(maf, "run_feed", lambda url, **kw: seen.update(url=url, **kw) or 0)
    assert cli_main(["control-plane", "mission-feed", "--control-plane", "http://x:1",
                     "--host-id", "h"]) == 0
    assert seen["url"] == "http://x:1" and seen["host_id"] == "h" and seen["refresher"] is None
    assert "no host configuration" not in capsys.readouterr().err


# ------------------------------------------------------------ fabric agent
def _agent_setup(monkeypatch, tmp_path, live, loader_cfgs):
    """Run `devfabric agent` with a fake FabricAgent so the loop is observable."""
    from herdr_engineering import cli as climod
    from herdr_engineering import fabric, fabric_agent
    from herdr_engineering import host_config as hc

    clock = Clock()
    log = {"clients": [], "cps": [], "clock": clock}

    class FakeAgent:
        def __init__(self, client, config):
            self.client, self.cfg = client, config
            log["clients"].append((client.gateway_url, client.token))

        def retarget(self, client, cp):
            self.client = client
            log["clients"].append((client.gateway_url, client.token))
            log["cps"].append(cp)

        def scan_once(self):
            return {}

        def stop(self):
            pass

        def run(self, after_cycle=None, **kw):
            for _ in range(len(loader_cfgs)):
                log["clock"].t += 400  # each pass is past the refresh interval
                after_cycle()

    monkeypatch.setattr(fabric_agent, "FabricAgent", FakeAgent)
    monkeypatch.setattr(fabric, "tailnet_ipv4", lambda: "100.1.1.1")
    monkeypatch.setattr(hc, "host_config", lambda: live)
    items = list(loader_cfgs)

    def loader():
        r = items.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    real_init = hc.ConfigRefresher.__init__

    def init(self, initial, **kw):
        real_init(self, initial, loader=loader, interval=300, clock=clock)
    monkeypatch.setattr(hc.ConfigRefresher, "__init__", init)
    monkeypatch.setenv("HOME", str(tmp_path))
    return climod, log


def _fab(url, token, source="fetch"):
    return HostConfig("http://cp", "h", "r", {"fabric": {"url": url, "token": token}}, source)


def test_fabric_agent_follows_rotated_token_and_url(monkeypatch, tmp_path):
    live = _fab("http://gw1:29999", "tok1")
    climod, log = _agent_setup(monkeypatch, tmp_path, live, [_fab("http://gw1:29999", "tok2")])
    assert climod.main(["devfabric", "agent"]) == 0
    assert log["clients"][0] == ("http://gw1:29999", "tok1")
    assert log["clients"][-1] == ("http://gw1:29999", "tok2")


def test_fabric_agent_cache_fallback_and_transient_keep_live_config(monkeypatch, tmp_path):
    live = _fab("http://gw1:29999", "tok1")
    climod, log = _agent_setup(monkeypatch, tmp_path, live, [
        _fab("http://stale:29999", "stale", "cache"),
        HostConfigUnavailable("unreachable and no cached configuration exists")])
    assert climod.main(["devfabric", "agent"]) == 0
    assert log["clients"] == [("http://gw1:29999", "tok1")]  # never rebuilt


def test_fabric_agent_revoked_exits_2(monkeypatch, tmp_path, capsys):
    live = _fab("http://gw1:29999", "tok1")
    climod, log = _agent_setup(monkeypatch, tmp_path, live,
                               [HostConfigUnavailable("token rejected", revoked=True)])
    assert climod.main(["devfabric", "agent"]) == 2
    err = capsys.readouterr().err
    assert "token rejected" in err and "tok1" not in err
