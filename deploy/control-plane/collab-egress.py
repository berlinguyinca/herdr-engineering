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
import http
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
CONNECT_TIMEOUT = 10
SVC_TIMEOUT = 120
BODY_MAX = 20 * 1024 * 1024
LOG_REPEAT_S = 60            # one log line per blocked (host, port, reason) per minute
LOG_PER_MIN = 120            # and at most this many lines a minute in all; the rest are counted
NAT64 = ipaddress.ip_network("64:ff9b::/96")
NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
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
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour or (ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF) if ip in NAT64 else None)
        if ip.teredo:
            embedded = ip.teredo[1]
        if embedded is not None and forbidden(embedded):
            return True
        if ip.ipv4_mapped:
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
            # a name whose last label is a number is an IPv4 address in some other spelling (2130706433, 0x7f.1, 0177.1)
            if not HOST_RE.fullmatch(host) or len(host) > 253 or NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):
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


class BlockedLog:
    """The JSONL file the runner turns into host-access requests. Bounded: a blocked (host, port, reason) is written at
    most once per LOG_REPEAT_S, and at most LOG_PER_MIN lines a minute in all; what is dropped by the second rule is
    reported as one {"suppressed": n, "ts"} line when writing resumes."""

    def __init__(self, path: str):
        self.path = path
        self.seen: dict = {}
        self.window = 0
        self.count = 0
        self.suppressed = 0

    def blocked(self, host: str, port: int, reason: str = "") -> None:
        now = time.time()
        key = (host, port, reason)
        if now - self.seen.get(key, -1e9) < LOG_REPEAT_S:
            return
        if len(self.seen) > 10000:
            self.seen = {k: t for k, t in self.seen.items() if now - t < LOG_REPEAT_S}
        minute = int(now // 60)
        if minute != self.window:
            self.window, self.count = minute, 0
        if self.count >= LOG_PER_MIN:
            self.suppressed += 1
            return
        self.seen[key] = now
        lines = []
        if self.suppressed:
            lines.append({"suppressed": self.suppressed, "ts": now})
            self.suppressed = 0
        line = {"host": host, "port": port, "ts": now}
        if reason:
            line["reason"] = reason
        lines.append(line)
        self.count += len(lines)
        self._write("".join(json.dumps(x) + "\n" for x in lines))

    def _write(self, text: str) -> None:
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, text.encode())
            finally:
                os.close(fd)
        except OSError as e:
            print(f"collab-egress: cannot write {self.path}: {e.strerror}", file=sys.stderr)


async def resolve(host: str, port: int):
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), CONNECT_TIMEOUT)


dial = asyncio.open_connection


# --- the server ---

def reply(writer, status: int, reason: str, body: bytes = b"", headers=()) -> None:
    head = [f"HTTP/1.1 {status} {reason}", f"Content-Length: {len(body)}", "Connection: close"]
    head += [f"{k}: {v}" for k, v in headers]
    if body and not any(k.lower() == "content-type" for k, _ in headers):
        head.append("Content-Type: text/plain; charset=utf-8")
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)


async def pipe(reader, writer, idle: float = 0, activity=None) -> None:
    """Copies until EOF. With idle > 0, gives up once neither direction (the shared `activity` list) moved for that long."""
    try:
        while True:
            if idle > 0:
                try:
                    data = await asyncio.wait_for(reader.read(65536), idle)
                except TimeoutError:
                    if time.monotonic() - activity[0] < idle:
                        continue
                    break
            else:
                data = await reader.read(65536)
            if not data:
                break
            if activity is not None:
                activity[0] = time.monotonic()
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
    """Every limit refuses rather than queues: over a cap the client gets 503 at once, so a box cannot make the
    host hold more than max_conns sockets or more than svc_budget bytes of /svc bodies at a time."""

    def __init__(self, args):
        self.a = args
        self.log = BlockedLog(args.log)
        self.open = 0
        self.svc_active = 0
        self.svc_bytes = 0

    async def handle(self, reader, writer):
        if self.open >= self.a.max_conns:
            reply(writer, 503, "Service Unavailable", b"too many connections\n")
            try:                                         # swallow what the client already sent, so closing does not
                writer.write_eof()                       # reset the socket before it reads the 503 (bounded: 0.2 s)
                await asyncio.wait_for(reader.read(HEAD_MAX), 0.2)
            except (TimeoutError, OSError):
                pass
            return await close(writer)
        self.open += 1
        try:
            await self._handle(reader, writer)
        except Exception as e:                          # never let one client take the proxy down; never echo details
            print(f"collab-egress: request failed: {type(e).__name__}", file=sys.stderr)
        finally:
            self.open -= 1
            await close(writer)

    async def _handle(self, reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.a.head_timeout)
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
            self.log.blocked(host, port)
            return reply(writer, 403, "Forbidden", b"access pending: this host:port is not approved for this room yet\n")
        try:
            infos = await resolve(host, port)
        except (TimeoutError, OSError):
            return reply(writer, 502, "Bad Gateway", b"cannot resolve host\n")
        upstream, permitted = None, 0
        for family, _, _, _, sockaddr in infos:
            ip = sockaddr[0]
            if forbidden(ip) and (str(ipaddress.ip_address(ip)), port) not in allow:
                continue
            permitted += 1
            try:
                upstream = await asyncio.wait_for(dial(ip, sockaddr[1], family=family), CONNECT_TIMEOUT)
                break
            except (TimeoutError, OSError):
                continue
        if upstream is None:
            if not permitted:                            # listed, but it resolves only to internal addresses
                self.log.blocked(host, port, "forbidden-address")
                return reply(writer, 403, "Forbidden", b"no permitted address for this host\n")
            return reply(writer, 502, "Bad Gateway", b"cannot connect to host\n")
        ur, uw = upstream
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        activity = [time.monotonic()]
        try:
            await asyncio.gather(pipe(reader, uw, self.a.idle_timeout, activity), pipe(ur, writer, self.a.idle_timeout, activity))
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
        cost = int(length) + BODY_MAX                    # the request body plus the largest response it may bring back
        if self.svc_active >= self.a.svc_concurrency or self.svc_bytes + cost > self.a.svc_budget_mb * 1024 * 1024:
            return reply(writer, 503, "Service Unavailable", b"too many service requests at once; retry shortly\n")
        self.svc_active += 1
        self.svc_bytes += cost
        try:
            await self._svc(reader, writer, method, svc, path, query, headers, int(length))
        finally:
            self.svc_active -= 1
            self.svc_bytes -= cost

    async def _svc(self, reader, writer, method, svc, path, query, headers, length):
        body = b""
        if length:
            try:
                body = await asyncio.wait_for(reader.readexactly(length), SVC_TIMEOUT)
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
                    if k.lower() not in STRIP_OUT and not k.lower().startswith("proxy-")
                    and redact(k.encode("latin-1", "replace")) == k.encode("latin-1", "replace")]
        try:
            phrase = http.HTTPStatus(status).phrase       # never the upstream's own reason text: it could carry the key
        except ValueError:
            phrase = "Upstream Status"
        reply(writer, status, phrase, redact(rbody), rheaders)
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
    s.add_argument("--max-conns", type=int, default=128, help="open connections at once; more get 503 (default 128)")
    s.add_argument("--svc-concurrency", type=int, default=4, help="/svc requests at once; more get 503 (default 4)")
    s.add_argument("--svc-budget-mb", type=int, default=128,
                   help="MiB of /svc request body + response cap reserved at once (default 128)")
    s.add_argument("--head-timeout", type=float, default=10, help="seconds to send the request head (default 10)")
    s.add_argument("--idle-timeout", type=float, default=0,
                   help="close a tunnel after this many seconds with no traffic either way; 0 = never (default)")
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
