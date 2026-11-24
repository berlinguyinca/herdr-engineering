"""Browser defences shared by the control-plane listener and the dashboard proxy.

A web page in any browser that can reach the control plane can send it requests. In open mode every caller is
operator, so the only barrier is what the browser enforces; these checks add the rest:

* ``host_ok``: DNS rebinding needs a DNS name, so a request whose Host is a bare name is refused unless the operator
  listed it. IP literals and ``localhost`` always pass.
* ``browser_write_refusal``: a state-changing request that a browser marks as cross-origin (``Origin``) or cross-site
  (``Sec-Fetch-Site``) is refused, and a browser request with a body must say ``application/json`` (a cross-site form
  or ``text/plain`` post needs no preflight). Callers that send neither header (agents, the CLI) are unaffected.
"""
import ipaddress
from urllib.parse import urlsplit

_JSON = "application/json"
_SAME_SITE = frozenset({"same-origin", "none"})


def _host_name(host_header):
    """The host part of a Host/authority value: lower-cased, no port, IPv6 brackets removed."""
    host = (host_header or "").strip().lower()
    if host.startswith("[") and "]" in host:
        return host[1:host.index("]")]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_ok(host_header, extra_hosts):
    """A Host an attacker cannot make point here: an IP literal, localhost, or a configured name (with or without
    a port)."""
    host = (host_header or "").strip().lower()
    name = _host_name(host)
    if host in extra_hosts or name in extra_hosts or name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def browser_write_refusal(headers, extra_hosts=()):
    """``(status, message)`` when a state-changing request must be refused as a browser attack, else None.

    An Origin is accepted when its host and port equal the Host the request used, or exactly equal an entry the
    operator configured (a bare name allows only the default port). With no
    Origin, ``Sec-Fetch-Site`` must be same-origin or none. A request carrying either header (a browser) and a body
    must be JSON."""
    origin = headers.get("Origin")
    site = headers.get("Sec-Fetch-Site")
    if origin is not None:
        parts = urlsplit(origin)
        netloc = parts.netloc.lower()
        same = (parts.scheme in {"http", "https"} and bool(netloc)
                and (netloc == (headers.get("Host") or "").strip().lower() or netloc in extra_hosts))
        if not same:
            return 403, "cross-origin request refused"
    elif site is not None and site.strip().lower() not in _SAME_SITE:
        return 403, "cross-site request refused"
    if origin is not None or site is not None:
        has_body = (headers.get("Content-Length") or "0").strip() not in {"", "0"}
        if has_body and headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != _JSON:
            return 415, "browser requests with a body must be application/json"
    return None
