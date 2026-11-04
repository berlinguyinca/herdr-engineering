"""Private CA and leaf certificates for the fabric HTTPS listeners (spec: herdr-fabric-url-resolution-20261028, D5).

``dev.lan`` is not a public name, so ACME is not an option: the fleet runs its own CA and clients enroll its root
once (``devfabric ca export``). The root is *name-constrained* to the fabric domains, so installing it on a phone
cannot be used to impersonate any other site. Leaves obey Apple's limits for user-trusted roots: SAN present,
EKU serverAuth, validity <= 398 days. Keys are generated with the system ``openssl`` and stay in ``directory``
(0700, keys 0600); nothing here ever touches the repository.
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import ssl
import subprocess
import tempfile
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

LEAF_MAX_DAYS = 397  # Apple rejects TLS server certs valid for more than 398 days


class CAError(RuntimeError):
    pass


def _openssl(*args: str, cwd: Path | None = None) -> str:
    exe = shutil.which("openssl")
    if not exe:
        raise CAError("openssl is required to manage the fabric CA but was not found on PATH")
    proc = subprocess.run([exe, *args], capture_output=True, text=True, cwd=cwd)
    if proc.returncode != 0:
        raise CAError(f"openssl {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _covered(name: str, domain: str) -> bool:
    n = name.lower().lstrip("*").lstrip(".")
    d = domain.lower().lstrip(".")
    return n == d or n.endswith("." + d)


class PrivateCA:
    def __init__(self, directory: Path | str) -> None:
        self.dir = Path(directory).expanduser()

    @property
    def cert_path(self) -> Path:
        return self.dir / "ca.pem"

    @property
    def key_path(self) -> Path:
        return self.dir / "ca.key"

    @property
    def domains_path(self) -> Path:
        return self.dir / "domains.txt"

    def exists(self) -> bool:
        return self.cert_path.exists() and self.key_path.exists()

    def domains(self) -> list[str]:
        return self.domains_path.read_text().split() if self.domains_path.exists() else []

    def init(self, domains: Iterable[str] = ("dev.lan",), *, name: str = "Herdr Fabric CA",
             days: int = 3650) -> Path:
        """Create the root. Refuses to replace an existing one: re-enrolling every device is not a side effect."""
        domains = [d.lower().lstrip(".") for d in domains]
        if not domains or any(not d or " " in d for d in domains):
            raise CAError("at least one valid fabric domain is required")
        if self.exists():
            raise CAError(f"a CA already exists in {self.dir}; delete it deliberately to start over")
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        constraints = ",".join(f"permitted;DNS:{d}" for d in domains)
        with tempfile.TemporaryDirectory() as tmp:
            ext = Path(tmp) / "ca.ext"
            ext.write_text(
                "basicConstraints=critical,CA:TRUE,pathlen:0\n"
                "keyUsage=critical,keyCertSign,cRLSign\n"
                "subjectKeyIdentifier=hash\n"
                f"nameConstraints=critical,{constraints}\n")
            old = os.umask(0o077)
            try:
                _openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                         "-keyout", str(self.key_path), "-out", str(self.cert_path), "-days", str(days),
                         "-subj", f"/CN={name}", "-extensions", "v3", "-config", self._req_config(tmp, ext))
            finally:
                os.umask(old)
        os.chmod(self.key_path, 0o600)
        self.domains_path.write_text("\n".join(domains) + "\n")
        return self.cert_path

    @staticmethod
    def _req_config(tmp: str, ext: Path) -> str:
        cfg = Path(tmp) / "req.cnf"
        cfg.write_text("[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=x\n[v3]\n" + ext.read_text())
        return str(cfg)

    def issue(self, names: Iterable[str], *, days: int = LEAF_MAX_DAYS, basename: str = "gateway") -> tuple[Path, Path]:
        """Issue a server certificate for ``names`` (each must sit inside the CA's name constraints)."""
        names = list(dict.fromkeys(n.lower() for n in names))
        if not self.exists():
            raise CAError("no CA: run `devfabric ca init` first")
        if not names:
            raise CAError("at least one name is required")
        if not 0 < days <= LEAF_MAX_DAYS:
            raise CAError(f"leaf validity must be 1..{LEAF_MAX_DAYS} days (Apple limit)")
        allowed = self.domains()
        for n in names:
            if not any(_covered(n, d) for d in allowed):
                raise CAError(f"{n} is outside the CA's name constraints {allowed}")
        san = ",".join(f"DNS:{n}" for n in names)
        cert, key = self.dir / f"{basename}.pem", self.dir / f"{basename}.key"
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            csr = t / "leaf.csr"
            ext = t / "leaf.ext"
            ext.write_text(f"basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                           f"extendedKeyUsage=serverAuth\nsubjectAltName={san}\nsubjectKeyIdentifier=hash\n"
                           f"authorityKeyIdentifier=keyid\n")
            old = os.umask(0o077)
            try:
                _openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                         "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={names[0]}")
                _openssl("x509", "-req", "-in", str(csr), "-CA", str(self.cert_path), "-CAkey", str(self.key_path),
                         "-CAcreateserial", "-CAserial", str(t / "serial"), "-out", str(cert), "-days", str(days),
                         "-sha256", "-extfile", str(ext))
            finally:
                os.umask(old)
        os.chmod(key, 0o600)
        return cert, key

    def expires(self, cert: Path) -> datetime:
        out = _openssl("x509", "-in", str(cert), "-noout", "-enddate").strip().split("=", 1)[1]
        return datetime.strptime(out, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)

    def needs_renewal(self, cert: Path, within_days: int = 30) -> bool:
        return not cert.exists() or self.expires(cert) - datetime.now(UTC) < timedelta(days=within_days)

    def mobileconfig(self, *, identifier: str = "us.herdr.fabric-ca") -> bytes:
        """iOS/macOS profile that installs the root. On iOS it must then be switched on under
        Settings > General > About > Certificate Trust Settings (Apple requires this manual step)."""
        der = ssl.PEM_cert_to_DER_cert(self.cert_path.read_text())
        payload = {
            "PayloadType": "com.apple.security.root", "PayloadVersion": 1,
            "PayloadIdentifier": f"{identifier}.cert", "PayloadUUID": str(uuid.uuid4()).upper(),
            "PayloadDisplayName": "Herdr Fabric CA", "PayloadContent": der,
        }
        profile = {
            "PayloadType": "Configuration", "PayloadVersion": 1, "PayloadIdentifier": identifier,
            "PayloadUUID": str(uuid.uuid4()).upper(), "PayloadDisplayName": "Herdr Fabric",
            "PayloadDescription": "Trust the private certificate authority for the Herdr dev fabric (dev.lan).",
            "PayloadContent": [payload],
        }
        return plistlib.dumps(profile, fmt=plistlib.FMT_XML)

    def server_context(self, cert: Path, key: Path) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(cert), str(key))
        ctx.set_alpn_protocols(["http/1.1"])  # the proxy path is HTTP/1.1; never negotiate h2
        return ctx


def fingerprint_sha256(pem_path: Path) -> str:
    der = ssl.PEM_cert_to_DER_cert(Path(pem_path).read_text())
    return hashlib.sha256(der).hexdigest()


__all__ = ["CAError", "LEAF_MAX_DAYS", "PrivateCA", "fingerprint_sha256"]
