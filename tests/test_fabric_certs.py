from __future__ import annotations

import socket
import ssl
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdr_engineering import fabric_certs as fc
from herdr_engineering.fabric_tls import PrivateCA


def _self_signed(directory: Path, cn: str, days: int) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / "fullchain.pem", directory / "privkey.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", str(days), "-subj", f"/CN={cn}", "-addext", f"subjectAltName=DNS:{cn},DNS:*.{cn}"],
                   check=True, capture_output=True)
    return cert, key


class FakeCertbot:
    """Stands in for certbot: writes a fresh cert into the live dir, or fails."""
    def __init__(self, acme: fc.AcmeRoute53, days: int = 90, fail: bool = False):
        self.acme, self.days, self.fail, self.calls = acme, days, fail, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "rate limited\nboom")
        _self_signed(self.acme.live, self.acme.domain, self.days)
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _acme(tmp_path, **kw) -> fc.AcmeRoute53:
    return fc.AcmeRoute53("local.example.test", tmp_path / "acme", **kw)


def test_command_requests_apex_and_wildcard_without_email(tmp_path):
    cmd = _acme(tmp_path).command()
    assert cmd.count("-d") == 2 and "local.example.test" in cmd and "*.local.example.test" in cmd
    assert "--register-unsafely-without-email" in cmd and "-m" not in cmd
    assert "--dns-route53" in cmd and "--server" not in cmd
    assert "--force-renewal" not in cmd


def test_command_staging_email_force(tmp_path):
    cmd = _acme(tmp_path, staging=True, email="ops@example.test").command(force=True)
    assert "acme-staging-v02" in cmd[cmd.index("--server") + 1]
    assert cmd[cmd.index("-m") + 1] == "ops@example.test" and "--register-unsafely-without-email" not in cmd
    assert "--force-renewal" in cmd


def test_obtain_reports_change_and_state_dir_private(tmp_path):
    acme = _acme(tmp_path)
    acme.runner = FakeCertbot(acme)
    assert acme.obtain() is True
    assert (acme.directory.stat().st_mode & 0o777) == 0o700


def test_obtain_failure_raises_with_tail(tmp_path):
    acme = _acme(tmp_path)
    acme.runner = FakeCertbot(acme, fail=True)
    with pytest.raises(fc.CertError, match="rate limited"):
        acme.obtain()


def test_obtain_success_without_file_is_an_error(tmp_path):
    acme = _acme(tmp_path)
    acme.runner = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")
    with pytest.raises(fc.CertError, match="missing"):
        acme.obtain()


def test_due_for_renewal_uses_the_certs_own_lifetime(tmp_path):
    cert, _ = _self_signed(tmp_path / "c", "a.test", 90)
    assert fc.due_for_renewal(tmp_path / "nope.pem") is True
    now = datetime.now(UTC)
    assert fc.due_for_renewal(cert, now) is False
    assert fc.due_for_renewal(cert, now + timedelta(days=59)) is False   # 31 days left of 90 > 30
    assert fc.due_for_renewal(cert, now + timedelta(days=61)) is True
    short, _ = _self_signed(tmp_path / "s", "b.test", 6)                 # a 6-day cert is due after 4 days
    assert fc.due_for_renewal(short, now + timedelta(days=3)) is False
    assert fc.due_for_renewal(short, now + timedelta(days=5)) is True


def test_check_is_noop_unless_renew_node(tmp_path):
    acme = _acme(tmp_path)
    runner = acme.runner = FakeCertbot(acme)
    assert fc.CertManager(public=acme, renew=False).check() == {"renewed": [], "error": None}
    assert runner.calls == []


def test_check_issues_missing_cert_and_publishes_new_context(tmp_path):
    acme = _acme(tmp_path)
    acme.runner = FakeCertbot(acme)
    seen = []
    mgr = fc.CertManager(public=acme, renew=True, on_change=seen.append)
    assert mgr.check()["renewed"] == ["local.example.test"]
    assert len(seen) == 1 and isinstance(seen[0], ssl.SSLContext)


def test_check_does_nothing_when_cert_fresh(tmp_path):
    acme = _acme(tmp_path)
    _self_signed(acme.live, acme.domain, 90)
    runner = acme.runner = FakeCertbot(acme)
    assert fc.CertManager(public=acme, renew=True).check()["renewed"] == []
    assert runner.calls == []


def test_failure_backs_off_for_a_day_then_retries(tmp_path):
    acme = _acme(tmp_path)
    runner = acme.runner = FakeCertbot(acme, fail=True)
    clock = [1000.0]
    mgr = fc.CertManager(public=acme, renew=True, now=lambda: clock[0])
    assert "rate limited" in mgr.check()["error"]
    for _ in range(5):
        clock[0] += 3600
        assert mgr.check()["error"]            # still backing off, certbot not called again
    assert len(runner.calls) == 1
    clock[0] += 24 * 3600
    runner.fail = False
    assert mgr.check()["error"] is None and len(runner.calls) == 2 and mgr.last_error is None


def test_status_reports_public_and_private(tmp_path):
    acme = _acme(tmp_path)
    _self_signed(acme.live, acme.domain, 90)
    st = fc.CertManager(public=acme).status()
    assert st["public"]["domain"] == "local.example.test" and st["public"]["due"] is False
    assert 88 <= st["public"]["lifetime_days"] <= 90


def test_load_requires_some_certificate(tmp_path):
    with pytest.raises(fc.CertError):
        fc.CertManager(public=_acme(tmp_path)).load()


def _served_cn(ctx: ssl.SSLContext, sni: str, trust: Path) -> str:
    """Handshake against a one-shot TLS server using ``ctx``; return the CN of the cert the client saw."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        try:
            ctx.wrap_socket(conn, server_side=True).close()
        except OSError:
            pass
        srv.close()
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cctx.check_hostname = False
    cctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw, cctx.wrap_socket(raw, server_hostname=sni) as tls:
        der = tls.getpeercert(binary_form=True)
    t.join(5)
    pem = ssl.DER_cert_to_PEM_cert(der)
    p = trust.parent / "seen.pem"
    p.write_text(pem)
    out = subprocess.run(["openssl", "x509", "-in", str(p), "-noout", "-subject"], capture_output=True, text=True, check=True).stdout
    return out.strip().split("CN")[-1].strip(" =")


def test_sni_selects_public_vs_private_certificate(tmp_path):
    acme = _acme(tmp_path)
    _self_signed(acme.live, acme.domain, 90)
    ca = PrivateCA(tmp_path / "ca")
    ca.init(["dev.lan"])
    ca.issue(["dev.lan"])
    mgr = fc.CertManager(public=acme, private_ca=ca)
    ctx = mgr.load()
    assert "local.example.test" in _served_cn(ctx, "svc.local.example.test", tmp_path / "x")
    assert "dev.lan" in _served_cn(ctx, "dev.lan", tmp_path / "x")
    assert "dev.lan" in _served_cn(ctx, "api.dev.lan", tmp_path / "x")
    # unknown SNI and no SNI fall back to the public certificate
    assert "local.example.test" in _served_cn(ctx, "other.example", tmp_path / "x")


def _ts(tmp_path, days=90, fail=False, later=None):
    calls = []
    ts = fc.TailscaleCert("bender.tail.ts.net", tmp_path / "ts")

    def runner(cmd, **kw):
        assert cmd[:2] == ["tailscale", "cert"] and cmd[-1] == "bender.tail.ts.net"
        if fail:
            return subprocess.CompletedProcess(cmd, 1, "", "not permitted")
        d = tmp_path / "gen"
        calls.append(1)
        cert, key = _self_signed(d, "bender.tail.ts.net", later if later and len(calls) > 1 else days)
        Path(cmd[cmd.index("--cert-file") + 1]).write_bytes(cert.read_bytes())
        Path(cmd[cmd.index("--key-file") + 1]).write_bytes(key.read_bytes())
        return subprocess.CompletedProcess(cmd, 0, "", "")
    ts.runner = runner
    return ts


def test_tailscale_cert_obtain_keys_private_and_failure_reported(tmp_path):
    ts = _ts(tmp_path)
    assert ts.obtain() is True and (ts.privkey.stat().st_mode & 0o777) == 0o600
    with pytest.raises(fc.CertError, match="not permitted"):
        _ts(tmp_path / "x", fail=True).obtain()


def test_sni_serves_the_tailnet_name_next_to_public_and_private(tmp_path):
    acme = _acme(tmp_path)
    _self_signed(acme.live, acme.domain, 90)
    ts = _ts(tmp_path)
    ts.obtain()
    ctx = fc.CertManager(public=acme, tailnet=ts).load()
    assert "bender.tail.ts.net" in _served_cn(ctx, "bender.tail.ts.net", tmp_path / "x")
    assert "local.example.test" in _served_cn(ctx, "svc.local.example.test", tmp_path / "x")
    only = fc.CertManager(tailnet=ts)                      # a node with just the tailnet name still starts
    assert "bender.tail.ts.net" in _served_cn(only.load(), "whatever", tmp_path / "x")


def test_renewal_pass_refreshes_a_due_tailnet_cert(tmp_path):
    ts = _ts(tmp_path, days=2, later=90)                    # the renewed cert is longer-lived than the first
    ts.obtain()
    ts.due = lambda now=None: True
    seen = []
    mgr = fc.CertManager(tailnet=ts, renew=True, on_change=seen.append)
    assert mgr.check()["renewed"] == ["bender.tail.ts.net"] and len(seen) == 1


def test_files_replaced_by_someone_else_are_reloaded_without_renew_rights(tmp_path):
    import os
    acme = _acme(tmp_path)
    _self_signed(acme.live, acme.domain, 30)
    seen = []
    mgr = fc.CertManager(public=acme, renew=False, on_change=seen.append)
    mgr.load()
    assert mgr.check() == {"renewed": [], "error": None} and seen == []        # unchanged: nothing happens
    first = fc.validity(acme.fullchain)[1]
    _self_signed(acme.live, acme.domain, 90)                                  # a host timer renewed the files
    os.utime(acme.fullchain, ns=(1, 1))                                       # (mtime is the signal; make it certain)
    res = mgr.check()
    assert res.get("reloaded") is True and len(seen) == 1 and fc.validity(acme.fullchain)[1] > first
    assert mgr.check() == {"renewed": [], "error": None}                      # and only once


def test_half_written_files_keep_the_old_context(tmp_path):
    acme = _acme(tmp_path)
    _self_signed(acme.live, acme.domain, 30)
    seen = []
    mgr = fc.CertManager(public=acme, on_change=seen.append)
    mgr.load()
    acme.fullchain.write_text("-----BEGIN CERTIFICATE-----\ngarbage")
    assert mgr.reload_if_changed() is False and seen == []
    assert mgr._by_suffix                                                      # still serving the previous contexts


def test_tailnet_cert_is_not_due_without_a_cli_and_no_runner(tmp_path):
    ts = fc.TailscaleCert("n.ts.net", tmp_path, tailscale="/nonexistent/tailscale")
    _self_signed(tmp_path / "g", "n.ts.net", 2)
    ts.directory.mkdir(parents=True, exist_ok=True)
    (tmp_path / "n.ts.net.crt").write_bytes((tmp_path / "g" / "fullchain.pem").read_bytes())
    assert ts.due() is False


def test_certbot_is_found_beside_the_python_and_a_missing_binary_is_a_clean_error(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "certbot").write_text("#!/bin/sh\n")
    monkeypatch.setattr(fc.sys, "executable", str(fake / "python"))
    assert _acme(tmp_path).command()[0] == str(fake / "certbot")
    assert _acme(tmp_path, certbot="/opt/cb").command()[0] == "/opt/cb"
    monkeypatch.setattr(fc.sys, "executable", str(tmp_path / "none" / "python"))
    monkeypatch.setattr(fc.shutil, "which", lambda n: None)

    def missing(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])
    a = _acme(tmp_path)
    a.runner = missing
    with pytest.raises(fc.CertError, match="could not run certbot"):
        a.obtain()
