"""Contract-level fake servers for the slice 5 tests: a real HTTP server on 127.0.0.1 that speaks the REAL GitHub REST and Gmail REST
shapes the product code uses. They are fakes of the PROVIDERS, never of our code: the product's own request construction, auth
headers, response parsing and error mapping all run against them over a real socket. They are NOT the real services, so what they
prove is "the code follows the documented contract", not "GitHub or Gmail behaves this way today".

Fault knobs (all per server): ``down`` (every call 503), ``fail_next`` (the next N calls answer a status without doing anything),
``drop_after_create`` (the work is done, then the connection is cut with no answer: the ambiguous case), ``revoked`` (Gmail token
endpoint answers invalid_grant). ``requests`` records (method, path, query, body) of every call, never a header.
"""
from __future__ import annotations

import base64
import email
import json
import threading
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Base:
    """Start/stop plumbing and the fault knobs shared by both fakes."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.requests: list[tuple[str, str, dict, object]] = []
        self.down = False
        self._fail: list[int] = []
        self.drop_after_create = False
        self._httpd = _Server(("127.0.0.1", 0), self._handler())
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def fail_next(self, status: int, times: int = 1) -> None:
        with self.lock:
            self._fail.extend([status] * times)

    def _take_failure(self) -> int | None:
        with self.lock:
            if self.down:
                return 503
            return self._fail.pop(0) if self._fail else None

    def count(self, method: str, path_prefix: str) -> int:
        with self.lock:
            return sum(1 for m, p, _q, _b in self.requests if m == method and p.startswith(path_prefix))

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):          # noqa: D401 - silence
                pass

            def _serve(self):
                parts = urllib.parse.urlsplit(self.path)
                query = {k: v[-1] for k, v in urllib.parse.parse_qs(parts.query).items()}
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                headers = {k.lower(): v for k, v in self.headers.items()}
                with fake.lock:
                    fake.requests.append((self.command, parts.path, query, raw.decode("utf-8", "replace")))
                failure = fake._take_failure()
                if failure is not None:
                    return self._send(failure, {"message": "injected"})
                status, body, extra, drop = fake.route(self.command, parts.path, query, raw, headers)
                if drop:
                    self.close_connection = True
                    self.connection.close()
                    return None
                return self._send(status, body, extra)

            def _send(self, status, body, extra=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = do_PUT = _serve

        return Handler


class FakeGitHubServer(_Base):
    """``POST /repos/{o}/{r}/issues`` and ``GET /repos/{o}/{r}/issues`` (state, since, per_page, page, Link), Bearer auth."""

    def __init__(self, token: str = "ghp_FAKE_TOKEN_SENTINEL_0123456789abcdef") -> None:
        self.token = token
        self.repos: dict[str, list[dict]] = {}
        self.next_number = 1
        super().__init__()

    def add_repo(self, name: str) -> None:
        with self.lock:
            self.repos.setdefault(name.lower(), [])

    def issues(self, name: str) -> list[dict]:
        with self.lock:
            return list(self.repos.get(name.lower(), []))

    def route(self, method, path, query, raw, headers):
        if headers.get("authorization") != f"Bearer {self.token}":
            return 401, {"message": "Bad credentials"}, {}, False
        segs = path.strip("/").split("/")
        if not (len(segs) == 4 and segs[0] == "repos" and segs[3] == "issues"):
            return 404, {"message": "Not Found"}, {}, False
        repo = f"{segs[1]}/{segs[2]}".lower()
        with self.lock:
            if repo not in self.repos:
                return 404, {"message": "Not Found"}, {}, False
            if method == "POST":
                try:
                    doc = json.loads(raw)
                except ValueError:
                    return 400, {"message": "Problems parsing JSON"}, {}, False
                title = doc.get("title")
                if not isinstance(title, str) or not title.strip():
                    return 422, {"message": "Validation Failed"}, {}, False
                n = self.next_number
                self.next_number += 1
                stamp = iso(_now())
                issue = {"number": n, "title": title, "body": doc.get("body") or "", "state": "open",
                         "labels": [{"name": x} for x in doc.get("labels") or []], "html_url": f"https://github.com/{repo}/issues/{n}",
                         "created_at": stamp, "updated_at": stamp}
                self.repos[repo].append(issue)
                if self.drop_after_create:
                    self.drop_after_create = False
                    return 0, {}, {}, True
                return 201, issue, {}, False
            if method == "GET":
                per = max(1, min(int(query.get("per_page", 30)), 100))
                page = max(1, int(query.get("page", 1)))
                rows = sorted(self.repos[repo], key=lambda i: i["updated_at"], reverse=True)
                if "since" in query:
                    rows = [i for i in rows if i["updated_at"] >= query["since"]]
                chunk = rows[(page - 1) * per: page * per]
                extra = {}
                if page * per < len(rows):
                    extra["Link"] = f'<{self.base}{path}?page={page + 1}>; rel="next"'
                return 200, chunk, extra, False
        return 405, {"message": "Method Not Allowed"}, {}, False


class FakeGmailServer(_Base):
    """The OAuth token endpoint plus ``users.drafts`` create/list/get and ``users.messages.send`` (which must stay unused)."""

    def __init__(self, refresh_token: str = "refresh-u1", access_token: str = "ya29.FAKE_ACCESS_SENTINEL") -> None:
        self.refresh_token, self.access_token = refresh_token, access_token
        self.revoked = False
        self.drafts: list[dict] = []
        self.sent: list[dict] = []
        super().__init__()

    def route(self, method, path, query, raw, headers):
        if path == "/token":
            form = urllib.parse.parse_qs(raw.decode())
            if self.revoked or form.get("refresh_token", [""])[0] != self.refresh_token:
                return 400, {"error": "invalid_grant"}, {}, False
            return 200, {"access_token": self.access_token, "expires_in": 3600, "token_type": "Bearer"}, {}, False
        if headers.get("authorization") != f"Bearer {self.access_token}":
            return 401, {"error": {"code": 401, "message": "Invalid Credentials"}}, {}, False
        base = "/gmail/v1/users/me"
        with self.lock:
            if method == "POST" and path == base + "/messages/send":
                self.sent.append(json.loads(raw))
                return 200, {"id": "sent-1", "threadId": "t-sent"}, {}, False
            if method == "POST" and path == base + "/drafts":
                doc = json.loads(raw)
                msg = email.message_from_bytes(base64.urlsafe_b64decode(doc["message"]["raw"] + "=="), policy=policy.default)
                n = len(self.drafts) + 1
                draft = {"id": f"r-{n}", "message_id": f"m-{n}", "thread_id": doc["message"].get("threadId") or f"th-{n}",
                         "headers": {k.lower(): " ".join(str(v).split()) for k, v in msg.items()}, "body": msg.get_content()}
                self.drafts.append(draft)
                if self.drop_after_create:
                    self.drop_after_create = False
                    return 0, {}, {}, True
                return 200, {"id": draft["id"], "message": {"id": draft["message_id"], "threadId": draft["thread_id"]}}, {}, False
            if method == "GET" and path == base + "/drafts":
                rows = list(reversed(self.drafts))[: int(query.get("maxResults", 100))]
                return 200, {"drafts": [{"id": d["id"], "message": {"id": d["message_id"], "threadId": d["thread_id"]}}
                                        for d in rows]}, {}, False
            if method == "GET" and path.startswith(base + "/drafts/"):
                did = path.rsplit("/", 1)[1]
                d = next((x for x in self.drafts if x["id"] == did), None)
                if d is None:
                    return 404, {"error": {"code": 404, "message": "Requested entity was not found."}}, {}, False
                wanted = [h.lower() for h in urllib.parse.parse_qs(urllib.parse.urlsplit(self._raw_path(path, query)).query)
                          .get("metadataHeaders", [])] or list(d["headers"])
                hdrs = [{"name": k, "value": v} for k, v in d["headers"].items() if k in wanted]
                return 200, {"id": d["id"], "message": {"id": d["message_id"], "threadId": d["thread_id"],
                                                         "payload": {"headers": hdrs}}}, {}, False
        return 404, {"error": {"code": 404, "message": "Not Found"}}, {}, False

    @staticmethod
    def _raw_path(path, query):
        return path + "?" + urllib.parse.urlencode(query)

    def opener(self):
        """The transport the product's ``GoogleWorkspace`` is given: it sends the REAL request (built by the product, with the
        product's headers and body) to this server instead of to Google's hostnames. Nothing about the request is altered."""
        base = self.base

        def open_(req, timeout):
            url = req.full_url
            for host, prefix in (("https://gmail.googleapis.com", ""), ("https://oauth2.googleapis.com", "")):
                if url.startswith(host):
                    url = base + prefix + url[len(host):]
            fwd = urllib.request.Request(url, data=req.data, headers=dict(req.header_items()), method=req.get_method())
            return urllib.request.urlopen(fwd, timeout=timeout)     # noqa: S310 - loopback test server
        return open_


def hours_ago(h: float) -> str:
    return iso(_now() - timedelta(hours=h))
