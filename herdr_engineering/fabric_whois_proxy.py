"""The whois sidecar (fabric trust slice 6a): ``herdr-eng fabric whois-proxy``.

The isolated collab-public container must not see the tailscaled LocalAPI socket (it can do far more than identify a peer). This
small process runs beside tailscaled, holds the socket, and exposes exactly ONE question on a unix socket the container mounts
read-only: ``GET /whois?addr=<tailnet ip>``. Everything else is a 404/400; there is no pass-through of any LocalAPI path, and the
answer is reduced to the fields the collab service reads (node name, tags, login name). Addresses outside the tailnet ranges are
never forwarded to tailscaled.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import socketserver
import stat
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, urlsplit

from .fabric_access import DEFAULT_SOCKETS, TailscaleWhois

TAILNET_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
SOCKET_MODE = 0o660


def parse_addr(path: str) -> tuple[int, str | None]:
    """(status, normalized ip). 404 = not the whois route, 400 = malformed query, 200 = a valid address."""
    split = urlsplit(path)
    if split.path != "/whois" or split.fragment:
        return 404, None
    try:
        pairs = parse_qsl(split.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
    except ValueError:
        return 400, None
    if len(pairs) != 1 or pairs[0][0] != "addr":
        return 400, None
    try:
        return 200, str(ipaddress.ip_address(pairs[0][1]))
    except ValueError:
        return 400, None


class _UnixHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True

    def server_bind(self) -> None:
        old = os.umask(0o117)                     # the socket is created 0660, never wider, even for an instant
        try:
            socketserver.TCPServer.server_bind(self)
        finally:
            os.umask(old)
        os.chmod(self.server_address, SOCKET_MODE)
        self.server_name, self.server_port = "whois-sidecar", 0

    def get_request(self):
        request, _ = super().get_request()
        return request, ("unix", 0)


def _handler(whois: TailscaleWhois):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 5

        def log_message(self, *a) -> None:
            pass

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            status, ip = parse_addr(self.path)
            if ip is None:
                return self._send(status, {"error": "not found" if status == 404 else "bad request"})
            if not any(ipaddress.ip_address(ip) in net for net in TAILNET_NETS):
                return self._send(404, {"error": "not found"})
            try:
                ident = whois.lookup(ip)
            except Exception:                      # noqa: BLE001 - tailscaled unreachable or odd: a failure, never an identity
                return self._send(502, {"error": "tailscaled unavailable"})
            if ident is None:
                return self._send(404, {"error": "not found"})
            return self._send(200, {"Node": {"Name": ident.node or "", "Tags": list(ident.tags)},
                                    "UserProfile": {"LoginName": ident.user or "tagged-devices"}})

        def _refuse(self) -> None:
            self._send(405, {"error": "method not allowed"})
            self.close_connection = True

        do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _refuse  # noqa: N815
    return H


def make_server(listen_socket: str, tailscaled_sockets: tuple[str, ...] = DEFAULT_SOCKETS) -> _UnixHTTPServer:
    try:
        if stat.S_ISSOCK(os.stat(listen_socket).st_mode):
            os.unlink(listen_socket)               # a stale socket of a previous run; never any other kind of file
    except FileNotFoundError:
        pass
    return _UnixHTTPServer(listen_socket, _handler(TailscaleWhois(sockets=tailscaled_sockets, timeout=2.0)))


def serve(listen_socket: str, tailscaled_sockets: tuple[str, ...] = DEFAULT_SOCKETS) -> None:
    server = make_server(listen_socket, tailscaled_sockets)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            os.unlink(listen_socket)
        except OSError:
            pass
