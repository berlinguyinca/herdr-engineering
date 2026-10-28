"""Publicly trusted certificates for the fabric gateway (spec: herdr-fabric-url-resolution-20261028, D5 revised).

Devices should need no enrollment, so the fleet name (e.g. ``local.metabolomics.us``) gets a real Let's Encrypt
certificate: ``certbot`` proves control of the name with a DNS-01 challenge in Route 53 and the gateway serves the
result. The A records point at tailnet addresses, so the name is only *reachable* from the tailnet even though the
certificate is public. The private CA from ``fabric_tls`` stays available for ``dev.lan``; the certificate presented
is chosen by SNI so both names keep working side by side.

Renewal is decided from each certificate's own lifetime (renew with a third left), runs on one node only, never
retries more than once a day after a failure, and swaps the live TLS context without a restart.
"""
from __future__ import annotations

import logging
import ssl
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .fabric_tls import CAError, PrivateCA, _openssl

log = logging.getLogger("herdr.fabric.certs")

RENEW_FRACTION = 1 / 3          # renew when this share of the lifetime is left (LE certs are 60-90 days)
FAILURE_BACKOFF = 24 * 3600.0   # after a failed issuance wait a day: LE rate limits are per registered domain


class CertError(RuntimeError):
    pass


def _parse_openssl_time(text: str) -> datetime:
    return datetime.strptime(text.strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)


def validity(cert: Path) -> tuple[datetime, datetime]:
    """(notBefore, notAfter) of the first certificate in a PEM file."""
    out = _openssl("x509", "-in", str(cert), "-noout", "-startdate", "-enddate")
    fields = dict(line.split("=", 1) for line in out.strip().splitlines())
    return _parse_openssl_time(fields["notBefore"]), _parse_openssl_time(fields["notAfter"])


def due_for_renewal(cert: Path, now: datetime | None = None, fraction: float = RENEW_FRACTION) -> bool:
    if not Path(cert).exists():
        return True
    start, end = validity(cert)
    now = now or datetime.now(UTC)
    return (end - now) <= (end - start) * fraction


@dataclass
class AcmeRoute53:
    """A Let's Encrypt certificate for ``domain`` and ``*.domain`` obtained with certbot's Route 53 DNS plugin.

    State (account, keys, certificates) lives under ``directory`` (0700); the process needs AWS credentials that can
    change ``_acme-challenge.<domain>`` TXT records. certbot runs unprivileged and is never given a shell string.
    """
    domain: str
    directory: Path
    staging: bool = False
    email: str | None = None            # None registers without an address (nothing is sent to a third party)
    certbot: str = "certbot"
    runner: Callable[..., subprocess.CompletedProcess] = field(default=subprocess.run, repr=False)

    def __post_init__(self) -> None:
        self.directory = Path(self.directory).expanduser()
        self.domain = self.domain.lower().strip(".")

    @property
    def live(self) -> Path:
        return self.directory / "config" / "live" / self.domain

    @property
    def fullchain(self) -> Path:
        return self.live / "fullchain.pem"

    @property
    def privkey(self) -> Path:
        return self.live / "privkey.pem"

    @property
    def names(self) -> tuple[str, str]:
        return (self.domain, f"*.{self.domain}")

    def command(self, *, force: bool = False) -> list[str]:
        cmd = [self.certbot, "certonly", "--non-interactive", "--agree-tos", "--dns-route53",
               "--cert-name", self.domain, "--keep-until-expiring",
               "--config-dir", str(self.directory / "config"), "--work-dir", str(self.directory / "work"),
               "--logs-dir", str(self.directory / "logs")]
        for name in self.names:
            cmd += ["-d", name]
        cmd += ["-m", self.email] if self.email else ["--register-unsafely-without-email"]
        if self.staging:
            cmd += ["--server", "https://acme-staging-v02.api.letsencrypt.org/directory"]
        if force:
            cmd.append("--force-renewal")
        return cmd

    def obtain(self, *, force: bool = False) -> bool:
        """Issue or renew. Returns True when the certificate on disk changed."""
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        before = validity(self.fullchain)[1] if self.fullchain.exists() else None
        proc = self.runner(self.command(force=force), capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            raise CertError(f"certbot failed ({proc.returncode}): {' | '.join(tail)[:400]}")
        if not self.fullchain.exists():
            raise CertError(f"certbot reported success but {self.fullchain} is missing")
        return before is None or validity(self.fullchain)[1] != before

    def due(self, now: datetime | None = None) -> bool:
        return due_for_renewal(self.fullchain, now)


def server_context(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    ctx.set_alpn_protocols(["http/1.1"])   # the proxy path is HTTP/1.1; never negotiate h2
    return ctx


@dataclass
class CertManager:
    """Owns the gateway's certificates: picks one by SNI, reloads after renewal, and renews on a safe schedule."""
    public: AcmeRoute53 | None = None
    private_ca: PrivateCA | None = None
    private_names: tuple[str, ...] = ("dev.lan",)
    private_leaf: str = "gateway"
    renew: bool = False                 # exactly one node of an HA pair sets this
    now: Callable[[], float] = time.time
    backoff: float = FAILURE_BACKOFF
    on_change: Callable[[ssl.SSLContext], None] | None = None
    last_error: str | None = None
    _next_attempt: float = 0.0
    _by_suffix: dict[str, ssl.SSLContext] = field(default_factory=dict, repr=False)
    _default: ssl.SSLContext | None = field(default=None, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- building --------------------------------------------------------------------------------
    def load(self) -> ssl.SSLContext:
        """(Re)read certificates from disk and build the SNI-dispatching context."""
        by_suffix: dict[str, ssl.SSLContext] = {}
        default: ssl.SSLContext | None = None
        if self.public and self.public.fullchain.exists():
            ctx = server_context(self.public.fullchain, self.public.privkey)
            by_suffix[self.public.domain] = ctx
            default = ctx
        if self.private_ca and (self.private_ca.dir / f"{self.private_leaf}.pem").exists():
            ctx = server_context(self.private_ca.dir / f"{self.private_leaf}.pem",
                                 self.private_ca.dir / f"{self.private_leaf}.key")
            for name in self.private_names:
                by_suffix[name.lower()] = ctx
            default = default or ctx
        if default is None:
            raise CertError("no certificate available: run `devfabric cert issue` (public) or `devfabric ca issue` (private)")
        with self._lock:
            self._by_suffix, self._default = by_suffix, default
        dispatcher = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        dispatcher.minimum_version = ssl.TLSVersion.TLSv1_2
        dispatcher.set_alpn_protocols(["http/1.1"])
        dispatcher.load_cert_chain(*self._default_files())
        dispatcher.sni_callback = self._pick
        return dispatcher

    def _default_files(self) -> tuple[str, str]:
        if self.public and self.public.fullchain.exists():
            return str(self.public.fullchain), str(self.public.privkey)
        assert self.private_ca is not None
        return str(self.private_ca.dir / f"{self.private_leaf}.pem"), str(self.private_ca.dir / f"{self.private_leaf}.key")

    def _pick(self, sslobj: ssl.SSLObject, name: str | None, _orig: ssl.SSLContext) -> None:
        with self._lock:
            by_suffix, default = self._by_suffix, self._default
        chosen = default
        if name:
            n = name.lower().rstrip(".")
            for suffix, ctx in by_suffix.items():
                if n == suffix or n.endswith("." + suffix):
                    chosen = ctx
                    break
        if chosen is not None:
            sslobj.context = chosen

    def context_for(self, name: str) -> ssl.SSLContext | None:
        with self._lock:
            n = name.lower()
            for suffix, ctx in self._by_suffix.items():
                if n == suffix or n.endswith("." + suffix):
                    return ctx
        return None

    # -- renewal ---------------------------------------------------------------------------------
    def check(self) -> dict:
        """One renewal pass. Cheap when nothing is due; never raises."""
        result = {"renewed": [], "error": None}
        if not self.renew:
            return result
        if self.now() < self._next_attempt:
            result["error"] = self.last_error
            return result
        try:
            changed = False
            if self.public and self.public.due():
                if self.public.obtain():
                    changed = True
                    result["renewed"].append(self.public.domain)
            if self.private_ca and self.private_ca.exists():
                leaf = self.private_ca.dir / f"{self.private_leaf}.pem"
                if self.private_ca.needs_renewal(leaf):
                    self.private_ca.issue(list(self.private_names), basename=self.private_leaf)
                    changed = True
                    result["renewed"].append(",".join(self.private_names))
            self.last_error = None
            if changed:
                self._publish()
        except (CertError, CAError, OSError, subprocess.SubprocessError) as exc:
            self.last_error = str(exc)
            self._next_attempt = self.now() + self.backoff
            result["error"] = self.last_error
            log.warning("certificate renewal failed; next attempt in %.0fh: %s", self.backoff / 3600, exc)
        return result

    def _publish(self) -> None:
        ctx = self.load()
        if self.on_change:
            self.on_change(ctx)

    def run_forever(self, stop: threading.Event, interval: float = 3600.0) -> None:
        while not stop.is_set():
            self.check()
            stop.wait(interval)

    def status(self) -> dict:
        out: dict = {"renew": self.renew, "last_error": self.last_error,
                     "next_attempt_in": max(0.0, self._next_attempt - self.now())}
        if self.public and self.public.fullchain.exists():
            start, end = validity(self.public.fullchain)
            out["public"] = {"domain": self.public.domain, "not_after": end.isoformat(),
                             "lifetime_days": (end - start).days, "due": self.public.due(), "staging": self.public.staging}
        if self.private_ca and (self.private_ca.dir / f"{self.private_leaf}.pem").exists():
            leaf = self.private_ca.dir / f"{self.private_leaf}.pem"
            out["private"] = {"names": list(self.private_names), "not_after": self.private_ca.expires(leaf).isoformat(),
                              "due": self.private_ca.needs_renewal(leaf)}
        return out
