"""Memory-only human identity; never changes legacy dashboard/operator authentication."""
from __future__ import annotations

import ipaddress
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from ..https_origin import https_origin
from .collab_cognito import CognitoAuthenticator, SyncedDirectory


def strict_origin(value):
    """An exact HTTPS origin, including a valid numeric port; no URL components or whitespace."""
    origin = https_origin(value)
    if origin is None or value != origin:
        return None
    try:
        parts = urlsplit(origin)
        port = parts.port
        if port is not None and (not 1 <= port <= 65535 or port == 443
                                 or parts.netloc.rsplit(':', 1)[1] != str(port)):
            return None
    except ValueError:
        return None
    return origin


def parse_cognito(raw):
    """Host-only policy; identifiers come from the existing collaboration section."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {'origin', 'sync_dir', 'trusted_proxy_ips'}:
        raise ValueError('memory.cognito must contain only origin, sync_dir and trusted_proxy_ips')
    origin = strict_origin(raw.get('origin'))
    directory = raw.get('sync_dir', '/etc/herdr/collab-sync')
    peers = raw.get('trusted_proxy_ips', [])
    if not origin:
        raise ValueError('memory.cognito.origin must be a bare canonical HTTPS origin')
    if not isinstance(directory, str) or not Path(directory).is_absolute() or any(ord(c) < 32 for c in directory):
        raise ValueError('memory.cognito.sync_dir must be an absolute path')
    if not isinstance(peers, list) or len(peers) > 16:
        raise ValueError('memory.cognito.trusted_proxy_ips must be a bounded list of exact IPs')
    try:
        parsed = [ipaddress.ip_address(p) for p in peers if isinstance(p, str)]
        if len(parsed) != len(peers) or any(p.is_unspecified or p.is_multicast for p in parsed):
            raise ValueError()
    except ValueError:
        raise ValueError('memory.cognito.trusted_proxy_ips must contain exact unicast IPs') from None
    return {'origin': origin, 'sync_dir': directory, 'trusted_proxy_ips': [str(p) for p in parsed]}


class MemoryCognito:
    def __init__(self, policy, collab):
        self.policy = parse_cognito(policy)
        cfg = collab or {}
        self.origin = self.policy['origin']
        self.authority = urlsplit(self.origin).netloc
        directory = SyncedDirectory(self.policy['sync_dir'], cfg.get('cognito_group'))
        self.auth = CognitoAuthenticator(pool_id=cfg.get('cognito_pool'),
            client_ids=cfg.get('cognito_clients') or [], required_group=cfg.get('cognito_group'),
            jwks_fetch=directory.jwks, group_lookup=directory.groups,
            recheck_seconds=0, lookup_grace=0, open_signup=False)
        domain = cfg.get('cognito_domain', '').rstrip('/')
        client = cfg.get('cognito_web_client')
        if not strict_origin(domain) or not isinstance(client, str) or client not in self.auth.client_ids:
            raise ValueError('memory.cognito needs the existing collaboration Hosted UI and web client')
        self.public_config = {'enabled': True, 'origin': self.origin, 'client_id': client,
                              'hosted_ui_domain': domain, 'redirect_uri': self.origin + '/'}

    def browser_allowed(self, handler):
        """Never infer TLS or the external origin from forwarded headers."""
        host = handler.headers.get('Host', '')
        if host != self.authority or strict_origin('https://' + host) != self.origin:
            return False
        origin = handler.headers.get('Origin')
        if origin is not None and (strict_origin(origin) != self.origin):
            return False
        site = handler.headers.get('Sec-Fetch-Site')
        if site is not None and site not in ('same-origin', 'none'):
            return False
        if isinstance(handler.connection, ssl.SSLSocket):
            return True
        # A terminating proxy is trusted only by an explicitly configured socket peer IP,
        # scoped to this one configured HTTPS authority. X-Forwarded-* grants nothing.
        return handler.client_address[0] in self.policy['trusted_proxy_ips']

    def principal(self, authorization):
        scheme, _, token = authorization.partition(' ')
        if scheme != 'Bearer' or not token or token != token.strip() or len(token) > 16384:
            return None
        try:
            got = self.auth.verify(token)
        except Exception:
            return None
        if got is None:
            return None
        return {'kind': 'user', 'id': got[0], 'authenticated_by': 'cognito'}
