#!/usr/bin/env python3
"""collab-egress.py — the only way out of a helper's sandbox.

  serve --sock PATH --allow-file PATH --secrets-file PATH --services-file PATH --log PATH
      Host side. Listens on a unix socket that is bound into the box. Two kinds of request:
        * CONNECT host:port   a TLS tunnel, only to a host:port listed in the allow file (one `host:port` per line,
                              `[v6]:port` for IPv6, `#` comments). The list is re-read on every connection. An
                              unlisted target gets 403 and a JSON line {"host","port","ts"} appended to --log, which
                              the runner turns into a host-access request. The proxy resolves the name itself and
                              refuses loopback, link-local, private, CGNAT, multicast and other non-global addresses
                              unless that exact IP:port is itself listed; it connects to the address it checked.
        * GET|POST /svc/<name>/<path>   forwarded over https to the service's upstream_url (--services-file: a JSON
                              list of {name, upstream_url, header, secret_name}) with `header` set from the secrets
                              file (a JSON object {secret_name: full header value}). The box never holds the value;
                              it is stripped from anything that comes back.
  relay --sock PATH --listen 127.0.0.1:PORT
      Box side. A TCP listener on the box's own loopback that hands every connection to the unix socket.
Standard library only.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.parse
import zlib

HEAD_MAX = 16384
HEAD_TIMEOUT = 30
CONNECT_TIMEOUT = 10
SVC_TIMEOUT = 120
BODY_MAX = 20 * 1024 * 1024
MAX_CLIENTS = 256
HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))*")
PORT_RE = re.compile(r"[0-9]{1,5}")
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
HOP = {"connection", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
STRIP_IN = HOP | {"authorization", "cookie", "accept-encoding"}
STRIP_OUT = HOP | {"set-cookie", "content-encoding"}


# --- targets and the allow list ---

def forbidden(ip) -> bool:
    """True for any address a tunnel must not reach unless it is listed literally: everything that is not global."""
    ip = ipaddress.ip_address(ip)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local


def parse_target(text: str):
    """`host:port` or `[v6]:port` -> (canonical host, port), or None. Hostnames are lowercased, one trailing dot dropped;
    IP literals are canonicalised. Userinfo, spaces, control characters and odd ports are refused."""
    if not text or len(text) > 300 or any(c <= " " or c == "\x7f" or c in "@/\\?#%" for c in text):
        return None
    if text.startswith("["):
        end = text.find("]")
        if end < 0 or text[end + 1:end + 2] != ":":
            return None
        host, port = text[1:end], text[end + 2:]
        try:
            host = str(ipaddress.IPv6Address(host))
        except ValueError:
            return None
    else:
        host, sep, port = text.rpartition(":")
        if not sep or ":" in host:
            return None
        host = host.lower()
        if host.endswith("."):
            host = host[:-1]
        try:
            host = str(ipaddress.IPv4Address(host))
        except ValueError:
            if not HOST_RE.fullmatch(host) or len(host) > 253:
                return None
    if not PORT_RE.fullmatch(port) or not 1 <= int(port) <= 65535:
        return None
    return host, int(port)


def read_allow(path: str) -> set:
    out = set()
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.split("#", 1)[0].strip()
        t = parse_target(line) if line else None
        if t:
            out.add(t)
    return out


def log_blocked(path: str, host: str, port: int) -> None:
    line = json.dumps({"host": host, "port": port, "ts": time.time()}) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except OSError as e:
        print(f"collab-egress: cannot write {path}: {e.strerror}", file=sys.stderr)


# --- the server ---

def reply(writer, status: int, reason: str, body: bytes = b"", headers=()) -> None:
    head = [f"HTTP/1.1 {status} {reason}", f"Content-Length: {len(body)}", "Connection: close"]
    head += [f"{k}: {v}" for k, v in headers]
    if body and not any(k.lower() == "content-type" for k, _ in headers):
        head.append("Content-Type: text/plain; charset=utf-8")
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)


async def pipe(reader, writer) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except OSError:
            pass


async def close(writer) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except OSError:
        pass


class Server:
    def __init__(self, args):
        self.a = args
        self.slots = asyncio.Semaphore(MAX_CLIENTS)

    async def handle(self, reader, writer):
        async with self.slots:
            try:
                await self._handle(reader, writer)
            except Exception as e:                      # never let one client take the proxy down; never echo details
                print(f"collab-egress: request failed: {type(e).__name__}", file=sys.stderr)
            finally:
                await close(writer)

    async def _handle(self, reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            return
        try:
            text = raw.decode("latin-1")
        except UnicodeDecodeError:
            return
        lines = text[:-4].split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or parts[2] not in ("HTTP/1.1", "HTTP/1.0"):
            return reply(writer, 400, "Bad Request", b"bad request\n")
        method, target, _ = parts
        headers = []
        for h in lines[1:]:
            k, sep, v = h.partition(":")
            if not sep or not k or k.strip() != k or any(c < " " and c != "\t" for c in v):
                return reply(writer, 400, "Bad Request", b"bad header\n")
            headers.append((k, v.strip()))
        if method == "CONNECT":
            return await self.connect(reader, writer, target)
        if target.startswith("http://"):                 # absolute form, as a client sends to a proxy
            u = urllib.parse.urlsplit(target)
            if u.hostname in ("127.0.0.1", "localhost") and u.path.startswith("/svc/"):
                target = u.path + (f"?{u.query}" if u.query else "")
        if target.startswith("/svc/"):
            return await self.svc(reader, writer, method, target, headers)
        reply(writer, 403, "Forbidden", b"only CONNECT (https) and /svc/<name>/ are served here\n")

    async def connect(self, reader, writer, target):
        t = parse_target(target)
        if t is None:
            return reply(writer, 400, "Bad Request", b"bad CONNECT target\n")
        host, port = t
        allow = read_allow(self.a.allow_file)
        if t not in allow:
            log_blocked(self.a.log, host, port)
            return reply(writer, 403, "Forbidden", b"access pending: this host:port is not approved for this room yet\n")
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), CONNECT_TIMEOUT)
        except (TimeoutError, OSError):
            return reply(writer, 502, "Bad Gateway", b"cannot resolve host\n")
        upstream = None
        for family, _, _, _, sockaddr in infos:
            ip = sockaddr[0]
            if forbidden(ip) and (str(ipaddress.ip_address(ip)), port) not in allow:
                continue
            try:
                ur, uw = await asyncio.wait_for(asyncio.open_connection(sockaddr[0], sockaddr[1], family=family), CONNECT_TIMEOUT)
                upstream = (ur, uw)
                break
            except (TimeoutError, OSError):
                continue
        if upstream is None:
            return reply(writer, 403, "Forbidden", b"no permitted address for this host\n")
        ur, uw = upstream
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        try:
            await asyncio.gather(pipe(reader, uw), pipe(ur, writer))
        finally:
            await close(uw)

    async def svc(self, reader, writer, method, target, headers):
        if method not in ("GET", "POST"):
            return reply(writer, 405, "Method Not Allowed", b"GET or POST only\n")
        rest = target[len("/svc/"):]
        name, _, path = rest.partition("/")
        path, _, query = path.partition("?")
        segments = urllib.parse.unquote(path).split("/")
        if not NAME_RE.fullmatch(name) or ".." in segments or "." in segments or any(c in target for c in "\r\n\0\\"):
            return reply(writer, 400, "Bad Request", b"bad service path\n")
        try:
            with open(self.a.services_file, encoding="utf-8") as f:
                services = json.load(f)
        except (OSError, ValueError):
            services = []
        svc = next((s for s in services if isinstance(s, dict) and s.get("name") == name), None) \
            if isinstance(services, list) else None
        if svc is None:
            return reply(writer, 404, "Not Found", b"no such service in this room\n")
        hlower = {k.lower(): v for k, v in headers}
        if "transfer-encoding" in hlower:
            return reply(writer, 411, "Length Required", b"send a Content-Length body\n")
        length = hlower.get("content-length", "0")
        if not re.fullmatch(r"[0-9]{1,9}", length) or int(length) > BODY_MAX:
            return reply(writer, 413, "Payload Too Large", b"body too large\n")
        body = b""
        if int(length):
            try:
                body = await asyncio.wait_for(reader.readexactly(int(length)), SVC_TIMEOUT)
            except (TimeoutError, asyncio.IncompleteReadError):
                return
        secret = self._secret(svc)
        bound = svc.get("header") if isinstance(svc.get("header"), str) else ""
        up = urllib.parse.urlsplit(svc.get("upstream_url") or "") if isinstance(svc.get("upstream_url"), str) else None
        if (secret is None or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", bound) or up is None or up.scheme != "https"
                or not up.hostname or up.username or up.password or any(c in secret for c in "\r\n\0")):
            return reply(writer, 502, "Bad Gateway", b"service is not configured\n")
        drop = STRIP_IN | {bound.lower()}
        out_headers = [(k, v) for k, v in headers if k.lower() not in drop and not k.lower().startswith("proxy-")]
        out_headers += [(bound, secret), ("Accept-Encoding", "identity")]
        upath = up.path.rstrip("/") + "/" + path
        if query:
            upath += "?" + query
        try:
            status, reason, rheaders, rbody = await asyncio.to_thread(
                fetch, up.hostname, up.port or 443, method, upath, out_headers, body)
        except Exception:
            return reply(writer, 502, "Bad Gateway", b"upstream request failed\n")
        redact = redactor(secret)
        rheaders = [(k, redact(v.encode("latin-1", "replace")).decode("latin-1")) for k, v in rheaders
                    if k.lower() not in STRIP_OUT and not k.lower().startswith("proxy-")]
        reply(writer, status, reason or "OK", redact(rbody), rheaders)
        await writer.drain()

    def _secret(self, svc):
        name = svc.get("secret_name")
        try:
            with open(self.a.secrets_file, encoding="utf-8") as f:
                secrets = json.load(f)
        except (OSError, ValueError):
            return None
        v = secrets.get(name) if isinstance(secrets, dict) and isinstance(name, str) else None
        return v if isinstance(v, str) and v else None


def fetch(host, port, method, path, headers, body):
    conn = http.client.HTTPSConnection(host, port, timeout=SVC_TIMEOUT, context=ssl.create_default_context())
    try:
        conn.putrequest(method, path, skip_accept_encoding=True)
        for k, v in headers:
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        r = conn.getresponse()
        data = r.read(BODY_MAX + 1)
        if len(data) > BODY_MAX:
            raise ValueError("response too large")
        enc = (r.getheader("Content-Encoding") or "identity").lower()
        if enc == "gzip":
            data = gzip.GzipFile(fileobj=io.BytesIO(data)).read(BODY_MAX + 1)
        elif enc == "deflate":
            data = zlib.decompressobj().decompress(data, BODY_MAX + 1)
        elif enc != "identity":
            raise ValueError("unsupported encoding")
        if len(data) > BODY_MAX:
            raise ValueError("response too large")
        return r.status, r.reason, r.getheaders(), data
    finally:
        conn.close()


def redactor(secret: str):
    """Strips the bound header value, and the bare credential after a scheme word, from anything returned."""
    needles = {secret}
    scheme, sep, cred = secret.partition(" ")
    if sep and len(cred) >= 8:
        needles.add(cred)
    pats = sorted((n.encode() for n in needles if n), key=len, reverse=True)

    def redact(data: bytes) -> bytes:
        for p in pats:
            data = data.replace(p, b"[redacted]")
        return data
    return redact


async def serve(args):
    srv = Server(args)
    try:
        os.unlink(args.sock)
    except FileNotFoundError:
        pass
    old = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(srv.handle, path=args.sock, limit=HEAD_MAX)
    finally:
        os.umask(old)
    os.chmod(args.sock, 0o600)
    async with server:
        await server.serve_forever()


# --- the relay inside the box ---

async def relay(args):
    host, _, port = args.listen.rpartition(":")
    if host not in ("127.0.0.1", "localhost") or not PORT_RE.fullmatch(port) or not 1 <= int(port) <= 65535:
        sys.exit("collab-egress relay: --listen must be 127.0.0.1:PORT")
    # refuse to come up at all if the proxy is not there: the box then has no way out and the sandbox fails closed
    _, w = await asyncio.wait_for(asyncio.open_unix_connection(args.sock), CONNECT_TIMEOUT)
    await close(w)

    async def one(reader, writer):
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_unix_connection(args.sock), CONNECT_TIMEOUT)
        except (TimeoutError, OSError):
            return await close(writer)
        try:
            await asyncio.gather(pipe(reader, uw), pipe(ur, writer))
        finally:
            await close(uw)
            await close(writer)

    server = await asyncio.start_server(one, "127.0.0.1", int(port))
    async with server:
        await server.serve_forever()


def main(argv=None):
    p = argparse.ArgumentParser(prog="collab-egress.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    for a in ("--sock", "--allow-file", "--secrets-file", "--services-file", "--log"):
        s.add_argument(a, required=True)
    r = sub.add_parser("relay")
    r.add_argument("--sock", required=True)
    r.add_argument("--listen", required=True)
    args = p.parse_args(argv)
    try:
        asyncio.run(serve(args) if args.cmd == "serve" else relay(args))
    except KeyboardInterrupt:
        pass
    except (TimeoutError, OSError) as e:
        sys.exit(f"collab-egress {args.cmd}: {getattr(e, 'strerror', None) or type(e).__name__}")


if __name__ == "__main__":
    main()
