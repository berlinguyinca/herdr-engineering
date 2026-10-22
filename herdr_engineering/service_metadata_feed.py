"""Read-only listener provenance observer; never owns fabric leases or sockets."""
from __future__ import annotations

import argparse
import copy
import json
import os
import select
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from . import host_config as _hc
from .fabric_agent import (
    ListenerGitMetadata,
    ListenerOwnership,
    _process_identity,
    scan_listening_ports,
)
from .host_config import HostConfigUnavailable, exit_unavailable, resolve_control_plane

MAX_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 4096
MAX_BATCH = 256


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MetadataClient:
    def __init__(self, base_url, api_key=None):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("invalid control-plane URL")
        self.url = base_url.rstrip("/")
        self.key = api_key
        self.http = urllib.request.build_opener(_NoRedirect())
        self.auth_rejected = False

    def retarget(self, url, api_key):
        if url:
            self.url = url.rstrip("/")
        if api_key is not None:
            self.key = api_key

    def request(self, route, body=None):
        # This observer has no gateway client and no lease mutation endpoints.
        path = route.split("?", 1)[0]
        if path not in {"/api/v1/services", "/api/v1/ingest"} or ("?" in route and path != "/api/v1/services"):
            raise ValueError("unsupported metadata transport route")
        if (body is None) != (path == "/api/v1/services"):
            raise ValueError("unsupported metadata transport method")
        raw = json.dumps(body, allow_nan=False).encode() if body is not None else None
        if raw is not None and len(raw) > 16384:
            raise ValueError("metadata envelope too large")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.key:
            headers["X-API-Key"] = self.key
        request = urllib.request.Request(self.url + route, data=raw, headers=headers)
        try:
            with self.http.open(request, timeout=3) as response:
                content = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as error:
            if path == "/api/v1/ingest" and error.code in (401, 403):
                self.auth_rejected = True
            raise
        if len(content) > MAX_BYTES:
            raise ValueError("control-plane reply too large")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("invalid control-plane reply")
        return result


def _port(value):
    return value if type(value) is int and 0 < value < 65536 else None


def _fingerprint(envelope):
    value = copy.deepcopy(envelope)
    payload = value["payload"]
    payload.pop("observed_at", None)
    if isinstance(payload.get("ownership"), dict):
        payload["ownership"].pop("observed_at", None)
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _retained_origin(record, host_id, source_port, pid, identity):
    """A server-verified launch receipt survives its pane, not its process."""
    receipt = record.get("verified_origin")
    proof = receipt.get("ownership") if isinstance(receipt, dict) else None
    current = record.get("ownership")
    if (not isinstance(proof, dict) or not isinstance(current, dict)
            or receipt.get("kind") != "herdr_launch_origin"
            or receipt.get("service_id") != record.get("service_id")
            or receipt.get("host_id") != host_id or record.get("host_id") != host_id
            or record.get("source_port") != source_port
            or proof.get("kind") != "herdr_process_ancestry" or proof.get("host_id") != host_id
            or proof.get("listener_pid") != pid or proof.get("listener_start") != identity["start"]
            or receipt.get("session_id") != proof.get("session_id")
            or record.get("session_id") != proof.get("session_id")
            or any(proof.get(key) != current.get(key) for key in (
                "kind", "host_id", "session_id", "herdr_session", "pane_id", "terminal_id",
                "listener_pid", "listener_start", "shell_pid", "shell_start"))):
        return None
    return copy.deepcopy(proof)


def _debug_listener(pid):
    """Read bounded local argv only to suppress browser debugging endpoints.

    Arguments never enter observations, logs, caches or error messages. An
    inaccessible/truncated command is unknown and cannot advertise a new app.
    """
    process = None
    try:
        if os.uname().sysname == "Darwin":
            process = subprocess.Popen(["ps", "-ww", "-p", str(pid), "-o", "command="],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 0.3
            data = bytearray()
            while len(data) <= 16384:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    return None
                chunk = os.read(process.stdout.fileno(), 16385 - len(data))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > 16384 or process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                return None
            # ps cannot preserve argv boundaries; conservative substring matches
            # may suppress an app but can never expose a debugging endpoint.
            return b"--remote-debugging-port" in data
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            data = stream.read(16385)
        if not data or len(data) > 16384:
            return None
        return any(arg == b"--remote-debugging-port" or arg.startswith(b"--remote-debugging-port=")
                   for arg in data.split(b"\0"))
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if process:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
            process.stdout.close()


class ServiceMetadataFeed:
    def __init__(self, client, *, host_id, state_file, clock=time.monotonic):
        if not isinstance(host_id, str) or not host_id or len(host_id) > 200:
            raise ValueError("invalid host identity")
        self.client = client
        self.host_id = host_id
        self.state_file = Path(state_file)
        self.clock = clock
        self.ownership = ListenerOwnership(host_id)
        self.git = ListenerGitMetadata()
        self.sent = OrderedDict()
        self.cursor = 0
        self.listener_cursor = 0
        self.absence_cursor = 0

    def _store(self):
        # The legacy agent remains the sole writer, including atomic replacements.
        with self.state_file.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("fabric state too large")
        state = json.loads(data)
        if not isinstance(state, dict) or len(state) > MAX_RECORDS:
            raise ValueError("invalid fabric state")
        return state

    def collect(self):
        summary = dict(registered=0, mapped=0, observed=0, owned=0, system=0,
                       linked=0, unconfirmed=0, unmapped=0, ambiguous=0,
                       unregistered_listeners=0, unregistered_owned=0,
                       listener_observed=0, new_discovered=0, listener_missing=0,
                       listener_unconfirmed=0,
                       debug_excluded=0, debug_unknown=0, scan_complete=False,
                       errors=0, truncated=False)
        envelopes = []
        deadline = self.clock() + 20
        try:
            state = self._store()
            # Only this host's records matter, and the full fleet inventory can
            # outgrow the reply cap, which silently stops every observation.
            query = urlencode({"host_id": self.host_id, "fields": "feed"})
            services = self.client.request(f"/api/v1/services?{query}").get("services")
            if not isinstance(services, list) or len(services) > MAX_RECORDS:
                raise ValueError("invalid service inventory")
            listeners = scan_listening_ports()
            observed_at = datetime.now(UTC).isoformat()
        except (OSError, ValueError, TypeError):
            return envelopes, {**summary, "errors": 1}
        index = defaultdict(list)
        native_records = {}
        legacy_ports = set()
        for service in services:
            if (isinstance(service, dict) and service.get("host_id") == self.host_id
                    and isinstance(service.get("service_id"), str)
                    and isinstance(service.get("service_name"), str)
                    and _port(service.get("source_port"))):
                if service.get("discovery_source") == "native_listener":
                    native_records[service["service_id"]] = service
                else:
                    index[(service["service_name"], service["source_port"])].append(service)
                    legacy_ports.add(service["source_port"])
        entries = list(state.items())
        remaining = max(0, deadline - self.clock())
        legacy_deadline = self.clock() + remaining * 0.55
        discovery_deadline = self.clock() + remaining * 0.9
        summary["registered"] = len(entries)
        unregistered = [entry for port, entry in listeners.items() if str(port) not in state]
        summary["unregistered_listeners"] = len(unregistered)
        summary["scan_complete"] = getattr(listeners, "complete", False) is True
        start = self.cursor % len(entries) if entries else 0
        ordered = entries[start:] + entries[:start]
        processed = 0
        for key, entry in ordered:
            if processed >= MAX_BATCH or self.clock() >= legacy_deadline:
                summary["truncated"] = True
                break
            processed += 1
            if not isinstance(entry, dict) or not str(key).isdigit():
                summary["unmapped"] += 1
                continue
            source_port = _port(int(key))
            label = entry.get("label")
            if (not source_port or not isinstance(label, str) or not label
                    or entry.get("source_port", source_port) != source_port):
                summary["unmapped"] += 1
                continue
            matches = index.get((label, source_port), [])
            if len(matches) != 1:
                summary["unmapped"] += 1
                if len(matches) > 1:
                    summary["ambiguous"] += 1
                continue
            summary["mapped"] += 1
            payload = dict(host_id=self.host_id, source_port=source_port,
                           observed_at=observed_at, state="unconfirmed")
            listener = listeners.get(source_port) or {}
            pid = listener.get("pid")
            if listener.get("pid_ambiguous"):
                summary["ambiguous"] += 1
                identity = None
            else:
                identity = _process_identity(pid)
            if identity:
                remaining = max(0, legacy_deadline - self.clock())
                proof = self.ownership.classify(pid, timeout=min(0.75, remaining))
                if proof is None:
                    proof = _retained_origin(matches[0], self.host_id, source_port, pid, identity)
                debug = _debug_listener(pid) if proof and proof.get("kind") == "herdr_process_ancestry" else False
                if debug is not False:
                    summary["debug_excluded" if debug else "debug_unknown"] += 1
                    summary["unconfirmed"] += 1
                    envelopes.append(dict(source="service-metadata-feed", entity_type="service",
                        entity_id=matches[0]["service_id"], event_type="service.metadata", payload=payload))
                    continue
                git = self.git.read(pid, proof, timeout=min(0.5, max(0, legacy_deadline - self.clock())))
                proof_matches = (proof is None or (proof.get("host_id") == self.host_id
                    and proof.get("listener_pid") == pid and proof.get("listener_start") == identity["start"]))
                if _process_identity(pid) == identity and proof_matches:
                    payload.update(state="observed", listener_pid=pid,
                                   listener_start=identity["start"], ownership=proof,
                                   repository_id=git.get("repository_id"), commit_sha=git.get("commit_sha"))
                    if proof and proof.get("kind") == "herdr_process_ancestry":
                        summary["owned"] += 1
                    elif proof and proof.get("kind") == "system_unit":
                        summary["system"] += 1
                    if git.get("repository_id") and git.get("commit_sha"):
                        summary["linked"] += 1
            summary["observed" if payload["state"] == "observed" else "unconfirmed"] += 1
            envelopes.append(dict(source="service-metadata-feed", entity_type="service",
                                  entity_id=matches[0]["service_id"], event_type="service.metadata",
                                  payload=payload))
        self.cursor = (start + processed) % len(entries) if entries else 0
        # New physical-instance observations have no routes, leases or proxy.
        # Only positive native ownership can introduce a service record.
        from .control_plane.service_catalog import service_listener_id

        advertised = set()
        candidates = sorted((port, listener) for port, listener in listeners.items() if str(port) not in state)
        offset = self.listener_cursor % len(candidates) if candidates else 0
        candidates = candidates[offset:] + candidates[:offset]
        visited = set()
        for port, listener in candidates[:MAX_BATCH]:
            if self.clock() >= discovery_deadline:
                summary["truncated"] = True
                break
            visited.add(port)
            pid = listener.get("pid")
            if not _port(port) or not pid or listener.get("pid_ambiguous"):
                continue
            identity = _process_identity(pid)
            if not identity:
                continue
            service_id = service_listener_id(self.host_id, pid, identity["start"], port)
            existing = native_records.get(service_id)
            proof = self.ownership.classify(pid, timeout=min(0.75, max(0, discovery_deadline - self.clock())))
            if proof is None and existing:
                proof = _retained_origin(existing, self.host_id, port, pid, identity)
            if (not proof or proof.get("kind") != "herdr_process_ancestry"
                    or proof.get("host_id") != self.host_id or proof.get("listener_pid") != pid
                    or proof.get("listener_start") != identity["start"]):
                continue
            summary["unregistered_owned"] += 1
            if port in legacy_ports:
                summary["unmapped"] += 1
                continue  # A legacy registration cannot be adopted by port alone.
            debug = _debug_listener(pid)
            if debug is not False:
                summary["debug_excluded" if debug else "debug_unknown"] += 1
                continue
            git = self.git.read(pid, proof, timeout=min(0.5, max(0, discovery_deadline - self.clock())))
            if _process_identity(pid) != identity:
                summary["unconfirmed"] += 1
                continue
            payload = dict(host_id=self.host_id, source_port=port, observed_at=observed_at,
                           state="observed", listener_pid=pid, listener_start=identity["start"],
                           ownership=proof, repository_id=git.get("repository_id"),
                           commit_sha=git.get("commit_sha"))
            process_name = listener.get("process")
            if isinstance(process_name, str) and len(process_name) <= 120 and process_name.isprintable():
                payload["owner"] = process_name
                payload["service_name"] = f"{process_name} on {self.host_id} (port {port})"
            envelopes.append(dict(source="service-metadata-feed", entity_type="service",
                                  entity_id=service_id, event_type="service.listener_observed", payload=payload))
            advertised.add(service_id)
            summary["listener_observed"] += 1
            if existing is None:
                summary["new_discovered"] += 1
            if git.get("repository_id") and git.get("commit_sha"):
                summary["linked"] += 1
        if len(unregistered) > MAX_BATCH:
            summary["truncated"] = True
        self.listener_cursor = (offset + len(visited)) % len(candidates) if candidates else 0
        records = list(native_records.items())
        offset = self.absence_cursor % len(records) if records else 0
        records = records[offset:] + records[:offset]
        examined = 0
        for service_id, record in records[:MAX_BATCH]:
            if self.clock() >= deadline:
                summary["truncated"] = True
                break
            examined += 1
            if service_id in advertised:
                continue
            port = record["source_port"]
            previous = record.get("listener_identity") or {}
            pid, start = previous.get("pid"), previous.get("start")
            if type(pid) is not int or pid <= 0 or not isinstance(start, str) or not start:
                continue
            if service_listener_id(self.host_id, pid, start, port) != service_id:
                continue
            listener = listeners.get(port)
            observation = "unconfirmed"
            if summary["scan_complete"] and listener is None:
                observation = "missing"
            elif listener is not None and not listener.get("pid_ambiguous") and listener.get("pid"):
                current = _process_identity(listener["pid"])
                if current and (listener["pid"] != pid or current["start"] != start):
                    if summary["scan_complete"]:
                        observation = "missing"
                elif current and port not in visited:
                    continue  # A bounded discovery pass did not inspect it yet.
            payload = dict(host_id=self.host_id, source_port=port, observed_at=observed_at,
                           state=observation, scan_complete=summary["scan_complete"], listener_pid=pid, listener_start=start)
            envelopes.append(dict(source="service-metadata-feed", entity_type="service",
                                  entity_id=service_id, event_type="service.listener_observed", payload=payload))
            summary["listener_missing" if observation == "missing" else "listener_unconfirmed"] += 1
        self.absence_cursor = (offset + examined) % len(records) if records else 0
        if len(records) > MAX_BATCH:
            summary["truncated"] = True
        return envelopes, summary

    def run_once(self, *, dry_run=False):
        envelopes, summary = self.collect()
        summary.update(posted=0, advertised=0, unchanged=0, dry_run=dry_run)
        if dry_run:
            return summary
        deadline = self.clock() + 10
        for envelope in envelopes:
            if self.clock() >= deadline:
                summary["truncated"] = True
                break
            key = envelope["entity_id"]
            fingerprint = _fingerprint(envelope)
            previous = self.sent.get(key)
            proof = envelope["payload"].get("ownership")
            refresh = bool(proof) and self.clock() - (previous[1] if previous else 0) >= 45
            if previous and previous[0] == fingerprint and not refresh:
                summary["unchanged"] += 1
                continue
            try:
                result = self.client.request("/api/v1/ingest", envelope)
                entity = result.get("entity")
                if result.get("ok") is not True or not isinstance(entity, dict) or entity.get("entity_id") != key:
                    raise ValueError("metadata observation rejected")
            except (OSError, ValueError, TypeError):
                summary["errors"] += 1
                break
            self.sent.pop(key, None)
            self.sent[key] = (fingerprint, self.clock())
            while len(self.sent) > 512:
                self.sent.popitem(last=False)
            summary["posted"] += 1
            if envelope["event_type"] == "service.listener_observed":
                summary["advertised"] += 1
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-plane", default=None,
                        help="control plane URL (default: host.json control_plane_url)")
    parser.add_argument("--host-id", default=None, help="host id (default: host.json host_id)")
    parser.add_argument("--state-file", default=str(Path(os.environ.get("HERDR_ENGINEERING_STATE",
                        str(Path.home() / ".local/share/herdr-engineering/state"))) / "fabric-agent.json"))
    parser.add_argument("--api-key-file", default=None,
                        help="read the ingest key from this file (default: ingest.api_key)")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", type=float)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.loop is not None and (not 30 <= args.loop <= 3600 or args.once):
        parser.error("--loop requires 30–3600 seconds and cannot be combined with --once")
    fully_explicit = bool(args.control_plane and args.host_id and args.api_key_file)
    try:
        cfg = _hc.host_config()
    except HostConfigUnavailable as exc:
        if not fully_explicit:
            return exit_unavailable(exc)
        cfg = None  # every value is given explicitly: no host config needed
    try:
        key = cfg.api_key if cfg else None
        if args.api_key_file:
            with Path(args.api_key_file).expanduser().open() as stream:
                key = stream.read(8193).strip()
        if key and len(key) > 8192:
            raise ValueError("invalid API key file")
        feed = ServiceMetadataFeed(MetadataClient(resolve_control_plane(args.control_plane), key),
                                   host_id=args.host_id or cfg.host_id, state_file=args.state_file)
    except (OSError, ValueError):
        parser.exit(1, "Service metadata observer configuration is unavailable or invalid.\n")
    refresher = None
    if args.loop is not None and not args.dry_run and cfg is not None and not fully_explicit:
        refresher = _hc.ConfigRefresher(cfg)
    stop = threading.Event()
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.is_set():
            summary = feed.run_once(dry_run=args.dry_run)
            print(json.dumps(summary), flush=True)
            if args.loop is None:
                return 1 if summary["errors"] else 0
            if refresher is not None:
                try:
                    refresher.sync(feed.client, follow_url=not args.control_plane,
                                   follow_key=not args.api_key_file)
                except HostConfigUnavailable as exc:
                    return exit_unavailable(exc)
            stop.wait(args.loop)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
