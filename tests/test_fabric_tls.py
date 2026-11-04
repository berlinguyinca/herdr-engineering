import os
import plistlib
import socket
import ssl
import stat

import pytest

from herdr_engineering.fabric_tls import LEAF_MAX_DAYS, CAError, PrivateCA, fingerprint_sha256


@pytest.fixture(scope="module")
def ca(tmp_path_factory):
    ca = PrivateCA(tmp_path_factory.mktemp("ca"))
    ca.init(["dev.lan"])
    return ca


def test_ca_is_private_and_not_overwritable(ca):
    assert stat.S_IMODE(os.stat(ca.key_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(ca.dir).st_mode) == 0o700
    with pytest.raises(CAError, match="already exists"):
        ca.init(["dev.lan"])


def test_leaf_validates_against_root_for_apex_and_wildcard(ca, tmp_path):
    cert, key = ca.issue(["dev.lan", "*.dev.lan"])
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    ctx = ca.server_context(cert, key)
    server = socket.socket()
    server.bind(("127.0.0.1", 0)); server.listen(1)

    import threading
    def serve():
        c, _ = server.accept()
        try:
            s = ctx.wrap_socket(c, server_side=True); s.sendall(b"ok"); s.close()
        except ssl.SSLError:
            c.close()
    for host in ("dev.lan", "app.dev.lan"):
        threading.Thread(target=serve, daemon=True).start()
        client = ssl.create_default_context(cafile=str(ca.cert_path))
        with socket.create_connection(server.getsockname(), timeout=5) as raw, \
                client.wrap_socket(raw, server_hostname=host) as tls:
            assert tls.recv(2) == b"ok"
    server.close()


def test_wrong_name_and_untrusted_root_are_rejected(ca, tmp_path):
    cert, key = ca.issue(["dev.lan"], basename="narrow")
    ctx = ca.server_context(cert, key)
    server = socket.socket(); server.bind(("127.0.0.1", 0)); server.listen(2)
    import threading
    def serve():
        for _ in range(2):
            c, _ = server.accept()
            try:
                ctx.wrap_socket(c, server_side=True).close()
            except (ssl.SSLError, OSError):
                c.close()
    threading.Thread(target=serve, daemon=True).start()
    trusting = ssl.create_default_context(cafile=str(ca.cert_path))
    with pytest.raises(ssl.SSLCertVerificationError), socket.create_connection(server.getsockname(), timeout=5) as raw:
        trusting.wrap_socket(raw, server_hostname="other.lan")
    with pytest.raises(ssl.SSLCertVerificationError), socket.create_connection(server.getsockname(), timeout=5) as raw:
        ssl.create_default_context().wrap_socket(raw, server_hostname="dev.lan")
    server.close()


def test_name_constraints_block_foreign_names(ca):
    with pytest.raises(CAError, match="outside"):
        ca.issue(["example.com"])
    with pytest.raises(CAError, match="outside"):
        ca.issue(["evil-dev.lan"])


def test_leaf_validity_capped_for_apple(ca):
    with pytest.raises(CAError, match="Apple"):
        ca.issue(["dev.lan"], days=LEAF_MAX_DAYS + 1)
    cert, _ = ca.issue(["dev.lan"], basename="short", days=10)
    assert ca.needs_renewal(cert, within_days=30) and not ca.needs_renewal(cert, within_days=5)


def test_leaf_has_san_eku_and_no_ca_flag(ca):
    from herdr_engineering.fabric_tls import _openssl
    cert, _ = ca.issue(["dev.lan", "*.dev.lan"], basename="inspect")
    text = _openssl("x509", "-in", str(cert), "-noout", "-text")
    assert "DNS:dev.lan, DNS:*.dev.lan" in text and "TLS Web Server Authentication" in text and "CA:FALSE" in text


def test_mobileconfig_carries_the_root(ca):
    profile = plistlib.loads(ca.mobileconfig())
    payload = profile["PayloadContent"][0]
    assert payload["PayloadType"] == "com.apple.security.root"
    assert ssl.DER_cert_to_PEM_cert(payload["PayloadContent"]).strip() == ca.cert_path.read_text().strip()
    assert len(fingerprint_sha256(ca.cert_path)) == 64
