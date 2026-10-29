
"""Control-plane HTTP/SSE API server (Spec §20-34, §42-47, §52, §85, §91-92).

The web UI is a *client* of this structured `/api/v1` JSON + SSE stream. Uses
the stdlib http.server (no new deps, consistent with the rest of the repo),
backed by the in-memory `ControlPlaneRepo`. Static web assets (design system +
web components + web client + app) are served from `--web-root`.

Private-by-default: binds loopback (or the tailnet) unless told otherwise.
Optional `X-API-Key` auth for multi-operator deployments (Spec §104).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..version import __version__
from .artifacts import ArtifactConflict, ArtifactError
from .envelope import EnvelopeConflict
from .events import SequenceCollision
from .federation import kind_for_role
from .graph import read_graph
from .ingest import INGEST_ENTITY_TYPES
from .redact import redact_delivery, redact_ingest_body, redact_payload
from .review_quorum import pr_review_quorum

log = logging.getLogger(__name__)

# Routes a host-side tool (session run, plannotator bridge) may POST with the
# narrow `agent` role, besides /api/v1/ingest. Nothing else.
_SUMMARY_FIX = re.compile(r"^/api/v1/sessions/[^/]+/summary/(?:fix|report)$")
# The per-host worker that runs full /summary reports where the session records live (ingest role).
_HOST_REPORT = re.compile(r"^/api/v1/host/summary-reports/(?:claim|result|publish)$")
# Terminal button: builds (never runs) the viewer-side attach command; same guard as summary fix.
_TERMINAL_ATTACH = re.compile(r"^/api/v1/sessions/[^/]+/terminal$")
_AGENT_POST = re.compile(r"^/api/v1/(?:llm/keys|reviews|llm/keys/[^/]+/revoke"
                        r"|reviews/[^/]+/(?:heartbeat|result|failure)"
                        r"|architecture/(?:snapshots|analyses|analyses/claim)"
                        # CI fix runner: claim, report state, report result. Never create or cancel.
                        r"|ci/fix/claim|ci/fix/[^/]+/(?:state|result))$")
# Report mutations have their own artifact owner/edit/review checks after this
# narrow role gate. This does not grant agents publication or generic mutation.
_REPORT_AGENT_POST = re.compile(
    r"^/api/v1/(?:artifact-reports(?:/[^/]+/jobs)?|"
    r"artifacts/[^/]+/(?:blobs|versions(?:/[^/]+/(?:feedback|checks))?))$")
# T12: the one POST an ingest key may make besides /api/v1/ingest: acknowledging a steering request addressed to it.
_STEER_ACK = re.compile(r"^/api/v1/steering/[^/]+/ack$")
# T16 gap C: the second: ask for a re-review of a PR whose head has no quorum (ingest-role key; scoped in the handler).
# Matched on the DECODED segments, as routing sees them: a raw-path regex let /review-quorum/%72ereview reach the
# handler without the Origin / Content-Type checks.
def _is_rereview(path):
    parts = [unquote(seg) for seg in path.split("/")]
    return (len(parts) == 8 and parts[:5] == ["", "api", "v1", "graph", "nodes"]
            and parts[6:] == ["review-quorum", "rereview"])


# The only routes the `reviewer` role may POST (review gate workers), and only it:
# an operator key is refused there too. See `_reviewer_route`.
_REVIEWER_ROOT = ["", "api", "v1", "review-gate"]
_REVIEWER_JOB_ACTIONS = frozenset({"heartbeat", "result", "failure"})


def _reviewer_route(parts):
    """``(action, job_id)`` for a reviewer worker route, else None.

    ``parts`` is the DECODED segment list (``/api/v1/review-gate/...`` split on
    "/" and unquoted per segment), so an encoded slash stays inside one segment
    and never matches: exactly ``review-gate/claim`` or
    ``review-gate/jobs/<job_id>/{heartbeat,result,failure}``. The job id shape
    (32 hex) is the handler's check (400).
    """
    if len(parts) == 5 and parts[:4] == _REVIEWER_ROOT and parts[4] == "claim":
        return "claim", None
    if (
        len(parts) == 7
        and parts[:4] == _REVIEWER_ROOT
        and parts[4] == "jobs"
        and parts[6] in _REVIEWER_JOB_ACTIONS
    ):
        return parts[6], parts[5]
    return None


_CLAIM_FIELDS = (
    "job_id",
    "repo",
    "number",
    "sha",
    "messages",
    "lease_seconds",
    "attempt",
    "claim_token",
)
_RESULT_OUTCOMES = frozenset({"recorded", "stale", "rejected"})
_FAILURE_OUTCOMES = frozenset({"released", "escalated", "stale"})
_REVIEWER_ID = re.compile(r"[A-Za-z0-9._:/-]{1,128}")
_REVIEWER_JOB_ID = re.compile(r"[0-9a-f]{32}")
_REVIEWER_CLAIM_TOKEN = re.compile(r"[0-9a-f]{64}")
_REVIEWER_SMALL_BODY = 8 * 1024
_REVIEWER_REASON_MAX = 500
_log = logging.getLogger(__name__)
# Optional build stamp; the compose build arg sets it (see Dockerfile). Falls
# back to "dev" in a local checkout / un-stamped image.
_COMMIT = os.environ.get("HERDR_ENGINEERING_COMMIT", "dev")


def _is_loopback(host: str) -> bool:
    """True if the host we bind resolves to a loopback address (127.0.0.1 / ::1)."""
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

_WEB_ROOT = Path(__file__).resolve().parent.parent.parent / "packages"
_INDEX = "index.html"
_MAX_JSON_BODY = 4 * 1024 * 1024
_MAX_ARCHITECTURE_BODY = 64 * 1024 * 1024  # a published graph snapshot; validated by the service
_ARCHITECTURE_KEYS = frozenset({"enabled", "explain", "cache", "request_ttl_seconds", "agent_hosts",
                                "require_host_binding"})
_MAX_SSE_SECONDS = 3600
_SSE_IDLE_SECONDS = 10  # keep-alive comment cadence while the stream is idle


class _DaemonThreadingServer(ThreadingHTTPServer):
    # Handler threads are daemons so a stuck SSE client can never block
    # server shutdown / process exit.
    daemon_threads = True
    # The stdlib default backlog (5) resets connections when dozens of agents
    # emit at once (60+ agents is the design point).
    request_queue_size = 256

    def __init__(self, address, handler, *, request_workers=128, request_queue=256):
        self.request_workers = max(1, int(request_workers))
        self.request_queue = max(5, int(request_queue))
        self.request_queue_size = self.request_queue
        self._request_slots = threading.BoundedSemaphore(self.request_workers)
        self.rejected_requests = 0
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            self.rejected_requests += 1
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nRetry-After: 2\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()



def _dashboard_host_ok(host_header, extra_hosts):
    """A Host an attacker cannot make point here: an IP literal, localhost, or a configured name.
    DNS rebinding needs a DNS name, so a bare name is refused unless the operator listed it."""
    import ipaddress
    host = host_header.strip().lower()
    if host in extra_hosts:
        return True
    name = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True

def _content_length(raw):
    """Non-negative int, or None when the header is malformed (never raises)."""
    if raw is None or raw == "":
        return 0
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 9 else None


class _RateLimiter:
    """Tiny in-memory per-client sliding-window limiter (best-effort).

    Guards against runaway clients / accidental tight loops; not a security
    boundary. Keyed by client IP with a generous default so it never hinders
    normal use within the Tailnet. SSE streams are excluded (long-lived).
    """

    def __init__(self, max_requests=600, window_seconds=60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits = {}  # key -> list of monotonic timestamps
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.max_requests:
                self._hits[key] = hits
                return False
            hits.append(now)
            if len(self._hits) > 20000:   # bounded: forget keys with no recent hits
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self.window}
            self._hits[key] = hits
            return True


_FLEET_CONFIG_PATH = "/api/v1/fleet/config"
# Host updaters (InferWeave deploy/pipelines/deploy_event.py) POST here with their per-host fleet
# token as a Bearer credential; the /api/v1 spelling is accepted as well.
_DEPLOYMENT_EVENTS_PATHS = frozenset({"/api/deployments/events", "/api/v1/deployments/events"})


def _gate_pr(snap):
    """A PrSnapshot as plain JSON (no approved_patch_id; findings are already redacted)."""
    return {
        "key": str(snap.key), "head_sha": snap.head_sha, "state": snap.state.value,
        "review_rounds": snap.review_rounds, "ci_fix_rounds": snap.ci_fix_rounds,
        "reason": snap.reason, "approved_sha": snap.approved_sha,
        "last_findings": [asdict(f) for f in snap.last_findings],
    }


class ControlPlaneServer:
    """Structured control-plane API + SSE + static web server."""

    def __init__(self, repo, *, api_key=None, api_keys=None, host="127.0.0.1",
                 port=0, web_root=None, source=None, allow_open=False,
                 rustfs=None, dispatcher=None, github_monitor=None,
                 artifact_store=None, publication_controller=None,
                 artifact_job_manager=None, artifact_review_manager=None,
                 artifact_acl=None, handoff_registry=None,
                 collab=None, collab_tls_terminated=False,
                 collab_require_https=False, fleet_config=None, ci_actions=False, federation_source=None,
                 public_port=None, public_host=None, collab_only=False,
                 operator_port=None, operator_user=None, operator_email=None,
                 operator_hosts=(), collab_operator_url=None, collab_dashboard_hosts=(),
                 summary_config=None, summary_client=None, summary_report_config=None, usage_retention=None, lineage_key_scopes=None,
                 review_quorum=None, system_graph: bool = False, attention=None, ci_fix=None, missions=None, github_pg_dsn=None,
                 architecture_enabled=None, architecture_explain=None, architecture_options=None,
                 request_workers=128, request_queue=256,
                 assistant_service=None, assistant_config=None, fabric_resolver=None, fabric_services=None, fabric_names=None,
                 scheduler_admission=False, scheduler_limits=None):
        """
        Auth (Spec §104): either a single `api_key` (all-access) or a mapping
        `api_keys={key: role}` with roles "viewer" (read-only) / "operator"
        (read + mutate) / "ingest" (read + POST /api/v1/ingest only) /
        "agent" (ingest + host-side tool routes) / "reviewer" (read + POST the
        four review-gate worker routes only; no other role may POST those).
        No key configured = open (private-by-default bind).

        `allow_open` is an explicit opt-in that permits an unauthenticated
        server on a non-loopback bind. Only use it when the host port is bound
        to the Tailscale interface (or proxied by `tailscale serve`), because
        the Tailnet is then the trust boundary — all machines belong to each
        other and are encrypted end-to-end via WireGuard.
        """
        self.repo = repo
        self.request_workers = max(1, int(request_workers))
        self.request_queue = max(5, int(request_queue))
        from .retention import usage_retention_config
        # control_plane.llm_proxy raw/rollup retention (validated; None = defaults).
        self.usage_retention = usage_retention_config(usage_retention)
        # control_plane.ci_fix (validated; None = defaults, which are disabled: the routes then 404).
        from .ci_fix import CIFixService, ci_fix_config
        self.ci_fix_config = ci_fix_config(ci_fix)
        self.ci_fix = CIFixService(repo, self.ci_fix_config) if self.ci_fix_config["enabled"] else None
        from .ci_monitor import CIMonitor
        self.ci_monitor = CIMonitor(on_transition=lambda key, event, row:
                                    repo.record_event("ci_pipeline", key, event, row),
                                    actions_enabled=ci_actions,
                                    on_observation=repo.observe_ci_pipeline)
        # control_plane.deployment_definitions: a checkout of InferWeave deploy/pipelines (read live;
        # absent = none, and deployment ingest stays accept-all with a warning). Targets come from the
        # CI monitor's last snapshot, never from a provider call here. It is only read once definitions
        # are loaded, and start=True (idempotent) keeps the shared poller running from then on: without
        # the review gate nothing else starts it, and a stopped poller would leave every target None.
        from .deployment_definitions import DeploymentDefinitions
        self.deployment_definitions = DeploymentDefinitions(
            lambda: self._control_plane_value(fleet_config, "deployment_definitions"),
            ci_snapshot=lambda: self.ci_monitor.snapshot(start=True))
        # control_plane.missions (validated; outcomes are written when a mission ends, on by default).
        from .mission_outcome import MissionOutcomes, missions_config
        self.missions_config = missions_config(missions)
        self.mission_outcomes = MissionOutcomes(repo, self.missions_config)
        repo.mission_outcomes = self.mission_outcomes
        from .attention_inbox import AttentionInbox, attention_config
        self.attention_config = attention_config(attention)
        self.attention = None
        if self.attention_config["enabled"] and not collab_only:
            self.attention = AttentionInbox(repo, config=self.attention_config, health=self._health,
                                            ci_snapshot=self.ci_monitor.snapshot, ci_fix=self.ci_fix)
        if github_monitor is None:
            # control-plane-web only (the worker never builds a server): the GitHub
            # catalog persists in control_plane.pg_dsn when set, else in memory.
            from ..github import build_monitor
            github_monitor = build_monitor(github_pg_dsn)
        self.github_monitor = github_monitor
        self.dispatcher = dispatcher or self._dispatch_native_message
        if source is not None:
            from .bridge import load_into
            load_into(repo, source)
        self.source = source
        self.agent_summaries = None
        self.summary_report = None
        self._summary_stop = threading.Event()
        self._summary_scanner = None
        if not collab_only:
            from .agent_summaries import AgentSummaryService
            self.agent_summaries = AgentSummaryService(repo, summary_config or {},
                                                       persist=repo.persist_agent_summary,
                                                       model_client=summary_client)
            self.agent_summaries.hydrate(repo.agent_summary_events())
            from .summary_report import SummaryReportService
            self.summary_report = SummaryReportService(repo, summary_report_config or {})
        # Optional durable object store (RustFS / S3). When wired, artifact
        # payloads are stored content-addressed here instead of the repo's
        # in-memory blob stand-in, so they survive a web restart.
        self.rustfs = rustfs
        self.artifact_store = artifact_store
        self.publication_controller = publication_controller
        self.artifact_job_manager = artifact_job_manager
        self.artifact_review_manager = artifact_review_manager
        self.artifact_acl = artifact_acl
        from .handoffs import HandoffRegistry
        self.handoffs = handoff_registry or HandoffRegistry(repo)
        from .scheduler_service import SchedulerService
        self.scheduler_admission = scheduler_admission is True
        self.scheduler = SchedulerService(repo, **(scheduler_limits or {}))
        # Herdr Personal Assistant (Rakazo integration, P1/P2). Canonical owner;
        # feature-flag gated (off by default) and inert until enabled.
        if assistant_service is None:
            from .assistant import AssistantService
            from .assistant import assistant_config as _build_assistant_config
            self.assistant = AssistantService(
                config=_build_assistant_config(assistant_config or {}), repo=repo)
        else:
            self.assistant = assistant_service
        from .ingest import IngestService
        # operator YAML: {api key: {rooms: [...], missions: [...]}} -> principal ids (the key itself is never kept)
        scopes = {}
        for key, allow in (lineage_key_scopes or {}).items():
            digest = hashlib.sha256(str(key).encode()).hexdigest()[:12]
            scopes[f"key:{digest}"] = {"rooms": frozenset(allow.get("rooms") or ()),
                                       "missions": frozenset(allow.get("missions") or ())}
        self.ingest = IngestService(repo, key_scopes=scopes, system_graph=system_graph)
        # ADR-0008 P1: hot `control_plane.federation` section (None = off). Nothing mints a peer principal yet.
        self.ingest.peer_source = federation_source
        self._limiter = _RateLimiter()
        # Fleet telemetry has a separate bounded budget so bursts cannot starve
        # dashboard reads or operator commands from the same host/proxy IP.
        self._ingest_limiter = _RateLimiter(max_requests=6000)
        self._stream_limiter = _RateLimiter(max_requests=120)
        # Fail-closed (Spec §104): never default a blank/missing role to
        # "operator". Keep roles verbatim after strip; empty/None/unknown
        # roles are treated as "viewer" (read-only) by `_require`.
        self._keys = {k: (v.strip() if isinstance(v, str) else "")
                      for k, v in (api_keys or {}).items()}
        if api_key is not None:
            self._keys[api_key] = "operator"
        self.api_key = api_key  # kept for backward compat / introspection
        self.allow_open = allow_open
        # Fail-closed (Spec §104): an unauthenticated server is only ever
        # allowed on a loopback bind unless the operator explicitly opts in
        # with allow_open (Tailnet trust boundary). Never silently exposed.
        if not self._keys and not _is_loopback(host) and not allow_open:
            raise ValueError(
                "refusing to bind an unauthenticated control plane to a "
                f"non-loopback address {host!r}; configure keys in the central "
                "fleet config (control_plane.api_keys) or --api-keys, or pass "
                "--allow-open to trust the Tailnet boundary (Spec §104)")
        # HCN (feature-flagged, off by default). Bearer credentials must never
        # cross a network in clear text, so a non-loopback bind needs an explicit
        # assertion that TLS is terminated in front of this server.
        if (collab is not None and not _is_loopback(host)
                and not (collab_tls_terminated or collab_require_https)):
            raise ValueError(
                "refusing to enable collaboration on non-loopback address "
                f"{host!r} without collab_tls_terminated (bearer credentials "
                "require TLS)")
        self.fleet_config = fleet_config
        # callable(urls, source_machine) -> {'resolutions': [...]}; None when no fabric gateway is configured
        self.fabric_resolver = fabric_resolver
        self.fabric_services = fabric_services      # callable() -> {'leases': [...]} | None
        self.fabric_names = fabric_names            # callable() -> {'url_base', 'aliases', 'scheme'} | None
        self.review_quorum = review_quorum      # QuorumConfig | None (feature off)
        from .review_quorum import RereviewLedger
        self.rereview = RereviewLedger(repo)
        self._refusal_logged: dict = {}    # T16 gap C: durable re-review requests (the route 404s when the quorum is off)
        self.collab = collab
        self.collab_require_https = collab_require_https
        # ADR-0008 P2: signed read-only export endpoints (/api/v1/federation/*). Inert unless the hot YAML switches
        # `control_plane.federation` on; the gateway is rebuilt if `collab` is swapped (tests do).
        self.federation_source = federation_source
        self._fed_gateway = None
        if collab is not None and federation_source is not None:
            collab.federation_source = federation_source
        # Optional second listener that serves ONLY collaboration (API, MCP and its page);
        # it is the only one an internet-facing proxy should be pointed at.
        if public_port is not None and collab is None:
            raise ValueError("a public collaboration listener needs collab enabled")
        self.collab_only = collab_only
        if operator_port is not None and not (collab_only and operator_user and operator_hosts):
            raise ValueError("the operator listener needs collab-only mode, a user and allowed hosts")
        self.operator_port, self.operator_user = operator_port, operator_user
        self.operator_email = operator_email
        self.operator_hosts = frozenset(h.lower() for h in operator_hosts)
        self._operator_httpd = None
        # Dashboard side: forward /api/v1/collab/* to the operator listener of the isolated
        # collab service, so the local owner needs no sign-in.
        self.collab_operator_url = collab_operator_url.rstrip("/") if collab_operator_url else None
        self.collab_dashboard_hosts = frozenset(h.lower() for h in collab_dashboard_hosts)
        # "Let the agent fix this" on a dead session: start a text-only helper through the collab
        # service's summary-fix room (it answers 409 helper_unavailable when none is configured).
        from .summary_fix import http_helper_spawner, inprocess_helper_spawner
        self.summary_fix_helper = (
            inprocess_helper_spawner(collab, operator_user) if collab is not None and operator_user
            else http_helper_spawner(self.collab_operator_url) if self.collab_operator_url else None)
        if collab_only and public_port is None:
            raise ValueError("collab-only mode needs the public collaboration port")
        self.public_port = public_port
        self.public_host = public_host or host
        self._public_httpd = None
        if collab is not None:
            collab.hydrate_or_degrade(repo.take_collab_replay())     # T16 H1: a bad log disables collaboration only
            if not collab_only:
                # membership-scoped lineage reads (lineage-events.md, "Scope semantics"): the
                # isolated collab-only process never sees fabric data, so it serves none
                collab.graph_source = lambda: repo.graph
                # External agents (T10) report lineage about themselves through the HCN route; the service
                # builds the envelope, this only registers the room that enrollment bound it to.
                self.ingest.reserved_node = collab.is_external_node
                self.ingest.service_node = collab.is_hcn_node
                collab.lineage_sink = self._external_lineage
                collab.quorum_config = review_quorum        # T15: review items and the "reviewed at head" evidence use the T06 policy
        self.host = host
        self.port = port
        self.web_root = Path(web_root) if web_root else _WEB_ROOT
        self._subscribers = set()
        self._lock = threading.Lock()
        from .request_metrics import RequestMetrics
        self.request_metrics = RequestMetrics()
        self._ready = threading.Event()
        self._httpd = None
        self.gate_service = self.gate_store = self.gate_queue = self.gate_main = None
        self.gate_coordinator = self.gate_reviewer_config = None
        self._start_review_gate(repo, fleet_config)
        from ..architecture.service import ArchitectureService
        arch_opts = self._architecture_options(fleet_config, architecture_options)
        self.architecture = ArchitectureService(
            repo, enabled=self._architecture_enabled(fleet_config, architecture_enabled),
            cache_graphs=arch_opts["cache_graphs"], cache_bytes=arch_opts["cache_bytes"],
            request_ttl=arch_opts["request_ttl"])
        # control_plane.architecture.agent_hosts: {api key: [host ids]} -> principal id -> allowed host ids.
        # An agent key listed there may publish / claim / report only for those hosts; an agent key NOT listed
        # is trusted to name any host_id (keys carry a role only; there is no per-host enrollment) -- unless
        # control_plane.architecture.require_host_binding is true (the recommended production setting): then an
        # unlisted agent key is refused on every architecture POST.
        self.architecture_require_host_binding = arch_opts["require_host_binding"]
        self.architecture_agent_hosts = {}
        for key, hosts in arch_opts["agent_hosts"].items():
            if self._keys.get(key) != "agent":
                raise ValueError("control_plane.architecture.agent_hosts: each entry must name a configured "
                                 "api key with the agent role")
            digest = hashlib.sha256(key.encode()).hexdigest()[:12]
            self.architecture_agent_hosts[f"key:{digest}"] = frozenset(hosts)
        self.architecture.gate_store_fn = lambda: self.gate_store
        from .intel_bridge import install as install_intelligence
        self.intelligence, self.intelligence_bridge = install_intelligence(
            repo, lambda: self._intelligence_section(fleet_config), github_pg_dsn)
        repo.egress_fn = lambda: self._intelligence_section(fleet_config).get("egress") or {}
        self.intelligence.metrics_fn = self._intelligence_metrics
        self.intelligence.github_app_fn = lambda: getattr(self, "gate_app", None)
        self.intelligence.ci_repair_fn = self._intelligence_ci_repair
        if getattr(self, "assistant", None) is not None:
            self.assistant.intelligence_read = self.intelligence.assistant_read
        self._configure_architecture_explain(fleet_config, architecture_explain)
        if collab is not None:
            collab.architecture = self.architecture
        # live stream (Spec §52)
        repo.subscribe(self._broadcast)

    def _intelligence_ci_repair(self, repository, run, operation_id, actor):
        """Executor hook for an approved ``request_ci_repair``: the existing CI-fix request, idempotent on the proposal id."""
        if self.ci_fix is None:
            raise ValueError("CI fix agents are not enabled (control_plane.ci_fix)")
        return self.ci_fix.request(self.ci_monitor, repository, run, operation_id, actor=actor)

    def _intelligence_metrics(self) -> dict:
        from collections import Counter
        bridge = self.intelligence_bridge
        return {"bridge": dict(bridge.stats), "queue_depth": bridge._q.qsize(),
                "incidents": dict(Counter(i["state"] for i in bridge.board.summary()))}

    @staticmethod
    def _control_plane_value(fleet_config, key):
        """One `control_plane` value from the last good fleet config; None when absent.

        Uses ``current()`` (last good), not ``secure_section``: for deployment definitions a
        failing-closed read would mean "not configured", which is the permissive state."""
        if fleet_config is None:
            return None
        from ..fleet_config import FleetConfigError
        try:
            return fleet_config.current().section("control_plane").get(key)
        except FleetConfigError:
            return None

    def deployments_snapshot(self) -> dict:
        """Deployment rows plus the definitions status (incl. the accept-all warning flag)."""
        return self.deployment_definitions.annotate(self.repo.deployments.snapshot())

    @staticmethod
    def _intelligence_section(fleet_config):
        """`control_plane.project_intelligence` read on every request (hot kill switch); {} when absent."""
        if fleet_config is None:
            return {}
        from ..fleet_config import FleetConfigError
        try:
            section = fleet_config.current().section("control_plane")
        except FleetConfigError:
            return {}
        value = (section or {}).get("project_intelligence")
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _architecture_enabled(fleet_config, override):
        """`control_plane.architecture.enabled` in the central fleet config; off unless set."""
        if override is not None:
            return bool(override)
        if fleet_config is None:
            return False
        from ..fleet_config import FleetConfigError
        try:
            section = fleet_config.current().section("control_plane")
        except FleetConfigError:
            return False
        arch = (section or {}).get("architecture") or {}
        unknown = set(arch) - _ARCHITECTURE_KEYS
        if unknown:
            raise ValueError(f"control_plane.architecture: unknown key(s) {sorted(unknown)}")
        if not isinstance(arch.get("enabled", False), bool):
            raise ValueError("control_plane.architecture.enabled must be a boolean")
        return arch.get("enabled", False)

    @staticmethod
    def _architecture_options(fleet_config, override):
        """Validated cache / request-expiry / agent-host settings of `control_plane.architecture` (read once at
        startup: a change needs a restart). A bad value fails startup with the offending key named."""
        from ..architecture import service as svc
        section = {}
        if fleet_config is not None:
            from ..fleet_config import FleetConfigError
            try:
                section = dict((fleet_config.current().section("control_plane") or {}).get("architecture") or {})
            except FleetConfigError:
                section = {}
        section.update(override or {})
        unknown = set(section) - _ARCHITECTURE_KEYS
        if unknown:
            raise ValueError(f"control_plane.architecture: unknown key(s) {sorted(unknown)}")

        def whole(value, name, low, high):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"control_plane.architecture.{name} must be an integer in [{low}, {high}]")
            return value
        cache = section.get("cache") or {}
        if not isinstance(cache, dict) or set(cache) - {"max_graphs", "max_bytes"}:
            raise ValueError("control_plane.architecture.cache takes only max_graphs and max_bytes")
        hosts = section.get("agent_hosts") or {}
        if not isinstance(hosts, dict) or not all(
                isinstance(v, list) and v and all(isinstance(h, str) and svc._HOST_ID.fullmatch(h) for h in v)
                for v in hosts.values()):
            raise ValueError("control_plane.architecture.agent_hosts must map an api key to a non-empty list of host ids")
        binding = section.get("require_host_binding", False)
        if not isinstance(binding, bool):
            raise ValueError("control_plane.architecture.require_host_binding must be a boolean")
        return {"require_host_binding": binding,
                "cache_graphs": whole(cache.get("max_graphs", svc.DEFAULT_CACHE_GRAPHS), "cache.max_graphs", 1, 1000),
                "cache_bytes": whole(cache.get("max_bytes", svc.DEFAULT_CACHE_BYTES), "cache.max_bytes",
                                     1024 * 1024, 64 * 1024 ** 3),
                "request_ttl": whole(section.get("request_ttl_seconds", svc.DEFAULT_REQUEST_TTL),
                                     "request_ttl_seconds", 60, 7 * 86400),
                "agent_hosts": {str(k): list(v) for k, v in hosts.items()}}

    def _configure_architecture_explain(self, fleet_config, override):
        """Optional LLM explanations: only from an explicit, validated `architecture.explain` section."""
        from ..architecture import explain as ex
        section = override
        if section is None and fleet_config is not None:
            from ..fleet_config import FleetConfigError
            try:
                section = ((fleet_config.current().section("control_plane") or {}).get("architecture") or {}).get("explain")
            except FleetConfigError:
                section = None
        cfg = ex.parse_config(section)
        self.architecture.explain_config = cfg
        if cfg and cfg.get("api_key_file"):
            try:
                self.architecture.explain_key = Path(cfg["api_key_file"]).expanduser().read_text().strip()
            except OSError as exc:
                raise ValueError(f"control_plane.architecture.explain.api_key_file is unreadable: {exc.strerror}") from exc

    def _external_lineage(self, envelope, principal, valid=None):
        """Ingest one envelope the HCN service built for an external agent. The principal's allowed room is
        registered from the envelope's own room (which the service took from the enrollment), so the
        existing per-principal scope check applies unchanged."""
        # a room service (T14 consultation thread) writes under the subject's mission label: the envelope's own
        # mission id, which the service took from the node's scope, is the one mission it may address
        mission = envelope.get("mission_id") if str(principal["id"]).startswith("collab:room:") else None
        self.ingest.key_scopes[principal["id"]] = {
            "rooms": frozenset({envelope["room_id"]}), "missions": frozenset({mission} if mission else ())}
        return self.ingest.ingest(envelope, principal, valid=valid)

    def _start_review_gate(self, repo, fleet_config):
        """Build and start the PR review gate when the central config enables it.

        Last step of construction, so no earlier failure leaks the gate thread. An
        enabled-but-invalid config raises ValueError (the same fail-loud stance as
        the bind/collab checks above): a half-configured gate never starts.
        """
        if fleet_config is None:
            return
        from ..fleet_config import FleetConfigError
        from ..review_gate.config import load_review_gate_config
        try:
            section = fleet_config.current().section("control_plane")
        except FleetConfigError:
            return  # no control_plane section: nothing to enable
        config = load_review_gate_config({"control_plane": section})
        if not config.enabled:
            return
        # Gate events are written require_durable=True. A sink that cannot
        # acknowledge a write would fail every one of them; refuse up front.
        # (No sink at all = in-memory repo; the CLI refuses that without a DSN.)
        sink = getattr(repo, "_sink", None)
        if sink is not None and not callable(getattr(sink, "record_required", None)):
            raise ValueError("review_gate: the event sink cannot make durable writes")
        from ..review_gate import github_app
        from ..review_gate.coordinator import ReviewCoordinator
        from ..review_gate.main_verify import MainVerifier
        from ..review_gate.notifier import AuthorNotifier
        from ..review_gate.queue import ReviewQueue
        from ..review_gate.registry import AuthorRegistry
        from ..review_gate.service import GateService
        from ..review_gate.store import PipelineStore
        self.gate_store = PipelineStore(
            repo,
            review_caps={e.repo: e.review_cap for e in config.repos},
            ci_fix_caps={e.repo: e.ci_fix_cap for e in config.repos})
        # The queue's real lease and the lease a Claim reports must be one value.
        self.gate_queue = ReviewQueue(repo, lease_seconds=config.reviewer.lease_seconds)
        app = github_app.GitHubAppClient(config.app)  # one client for gate + coordinator
        self.gate_app = app
        registry = AuthorRegistry(repo)
        notifier = AuthorNotifier(repo, config=config.notify, registry=registry,
                                  dispatcher=self.dispatcher)
        self.gate_main = MainVerifier(repo, app, notifier)
        self.gate_service = GateService(
            config=config,
            app=app,
            store=self.gate_store,
            queue=self.gate_queue,
            registry=registry,
            ci_snapshot=lambda: self.ci_monitor.snapshot(start=False),
            notifier=notifier,
            main_verifier=self.gate_main,
            repair_gate=self._build_repair_gate(app, fleet_config, repo, registry),
        )
        self.gate_reviewer_config = config.reviewer
        self.gate_coordinator = ReviewCoordinator(
            store=self.gate_store,
            queue=self.gate_queue,
            app=app,
            config=config.reviewer,
            enrolled={e.repo for e in config.repos},
        )
        # The gate must work with no browser open, and snapshot(start=False) never
        # starts the poller, so the server starts the shared monitor itself.
        self.ci_monitor.start()
        self.gate_service.start()

    def _build_repair_gate(self, app, fleet_config, repo=None, registry=None):
        """Inert unless ``project_intelligence.repair.enabled``; read on every PR so the kill switch is hot.

        Once repair has been seen enabled, an unreadable central config means "kill switch on" (reject), never
        "policy absent" (which would let merges proceed unchecked).
        """
        from ..fleet_config import FleetConfigError
        from . import repair_policy as rp
        from .repair_gate import make_repair_gate, policy_from_section
        seen = {"enabled": False}

        def policy():
            if fleet_config is None:
                return None
            try:
                section = fleet_config.current().section("control_plane")
            except FleetConfigError:
                return rp.RepairPolicy(kill_switch=True) if seen["enabled"] else None
            value = ((section or {}).get("project_intelligence") or {})
            result = policy_from_section(value.get("repair") if isinstance(value, dict) else None)
            seen["enabled"] = seen["enabled"] or result is not None
            return result

        def families():
            value = self._intelligence_section(fleet_config).get("repair")
            fam = value.get("model_families") if isinstance(value, dict) else None
            return fam if isinstance(fam, dict) else {}

        facts = None
        if repo is not None and registry is not None:
            from .repair_gate import make_facts_fn
            facts = make_facts_fn(app=app, repo=repo, registry=registry, families_fn=families,
                                  board_lookup=lambda k: self.intelligence_bridge.board._pr.get(k))
        extra = {"facts_fn": facts} if facts else {}
        if repo is not None:
            from .repair_gate import make_counters
            # looked up per call: the gate is built before project intelligence is installed
            attempt, open_prs = make_counters(app=app, repo=repo, board_lookup=lambda k: self.intelligence_bridge.board._pr.get(k))
            extra.update(attempt_fn=attempt, open_repair_prs_fn=open_prs)
        return make_repair_gate(app=app, policy_fn=policy, **extra)

    def _dispatch_native_message(self, record, message, artifact_ids):
        from ..errors import InvalidError
        from ..herdr_adapter import NativeHerdrAdapter

        if artifact_ids:
            if not all(isinstance(aid, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', aid)
                       for aid in artifact_ids):
                raise InvalidError('artifact references must be valid identifiers')
            message += '\n\nAttached HerdR artifact references:\n' + '\n'.join(
                f'- herdr-artifact://{aid}' for aid in artifact_ids)
        target = record.get("terminal_id")
        conversation = record.get("agent_session")
        if not target or not conversation or not record.get("host_id"):
            raise InvalidError("native terminal, conversation and host identity are required")
        adapter = NativeHerdrAdapter()
        machine = record.get("machine")
        if not machine and record["host_id"] not in {socket.gethostname(), socket.getfqdn()}:
            resolved = adapter.resolve_machine(record["host_id"])
            if resolved is None:
                raise InvalidError("remote host has no explicit native machine route")
            machine = resolved.id
        server = record.get("herdr_session")
        if not server:
            raise InvalidError("named HerdR server identity is required")
        agents = adapter.list_agents(machine=machine, session=server)
        matches = [agent for agent in agents if agent.terminal_id == target]
        if len(matches) != 1 or matches[0].agent_session != conversation:
            raise InvalidError("native session identity changed; refresh before delivery")
        if matches[0].interactive_ready is False or matches[0].launch_pending:
            raise InvalidError("native agent is not ready for prompt delivery")
        return adapter.send_input(target, message, machine=machine, session=server)

    def _health(self):
        """Repo stats + deployment context (source backend, image version/commit)."""
        h = self.repo.health()
        h["source"] = "postgres" if self.source is not None else "memory"
        h["rustfs"] = "s3" if self.rustfs is not None else "memory"
        h["version"] = __version__
        h["commit"] = _COMMIT
        if self.repo.agent_identities.conflict_count:
            h["agent_identity_degraded"] = True
            h["agent_identity_conflicts"] = self.repo.agent_identities.conflict_count
        h["performance"] = self.request_metrics.snapshot()
        h['performance'].update(request_workers=self.request_workers, request_queue=self.request_queue,
                                rejected_requests=getattr(self._httpd, 'rejected_requests', 0))
        if self.collab is not None and self.collab.degraded:
            h["collab_degraded"] = True       # no detail here: the reason is in the server log and the repair command
        h["capabilities"] = ["mission_activity_v1", "ci_v1", "plannotator_reviews_v1", "github_v1"]
        if self.collab is not None:
            h["capabilities"].append("collab_v1")
        if getattr(self, "assistant", None) is not None and self.assistant.config.get("enabled"):
            h["capabilities"].append("assistant_v1")
        if getattr(self, "intelligence", None) is not None and self.intelligence._cfg()[0]["enabled"]:
            h["capabilities"].append("project_intelligence_v1")
        if self.architecture.enabled:
            h["capabilities"].append("architecture_v1")
            if self.architecture.explain_config:
                h["capabilities"].append("architecture_explain_v1")
        return h

    # ------------------------------------------------------- SSE plumbing
    def _broadcast(self, event):
        dead = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    # ------------------------------------------------------------- server
    def serve(self):
        if self.collab_only:
            # No operator listener at all: this process serves the public collab surface only.
            self._httpd = _DaemonThreadingServer(
                (self.public_host, self.public_port), _make_handler(self, public=True))
            self.public_port = self.port = self._httpd.server_address[1]
            if self.operator_port is not None:
                self._operator_httpd = _DaemonThreadingServer(
                    (self.public_host, self.operator_port), _make_handler(self, operator=True))
                self.operator_port = self._operator_httpd.server_address[1]
                threading.Thread(target=self._operator_httpd.serve_forever, daemon=True).start()
            self._ready.set()
            self._httpd.serve_forever()
            return
        self._httpd = _DaemonThreadingServer(
            (self.host, self.port), _make_handler(self),
            request_workers=self.request_workers, request_queue=self.request_queue)
        self.port = self._httpd.server_address[1]
        if self.public_port is not None:
            self._public_httpd = _DaemonThreadingServer(
                (self.public_host, self.public_port), _make_handler(self, public=True))
            self.public_port = self._public_httpd.server_address[1]
            threading.Thread(target=self._public_httpd.serve_forever, daemon=True).start()
        self._ready.set()
        if self.agent_summaries and self.agent_summaries.config.enabled:
            self.agent_summaries.start()
            self._summary_scanner = threading.Thread(target=self._summary_loop, daemon=True)
            self._summary_scanner.start()
        threading.Thread(target=self._sweep_loop, daemon=True).start()
        if self.collab is not None and self.collab.lineage_sink is not None:
            threading.Thread(target=self._link_flush_loop, daemon=True).start()
        self._httpd.serve_forever()

    def _sweep_loop(self, interval=300):
        """Periodic retention, starting at once so re-hydrated expired rows do not linger."""
        while self._httpd is not None:
            self.sweep_once()
            time.sleep(interval)

    def sweep_once(self):
        """Inspector age retention for idle sessions too (ingest only prunes the active
        one) and token_usage raw retention (control_plane.llm_proxy.raw_retention_days)."""
        from .retention import sweep_token_usage
        try:
            self.repo.inspector.sweep()
        except Exception:
            pass
        try:
            sweep_token_usage(self.repo, raw_retention_days=self.usage_retention["raw_retention_days"])
        except Exception as exc:
            log.warning("token_usage retention sweep failed: %s", type(exc).__name__)

    def _link_flush_loop(self, interval=30):
        """T14: tell the lineage graph what it missed about cross-workspace links (a retraction the graph was down
        for), once at start and then on a timer. The pending set is derived from the durable link records."""
        while self._httpd is not None:
            try:
                self.collab.maintain()
            except Exception:
                pass
            time.sleep(interval)

    def wait_until_ready(self, timeout=5.0):
        self._ready.wait(timeout=timeout)

    def _summary_loop(self):
        while not self._summary_stop.is_set():
            try:
                for session in self.repo.list_sessions():
                    if self._summary_stop.is_set():
                        break
                    sid = session["session_id"]
                    if self.repo.inspector.has_session(sid):
                        self.agent_summaries.request(sid)
            except Exception:
                pass  # Retain continuity notes through temporary storage/model outages.
            self._summary_stop.wait(2)

    def _refusal_log_due(self, key, every: float = 300.0, cap: int = 512) -> bool:
        """At most one refused-request log line per key per `every` seconds (an unnamed key cannot flood the log)."""
        import time as _t
        now = _t.monotonic()
        last = self._refusal_logged.get(key)
        if last is not None and now - last < every:
            return False
        if len(self._refusal_logged) >= cap:
            self._refusal_logged.clear()
        self._refusal_logged[key] = now
        return True

    def shutdown(self):
        if self.gate_service is not None:
            self.gate_service.stop()
        self._summary_stop.set()
        if self._summary_scanner:
            self._summary_scanner.join(timeout=2)
        if self.agent_summaries:
            self.agent_summaries.close()
        self.ci_monitor.close()
        self.github_monitor.close()
        if self._operator_httpd:
            self._operator_httpd.shutdown()
            self._operator_httpd.server_close()
        if self._public_httpd:
            self._public_httpd.shutdown()
            self._public_httpd.server_close()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()


def _fabric_session(repo, inst):
    """The fabric session an agent's instance_id points at. Besides a full session id it may be
    ``pane:<host>:<pane id>`` (what a host-side runner knows right after it opens a herdr pane, before
    the pane's plugin has reported its server name); that resolves to the newest session on that pane."""
    sess = repo.get_session(inst)
    if sess or not inst.startswith("pane:"):
        return sess
    _, _, rest = inst.partition(":")
    host, _, pane = rest.partition(":")
    for cand in repo.list_sessions():                  # newest first
        if cand.get("host_id") == host and cand.get("pane_id") == pane:
            return cand
    return None


def _fabric_enriched_flow(repo, payload: bytes) -> bytes:
    """Dashboard side only: join the owner's own agents (instance_id = a fabric session id) to the
    sessions, hosts and missions this control plane knows. The collab service never sees fabric data."""
    try:
        flow = json.loads(payload)
        if "nodes" not in flow:                      # a room-state reply carries the flow under "flow"
            if not isinstance(flow.get("flow"), dict):
                return payload
            enriched = json.loads(_fabric_enriched_flow(repo, json.dumps(flow["flow"]).encode()))
            flow["flow"] = enriched
            return json.dumps(flow).encode()
        for node in flow.get("nodes", []):
            inst = node.get("instance_id")
            if node.get("kind") != "agent" or not inst:
                continue
            sess = _fabric_session(repo, inst)
            if not sess:
                node["fabric"] = {"session_id": inst, "status": "unknown"}
                continue
            mission = repo.get_mission(sess.get("mission_id")) if sess.get("mission_id") else None
            node["fabric"] = {"session_id": sess.get("session_id", inst), "status": sess.get("status"), "host": sess.get("host_id"),
                              "agent_role": sess.get("agent_role"), "mission_id": sess.get("mission_id"),
                              "mission_title": (mission or {}).get("title"), "cwd": sess.get("cwd"),
                              "branch": sess.get("branch"), "name": sess.get("name"),
                              "display_name": sess.get("display_name"), "agent_handle": sess.get("agent_handle"),
                              "parent_session_id": sess.get("parent_session_id")}
        return json.dumps(flow).encode()
    except Exception:           # the flow view must never break because fabric data is odd
        return payload


def _with_collab_tasks(server, repo, mission_key, timeline):
    """Add the collaboration tasks of agents linked to this mission's sessions to its timeline.
    Dashboard side only (it asks the operator listener); statuses and names, never task text. Any
    failure leaves the timeline as it was."""
    if not server.collab_operator_url:
        return timeline
    try:
        m = repo.get_mission(mission_key)
        keys = {mission_key, (m or {}).get("mission_id"), (m or {}).get("mission_key")} - {None}
        ids = []
        for s in repo.list_sessions():
            if s.get("mission_id") in keys:             # an agent may be linked by full id or by pane
                ids += [s["session_id"], f"pane:{s.get('host_id')}:{s.get('pane_id')}"]
        ids = ids[:100]
        if not ids:
            return timeline
        req = urllib.request.Request(
            server.collab_operator_url + "/api/v1/collab/fleet/task-activity",
            data=json.dumps({"instance_ids": ids}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:   # noqa: S310 (operator-configured URL)
            rows = json.loads(resp.read(1_000_000)).get("activity", [])
        extra = [{"ts": r["updated_at"], "kind": "collab", "status": r["status"],
                  "title": f"@{r['agent']} {r['direction']} a task in {r.get('room') or 'a room'} ({r['status']})",
                  "ref": None, "entity_id": r["task_id"]} for r in rows]
        return sorted(timeline + extra, key=lambda i: (i["ts"] or "", i["kind"], i["entity_id"] or ""))
    except Exception:
        return timeline


_PUBLIC_STATIC = {
    "/": ("collab.html", "text/html; charset=utf-8"),
    "/collab-app.js": ("collab-app.js", "text/javascript"),
    "/collab.css": ("collab.css", "text/css"),
    "/collab-view.js": ("collab-view.js", "text/javascript"),
    "/collab-scripts.js": ("collab-scripts.js", "text/javascript"),
    "/collab-flow.js": ("collab-flow.js", "text/javascript"),
    "/collab-links.js": ("collab-links.js", "text/javascript"),
    "/collab-tabs.js": ("collab-tabs.js", "text/javascript"),
    "/collab-proposals.js": ("collab-proposals.js", "text/javascript"),
    "/collab-lineage.js": ("collab-lineage.js", "text/javascript"),
    "/collab-attention.js": ("collab-attention.js", "text/javascript"),
    "/collab-mention.js": ("collab-mention.js", "text/javascript"),
    "/collab-context.js": ("collab-context.js", "text/javascript"),
    "/collab-helpers.js": ("collab-helpers.js", "text/javascript"),
    "/collab-workbench.js": ("collab-workbench.js", "text/javascript"),
    "/motion.js": ("motion.js", "text/javascript"),
    "/patch.js": ("patch.js", "text/javascript"),
    "/conversation-composer.js": ("conversation-composer.js", "text/javascript"),
    "/conversation-markdown.js": ("conversation-markdown.js", "text/javascript"),
    "/conversation-json.js": ("conversation-json.js", "text/javascript"),
    "/conversation.css": ("conversation.css", "text/css"),
    "/guide": ("collab-guide.html", "text/html; charset=utf-8"),
    "/collab-guide.js": ("collab-guide.js", "text/javascript"),
    "/collab-guide.md": ("collab-guide.md", "text/markdown; charset=utf-8"),
    "/herdr-web-client/collab.js": ("herdr-web-client/collab.js", "text/javascript"),
    "/herdr-web-components/util.js": ("herdr-web-components/util.js", "text/javascript"),
    "/herdr-design-system/theme.css": ("herdr-design-system/theme.css", "text/css"),
    "/herdr-design-system/base.css": ("herdr-design-system/base.css", "text/css"),
}
_SCRIPT_FILES = {"join.sh": "join.sh", "worker.sh": "worker.sh", "sandbox.sh": "sandbox.sh"}
_PUBLIC_CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
               "connect-src 'self' https://*.amazoncognito.com; img-src 'self' data:; "
               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _make_handler(server, public=False, operator=False):
    class _Handler(BaseHTTPRequestHandler):
        server_version = "HerdrControlPlane/0.1"
        protocol_version = "HTTP/1.1"
        # Public listener only: a client that stalls mid-request cannot hold a thread for ever.
        # (The operator listener is tailnet-only and keeps its blocking behaviour.)
        timeout = 30

        def log_message(self, fmt, *args):  # keep test output clean
            pass

        # ------------------------------------------------------------ auth
        def _role(self):
            if not server._keys:
                parts = self._decoded_parts()
                runner_write = (self.command == "POST" and parts[:5] == ["", "api", "v1", "ci", "fix"]
                                and ((len(parts) == 6 and parts[5] == "claim")
                                     or (len(parts) == 7 and parts[6] in {"state", "result"})))
                if server.ci_fix is not None and runner_write:
                    # Only these runner writes use the dedicated key. Browser reads and
                    # create/cancel retain the existing open tailnet posture.
                    supplied = self.headers.get_all("X-API-Key") or []
                    if len(supplied) == 1 and secrets.compare_digest(
                            supplied[0].encode(), server.ci_fix_config["agent_api_key"].encode()):
                        return "agent"
                    return None
                # open — only reachable via loopback or an explicit --allow-open
                # (Tailnet trust boundary); the fail-closed guard enforces this
                return "operator"
            supplied = self.headers.get("X-API-Key", "")
            # constant-time compare to avoid a timing side channel
            for candidate, role in server._keys.items():
                if secrets.compare_digest(supplied, candidate):
                    return role
            return None

        def _principal(self):
            """Who the request authenticated as (server-side actor of ingest envelopes).

            The API key itself is never recorded, only a short digest so
            events from one credential can be grouped.
            """
            if not server._keys:
                return {"kind": "operator", "id": "open-listener", "authenticated_by": "open_listener"}
            supplied = self.headers.get("X-API-Key", "")
            for candidate, role in server._keys.items():
                if secrets.compare_digest(supplied, candidate):
                    digest = hashlib.sha256(candidate.encode()).hexdigest()[:12]
                    # "external" and "collab" (HCN service) and "peer"/"peer-user" (federation YAML, ADR-0008) are
                    # principal kinds only the server can mint; a key whose configured role spells one of them is an
                    # ordinary unprivileged key, never that principal.
                    kind = kind_for_role(role)
                    return {"kind": kind, "id": f"key:{digest}", "authenticated_by": "api_key"}
            return {"kind": "unknown", "id": None, "authenticated_by": "none"}

        def _deny(self, status, message):
            """Send the error response, close the connection, and report the refusal.

            The request body (if any) is deliberately left unread, so the connection
            must not be reused: on keep-alive the unread bytes would be parsed as the
            next request (request smuggling behind a filtering proxy).
            """
            payload = json.dumps({"error": message}).encode()
            self._response_status = status
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
            return True

        def _resource_principal(self):
            """Stable resource owner identity derived from server authentication."""
            supplied = self.headers.get("X-API-Key", "")
            digest = hashlib.sha256(supplied.encode()).hexdigest()[:16]
            return f"api-key:{digest}:{self._role() or 'local'}"

        def _artifact_version_allowed(self, artifact_id, version_id, permission):
            """Permission on the artifact AND the version must belong to it (registry-backed only)."""
            if not self._artifact_allowed(artifact_id, permission):
                return False
            if server.artifact_store is None:
                return True
            return bool(version_id) and self._artifact_call("get_version", version_id, artifact_id) is not None

        def _artifact_forbidden(self):
            # Versioned registry artifacts (reports) must not leak existence.
            if server.artifact_store is not None:
                return self._deny(404, "not found")
            return self._deny(403, "forbidden")

        def _artifact_allowed(self, artifact_id, permission):
            if server.artifact_acl is None:
                return True
            if self._artifact_acl_allows(artifact_id, permission):
                return True
            # Versioned-registry artifacts (native reports) are also private to their
            # durable owner and grants, which the registry itself records. Reads stay
            # ACL-only: an unauthorized read must never reach the store.
            return bool(permission != "read" and server.artifact_store is not None
                        and artifact_id and self._artifact_call(
                "report_permission", artifact_id, self._resource_principal(), permission))

        def _artifact_acl_allows(self, artifact_id, permission):
            principal = self._resource_principal()
            if server.artifact_acl.can(artifact_id, principal, permission):
                return True
            record = server.repo.get_artifact(artifact_id)
            if record and record.get('created_by') == principal:
                return True
            # Uploads made before artifact ACLs were introduced were readable by
            # all viewers. Explicit grants take precedence over that legacy rule.
            return bool(permission == 'read' and record and not record.get('created_by')
                        and not server.artifact_acl.has_grants(artifact_id))

        def _require(self, min_role):
            """Return True when the request was refused (response already sent), else None.

            Role model (Spec §104): viewer = read-only; operator = read +
            mutate; ingest = read + POST /api/v1/ingest only (read-only
            everywhere else); agent = ingest plus exactly the host-side tool routes
            (LLM key mint/revoke, review create/result); reviewer = read-only plus
            POST to exactly the four review-gate worker routes (claim, job
            heartbeat/result/failure), which NO other role may POST, operator
            included (so open/keyless mode cannot use them). Unknown roles are
            treated as viewer (fail-closed for mutations).

            The return value MUST be truthy on refusal: callers do
            ``if self._require(...): return``. (It used to return the result of
            ``_send_json`` -- always None -- so a 401/403 was sent and the request
            then executed anyway.)
            """
            role = self._role()
            if role is None:
                return self._deny(401, "unauthorized")
            if self.command == "POST" and self._is_reviewer_namespace():
                # Decided on the DECODED path (routing unquotes), before the
                # operator shortcut below: only `reviewer` gets past here.
                if role != "reviewer":
                    return self._deny(403, "reviewer role required")
                if _reviewer_route(self._decoded_parts()) is None:
                    return self._deny(404, "not found")
                return None
            if role == "reviewer" and min_role != "viewer":
                return self._deny(403, "read-only")
            if min_role == "operator":
                if role == "operator":
                    return None
                path = self.path.split("?")[0]
                if (role == "ingest" and self.command == "POST"
                        and path == "/api/v1/ingest"):
                    return None
                if (role == "agent" and self.command == "POST"
                        and (path == "/api/v1/ingest" or _AGENT_POST.match(path)
                             or _REPORT_AGENT_POST.match(path))):
                    return None
                return self._deny(403, "read-only")
            if min_role == "ingest":
                if role in ("operator", "ingest", "agent"):
                    return None
                return self._deny(403, "read-only")
            return None

        def _decoded_parts(self):
            """Path segments unquoted exactly as ``_route_api`` routes them."""
            return [unquote(seg) for seg in self.path.split("?")[0].split("/")]

        def _is_reviewer_namespace(self):
            """True for any POST target under review-gate/claim or review-gate/jobs."""
            parts = self._decoded_parts()
            return (
                len(parts) > 4
                and parts[:4] == ["", "api", "v1", "review-gate"]
                and parts[4] in ("claim", "jobs")
            )

        # ------------------------------------------------------------ routes
        # -------------------------------------------- public (collab-only) listener
        def _public(self):
            """Everything not on the allow-list is a plain 404: the legacy API, the
            dashboard and its static files do not exist on this listener."""
            path = self.path.split("?")[0]
            # Request framing must be unambiguous: the edge and this server must agree where a
            # request ends, or a pipelined second request could be smuggled behind the first.
            cl = self.headers.get_all("Content-Length") or []
            if (len(cl) > 1 or "Transfer-Encoding" in self.headers
                    or (self.command == "GET" and cl and cl[0].strip() not in ("", "0"))):
                return self._deny(400, "ambiguous request framing")
            if path == "/.well-known/agent-card.json" and self.command == "GET":
                return self._agent_card()
            if self.command == "GET" and path.startswith("/api/v1/collab/scripts/"):
                return self._script(path.rsplit("/", 1)[1])
            if self._is_collab_path() or path in ("/mcp", "/a2a"):
                if path == "/mcp":
                    return self._mcp()
                return self._a2a() if path == "/a2a" else self._collab()
            if self.command == "GET" and path in _PUBLIC_STATIC:
                name, ctype = _PUBLIC_STATIC[path]
                target = (server.web_root / name)
                if not target.is_file():
                    return self._deny(404, "not found")
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Security-Policy", _PUBLIC_CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(data)
                return None
            return self._deny(404, "not found")

        def _script(self, name):
            """The bash a `curl ... | bash` runs: templates with this site's address filled in. They contain no
            secret (the join token travels in the caller's environment, never in the URL or these files)."""
            import re as _re
            if server.collab is None or name not in _SCRIPT_FILES:
                return self._deny(404, "not found")
            if self._rate_limit(self._proxied_client()):
                return None
            if (server.collab_require_https
                    and self.headers.get("X-Forwarded-Proto", "").lower() != "https"):
                return self._deny(400, "collaboration is only served over https")
            base = ((server.collab.public_config or {}).get("public_url") or "").rstrip("/")
            target = server.web_root / "collab-scripts" / _SCRIPT_FILES[name]
            if not _re.fullmatch(r"(https://[A-Za-z0-9.-]+|http://(127\.0\.0\.1|localhost))(:[0-9]{1,5})?", base) or not target.is_file():
                return self._deny(404, "not found")
            text = target.read_text(encoding="utf-8").replace("'__HERDR_BASE__'", f"'{base}'").replace("__HERDR_BASE__", base)
            data = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/x-shellscript; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return None

        def _agent_card(self):
            from . import collab_a2a
            if server.collab is None:
                return self._deny(404, "not found")
            base = (server.collab.public_config or {}).get("public_url") or ""
            if not base:
                return self._deny(404, "not found")
            self.send_response(200)
            data = json.dumps(collab_a2a.agent_card(base, delegation=server.collab.delegation_enabled)).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "public, max-age=300")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _a2a(self):
            from . import collab_a2a
            return self._jsonrpc(collab_a2a.handle, "A2A")

        def _mcp(self):
            from . import collab_mcp
            return self._jsonrpc(collab_mcp.handle, "MCP")

        def _jsonrpc(self, handle, label):
            from .collab import MAX_BODY
            if server.collab is None:
                return self._deny(404, "not found")
            if self._rate_limit(self._proxied_client()):
                return None
            if self.command != "POST":
                return self._deny(405, "POST only")
            if (server.collab_require_https
                    and self.headers.get("X-Forwarded-Proto", "").lower() != "https"):
                return self._deny(400, "collaboration is only served over https")
            if self.headers.get("Origin"):
                return self._deny(403, f"browser origins may not call the {label} endpoint")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                return self._deny(415, "application/json required")
            length = _content_length(self.headers.get("Content-Length"))
            if length is None or length <= 0 or length > MAX_BODY:
                return self._deny(413, "request body size not allowed")
            try:
                message = json.loads(self.rfile.read(length))
            except (ValueError, RecursionError):
                return self._json({"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32700, "message": "parse error"}}, 400)
            try:
                status, payload = handle(
                    server.collab, message, self.headers.get("Authorization", ""),
                    self.headers.get("X-Herdr-Device"))
            except Exception:
                return self._json({"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32603, "message": "internal error"}}, 500)
            if payload is None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            return self._json(payload, status)

        def _federation(self):
            """ADR-0008 P2. Main listener only; every refusal is the same 404 (see federation_export). The response
            shape does not depend on whether federation is on: the same rate limiter is consumed, the connection is
            always closed afterwards, and the body is the same bytes."""
            from .federation_export import NOT_FOUND, FederationGateway
            gw = server._fed_gateway
            if gw is None or gw.service is not server.collab:
                gw = server._fed_gateway = FederationGateway(server.collab, server.federation_source)
            if self._rate_limit(self._proxied_client()):
                return None
            cl = self.headers.get_all("Content-Length") or []
            if (len(cl) > 1 or "Transfer-Encoding" in self.headers or (cl and cl[0].strip() not in ("", "0"))
                    or (server.collab_require_https
                        and self.headers.get("X-Forwarded-Proto", "").lower() != "https")):
                return self._fed_send(*NOT_FOUND)
            return self._fed_send(*gw.handle(self.command, self.path, self.headers, b""))

        def _fed_send(self, status, payload, extra):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")      # always: an unread body must not become the next request
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        def _is_federation_path(self):
            return self.path.startswith("/api/v1/federation/") or self.path.split("?")[0] == "/api/v1/federation"

        def do_GET(self):
            return self._timed_request(self._do_GET)

        def _timed_request(self, dispatch):
            self._request_id = secrets.token_hex(8)
            self._request_started = time.monotonic()
            self._response_status = 500
            self._request_cause = None
            try:
                return dispatch()
            except Exception as exc:
                self._request_cause = type(exc).__name__
                raise
            finally:
                elapsed_ms = (time.monotonic() - self._request_started) * 1000
                server.request_metrics.record(self.path,
                    elapsed_ms, self._response_status, self._request_cause, self._request_id)
                route = server.request_metrics.route(self.path)
                if route is not None and (self._response_status >= 500 or elapsed_ms >= 2000):
                    log.warning('request id=%s route=%s status=%s duration_ms=%.2f cause=%s',
                                self._request_id, route, self._response_status,
                                elapsed_ms, self._request_cause or 'unknown')

        def send_response(self, code, message=None):
            self._response_status = code
            super().send_response(code, message)

        def end_headers(self):
            # Federation deliberately makes off/on refusal responses identical.
            if hasattr(self, '_request_id') and not self._is_federation_path():
                self.send_header('X-Request-ID', self._request_id)
                self.send_header('Server-Timing', f'app;dur={(time.monotonic() - self._request_started) * 1000:.2f}')
            super().end_headers()

        def _do_GET(self):
            if operator:
                return self._operator()
            if public:
                return self._public()
            if self._is_federation_path():
                return self._federation()
            if self._is_collab_path():
                return self._collab()
            if self._path_only() == _FLEET_CONFIG_PATH:
                return self._fleet_config_get()
            if self._path_only() == "/api/v1/health" and self._role() is None:
                # Keyed mode without a valid key: liveness only (deploy.sh's health
                # check needs status + commit), no counters, sources or capabilities.
                h = server._health()
                return self._json({k: h.get(k) for k in ("status", "version", "commit")})
            err = self._require("viewer")
            if err:
                return err
            if self.path.startswith("/api/v1/"):
                return self._route_api()
            return self._serve_static()

        def do_POST(self):
            return self._timed_request(self._do_POST)

        def _do_POST(self):
            if operator:
                return self._operator()
            if public:
                return self._public()
            if self._is_federation_path():
                return self._federation()        # read-only: answers the same 404 as everything else
            # Unambiguous framing, as on the public/operator listeners: this server reads
            # only Content-Length, so a chunked or doubly-sized body would be parsed as
            # the next request on a keep-alive connection. Applies to collab paths too.
            if (len(self.headers.get_all("Content-Length") or []) > 1
                    or "Transfer-Encoding" in self.headers):
                return self._deny(400, "ambiguous request framing")
            if self._is_collab_path():
                return self._collab()
            if self._path_only() == _FLEET_CONFIG_PATH + "/applied":
                return self._fleet_config_applied()
            if self._path_only() == "/api/v1/intelligence/webhooks/github":
                return self._intelligence_webhook()
            if self._path_only() == _FLEET_CONFIG_PATH + "/telemetry":
                return self._fleet_resource_telemetry()
            if self._path_only() in _DEPLOYMENT_EVENTS_PATHS:
                return self._deployment_event()
            # The ingest route is reachable by the ingest role; every other
            # POST requires the operator role (Spec §104).
            path = self.path.split("?")[0]
            # Decide on the DECODED resource: routing unquotes each segment, so a raw
            # prefix test is bypassed by e.g. /api/v1/%72eview-gate/...
            segs = path.split("/")
            res = unquote(segs[3]) if len(segs) > 3 and segs[:3] == ["", "api", "v1"] else ""
            if (res.startswith("reviews") or res in {"attention", "architecture", "scheduler"}
                    or (res in {"ci", "review-gate"} and len(segs) > 4) or _SUMMARY_FIX.match(path)
                    or _TERMINAL_ATTACH.match(path) or _STEER_ACK.match(path) or _is_rereview(path)):
                origin = self.headers.get("Origin")
                if origin and (urlsplit(origin).netloc != self.headers.get("Host")
                               or urlsplit(origin).scheme not in {"http", "https"}):
                    return self._deny(403, "cross-origin integration mutation refused")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                    return self._deny(415, "integration mutations require application/json")
            if path.split("/")[3:4] and path.split("/")[3] in {
                    "artifacts", "artifact-reports", "artifact-jobs", "artifact-reviews", "publications"}:
                origin = self.headers.get("Origin")
                if origin and (urlsplit(origin).netloc != self.headers.get("Host")
                               or urlsplit(origin).scheme not in {"http", "https"}):
                    return self._deny(403, "cross-origin artifact mutation refused")
            min_role = ("ingest" if path == "/api/v1/ingest" or _STEER_ACK.match(path) or _is_rereview(path)
                        or _HOST_REPORT.match(path)
                       else "operator")
            if _is_rereview(path) and self._role() == "reviewer":
                # _require would refuse it too ("read-only"); say why. The review gate dispatches its own re-reviews
                # (review_jobs); this route only records an ask for AutoSpec, and a gate worker never makes one.
                return self._deny(403, "a review gate worker may not request a re-review: the gate re-reviews on its own")
            err = self._require(min_role)
            if err:
                return err
            if self.path.startswith("/api/v1/"):
                return self._route_api()
            return self._send_json(404, {"error": "not found"})

        def _intelligence_webhook(self):
            # HMAC-authenticated (no API key); 404 while unconfigured. Body is bounded before it is read.
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._deny(400, "bad length")
            if length < 0 or length > 1_000_000:
                return self._send_json(413, {"error": "body too large"})
            status, body = server.intelligence.post_webhook(self.headers, self.rfile.read(length))
            return self._send_json(status, body)

        def do_PATCH(self):
            # PATCH/DELETE are enabled ONLY for the assistant resource family.
            # Every other /api/v1 resource keeps its prior 501 behavior: their
            # _api_post branches do not gate on self.command, so routing an
            # arbitrary PATCH/DELETE there could trigger an unintended mutation
            # (e.g. DELETE /api/v1/handoffs must not create a handoff).
            return self._timed_request(self._mutate_verb)

        def do_DELETE(self):
            return self._timed_request(self._mutate_verb)

        _ASSISTANT_MUTABLE_RESOURCES = {"assistants", "assistant-runs",
                                        "assistant-approvals", "assistant-memory",
                                        "assistant-routines", "assistant-connectors",
                                        "assistant-delegations"}

        def _mutate_verb(self):
            if operator:
                return self._operator()
            if public:
                return self._public()
            if self._is_federation_path():
                return self._federation()
            if self._is_collab_path():
                return self._collab()
            if (len(self.headers.get_all("Content-Length") or []) > 1
                    or "Transfer-Encoding" in self.headers):
                return self._deny(400, "ambiguous request framing")
            if self._path_only() == _FLEET_CONFIG_PATH + "/applied":
                return self._fleet_config_applied()
            path = self.path.split("?")[0]
            segs = path.split("/")
            resource = unquote(segs[3]) if (len(segs) > 3 and segs[:3] == ["", "api", "v1"]
                                            and path.startswith("/api/v1/")) else ""
            if resource not in self._ASSISTANT_MUTABLE_RESOURCES:
                # preserve the pre-existing 501 for PATCH/DELETE elsewhere
                return self._send_json(501, {"error": "not implemented"})
            err = self._require("operator")
            if err:
                return err
            return self._route_api()

        # ------------------------------------------------ fleet config (hosts)
        def _path_only(self):
            return self.path.split("?")[0].rstrip("/")

        def _fleet_host(self, cfg):
            """Host id owning the X-Host-Token in ``cfg``, or None. Never reveals why.
            Callers pass ONE snapshot per request, so a reload between the token
            check and the slice cannot hand out (or fail on) a different file."""
            token = self.headers.get("X-Host-Token", "")
            return cfg.host_for_token(token) if token else None

        def _fleet_config_get(self):
            """Host endpoints bypass the X-API-Key gate on purpose: the per-host
            token is their credential, and an operator key grants no host slice."""
            store = server.fleet_config
            if store is None:
                return self._deny(404, "not found")
            cfg = store.current()
            host = self._fleet_host(cfg)
            if host is None:
                return self._deny(401, "unauthorized")
            return self._json({"revision": cfg.revision, "values": cfg.slice_for(host)})

        def _fleet_config_applied(self):
            store = server.fleet_config
            if store is None:
                return self._deny(404, "not found")
            host = self._fleet_host(store.current())
            if host is None:
                return self._deny(401, "unauthorized")
            try:
                body = self._read_json()
            except ValueError as exc:
                return self._deny(400, str(exc))
            if (not isinstance(body, dict) or body.get("host_id") != host
                    or not isinstance(body.get("revision"), str)
                    or not body["revision"] or len(body["revision"]) > 64):
                return self._deny(401, "unauthorized")
            store.record_applied(host, body["revision"])
            return self._json({"ok": True})

        def _fleet_resource_telemetry(self):
            if server.fleet_config is None:
                return self._deny(404, "not found")
            host = self._fleet_host(server.fleet_config.current())
            if host is None:
                return self._deny(401, "unauthorized")
            try:
                body = self._read_json()
                if not isinstance(body, dict):
                    return self._deny(400, "resource snapshot must be an object")
                return self._json(server.scheduler.observe_host(host, body))
            except (TypeError, ValueError):
                return self._deny(400, "invalid resource observation")
            except RuntimeError:
                return self._deny(503, "resource persistence unavailable")

        def _deployment_host(self):
            """Fleet host id owning the request's host token, or None (never says why).

            The deploy event emitter sends ``Authorization: Bearer <host token>``; the
            fleet's own ``X-Host-Token`` header is accepted too, but never both at once.
            The token is checked by ``host_for_token`` (sha256, constant-time compare)
            and grants this one route only: ``_role`` reads X-API-Key alone.
            """
            auth = self.headers.get_all("Authorization") or []
            direct = self.headers.get_all("X-Host-Token") or []
            token = None
            if len(auth) == 1 and not direct:
                scheme, _, value = auth[0].strip().partition(" ")
                value = value.strip()
                if scheme.lower() == "bearer" and value and not any(c.isspace() for c in value):
                    token = value
            elif len(direct) == 1 and not auth:
                token = direct[0]
            return server.fleet_config.current().host_for_token(token) if token else None

        def _deployment_event(self):
            """POST one deployment event. Auth and size are decided before the body is read."""
            if server.fleet_config is None:
                return self._deny(404, "not found")
            host = self._deployment_host()
            if host is None:
                return self._deny(401, "unauthorized")
            from .deployments_monitor import MAX_REQUEST_BYTES
            length = _content_length(self.headers.get("Content-Length"))
            if not length:
                return self._deny(400, "a deployment event body is required")
            if length > MAX_REQUEST_BYTES:
                return self._deny(413, f"deployment event exceeds {MAX_REQUEST_BYTES} bytes")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                return self._deny(415, "deployment events require application/json")
            try:
                body = self._read_json(MAX_REQUEST_BYTES)
            except RecursionError:
                # json.loads raises this (not ValueError) on deeply nested input.
                return self._deny(400, "invalid JSON body: nested too deeply")
            except ValueError as exc:
                return self._deny(400, str(exc))
            from .deployment_definitions import (
                DeploymentAuthorizationError,
                DeploymentDefinitionsUnavailable,
            )
            # With definitions loaded a host reports only its own components; refused events
            # are never ingested (not stored, not persisted). A malformed body falls through
            # to ingest's own 400.
            if isinstance(body, dict) and isinstance(body.get("component"), str):
                try:
                    server.deployment_definitions.authorize(body["component"], host)
                except DeploymentAuthorizationError as exc:
                    return self._deny(403, str(exc))
                except DeploymentDefinitionsUnavailable as exc:
                    return self._deny(503, str(exc))
            try:
                server.repo.deployments.ingest(body, reported_by=host)
            except ValueError as exc:
                return self._deny(400, str(exc))
            return self._json({"ok": True})

        # --------------------------------------------------- collaboration (HCN)
        def _is_collab_path(self):
            path = self.path.split("?")[0]
            return path == "/api/v1/collab" or path.startswith("/api/v1/collab/")

        def _operator(self):
            """Tailnet-only listener for the local owner: collaboration API only, no sign-in.

            Open by design (the tailnet is the trust boundary, as for the dashboard), so it
            refuses DNS-rebinding (Host allowlist), cross-site writes (Origin check and
            application/json in _collab) and every path outside /api/v1/collab."""
            cl = self.headers.get_all("Content-Length") or []
            if (len(cl) > 1 or "Transfer-Encoding" in self.headers
                    or (self.command == "GET" and cl and cl[0].strip() not in ("", "0"))):
                return self._deny(400, "ambiguous request framing")
            if (self.headers.get("Host", "").lower() not in server.operator_hosts
                    or not self._is_collab_path()):
                return self._deny(404, "not found")
            return self._collab(operator=True)

        def _collab_proxy(self):
            """Dashboard -> collab service operator listener (same user, no sign-in)."""
            err = self._require("operator")
            if err:
                return err
            # This path acts as the owner without any credential, so it needs the same browser
            # defences as the operator listener: no DNS-rebound names, no cross-site writes.
            if not _dashboard_host_ok(self.headers.get("Host", ""), server.collab_dashboard_hosts):
                return self._deny(404, "not found")
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if origin and (urlsplit(origin).netloc != self.headers.get("Host")
                               or urlsplit(origin).scheme not in {"http", "https"}):
                    return self._deny(403, "cross-origin mutation refused")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                    return self._deny(415, "mutations require application/json")
            from .collab import MAX_BODY
            length = _content_length(self.headers.get("Content-Length"))
            if self.command == "POST" and (length is None or length > MAX_BODY):
                return self._deny(413, "request body too large")
            data = self.rfile.read(length) if self.command == "POST" and length else None
            req = urllib.request.Request(
                server.collab_operator_url + self.path, data=data, method=self.command,
                headers={"Content-Type": "application/json"} if data is not None or self.command == "POST" else {})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:   # noqa: S310 (operator-configured URL)
                    status, payload = resp.status, resp.read(2_000_000)
            except urllib.error.HTTPError as exc:
                status, payload = exc.code, exc.read(2_000_000)
            except Exception:
                return self._json({"error": "collaboration service unavailable"}, 502)
            if status == 200 and self.command == "GET" and urlsplit(self.path).path.endswith(("/flow", "/state")):
                payload = _fabric_enriched_flow(server.repo, payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _collab(self, operator=False):
            """HCN routes bypass the legacy X-API-Key gate on purpose: legacy keys are
            shared secrets with no identity, so they grant nothing here. Authentication
            is per-user / per-agent (collab.authenticate)."""
            service = server.collab
            if service is None:
                if server.collab_operator_url and not operator and not public:
                    return self._collab_proxy()
                return self._deny(404, "not found")
            if self._rate_limit(self._proxied_client()):
                return None
            # On a shared plain-HTTP listener, refuse collaboration unless the request
            # came through the TLS-terminating proxy (`tailscale serve` sets this header).
            if (server.collab_require_https and not operator
                    and self.headers.get("X-Forwarded-Proto", "").lower() != "https"):
                return self._deny(400, "collaboration is only served over https")
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if origin and (urlsplit(origin).netloc != self.headers.get("Host")
                               or urlsplit(origin).scheme not in {"http", "https"}):
                    return self._deny(403, "cross-origin mutation refused")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                    return self._deny(415, "mutations require application/json")
            body = {}
            if self.command == "POST":
                from .collab import MAX_BODY
                length = _content_length(self.headers.get("Content-Length"))
                if length is None or length > MAX_BODY:
                    return self._deny(413, "request body too large")
                if length:
                    try:
                        body = json.loads(self.rfile.read(length))
                    except (ValueError, RecursionError):
                        return self._json({"error": "invalid JSON body"}, 400)
                    if not isinstance(body, dict):
                        return self._json({"error": "JSON object required"}, 400)
            split = urlsplit(self.path)
            parts = [unquote(seg) for seg in split.path.split("/")[4:] if seg]
            query = {k: v[0] for k, v in parse_qs(split.query).items()}
            if operator and self.command == "GET" and parts == ["config"]:
                return self._json({**service.public_config, "operator": {
                    "user_id": server.operator_user, "email": server.operator_email}}, 200)
            try:
                result = service.dispatch(self.command, parts, query, body,
                                          self.headers.get("Authorization", ""),
                                          self.headers.get("X-Herdr-Device"),
                                          operator=server.operator_user if operator else None,
                                          client=self._proxied_client())
            except Exception:
                return self._json({"error": "internal error"}, 500)  # never echo internals
            if isinstance(result, tuple):
                return self._json(result[1], result[0])
            return self._collab_sse(service, result)

        def _collab_sse(self, service, stream):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.connection.settimeout(30)
            except OSError:
                pass
            last = time.monotonic()
            frames = service.pump(stream, max_seconds=_MAX_SSE_SECONDS)
            try:
                for frame in frames:
                    if frame is None:
                        if time.monotonic() - last < 15:
                            continue
                        self.wfile.write(b": keep-alive\n\n")
                    else:
                        self.wfile.write(_raw_frame(frame))
                    self.wfile.flush()
                    last = time.monotonic()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                pass
            finally:
                frames.close()
            return None

        def _proxied_client(self):
            """Behind `tailscale serve` every peer is 127.0.0.1; key the limiter on the
            forwarded address, but only trust it from a loopback peer (the proxy)."""
            peer = self.client_address[0]
            fwd = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
            # The public listener is reachable only from the host's own proxy (possibly
            # across the Docker bridge), so a private peer is trusted there too.
            try:
                trusted = _is_loopback(peer) or (public and ipaddress.ip_address(peer).is_private)
            except ValueError:
                trusted = False
            return fwd[:64] if fwd and trusted else peer

        def _rate_limit(self, key=None):
            path = self.path.split("?")[0]
            who = key or self._proxied_client()       # T16 M12: the same client identity on every route
            if self.command == "GET" and path.rstrip("/").endswith("/events/stream"):
                # SSE is long-lived, so it is not counted like request bursts, but how often a
                # client may open one is still bounded (it costs a token check each time).
                limiter = server._stream_limiter
            elif self.command == "POST" and path == "/api/v1/ingest":
                limiter = server._ingest_limiter
            else:
                limiter = server._limiter
            if limiter.allow(who):
                return None
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", str(limiter.window))
            # The rejected POST body is unread. Close rather than interpreting
            # its bytes as another request on this HTTP/1.1 connection.
            self.send_header("Connection", "close")
            self.close_connection = True
            payload = json.dumps({"error": "too many requests"}).encode()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return True

        def _route_api(self):
            path = self.path.split("?")[0]
            # URL-decode each path segment: entity ids like ``w9:p5`` arrive
            # percent-encoded (``w9%3Ap5``) from the structured client
            # (encodeURIComponent), so decode before matching. ``unquote``
            # (not unquote_plus) keeps ``+`` literal, as path segments should.
            parts = [unquote(seg) for seg in path.split("/")]
            resource = parts[3]
            if self._rate_limit():
                return None
            try:
                from .artifact_report_api import route_report
                if route_report(self, server, parts, resource):
                    return None
                if self.command == "GET":
                    return self._api_get(parts, resource)
                return self._api_post(parts, resource)
            except KeyError:
                return self._send_json(404, {"error": "not found"})
            except EnvelopeConflict as exc:
                body = {"error": str(exc), "reason": exc.reason}
                if exc.last_sequence is not None:
                    body["last_sequence"] = exc.last_sequence
                return self._send_json(409, body)
            except SequenceCollision:
                return self._send_json(409, {"error": "sequence conflict", "reason": "conflict"})
            except PermissionError:
                # "reason" lets an emitter tell a lineage refusal from an auth failure
                # (which has no reason); the body is identical whether or not the
                # object exists.
                return self._send_json(403, {"error": "not permitted to modify this lineage object",
                                             "reason": "lineage_forbidden"})
            except ValueError as exc:
                if getattr(exc, "code", None) == "not_found":
                    return self._send_json(404, {"error": "not found"})
                message = "Invalid GitHub query." if resource == "github" else str(exc)
                return self._send_json(400, {"error": message})
            except ArtifactConflict as exc:
                return self._send_json(409, {"error": str(exc),
                                             "code": "artifact_version_conflict",
                                             "artifact_id": exc.artifact_id,
                                             "current_version_id": exc.current_version_id})
            except ArtifactError as exc:
                return self._send_json(404 if str(exc).startswith("no artifact") else 400,
                                       {"error": str(exc)})
            except RuntimeError as exc:
                self._request_cause = type(exc).__name__
                if resource == "github":
                    from ..github import GitHubError
                    if isinstance(exc, GitHubError):
                        status = {"not_found": 404, "permission": 403, "rate_limited": 429}.get(exc.reason, 503)
                        return self._send_json(status, {"error": str(exc), "reason": exc.reason})
                if resource == "reviews":
                    return self._send_json(503, {"error": "Review persistence is unavailable; no launch or decision was acknowledged."})
                return self._send_json(503, {"error": "Integration service is unavailable."})
            except Exception as exc:  # never drop a connection: surface 500
                self._request_cause = type(exc).__name__
                if resource == "ingest":
                    # Storage/driver errors can name tables, hosts or other events.
                    logging.getLogger(__name__).exception("ingest failed")
                    return self._send_json(500, {"error": "ingest failed"})
                message = "GitHub service is unavailable." if resource == "github" else str(exc)
                return self._send_json(500, {"error": message})

        def _review_gate_get(self, parts):
            if parts[-1] == "" and len(parts) > 5:
                parts = parts[:-1]  # one trailing slash is the same route
            if server.gate_service is None or len(parts) != 5:
                return self._json({"error": "not found"}, 404)
            if parts[4] == "prs":
                return self._json([_gate_pr(snap) for snap in server.gate_store.snapshots()])
            if parts[4] == "queue":
                jobs = server.gate_queue.snapshot()
                if self._role() != "operator":
                    # Who holds a claim is operator information (the claim token
                    # is what protects a claim; this only narrows the view).
                    jobs = [{k: v for k, v in j.items() if k != "claimed_by"} for j in jobs]
                return self._json({"depth": server.gate_queue.depth(),
                                   "jobs": jobs,
                                   "gate": server.gate_service.status()})
            if parts[4] == "notices":
                from ..review_gate.notifier import recent_notices
                return self._json(recent_notices(server.repo))
            if parts[4] == "main":
                return self._json({"repos": server.gate_main.status()})
            return self._json({"error": "not found"}, 404)

        def _review_gate_resume(self, parts):
            # /api/v1/review-gate/prs/<github.com>/<owner>/<name>/<number>/resume
            # (the canonical repo's slashes stay literal; %2F inside a segment works too)
            self._read_json()  # always consume the body: keep-alive must not parse it as a request
            if (server.gate_service is None or len(parts) < 8 or parts[4] != "prs"
                    or parts[-1] != "resume"):
                return self._json({"error": "not found"}, 404)
            from ..review_gate.models import PrKey
            from .project_links import canonical_repository
            # Same canonical form as the enrollment config (lowercase owner/name).
            repo = canonical_repository("/".join(parts[5:-2])) or ""
            try:
                key = PrKey.parse(f"{repo}#{parts[-2]}")
            except ValueError:
                return self._json({"error": "not found"}, 404)
            store = server.gate_store
            if key not in store.known_keys():
                return self._json({"error": "not found"}, 404)
            try:
                resumed = store.resume(key)
            except Exception:  # noqa: BLE001 - the durable write failed; nothing changed
                return self._json({"error": "Review gate state could not be persisted; "
                                            "nothing was resumed."}, 503)
            if not resumed:
                return self._json({"error": "pull request is not escalated"}, 409)
            return self._json(_gate_pr(store.snapshot(key)))

        def _architecture_get(self, parts):
            """/api/v1/architecture/... — read-only, bounded queries over immutable snapshots."""
            svc = server.architecture
            if not svc.enabled:
                return self._json({"error": "not found"}, 404)
            q = lambda k: _q(self.path, k) or None  # noqa: E731
            try:
                rest = parts[4:]
                if rest == ["snapshots"]:
                    return self._json(svc.list_snapshots(q("repository_id"), q("commit_sha"), q("host_id"),
                                                         q("cursor"), q("limit")))
                if rest == ["analyses"]:
                    return self._json(svc.analyses(_qint(self.path, "limit", 50)))
                if len(rest) == 2 and rest[0] == "analyses":
                    return self._json(svc.analyses(analysis_id=rest[1]))
                if len(rest) == 3 and rest[0] == "sessions" and rest[2] == "changes":
                    return self._json(svc.session_changes(rest[1], _qint(self.path, "limit", 200)))
                if rest == ["timeline"]:
                    return self._json(svc.timeline(q("repository_id") or "", _qint(self.path, "limit", 200)))
                if rest == ["cross-repo"]:
                    qs = parse_qs(urlsplit(self.path).query)
                    maps = []
                    for item in qs.get("map", [])[:50]:
                        bits = item.split("|")
                        if len(bits) != 3:
                            raise ValueError("map must be importer|package|provider")
                        maps.append(dict(zip(("importer", "package", "provider"), bits)))
                    return self._json(svc.cross_repo(qs.get("snapshot", [])[:20], maps))
                if rest == ["diff"]:
                    if not (q("base") and q("head")):
                        raise ValueError("base and head are required")
                    return self._json(svc.diff(q("base"), q("head"), q("cursor"), q("limit")))
                if len(rest) >= 2 and rest[0] == "snapshots":
                    sid, tail = rest[1], rest[2:]
                    if not tail:
                        return self._json(svc.get_snapshot(sid))
                    if tail == ["children"]:
                        return self._json(svc.children(sid, q("parent"), q("cursor"), q("limit")))
                    if tail == ["base"]:
                        return self._json(svc.pr_base(sid))
                    if tail == ["overlays"]:
                        return self._json(svc.overlays(sid))
                    if tail == ["correlations"]:
                        return self._json(svc.correlations(sid, q("node"), q("cursor"), q("limit")))
                    if tail == ["nodes"]:
                        return self._json(svc.search(sid, q("q") or "", q("kind"), q("cursor"), q("limit")))
                    if len(tail) == 2 and tail[0] == "nodes":
                        return self._json(svc.node(sid, tail[1]))
                    if len(tail) == 3 and tail[0] == "nodes" and tail[2] == "rooms":
                        if tail[1] not in svc._load(sid).nodes:
                            raise KeyError(tail[1])
                        return self._json(svc.node_rooms(sid, tail[1], reveal=self._role() == "operator"))
                    if len(tail) == 3 and tail[0] == "nodes" and tail[2] == "edges":
                        return self._json(svc.expand(sid, tail[1], q("direction") or "both", q("kind"),
                                                     q("cursor"), q("limit")))
            except KeyError:
                return self._json({"error": "not found"}, 404)
            except Exception as exc:
                from ..architecture.service import ArchitectureError
                if isinstance(exc, ArchitectureError):
                    return self._json({"error": str(exc)}, exc.status)
                raise
            return self._json({"error": "not found"}, 404)

        def _architecture_post(self, parts):
            svc = server.architecture
            if not svc.enabled:
                return self._json({"error": "not found"}, 404)
            from ..architecture.service import ArchitectureError
            # An agent key bound in control_plane.architecture.agent_hosts acts only for its hosts.
            hosts = (server.architecture_agent_hosts.get(self._principal()["id"])
                     if self._role() == "agent" else None)
            if self._role() == "agent" and hosts is None and server.architecture_require_host_binding:
                # Body left unread: _deny closes the connection so it cannot be parsed as a next request.
                return self._deny(403, "this API key is not bound to any host (control_plane.architecture.agent_hosts)")
            try:
                body = self._read_json(_MAX_ARCHITECTURE_BODY) or {}
                if parts[4:] == ["snapshots"]:
                    out = svc.publish(body, allowed_hosts=hosts)
                    return self._json(out, 200 if out["duplicate"] else 201)
                if parts[4:] == ["analyses"]:
                    return self._json(svc.record_analysis(body, allowed_hosts=hosts), 201)
                if len(parts) == 9 and parts[4] == "snapshots" and parts[6] == "nodes" and parts[8] == "explain":
                    return self._json(svc.explain(parts[5], parts[7]))
                if parts[4:] == ["analyses", "claim"]:
                    return self._json(svc.claim_analysis(body, allowed_hosts=hosts))
                if len(parts) == 7 and parts[4] == "analyses" and parts[6] == "cancel":
                    if self._role() != "operator":
                        return self._json({"error": "operator role required"}, 403)
                    return self._json(svc.cancel_analysis(parts[5]))
                if parts[4:] == ["analyses", "request"]:
                    if self._role() not in ("operator",):
                        return self._json({"error": "operator role required"}, 403)
                    out = svc.request_analysis(body)
                    return self._json(out, 200 if out["deduplicated"] else 201)
            except ArchitectureError as exc:
                return self._json({"error": str(exc)}, exc.status)
            except KeyError:
                return self._json({"error": "not found"}, 404)
            return self._json({"error": "not found"}, 404)

        def _reviewer_body(self, cap, fields):
            """Read a reviewer route's JSON object body: ``(body, None)`` or ``(None, (status, error))``.

            Fixed error strings only: nothing from the request is ever echoed back.
            """
            length = _content_length(self.headers.get("Content-Length"))
            if not length:
                return None, (400, "invalid request body")
            if length > cap:
                return None, (413, "request body too large")
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, RecursionError):
                return None, (400, "invalid request body")
            if not isinstance(body, dict) or set(body) != fields:
                return None, (400, "invalid request body")
            for name in sorted(fields & {"worker_id", "reviewer_model"}):
                value = body[name]
                if not isinstance(value, str) or not _REVIEWER_ID.fullmatch(value):
                    return None, (400, f"invalid {name}")
            if "output" in fields and not isinstance(body["output"], str):
                return None, (400, "invalid output")
            if "claim_token" in fields and not (
                isinstance(body["claim_token"], str)
                and _REVIEWER_CLAIM_TOKEN.fullmatch(body["claim_token"])
            ):
                return None, (400, "invalid claim_token")
            reason = body.get("reason", "")
            if not isinstance(reason, str) or len(reason) > _REVIEWER_REASON_MAX:
                return None, (400, "invalid reason")
            return body, None

        def _review_gate_worker(self, parts):
            """POST review-gate/claim and review-gate/jobs/<job_id>/{heartbeat,result,failure}.

            Reviewer role only (``_require`` enforced it; re-checked here as defence
            in depth). Every refusal goes through ``_deny`` (Connection: close), so an
            unread body can never be parsed as a next request.
            """
            if self._role() != "reviewer":
                return self._deny(403, "reviewer role required")
            route = _reviewer_route(parts)
            if route is None:
                return self._deny(404, "not found")
            coord, cfg = server.gate_coordinator, server.gate_reviewer_config
            if coord is None or cfg is None:
                return self._deny(503, "review gate disabled")
            action, job_id = route
            if job_id is not None and not _REVIEWER_JOB_ID.fullmatch(job_id):
                return self._deny(400, "invalid job id")
            fields = {
                "claim": {"worker_id", "reviewer_model"},
                "heartbeat": {"worker_id", "claim_token"},
                "result": {"worker_id", "output", "claim_token"},
                "failure": {"worker_id", "reason", "claim_token"},
            }[action]
            # JSON escaping can expand each output byte up to 6x ("\u0001"); the
            # coordinator's byte-exact max_output_bytes check is the real limit.
            cap = (
                6 * cfg.max_output_bytes + 4 * 1024 if action == "result" else _REVIEWER_SMALL_BODY
            )
            body, error = self._reviewer_body(cap, fields)
            if error is not None:
                return self._deny(*error)
            worker = body["worker_id"]
            try:
                if action == "claim":
                    got = coord.claim(worker, body["reviewer_model"])
                    job = None if got is None else {f: getattr(got, f) for f in _CLAIM_FIELDS}
                    return self._json({"job": job})
                token = body["claim_token"]
                if action == "heartbeat":
                    ok = coord.heartbeat(job_id, worker, claim_token=token) is True
                    return self._json({"ok": ok})
                if action == "result":
                    outcome = coord.submit(job_id, worker, body["output"], claim_token=token)
                    allowed = _RESULT_OUTCOMES
                else:
                    outcome = coord.fail(job_id, worker, body["reason"], claim_token=token)
                    allowed = _FAILURE_OUTCOMES
                if not isinstance(outcome, str) or outcome not in allowed:
                    raise TypeError("unexpected coordinator outcome")
                return self._json({"outcome": outcome})
            except Exception as exc:  # noqa: BLE001 - fixed 500, class name only in the log
                _log.error("review gate %s failed: %s", action, type(exc).__name__)
                return self._json({"error": "internal error"}, 500)

        def _api_get(self, parts, resource):
            r = server.repo
            if resource == "fabric" and len(parts) == 5 and parts[4] == "services":
                return self._fabric_services()
            if resource == "fabric" and len(parts) == 5 and parts[4] == "names":
                return self._fabric_names()
            if (resource == "artifacts" and len(parts) >= 5
                    and not self._artifact_allowed(parts[4], "read")):
                return self._json({"error": "not found"}, 404)
            if resource == "handoffs":
                if len(parts) == 5 and parts[4] == "tree":
                    return self._json(server.handoffs.tree())
                if len(parts) == 5:
                    row = server.handoffs.get(parts[4])
                    return self._json(row) if row else self._json({"error": "not found"}, 404)
                return self._json({"handoffs": server.handoffs.list(state=_q(self.path, "state"))})
            if resource == "scheduler":
                if len(parts) == 4:
                    return self._json(server.scheduler.overview())
                if parts[4:] == ["queue"]:
                    return self._json({"queue": server.scheduler.overview()["queue"]})
                return self._json({"error": "not found"}, 404)
            if resource == "health":
                return self._json(server._health())
            if resource == "architecture":
                return self._architecture_get(parts)
            if resource == "intelligence":
                status, body = server.intelligence.get(
                    parts[4:], {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()},
                    self._principal(), self._role())
                return self._json(body, status)
            if resource == "mission-activity" and len(parts) == 4:
                return self._json({"activities": r.mission_activity.list_summaries(),
                                   "server_time": r._stamp()})
            if resource == "mission-outcomes" and len(parts) == 4:
                from .mission_outcome import outcome_summaries
                try:
                    server.mission_outcomes.sweep()
                except Exception:  # noqa: BLE001
                    pass
                return self._json({"outcomes": outcome_summaries(r)})
            if resource == "missions":
                try:
                    server.mission_outcomes.sweep()   # throttled; closes missions that went silent
                except Exception:  # noqa: BLE001 - a sweep must never fail a read
                    pass
                if len(parts) == 6 and parts[5] == "activity":
                    return self._json({"activity": r.mission_activity.get(parts[4])})
                if len(parts) == 6 and parts[5] == "story":
                    from .mission_story import build
                    story = build(r, parts[4], outcomes=server.mission_outcomes, inbox=server.attention,
                                  config=server.missions_config)
                    return self._json(story) if story is not None else self._json({"error": "not found"}, 404)
                if len(parts) == 6 and parts[5] in ("tests", "builds", "reviews", "pulls"):
                    if r.get_mission(parts[4]) is None:
                        return self._json({"error": "not found"}, 404)
                    sub = parts[5]
                    if sub == "tests":
                        items = r.list_test_runs(mission_id=parts[4], kind="test")
                    elif sub == "builds":
                        items = r.list_test_runs(mission_id=parts[4], kind="build")
                    elif sub == "reviews":
                        items = r.list_reviews(mission_id=parts[4])
                    else:  # pulls
                        items = r.list_pull_requests(mission_id=parts[4])
                    # Delivery redaction scans string values for leaked secrets
                    # while preserving numeric counts, urls and status text.
                    return self._json({sub: [redact_delivery(item) for item in items]})
                if len(parts) > 5 and parts[5] == "timeline":
                    timeline = r.mission_timeline(parts[4])
                    return self._json({"timeline": _with_collab_tasks(server, r, parts[4], timeline)})
                if len(parts) > 4:
                    m = r.get_mission(parts[4])
                    return self._etag_json(m) if m else self._json(
                        {"error": "not found"}, 404)
                status = _q(self.path, "status")
                return self._etag_json({"missions": r.list_missions(status)})
            if resource == "related" and len(parts) == 4:
                from .relations import related
                kind, entity_id = _q(self.path, "kind"), _q(self.path, "id")
                try:
                    answer = related(r, kind, entity_id)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                return self._json(answer) if answer is not None else self._json({"error": "not found"}, 404)
            if resource == "attention" and len(parts) == 4:
                if server.attention is None:
                    return self._json({"error": "not found"}, 404)
                try:
                    return self._json(server.attention.snapshot(
                        state=_q(self.path, "state") or "open",
                        kind=_q(self.path, "kind") or None, severity=_q(self.path, "severity") or None))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
            if resource == "inspector":
                return self._inspector_global_get(parts)
            if resource == "sessions" and len(parts) > 5 and parts[5] == "inspector":
                return self._inspector_get(parts)
            if resource == "sessions" and len(parts) == 6 and parts[5] == "summary":
                if r.get_session(parts[4]) is None:
                    return self._json({"error": "agent not found"}, 404)
                body = server.agent_summaries.get(parts[4])
                if server.summary_report is not None and server.summary_report.enabled:
                    body = {**body, "full_report": server.summary_report.get(parts[4])}
                return self._json(body)
            if resource == "sessions":
                if len(parts) > 4:
                    s = r.get_session(parts[4])
                    if s:
                        s = self._agent_capabilities(s)
                    return self._etag_json(s) if s else self._json(
                        {"error": "not found"}, 404)
                status = _q(self.path, "status")
                return self._etag_json({"sessions": [self._agent_capabilities(s) for s in r.list_sessions(status)]})
            if resource == "services":
                if len(parts) > 4:
                    s = r.get_service(parts[4])
                    return self._etag_json(s) if s else self._json(
                        {"error": "not found"}, 404)
                services = r.list_services()
                host_filter = _q(self.path, "host_id")
                if host_filter:
                    services = [s for s in services if s.get("host_id") == host_filter]
                summary = {state: sum(s["lifecycle"]["state"] == state for s in services)
                           for state in ("current", "unavailable", "archived", "unknown")}
                summary["total"] = len(services)
                if _q(self.path, "include_archived") in {"false", "0"}:
                    services = [s for s in services if not s["lifecycle"]["archived"]]
                if _q(self.path, "fields") == "feed":
                    # The ownership feed needs identity and proof only; the full
                    # catalog projection is most of the bytes of a large inventory.
                    services = [{k: s.get(k) for k in _FEED_SERVICE_FIELDS} for s in services]
                return self._etag_json({"services": services, "summary": summary})
            if resource == "fleet" and parts[4:] == ["config", "status"]:
                if server.fleet_config is None:
                    return self._json({"error": "not found"}, 404)
                return self._json(server.fleet_config.status())
            if resource == "fleet" and len(parts) == 5 and parts[4] == "inventory":
                return self._json(r.fleet_inventory())
            if resource == "hosts":
                if len(parts) > 5 and parts[5] == "telemetry":
                    rng = _q(self.path, "range")
                    if rng is not None:  # unknown range: ValueError -> 400
                        return self._json({"samples": r.host_history(parts[4], rng), "range": rng})
                    # ?limit= (the page asks for 1): full samples are up to 16 KiB each.
                    limit = min(max(_qint(self.path, "limit", 100), 1), 500)
                    return self._json({"samples": r.list_telemetry(parts[4], limit)})
                if len(parts) == 5:
                    h = r.get_host(parts[4])
                    return self._etag_json(h) if h else self._json(
                        {"error": "not found"}, 404)
                return self._etag_json({"hosts": r.list_hosts()})
            if resource == "activity":
                evs = [_redact_event(e) for e in r.recent_events(
                    limit=_qint(self.path, "limit", 50))]
                return self._json({"events": evs})
            if resource == "analytics":
                kind = parts[4]
                if kind == "missions":
                    return self._json(r.mission_analytics())
                if kind == "models":
                    return self._json(r.model_analytics())
                if kind == "hosts":
                    return self._json(r.host_analytics())
                if kind == "tokens":
                    return self._json(r.token_analytics(
                        project=_q(self.path, "project"), since=_q(self.path, "since")))
                if kind == "stages":
                    return self._json(r.stage_analytics())
                if kind == "usage":
                    return self._json(r.usage_analytics(
                        mission_id=_q(self.path, "mission")))
                return self._json({"error": "unknown analytics kind"}, 404)
            if resource == "events":
                if len(parts) > 4 and parts[4] == "stream":
                    return self._sse()
                if len(parts) == 4:
                    # per-entity stream: /api/v1/events?entity_type=mission&entity_id=m1
                    etype = _q(self.path, "entity_type")
                    eid = _q(self.path, "entity_id")
                    if etype == "lineage":
                        return self._json({"error": "not found"}, 404)  # read via the scope-checked /api/v1/graph
                    if etype == "review_request":
                        return self._json({"error": "not found"}, 404)  # read through the review-quorum route
                    if etype and etype.startswith("collab"):
                        return self._json({"error": "not found"}, 404)  # HCN has its own authenticated API
                    if not etype or not eid:
                        return self._json(
                            {"error": "entity_type and entity_id required"}, 400)
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if "limit" in query or "before" in query:
                        try:
                            for key in ("limit", "before"):
                                if key in query and len(query[key]) != 1:
                                    raise ValueError(f"{key} must be supplied once")
                            raw_limit = query.get("limit", ["50"])[0]
                            if not re.fullmatch(r"[0-9]{1,3}", raw_limit):
                                raise ValueError("limit must be an integer between 1 and 200")
                            page = r.stream_page(etype, eid, limit=int(raw_limit),
                                                 before=query.get("before", [None])[0])
                        except ValueError as exc:
                            return self._json({"error": str(exc)},
                                              409 if str(exc) == "staleHistoryCursor" else 400)
                        return self._json({**page, "events": [_redact_event(event) for event in page["events"]]})
                    return self._json({"events": [_redact_event(event) for event in r.stream(etype, eid)]})
            if resource == "artifacts" and len(parts) > 5 and parts[5] == "download":
                data = r.get_artifact_data(parts[4])
                a = r.get_artifact(parts[4])
                if a is None and server.artifact_store is not None:
                    a = self._artifact_call('get_record', parts[4], principal_id=self._resource_principal())
                # payload lives in the object store, not the in-memory blob
                if data is None and a is not None and server.rustfs is not None \
                        and a.get("storage_key") and a.get("sha256"):
                    try:
                        data = server.rustfs.get(a["storage_key"], a["sha256"])
                    except Exception:
                        data = None
                if data is None or a is None:
                    return self._json({"error": "not found"}, 404)
                return self._bytes(data, a.get("mime_type", "application/octet-stream"))
            if resource == "artifacts" and len(parts) == 4:
                if server.artifact_store is not None:
                    artifacts = self._artifact_call(
                        "list_records", principal_id=self._resource_principal(), query=_q(self.path, "q"),
                        artifact_type=_q(self.path, "type"), status=_q(self.path, "status"),
                        limit=_qint(self.path, "limit", 200), offset=_qint(self.path, "offset", 0))
                    # Older uploads live in the durable event projection. Keep
                    # them discoverable while the versioned registry is adopted.
                    known = {a['artifact_id'] for a in artifacts}
                    legacy = r.list_artifacts(query=_q(self.path, 'q'),
                        artifact_type=_q(self.path, 'type'), status=_q(self.path, 'status'),
                        created_by=_q(self.path, 'created_by'),
                        limit=_qint(self.path, 'limit', 200), offset=_qint(self.path, 'offset', 0))
                    artifacts.extend(a for a in legacy if a['artifact_id'] not in known
                                     and not a.get('created_by'))
                else:
                    artifacts = r.list_artifacts(
                        query=_q(self.path, "q"), artifact_type=_q(self.path, "type"),
                        status=_q(self.path, "status"), created_by=_q(self.path, "created_by"),
                        limit=_qint(self.path, "limit", 200), offset=_qint(self.path, "offset", 0))
                if server.artifact_acl is not None:
                    artifacts = [a for a in artifacts if self._artifact_allowed(a['artifact_id'], 'read')]
                return self._json({"artifacts": artifacts})
            if resource == "artifacts" and len(parts) == 5:
                artifact = (self._artifact_call("get_record", parts[4], principal_id=self._resource_principal())
                            if server.artifact_store is not None else r.get_artifact(parts[4]))
                if artifact is None:
                    legacy = r.get_artifact(parts[4])
                    if legacy and not legacy.get('created_by'):
                        artifact = legacy
                if artifact and not self._artifact_allowed(parts[4], "read"):
                    artifact = None
                return self._json(artifact) if artifact else self._json({"error": "not found"}, 404)
            if resource == "artifacts" and server.artifact_store is not None:
                if len(parts) in {6, 7} and parts[5] == "versions" and not self._artifact_call(
                        "report_permission", parts[4], self._resource_principal(), "read"):
                    return self._json({"error": "not found"}, 404)
                if len(parts) == 6 and parts[5] == "versions":
                    method = "list_version_summaries" if _q(self.path, "summary") in {"true", "1"} else "list_versions"
                    return self._json({"artifact_id": parts[4],
                                       "versions": self._artifact_call(method, parts[4])})
                if len(parts) == 7 and parts[5] == "versions":
                    version = self._artifact_call("get_version", parts[6], parts[4])
                    return self._json(version) if version else self._json({"error": "not found"}, 404)
            if resource == "artifacts" and server.artifact_review_manager is not None:
                if len(parts) == 6 and parts[5] == "reviews":
                    return self._json({"artifact_id": parts[4],
                                       "reviews": server.artifact_review_manager.list_for_artifact(parts[4])})
            if resource == 'artifacts' and len(parts) == 6 and r.get_artifact(parts[4]):
                if parts[5] == 'versions' and server.artifact_store is None:
                    return self._json({'artifact_id': parts[4], 'versions': []})
                if parts[5] == 'reviews' and server.artifact_review_manager is None:
                    return self._json({'artifact_id': parts[4], 'reviews': []})
            if resource == "artifact-jobs" and server.artifact_job_manager is not None:
                if len(parts) == 5:
                    job = (self._artifact_call('get_job', parts[4]) if server.artifact_store is not None
                           else server.artifact_job_manager.get(parts[4]))
                    if job is None:
                        return self._json({'error': 'not found'}, 404)
                    if not self._artifact_allowed(job['artifact_id'], 'read'):
                        return self._json({'error': 'not found'}, 404)
                    server.artifact_job_manager.load([job])
                    if job.get("profile") == "report" and server.artifact_store is not None:
                        job = self._artifact_call("get_report_job", job["artifact_id"], parts[4])
                        if job is None:
                            return self._json({"error": "not found"}, 404)
                    return self._json(job)
            if resource == "artifact-reviews" and server.artifact_review_manager is not None:
                if len(parts) == 5:
                    review = server.artifact_review_manager.get(parts[4])
                    if review and not self._artifact_allowed(review['artifact_id'], 'read'):
                        return self._json({'error': 'not found'}, 404)
                    return self._json(review) if review else self._json({"error": "not found"}, 404)
            if resource == "publications" and server.publication_controller is not None:
                if len(parts) == 4:
                    publications = [p for p in server.publication_controller.list()
                                    if self._artifact_allowed(p['resource_id'], 'read')]
                    return self._json({"publications": publications})
                if len(parts) == 5:
                    publication = server.publication_controller.get(parts[4])
                    if publication and not self._artifact_allowed(publication['resource_id'], 'read'):
                        return self._json({'error': 'not found'}, 404)
                    return self._json(publication) if publication else self._json({"error": "not found"}, 404)
            if resource == "leases":
                return self._json({"leases": r.list_leases()})
            if resource == "llm":
                what = parts[4] if len(parts) > 4 else ""
                if what == "config":
                    return self._json(r.llm_config())
                if what == "keys":
                    return self._json({"keys": r.list_project_keys()})
                if what == "models":
                    return self._json({"models": list(r.list_custom_models().values())})
                if what in ("stats", "timeseries", "errors") and len(parts) == 5:
                    return self._llm_stats(what)
                return self._json({"error": "not found"}, 404)
            if resource == "graph":
                return self._graph_get(parts)
            if resource == "steering":
                return self._steering(parts)
            if resource == "ingest" and len(parts) == 5 and parts[4] == "sources":
                # T16 M4: emitter names, actor digests and sequences of other principals are operator
                # business; every other key sees only the sources it emitted itself.
                # ``?scope=all`` is the explicit full listing: operator-role keys only (403 otherwise). Without it an
                # operator key still sees everything and any other key only its own domains.
                is_operator = self._role() == "operator"
                scope = (parse_qs(urlsplit(self.path).query).get("scope") or [""])[0]
                if scope not in ("", "all", "own"):
                    return self._json({"error": "scope must be all or own"}, 400)
                if scope == "all" and not is_operator:
                    return self._json({"error": "forbidden"}, 403)
                own = None if (is_operator and scope != "own") else (self._principal().get("id") or "")
                return self._json({"sources": r.ledger.source_states(own), "server_time": r._stamp()})
            if resource == "ingest":
                return self._json({"endpoint": "POST /api/v1/ingest",
                                   "entities": sorted(INGEST_ENTITY_TYPES)})
            if resource == "github":
                what = parts[4] if len(parts) == 5 else ""
                if len(parts) > 5 or (len(parts) == 5 and what not in {"issues", "issue", "projects"}):
                    return self._json({"error": "not found"}, 404)
                query = _github_query(self.path, what)
                if what == "issue":
                    return self._json(server.github_monitor.issue_detail(
                        query["repository"], query["number"]))
                return self._github_list(what or "repositories", query)
            if resource == "ci":
                what = parts[4] if len(parts) > 4 else ""
                if what in {"summary", "tests"} and len(parts) == 5:
                    filters = _ci_observability_query(self.path, what)
                    if what == "summary":
                        return self._json(server.ci_monitor.summary(r, **filters))
                    view = server.ci_monitor.tests(r, **filters)
                    cached = server.ci_monitor.summary_snapshot(start=False)
                    view['scope']['stale'] |= bool(cached.get('stale'))
                    return self._json(view)
                if what == "performance" and len(parts) == 5:
                    from .ci_performance import annotate_performance
                    raw_days = _q(self.path, "days") or "14"
                    if raw_days not in ('7', '14'):
                        raise ValueError('CI performance days must be 7 or 14.')
                    days = int(raw_days)
                    snapshot = server.ci_monitor.snapshot(start=False)
                    view = r.ci_performance(repository=_q(self.path, "repository") or None,
                                            branch=_q(self.path, "branch") or None, days=days,
                                            inventory=snapshot.get('repos', []))
                    view['scope']['stale'] = view['scope']['stale'] or bool(snapshot.get('stale'))
                    try:
                        languages = server.github_monitor.repository_languages(start=False)
                    except Exception:
                        languages = {}
                    return self._json(annotate_performance(view, r, snapshot.get('repos', []),
                        languages if isinstance(languages, dict) else {}))
                if what == "history":
                    from .project_links import annotate_ci_history, cached_github_languages
                    return self._json(annotate_ci_history(server.ci_monitor.history(), r,
                        repository_languages=cached_github_languages(server.github_monitor)))
                if what == "reports":
                    from .project_links import annotate_ci_history, cached_github_languages
                    filters = _ci_reports_query(self.path)
                    view = {**r.ci_reports(**filters), "repos": server.ci_monitor.snapshot().get("repos", [])}
                    view = annotate_ci_history(view, r,
                        repository_languages=cached_github_languages(server.github_monitor))
                    return self._json(server.ci_monitor.report_redactor()(view))
                if what == "pipeline":
                    view = server.ci_monitor.detail(
                        _q(self.path, "repository"), _q(self.path, "number"),
                        **({"provider_url": _q(self.path, "provider_url")} if _q(self.path, "provider_url") is not None else {}))
                    view['diagnosis'] = r.ci_diagnosis(view.get('pipeline'),
                        redact=server.ci_monitor.report_redactor())
                    return self._json(view)
                if what == "log":
                    return self._json(server.ci_monitor.log(
                        _q(self.path, "repository"), _q(self.path, "number"), _q(self.path, "step"),
                        **({"provider_url": _q(self.path, "provider_url")} if _q(self.path, "provider_url") is not None else {})))
                if what == "fix" and server.ci_fix is not None and len(parts) in {5, 6}:
                    if len(parts) == 6 and parts[5] == "config":
                        return self._json(server.ci_fix.runner_config())
                    if len(parts) == 6:
                        return self._json(server.ci_fix.get(parts[5]))
                    return self._json(server.ci_fix.list(
                        run=_q(self.path, "run") or None, host=_q(self.path, "host") or None,
                        active=_q(self.path, "active") == "1"))
                if what:
                    return self._json({"error": "not found"}, 404)
                from .project_links import annotate_ci, cached_github_languages, mark_merged_pull_requests
                return self._json(mark_merged_pull_requests(annotate_ci(server.ci_monitor.snapshot(), r,
                    repository_languages=cached_github_languages(server.github_monitor)), server.gate_store))
            if resource == "reviews":
                from .reviews import ReviewService
                reviews = ReviewService(r)
                if len(parts) > 4:
                    record = reviews.get(parts[4])
                    return self._json(record) if record else self._json({"error": "not found"}, 404)
                rows = reviews.list_reviews()
                offset = max(0, _qint(self.path, "offset", 0))
                limit = min(500, max(1, _qint(self.path, "limit", 500)))
                summaries = [{key: value for key, value in row.items()
                              if key not in {"plan", "feedback", "receipt", "instance_id"}}
                             for row in rows[offset:offset + limit]]
                return self._json({"available": True, "reviews": summaries,
                                   "total": len(rows), "has_more": offset + limit < len(rows),
                                   "offset": offset, "checked_at": r._stamp()})
            if resource == "review-gate":
                return self._review_gate_get(parts)
            if resource in ("assistants", "assistant-runs", "assistant-approvals", "assistant-memory", "assistant-routines", "assistant-connectors", "assistant-delegations"):
                return self._assistant_get(parts, resource)
            if resource == "tests":
                return self._json(_tests_view())
            return self._json({"error": "not found"}, 404)

        def _api_post(self, parts, resource):
            r = server.repo
            if resource == "fabric" and len(parts) == 5 and parts[4] == "resolve-urls":
                return self._fabric_resolve_urls()
            if resource == "handoffs":
                body = self._read_json() or {}
                actor = self._principal()
                if len(parts) == 4:
                    row, replayed = server.handoffs.create(body, actor)
                    return self._json(row, 200 if replayed else 201)
                if len(parts) == 6 and parts[5] in {"claim", "accept", "reject", "complete",
                                                      "cancel", "retry", "start"}:
                    state = {"claim": "claimed", "accept": "accepted", "reject": "rejected",
                             "complete": "completed", "cancel": "cancelled", "retry": "queued",
                             "start": "in_progress"}[parts[5]]
                    if "actor" in body:
                        return self._json({"error": "actor is derived from authentication"}, 400)
                    return self._json(server.handoffs.transition(parts[4], state, actor))
            if resource == "inspector" and len(parts) == 5 and parts[4] == "preferences":
                return self._inspector_set_prefs(None)
            if (resource == "sessions" and len(parts) == 7 and parts[5] == "inspector"
                    and parts[6] == "preferences"):
                if r.get_session(parts[4]) is None and not r.inspector.has_session(parts[4]):
                    return self._json({"error": "session not found"}, 404)
                return self._inspector_set_prefs(parts[4])
            if resource == "architecture":
                return self._architecture_post(parts)
            if resource == "intelligence":
                payload = self._read_json() or {}
                key = self.headers.get("Idempotency-Key")
                if isinstance(payload, dict) and key and "idempotency_key" not in payload:
                    payload["idempotency_key"] = key
                status, body = server.intelligence.post(parts[4:], payload, self._principal(), self._role())
                return self._json(body, status)
            # artifact upload carries raw bytes, not JSON — handle first
            if resource == "artifacts" and len(parts) == 4:
                return self._upload_artifact(r, parts)
            if resource == "artifacts" and server.artifact_acl is not None and len(parts) == 6 and parts[5] == "acl":
                if not self._artifact_allowed(parts[4], 'admin'):
                    return self._artifact_forbidden()
                body = self._read_json() or {}
                permissions = set(body.get("permissions") or [])
                grant = server.artifact_acl.grant(
                    parts[4], str(body.get("principal_id") or ""), permissions,
                    expires_at=body.get("expires_at"))
                grant["granted_by"] = self._resource_principal()
                return self._json(grant, 201)
            if resource == "artifacts" and server.artifact_acl is not None and len(parts) == 7 and parts[5] == "acl" and parts[6] == "revoke":
                if not self._artifact_allowed(parts[4], 'admin'):
                    return self._artifact_forbidden()
                body = self._read_json() or {}
                server.artifact_acl.revoke(parts[4], str(body.get("principal_id") or ""))
                return self._json({"ok": True})
            if resource == "artifacts" and server.artifact_store is not None and len(parts) == 6 and parts[5] == "versions":
                if not self._artifact_allowed(parts[4], 'edit'):
                    return self._artifact_forbidden()
                if not self._artifact_call("report_permission", parts[4], self._resource_principal(), "edit"):
                    return self._deny(404, "not found")
                body = self._read_json() or {}
                if "created_by" in body:
                    return self._json({"error": "created_by is derived from authentication"}, 400)
                if body.get("report_descriptor") is not None and any(key in body for key in
                        {"validation", "job_id", "host_id", "agent_id", "session_id", "mission_id"}):
                    return self._json({"error": "report execution validation and context are worker-owned; use report checks for attestations"}, 400)
                body["created_by"] = self._resource_principal()
                body["provenance"] = {**(body.get("provenance") or {}), "principal_id": body["created_by"]}
                return self._json(self._artifact_call("commit_version", parts[4], **body), 201)
            if resource == "artifact-jobs" and server.artifact_job_manager is not None:
                if len(parts) == 4:
                    body = self._read_json() or {}
                    if body.get("profile") == "report" and server.artifact_store is None:
                        return self._json({"error": "versioned report registry is unavailable"}, 503)
                    if not self._artifact_allowed(body.get('artifact_id', ''), 'edit'):
                        return self._artifact_forbidden()
                    return self._json(server.artifact_job_manager.submit(
                        profile=body.get("profile"), artifact_id=body.get("artifact_id"),
                        base_version_id=body.get("base_version_id"), request=body.get("request") or {},
                        requested_by=self._resource_principal()), 201)
                if len(parts) == 6 and parts[5] == "cancel":
                    self._read_json()
                    try:
                        local = server.artifact_job_manager.get(parts[4])
                    except KeyError:
                        local = None
                    if local is not None and local.get("profile") == "report" and server.artifact_store is not None:
                        # Report jobs: the database row, not the manager, decides cancellation.
                        job = local
                    else:
                        job = (self._artifact_call('get_job', parts[4]) if server.artifact_store is not None
                               else server.artifact_job_manager.get(parts[4]))
                    if job is None:
                        return self._json({'error': 'not found'}, 404)
                    if not self._artifact_allowed(job['artifact_id'], 'edit'):
                        return self._artifact_forbidden()
                    server.artifact_job_manager.load([job])
                    if job.get("profile") == "report" and server.artifact_store is not None:
                        cancelled = self._artifact_call("cancel_report_job", job["artifact_id"], parts[4])
                        if cancelled is not None:
                            return self._json(cancelled)
                        persisted = self._artifact_call("get_report_job", job["artifact_id"], parts[4])
                        if persisted is None:
                            return self._json({"error": "not found"}, 404)
                        return self._json({"error": "report job is no longer queued; no cancellation recorded"}, 409)
                    return self._json(server.artifact_job_manager.cancel(parts[4]))
            if resource == "artifact-reviews" and server.artifact_review_manager is not None:
                if len(parts) == 4:
                    body = self._read_json() or {}
                    if not self._artifact_version_allowed(body.get('artifact_id', ''), body.get('version_id'), 'review'):
                        return self._artifact_forbidden()
                    return self._json(server.artifact_review_manager.request(
                        artifact_id=body.get("artifact_id"), version_id=body.get("version_id"),
                        requested_by=self._resource_principal()), 201)
                if len(parts) == 6 and parts[5] == "complete":
                    body = self._read_json() or {}
                    review = server.artifact_review_manager.get(parts[4])
                    if review is None:
                        return self._json({'error': 'not found'}, 404)
                    if not self._artifact_version_allowed(review['artifact_id'], review['version_id'], 'review'):
                        return self._artifact_forbidden()
                    return self._json(server.artifact_review_manager.complete(
                        parts[4], reviewer=self._resource_principal(), decision=body.get("decision"),
                        comments=body.get("comments") or []))
            if resource == "publications" and server.publication_controller is not None:
                if len(parts) == 5 and parts[4] == "requests":
                    body = self._read_json() or {}
                    if not self._artifact_version_allowed(body.get('resource_id', ''), body.get('version_id'), 'share'):
                        return self._artifact_forbidden()
                    return self._json(server.publication_controller.request(
                        resource_id=body.get("resource_id"), version_id=body.get("version_id"),
                        requested_by=self._resource_principal(), audience=body.get("audience"),
                        expires_at=float(body.get("expires_at", 0))), 201)
                if len(parts) == 6 and parts[5] in {"approve", "revoke"}:
                    self._read_json()
                    publication = server.publication_controller.get(parts[4])
                    if publication is None:
                        return self._json({'error': 'not found'}, 404)
                    if not self._artifact_version_allowed(
                            publication['resource_id'], publication['pinned_version_id'], 'share'):
                        return self._artifact_forbidden()
                    if parts[5] == "approve":
                        result = server.publication_controller.approve(parts[4], approver=self._resource_principal())
                    else:
                        result = server.publication_controller.revoke(parts[4], actor=self._resource_principal())
                    return self._json(result)
            if resource == "scheduler":
                body = self._read_json() or {}
                principal = self._resource_principal()
                admission = (parts[4:] == ["reservations"]
                             or (len(parts) == 6 and parts[4] == "reservations"
                                 and parts[5] in {"commit", "claim", "retry"}))
                if admission and not server.scheduler_admission:
                    return self._json({"error": "scheduler admission is disabled"}, 503)
                try:
                    if len(parts) == 5 and parts[4] == "plan":
                        return self._json(server.scheduler.plan(body, principal))
                    if len(parts) == 5 and parts[4] == "reservations":
                        return self._json(server.scheduler.reserve(body, principal), 201)
                    if len(parts) == 6 and parts[4] == "reservations" and parts[5] == "commit":
                        return self._json(server.scheduler.commit(body.get("reservation_id", "")))
                    if len(parts) == 6 and parts[4] == "reservations" and parts[5] == "claim":
                        return self._json(server.scheduler.claim_execution(body.get("reservation_id", "")))
                    if len(parts) == 6 and parts[4] == "reservations" and parts[5] == "retry":
                        return self._json(server.scheduler.retry_queued(body.get("reservation_id", "")))
                    if len(parts) == 6 and parts[4] == "reservations" and parts[5] in {"release", "cancel"}:
                        state = "cancelled" if parts[5] == "cancel" else "released"
                        return self._json(server.scheduler.release(body.get("reservation_id", ""), state=state))
                    if len(parts) == 6 and parts[4] == "leases" and parts[5] == "heartbeat":
                        return self._json(server.scheduler.heartbeat(
                            body.get("lease_id", ""), generation=body.get("generation"),
                            token=body.get("token"), ttl=body.get("ttl", 90)))
                    if len(parts) == 5 and parts[4] == "host-policy":
                        host_uid = body.get("host_uid")
                        host = next((h for h in r.list_hosts() if h.get("host_uid") == host_uid or h.get("host_id") == host_uid), None)
                        if host is None:
                            return self._json({"error": "host not found"}, 404)
                        updated = r.set_host_policy(host["host_id"], mode=body.get("mode"),
                                                    expected_revision=body.get("expected_revision"),
                                                    allow_tags=body.get("allow_tags", ()),
                                                    deny_tags=body.get("deny_tags", ()),
                                                    reason=body.get("reason"))
                        return self._json(updated)
                except KeyError:
                    return self._json({"error": "reservation not found"}, 404)
                except PermissionError as exc:
                    return self._json({"error": str(exc)}, 409)
                except (TypeError, ValueError, RuntimeError) as exc:
                    message = str(exc)
                    return self._json({"error": message}, 409 if "conflict" in message or "changed" in message else 400)
                return self._json({"error": "not found"}, 404)
            if resource == "review-gate" and len(parts) > 4 and parts[4] in ("claim", "jobs"):
                return self._review_gate_worker(parts)
            if resource == "review-gate":
                return self._review_gate_resume(parts)
            if resource == "ci" and len(parts) == 5 and parts[4] == "telemetry":
                from .ci_performance import MAX_REQUEST_BYTES, validate_ci_telemetry
                length = int(self.headers.get('Content-Length') or 0)
                if not 0 < length <= MAX_REQUEST_BYTES:
                    raise ValueError('Invalid CI telemetry bundle size.')
                return self._json(r.record_ci_telemetry(validate_ci_telemetry(self._read_json())))
            body = self._read_json() or {}
            if resource == "ci" and len(parts) == 5 and parts[4] == "reports":
                redact = server.ci_monitor.report_redactor()
                return self._json(redact(r.ingest_ci_report(body, redact=redact)))
            if resource == "ci" and len(parts) == 5 and parts[4] == "debug":
                from ..errors import HerdrEngineeringError
                from .ci_debug import debug_ci
                try:
                    receipt = debug_ci(server.ci_monitor, r, server.dispatcher,
                                       body.get("repository"), body.get("number"),
                                       body.get("session_id"), body.get("operation_id"),
                                       **({"provider_url": body["provider_url"]} if "provider_url" in body else {}),
                                       **({"mode": body["mode"]} if "mode" in body else {}))
                except HerdrEngineeringError as exc:
                    return self._json({"error": exc.message}, exc.http_status)
                status = {"delivered": 200, "queued": 202}.get(receipt.get("status"), 502)
                if receipt.get("error") in {"receipt_persistence_unavailable", "receipt_persistence_failed"}:
                    status = 503
                return self._json(receipt, status)
            if resource == "ci" and len(parts) >= 5 and parts[4] == "fix":
                return self._ci_fix_post(parts, body)
            if resource == "ci" and len(parts) == 5 and parts[4] == "actions":
                try:
                    return self._json(server.ci_monitor.action(
                        body.get("repository"), body.get("number"), body.get("action"),
                        **({"provider_url": body["provider_url"]} if "provider_url" in body else {})))
                except PermissionError as exc:
                    return self._json({"error": str(exc)}, 403)
                except Exception as exc:
                    from ..errors import HerdrEngineeringError
                    if isinstance(exc, HerdrEngineeringError):
                        return self._json({"error": exc.message}, exc.http_status)
                    raise
            if resource == "attention" and len(parts) == 5 and parts[4] in ("ack", "snooze", "clear"):
                if server.attention is None:
                    return self._json({"error": "not found"}, 404)
                principal = self._principal()
                actor = f"{principal['kind']}:{principal['id']}"
                try:
                    if parts[4] == "ack":
                        result = server.attention.acknowledge(body.get("key"), body.get("fingerprint"), actor)
                    elif parts[4] == "snooze":
                        result = server.attention.snooze(body.get("key"), body.get("fingerprint"),
                                                         body.get("seconds"), actor)
                    else:
                        result = server.attention.clear(body.get("key"), body.get("fingerprint"), actor)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                except PermissionError as exc:
                    return self._json({"error": str(exc)}, 429)
                return self._json(result)
            if resource == "steering":
                return self._steering(parts, body)
            if resource == "graph" and len(parts) == 8 and parts[4] == "nodes" and parts[6:] == ["review-quorum", "rereview"]:
                return self._rereview(parts[5], body)
            if resource == "reviews":
                from .reviews import ReviewService
                reviews = ReviewService(r)
                if len(parts) == 4:
                    return self._json(reviews.register(body), 201)
                if len(parts) == 6:
                    operation = {"heartbeat": reviews.heartbeat, "result": reviews.complete,
                                 "failure": reviews.fail}.get(parts[5])
                    if operation:
                        return self._json(operation(parts[4], body))
                return self._json({"error": "not found"}, 404)
            if resource == "hosts" and len(parts) == 6 and parts[5] == "telemetry":
                if not body:
                    return self._json({"error": "empty body"}, 400)
                r.record_telemetry(parts[4], body)
                return self._json({"ok": True})
            if resource == "sessions" and len(parts) == 6 and parts[5] == "messages":
                s = r.get_session(parts[4])
                if s is None:
                    return self._json({"error": "session not found"}, 404)
                references = body.get('artifact_ids') or []
                if not isinstance(references, list) or any(not isinstance(aid, str) for aid in references):
                    raise ValueError('artifact_ids must be a list of strings')
                if any(not self._artifact_allowed(aid, 'read') for aid in references):
                    return self._json({'error': 'artifact not found'}, 404)
                receipt = r.send_message(
                    parts[4], body.get("message", ""), body.get("artifact_ids"),
                    operation_id=body.get("operation_id"), dispatcher=server.dispatcher)
                status = {"delivered": 200, "queued": 202}.get(receipt["status"], 502)
                if receipt.get("error") in {"receipt_persistence_unavailable", "receipt_persistence_failed"}:
                    status = 503
                return self._json({"ok": receipt["status"] == "delivered", **receipt}, status)
            if resource == "sessions" and len(parts) == 6 and parts[5] == "terminal":
                from .terminal_attach import AttachRefused, attach_commands
                s = r.get_session(parts[4])
                if s is None:
                    return self._json({"error": "agent not found"}, 404)
                try:
                    return self._json(attach_commands(s, hosts=r.list_hosts(),
                                                      peer=self.client_address[0]))
                except AttachRefused as exc:
                    return self._json(exc.body, exc.status)
            if resource == "host" and len(parts) == 6 and parts[4] == "summary-reports" and parts[5] in {"claim", "result", "publish"}:
                from .summary_report import ReportRefused
                reports = server.summary_report
                host_id = body.get("host_id") if isinstance(body, dict) else None
                if not isinstance(host_id, str) or not host_id or len(host_id) > 253:
                    return self._json({"ok": False, "error": "host_id is required"}, 400)
                if reports is None or not reports.enabled:
                    if parts[5] == "publish":
                        return self._json({"ok": False, "error": "Full session reports are not enabled.",
                                           "reason": "reports_disabled"}, 409)
                    return self._json({"ok": True, "jobs": []} if parts[5] == "claim"
                                      else {"ok": True, "replayed": True})
                if parts[5] == "claim":
                    return self._json({"ok": True, "jobs": reports.claim(host_id)})
                try:
                    status, result = (reports.publish if parts[5] == "publish" else reports.finish)(host_id, body)
                except ReportRefused as exc:
                    return self._json(exc.body, exc.status)
                return self._json(result, status)
            if resource == "sessions" and len(parts) == 7 and parts[5:] == ["summary", "report"]:
                from .summary_report import ReportRefused
                if server.summary_report is None:
                    return self._json({"ok": False, "error": "Full session reports are not enabled.",
                                       "reason": "reports_disabled"}, 409)
                actor = {"role": self._role(), "via": "api_key" if server._keys else "open"}
                try:
                    status, result = server.summary_report.start(parts[4], actor=actor)
                except ReportRefused as exc:
                    return self._json(exc.body, exc.status)
                return self._json(result, status)
            if resource == "sessions" and len(parts) == 7 and parts[5:] == ["summary", "fix"]:
                from .summary_fix import SummaryFixRefused, fix_summary_item
                actor = {"role": self._role(), "via": "api_key" if server._keys else "open"}
                try:
                    status, result = fix_summary_item(server.agent_summaries, r, server.dispatcher,
                                                      parts[4], body, actor=actor,
                                                      helper_spawner=server.summary_fix_helper)
                except SummaryFixRefused as exc:
                    return self._json(exc.body, exc.status)
                return self._json(result, status)
            if resource == "sessions" and len(parts) == 6 and parts[5] in {"name", "summary"}:
                sid = parts[4]
                if r.get_session(sid) is None:
                    return self._json({"error": "agent not found"}, 404)
                if parts[5] == "summary":
                    return self._json(server.agent_summaries.request(sid, force=True), 202)
                from .agent_identity import AgentIdentityConflict
                try:
                    return self._json(r.rename_agent(sid, body.get("name"), body.get("expected_version")))
                except AgentIdentityConflict as exc:
                    return self._json({"error": str(exc)}, 409)
            if resource == "sessions" and len(parts) == 6 and parts[5] == "actions":
                s = r.get_session(parts[4])
                if s is None:
                    return self._json({"error": "session not found"}, 404)
                receipt = r.session_action(parts[4], body.get("action"),
                                           operation_id=body.get("operation_id"))
                return self._json({"ok": False, **receipt}, 501)
            if resource == "services" and len(parts) == 5 and parts[4] == "retire":
                try:
                    days = float(body.get("older_than_days", 7))
                except (TypeError, ValueError):
                    return self._json({"error": "older_than_days must be a number"}, 400)
                states = body.get("states", ["unknown"])
                if not (days >= 1 and isinstance(states, list)
                        and set(states) <= {"unknown", "unavailable"}):
                    return self._json({"error": "older_than_days >= 1 and states within unknown/unavailable"}, 400)
                # Dry run unless the caller explicitly says dry_run=false.
                return self._json(r.retire_services(
                    older_than_days=days, states=tuple(states), dry_run=body.get("dry_run", True) is not False))
            if resource == "token-usage" and len(parts) == 4:
                rows = body.get("records") if isinstance(body.get("records"), list) else [body]
                for row in rows[:500]:
                    r.record_token_usage_row(row)  # whitelisted fields only, never bodies
                return self._json({"ok": True, "recorded": min(len(rows), 500)})
            if resource == "llm" and len(parts) >= 5:
                what = parts[4]
                if what == "keys" and len(parts) == 5:
                    return self._json(r.create_project_key(body.get("project"),
                                                           body.get("label", "")))
                if what == "keys" and len(parts) == 7 and parts[6] == "revoke":
                    r.revoke_project_key(parts[5])
                    return self._json({"ok": True})
                if what == "models" and len(parts) == 5:
                    r.upsert_custom_model(body.get("name"), body.get("base_url"),
                                          body.get("upstream_model"), body.get("project"),
                                          fabric_model=body.get("fabric_model"),
                                          context_window=body.get("context_window", 8192))
                    return self._json({"ok": True})
                if what == "models" and len(parts) == 7 and parts[6] == "delete":
                    r.delete_custom_model(parts[5])
                    return self._json({"ok": True})
            if resource == "ingest" and len(parts) == 4:
                if not body:
                    return self._json({"error": "empty body"}, 400)
                if isinstance(body, dict) and "events" in body:
                    envelopes = body["events"]
                    if not isinstance(envelopes, list) or not 1 <= len(envelopes) <= 64:
                        raise ValueError("events must contain between 1 and 64 envelopes")
                    if any(not isinstance(envelope, dict) for envelope in envelopes):
                        raise ValueError("each event must be an object")
                    sanitized = [envelope if envelope.get('entity_type') in ('inspector', 'mission_activity')
                                 else redact_ingest_body(envelope) for envelope in envelopes]
                    entities = server.ingest.ingest_batch(sanitized, principal=self._principal())
                    return self._json({"ok": True, "entities": entities})
                # Inspector envelopes are redacted per field by the store: the
                # blanket key-hint redaction would mangle ``input_tokens`` and
                # ``passed``.
                if isinstance(body, dict) and body.get("entity_type") in ("inspector", "mission_activity"):
                    entity = server.ingest.ingest(body, principal=self._principal())
                else:
                    entity = server.ingest.ingest(redact_ingest_body(body), principal=self._principal())
                if (server.agent_summaries and server.agent_summaries.config.enabled
                        and body.get("entity_type") == "inspector"):
                    payload = body.get("payload") or {}
                    events = payload.get("events", [payload]) if isinstance(payload, dict) else []
                    changed = body.get("event_type") == "inspector.message" or any(
                        isinstance(event, dict) and event.get("kind") == "message" for event in events)
                    sid = body.get("entity_id")
                    if changed and r.get_session(sid) is not None:
                        server.agent_summaries.request(sid)
                return self._json({"ok": True, "entity": entity})
            if resource in ("assistants", "assistant-runs", "assistant-approvals", "assistant-memory", "assistant-routines", "assistant-connectors", "assistant-delegations"):
                # the body is already read above (line ~2170) into the local
                # ``body``; pass it in rather than re-reading (keep-alive safety)
                return self._assistant_post(parts, resource, body)
            return self._json({"error": "not found"}, 404)

        # ------------------------------------------------------------ assistant
        # Herdr Personal Assistant (P1/P2). Canonical owner: server.assistant.
        # Assistant ownership follows the server-authenticated API-key principal.
        # Legacy open listeners share one operator identity; neither that identity
        # nor client-supplied identity headers establish human approval authority.
        # Collaboration Bearer/Agent authentication belongs to its own dispatch.
        def _assistant_scope(self):
            return "default", self._principal()["id"]

        def _assistant_get(self, parts, resource):
            from .assistant import AssistantNotFound, AssistantUnavailable, RateLimited
            try:
                tenant_id, user_id = self._assistant_scope()
                svc = server.assistant
                svc.check_rate_limit(key=f"{tenant_id}:{user_id}")
                if resource == "assistants":
                    if len(parts) == 4:
                        return self._json({"assistants": [a.as_dict() for a in
                                                           svc.list_assistants(tenant_id=tenant_id, user_id=user_id)]})
                    if len(parts) == 5:
                        return self._json(svc.get_assistant(parts[4], tenant_id=tenant_id, user_id=user_id).as_dict())
                    if len(parts) == 6 and parts[5] == "threads":
                        return self._json({"threads": [t.as_dict() for t in
                                                        svc.list_threads(tenant_id=tenant_id, user_id=user_id,
                                                                         assistant_id=parts[4])]})
                    if len(parts) == 7 and parts[5] == "threads":
                        thread = svc.get_thread(parts[6], tenant_id=tenant_id, user_id=user_id)
                        messages = [m.as_dict() for m in
                                    svc.store.list_messages(thread.thread_id, tenant_id=tenant_id, user_id=user_id)]
                        return self._json({"thread": thread.as_dict(), "messages": messages})
                    if len(parts) == 6 and parts[5] == "permissions":
                        return self._json({"grants": [g.as_dict() for g in
                                                       svc.store.list_grants(tenant_id=tenant_id, user_id=user_id,
                                                                             assistant_id=parts[4])],
                                          "tools": svc.list_tools(tenant_id=tenant_id, user_id=user_id,
                                                                  assistant_id=parts[4])})
                    if len(parts) == 6 and parts[5] == "tools":
                        return self._json({"tools": svc.list_tools(tenant_id=tenant_id, user_id=user_id,
                                                                   assistant_id=parts[4])})
                    if len(parts) == 6 and parts[5] == "memory":
                        query = _q(self.path, "q") or ""
                        return self._json({"memory": [m.as_dict() for m in
                                                       svc.search_memory(tenant_id=tenant_id, user_id=user_id,
                                                                         assistant_id=parts[4], query=query)]})
                    if len(parts) == 6 and parts[5] == "diagnostics":
                        return self._json(svc.diagnostics(tenant_id=tenant_id, user_id=user_id,
                                                          assistant_id=parts[4]))
                    if len(parts) == 6 and parts[5] == "routines":
                        return self._json({"routines": [r.as_dict() for r in
                                                         svc.list_routines(tenant_id=tenant_id, user_id=user_id,
                                                                           assistant_id=parts[4])]})
                    if len(parts) == 7 and parts[5] == "routines":
                        return self._json(svc.get_routine(parts[6], tenant_id=tenant_id, user_id=user_id).as_dict())
                    if len(parts) == 7 and parts[5] == "routines" and parts[6] == "due":
                        return self._json({"routines": [r.as_dict() for r in
                                                         svc.due_routines(tenant_id=tenant_id, user_id=user_id)]})
                if resource == "assistant-routines" and len(parts) == 4:
                    return self._json({"routines": [r.as_dict() for r in
                                                     svc.store.list_routines(tenant_id=tenant_id, user_id=user_id)]})
                if resource == "assistant-routines" and len(parts) == 5:
                    return self._json(svc.get_routine(parts[4], tenant_id=tenant_id, user_id=user_id).as_dict())
                if resource == "assistant-runs" and len(parts) == 5:
                    return self._json(svc.get_run(parts[4], tenant_id=tenant_id, user_id=user_id).as_dict())
                if resource == "assistant-approvals" and len(parts) == 4:
                    state = _q(self.path, "state")
                    return self._json({"approvals": [a.as_dict() for a in
                                                      svc.list_approvals(tenant_id=tenant_id, user_id=user_id,
                                                                         state=state)]})
                if resource == "assistant-memory" and len(parts) == 5:
                    return self._json(svc.store.get_memory(parts[4], tenant_id=tenant_id, user_id=user_id).as_dict())
                if resource == "assistant-connectors" and len(parts) == 4:
                    return self._json({"connectors": [c.as_dict() for c in
                                                        svc.list_connectors(tenant_id=tenant_id, user_id=user_id)]})
                if resource == "assistant-connectors" and len(parts) == 5:
                    return self._json(svc.get_connector(parts[4], tenant_id=tenant_id, user_id=user_id).as_dict())
                if resource == "assistant-delegations" and len(parts) == 4:
                    return self._json({"delegations": [d.as_dict() for d in
                                                         svc.list_delegations(tenant_id=tenant_id, user_id=user_id)]})
                if resource == "assistant-delegations" and len(parts) == 5:
                    return self._json(svc.get_delegation(parts[4], tenant_id=tenant_id, user_id=user_id).as_dict())
                return self._json({"error": "not found"}, 404)
            except AssistantUnavailable as exc:
                return self._json({"error": str(exc), "code": "unavailable"}, 503)
            except RateLimited as exc:
                return self._json({"error": str(exc), "code": "rate_limited"}, 429)
            except (KeyError, AssistantNotFound):
                return self._json({"error": "not found"}, 404)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)

        def _assistant_post(self, parts, resource, body):
            from .assistant import (
                ApprovalRequired,
                AssistantError,
                AssistantNotFound,
                AssistantUnavailable,
                RateLimited,
            )
            try:
                tenant_id, user_id = self._assistant_scope()
                svc = server.assistant
                svc.check_rate_limit(key=f"{tenant_id}:{user_id}")
                body = body or {}
                if resource == "assistants":
                    if len(parts) == 4 and self.command == "POST":
                        created = svc.create_assistant(
                            tenant_id=tenant_id, user_id=user_id,
                            name=body.get("name", "Personal Assistant"),
                            persona=body.get("persona", "helpful"),
                            instructions=body.get("instructions", ""),
                            model_policy=body.get("model_policy", "auto"),
                            enabled=bool(body.get("enabled", True)))
                        return self._json(created.as_dict(), 201)
                    if len(parts) == 5 and self.command in ("PATCH", "POST"):
                        if body.get("archive"):
                            svc.archive_assistant(parts[4], tenant_id=tenant_id, user_id=user_id)
                            return self._json({"ok": True})
                        updated = svc.update_assistant(parts[4], tenant_id=tenant_id, user_id=user_id, patch=body)
                        return self._json(updated.as_dict())
                    if len(parts) == 5 and self.command == "DELETE":
                        svc.archive_assistant(parts[4], tenant_id=tenant_id, user_id=user_id)
                        return self._json({"ok": True})
                    if len(parts) == 6 and parts[5] == "threads" and self.command == "POST":
                        thread = svc.create_thread(tenant_id=tenant_id, user_id=user_id,
                                                  assistant_id=parts[4],
                                                  title=body.get("title", "New conversation"))
                        return self._json(thread.as_dict(), 201)
                    if len(parts) == 8 and parts[5] == "threads" and parts[7] == "messages":
                        run = svc.send_message(tenant_id=tenant_id, user_id=user_id,
                                               assistant_id=parts[4], thread_id=parts[6],
                                               content=body.get("content", ""))
                        return self._json({"run": run.as_dict()}, 202)
                    if len(parts) == 6 and parts[5] == "permissions" and self.command == "POST":
                        grant = svc.grant_tool(tenant_id=tenant_id, user_id=user_id,
                                               assistant_id=parts[4],
                                               tool_id=body.get("tool_id", ""),
                                               scope=body.get("scope", "*"),
                                               ttl_seconds=body.get("ttl_seconds"),
                                               preauthorized=bool(body.get("preauthorized", False)))
                        return self._json(grant.as_dict(), 201)
                    if len(parts) == 7 and parts[5] == "permissions" and parts[6] == "revoke":
                        svc.revoke_grant(body.get("grant_id", parts[5]), tenant_id=tenant_id, user_id=user_id)
                        return self._json({"ok": True})
                    if len(parts) == 6 and parts[5] == "memory" and self.command == "POST":
                        memo = svc.store_memory(tenant_id=tenant_id, user_id=user_id,
                                                assistant_id=parts[4],
                                                content=body.get("content", ""),
                                                namespace=body.get("namespace", "private"),
                                                source_ref=body.get("source_ref", ""),
                                                classification=body.get("classification", "general"))
                        return self._json(memo.as_dict(), 201)
                    if len(parts) == 7 and parts[5] == "memory" and parts[6] == "export":
                        return self._json(svc.export_memory(tenant_id=tenant_id, user_id=user_id,
                                                            assistant_id=parts[4]))
                if resource == "assistant-runs" and len(parts) == 6 and parts[5] == "cancel":
                    return self._json(svc.cancel_run(parts[4], tenant_id=tenant_id, user_id=user_id).as_dict())
                if resource == "assistant-runs" and len(parts) == 6 and parts[5] == "tool":
                    # the assistant is derived from the run (never trusted from the body)
                    _run = svc.get_run(parts[4], tenant_id=tenant_id, user_id=user_id)
                    action = svc.request_tool_action(
                        tenant_id=tenant_id, user_id=user_id,
                        assistant_id=_run.assistant_id,
                        tool_id=body.get("tool_id", ""), scope=body.get("scope", "*"),
                        run_id=parts[4], idempotency_key=body.get("idempotency_key", ""),
                        request_payload=body.get("payload") or {}, actor=user_id)
                    return self._json(action.as_dict())
                if resource == "assistant-approvals" and len(parts) == 6 and parts[5] == "decision":
                    decision = svc.decide_approval(parts[4], tenant_id=tenant_id, user_id=user_id,
                                                   decision=body.get("decision", ""),
                                                   actor=user_id)
                    return self._json(decision.as_dict())
                if resource == "assistant-memory" and len(parts) == 5 and self.command == "DELETE":
                    svc.forget_memory(parts[4], tenant_id=tenant_id, user_id=user_id)
                    return self._json({"ok": True})
                if resource == "assistant-memory" and len(parts) == 5 and self.command == "PATCH":
                    memo = svc.correct_memory(parts[4], tenant_id=tenant_id, user_id=user_id,
                                              new_content=body.get("content", ""),
                                              source_ref=body.get("source_ref", "correction"))
                    return self._json(memo.as_dict())
                if resource == "assistant-routines" and len(parts) == 4 and self.command == "POST":
                    routine = svc.create_routine(
                        tenant_id=tenant_id, user_id=user_id,
                        assistant_id=body.get("assistant_id", ""),
                        name=body.get("name", "Routine"),
                        trigger_type=body.get("trigger_type", "interval"),
                        trigger_spec=body.get("trigger_spec") or {},
                        timezone=body.get("timezone", "UTC"),
                        quiet_hours=body.get("quiet_hours"),
                        enabled=bool(body.get("enabled", True)))
                    return self._json(routine.as_dict(), 201)
                if resource == "assistant-routines" and len(parts) == 6 and parts[5] == "trigger":
                    return self._json(svc.trigger(parts[4], tenant_id=tenant_id, user_id=user_id).as_dict())
                if resource == "assistant-routines" and len(parts) == 5 and self.command == "DELETE":
                    svc.archive_routine(parts[4], tenant_id=tenant_id, user_id=user_id)
                    return self._json({"ok": True})
                if resource == "assistant-connectors" and len(parts) == 4 and self.command == "POST":
                    conn = svc.register_connector(
                        tenant_id=tenant_id, user_id=user_id,
                        name=body.get("name", "Connector"),
                        kind=body.get("kind", "http"),
                        base_url=body.get("base_url", ""),
                        auth=body.get("auth") or {})
                    return self._json(conn.as_dict(), 201)
                if resource == "assistant-connectors" and len(parts) == 5 and self.command == "DELETE":
                    svc.archive_connector(parts[4], tenant_id=tenant_id, user_id=user_id)
                    return self._json({"ok": True})
                if resource == "assistant-connectors" and len(parts) == 6 and parts[5] == "request":
                    return self._json(svc.connector_request(
                        tenant_id=tenant_id, user_id=user_id, connector_id=parts[4],
                        method=body.get("method", "GET"), path=body.get("path", "/"),
                        body=body.get("body") or {}, actor=user_id))
                if resource == "assistant-delegations" and len(parts) == 4 and self.command == "POST":
                    d = svc.delegate(tenant_id=tenant_id, user_id=user_id,
                                     assistant_id=body.get("assistant_id", ""),
                                     task=body.get("task", ""),
                                     target_system=body.get("target_system", "pi"),
                                     actor=user_id)
                    return self._json(d.as_dict(), 201)
                if resource == "assistant-delegations" and len(parts) == 6 and parts[5] == "cancel":
                    return self._json(svc.cancel_delegation(parts[4], tenant_id=tenant_id,
                                                            user_id=user_id).as_dict())
                if resource == "assistant-delegations" and len(parts) == 6 and parts[5] == "artifact":
                    return self._json(svc.register_delegation_artifact(
                        parts[4], tenant_id=tenant_id, user_id=user_id,
                        artifact_ref=body.get("artifact_ref", "")).as_dict())
                return self._json({"error": "not found"}, 404)
            except ApprovalRequired as exc:
                return self._json({"error": str(exc), "code": "approval_needed",
                                   "approval_id": exc.approval_id}, 403)
            except AssistantUnavailable as exc:
                return self._json({"error": str(exc), "code": "unavailable"}, 503)
            except RateLimited as exc:
                return self._json({"error": str(exc), "code": "rate_limited"}, 429)
            except AssistantError as exc:
                return self._json({"error": str(exc)}, 400)
            except (KeyError, AssistantNotFound):
                return self._json({"error": "not found"}, 404)

        def _steering(self, parts, body=None):
            """T12 delivery to the emitter that owns a steering target (keyed API; no browser, no HCN credential).
            ``GET /api/v1/steering/pending`` returns confirmed requests pinned to THIS key's graph principal;
            ``POST /api/v1/steering/<id>/ack`` reports what it did. Absent in collab-only mode (no graph)."""
            service = server.collab
            if service is None or server.collab_only or service.graph_source is None:
                return self._json({"error": "not found"}, 404)
            principal = self._principal()
            if principal.get("authenticated_by") != "api_key":
                return self._json({"error": "steering delivery needs an authenticated API key; "
                                            "an open listener has no principal to pin a request to",
                                   "code": "authentication_required"}, 403)
            who = principal.get("id")
            inst = _q(self.path, "instance_id")
            if inst is not None and not re.fullmatch(r"[A-Za-z0-9._:\-]{1,128}", inst):
                return self._json({"error": "invalid instance_id"}, 400)
            from .collab import CollabError
            try:
                if body is None and parts[4:] == ["pending"]:
                    return self._json(service.steering_pending(who, inst))
                if body is None and len(parts) == 6 and parts[4] == "targets":
                    cur = service.steering_current(who, parts[5], inst)
                    return self._json(cur) if cur is not None else self._json({"error": "not found"}, 404)
                if body is not None and len(parts) == 6 and parts[5] == "ack":
                    return self._json(service.steering_ack(who, parts[4], body, inst))
            except CollabError as exc:
                return self._json({"error": exc.message, **getattr(exc, "extra", {})}, exc.status)
            return self._json({"error": "not found"}, 404)

        def _rereview(self, pr_id, body):
            """T16 gap C: ``POST /api/v1/graph/nodes/{pr}/review-quorum/rereview`` ``{"head"?: <full sha>}`` records that
            the PR's CURRENT head needs another review round. Recording only: Herdr dispatches nothing. 201 new, 200 replay
            (one request per PR head); 404 hidden/absent/feature off; 403 a key the operator did not name for this PR's
            review evidence; 409 not needed / head moved / head unknown; 429 rate limited."""
            from .review_quorum import RereviewError
            if server.review_quorum is None:
                return self._json({"error": "review quorum is not configured"}, 404)
            principal = self._principal()
            if not isinstance(body, dict) or set(body) - {"head"}:
                return self._json({"error": "body may only carry head"}, 400)
            head = body.get("head")
            if head is not None and not (isinstance(head, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head)):
                return self._json({"error": "head must be a full lowercase commit id"}, 400)
            try:
                status, out = server.rereview.request(server.repo.graph, principal, pr_id, server.review_quorum, head)
            except KeyError:
                return self._json({"error": "not found"}, 404)
            except RereviewError as exc:
                if exc.code == "not_permitted" and server._refusal_log_due(principal.get("id")):
                    logging.getLogger(__name__).warning("review quorum: %s refused a re-review request", principal.get("id"))
                return self._json({"error": exc.message, "code": exc.code, **exc.extra}, exc.status)
            return self._json(out, status)

        def _llm_stats(self, what):
            """LLM page statistics (operator listener only; a bad parameter is a 400)."""
            window = _q(self.path, "window") or "24h"
            filters = {k: _q(self.path, k) for k in ("project", "model", "session", "mission", "source")}
            try:
                if what == "stats":
                    return self._json(server.repo.llm_stats(
                        window, _q(self.path, "group") or "model", **filters))
                if what == "timeseries":
                    return self._json(server.repo.llm_timeseries(
                        window, _q(self.path, "step") or "1h", _q(self.path, "metric") or "requests",
                        **filters))
                return self._json(server.repo.llm_errors(
                    window, _q(self.path, "class"), **filters))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)

        def _graph_get(self, parts):
            """Lineage graph reads (viewer level). Every result is filtered by the
            caller's principal; hidden and missing both answer 404."""
            parts = list(parts)
            if len(parts) == 7 and parts[4] == "nodes" and parts[6] == "review-quorum":
                if server.review_quorum is None:
                    return self._json({"error": "review quorum is not configured"}, 404)
                try:
                    out = pr_review_quorum(server.repo.graph, self._principal(), parts[5], server.review_quorum)
                    # T16 gap C: the recorded ask for THIS head (None for any other), so the dispatcher can poll one read
                    out["rereview_request"] = server.rereview.current(out["pr"], out.get("head"))
                    return self._json(out)
                except KeyError:
                    return self._json({"error": "not found"}, 404)
            out = read_graph(server.repo.graph, self._principal(), parts[4:], lambda name: _q(self.path, name))
            if len(parts) == 6 and parts[4] == "nodes" and server.collab is not None and not server.collab_only:
                # T16 gap A: an ingest key can read the node's CURRENT steering fence and gate digest (so a runtime
                # can fill ResolvedTarget.fence). Only for a node this key could read: read_graph answered 404 for
                # any other, and the same principal is used again here.
                view = server.collab.node_steering_view(server.repo.graph, self._principal(), parts[5])
                if view is not None:
                    out = {**out, **view}
            return self._json(out)

        def _agent_capabilities(self, session):
            operator = self._role() == "operator"
            return {**session, "capabilities": {"rename_agent": operator,
                                                "regenerate_summary": operator,
                                                "fix_summary_item": operator,
                                                "summary_report": operator and server.summary_report is not None
                                                and server.summary_report.enabled,
                                                "attach_terminal": operator}}

        def _named_inspector(self, data, sid):
            # Claim an existing provider preference before a helper/read path
            # can allocate the primary agent's default display identity.
            server.repo.get_session(sid)
            data["capabilities"] = {**data.get("capabilities", {}),
                                    "can_manage_agents": self._role() == "operator"}
            for agent in (data.get("summary") or {}).get("agents", []):
                aid = agent.get("agent_id") or "main"
                identity = sid if aid == "main" else f"{sid}#agent:{aid}"
                profile = server.repo.agent_identities.allocate(identity)
                agent.update(display_name=profile["display_name"], agent_handle=profile["handle"])
            return data

        # ---------------------------------------------------- agent inspector
        def _inspector_global_get(self, parts):
            store = server.repo.inspector
            what = parts[4] if len(parts) > 4 else ""
            if what == "health":
                return self._json(store.health())
            if what == "preferences":
                return self._json({"prefs": store.get_prefs(), "defaults": _defaults()})
            return self._json({"error": "not found"}, 404)

        def _inspector_get(self, parts):
            store = server.repo.inspector
            sid = parts[4]
            role = self._role() or "viewer"
            tail = parts[6:] if len(parts) > 6 else []
            try:
                if not tail:
                    if not store.has_session(sid):
                        if server.repo.get_session(sid) is None:
                            return self._json({"error": "session not found"}, 404)
                        return self._json(self._named_inspector(_empty_inspector(store, sid), sid))
                    return self._json(self._named_inspector(store.query(
                        sid, role, view=_q(self.path, "view") or "all",
                        q=_q(self.path, "q"), agent_id=_q(self.path, "agent_id"),
                        status=_q(self.path, "status"),
                        since_rev=_qint(self.path, "since_rev", 0),
                        limit=_qint(self.path, "limit", 200),
                        before_seq=(_qint(self.path, "before_seq", -1)
                                    if _q(self.path, "before_seq") else None)), sid))
                if tail[0] == "events" and len(tail) == 2:
                    if not tail[1].isdigit():
                        return self._json({"error": "invalid sequence"}, 400)
                    return self._json(store.get_event(sid, int(tail[1]), role))
                if tail[0] == "preferences":
                    return self._json({"prefs": store.get_prefs(sid),
                                       "session": store.raw_prefs(sid),
                                       "global": store.raw_prefs(),
                                       "defaults": _defaults()})
                if tail[0] == "stream":
                    if not store.has_session(sid) and server.repo.get_session(sid) is None:
                        return self._json({"error": "session not found"}, 404)
                    if not store.can_subscribe(sid):
                        return self._json({"error": "too many streams"}, 429)
                    return self._inspector_sse(sid, role)
            except KeyError:
                return self._json({"error": "not found"}, 404)
            return self._json({"error": "not found"}, 404)

        def _inspector_set_prefs(self, sid):
            body = self._read_json() or {}
            try:
                prefs = server.repo.inspector.set_prefs(body, sid)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json({"ok": True, "prefs": prefs})

        def _inspector_sse(self, sid, role):
            store = server.repo.inspector
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            # A stalled client must not pin a handler thread forever.
            try:
                self.connection.settimeout(30)
            except OSError:
                pass
            sub = store.subscribe(sid)
            try:
                self.wfile.write(_raw_frame({"type": "connected", "session_id": sid,
                                              "epoch": store.epoch}))
                self.wfile.flush()
                last_beat = started = time.monotonic()
                while time.monotonic() - started < _MAX_SSE_SECONDS:
                    item = sub.get(timeout=15)
                    if item is None:
                        if time.monotonic() - last_beat >= 15:
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                            last_beat = time.monotonic()
                        if sub.closed:
                            break
                        continue
                    if item["type"] == "event":
                        entry = store.stream_entry(sid, item["entry"], role)
                        if entry is None:
                            continue
                        item = {"type": "event", "event": entry}
                    self.wfile.write(_raw_frame(item))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                pass
            finally:
                store.unsubscribe(sid, sub)
            return None

        def _upload_artifact(self, r, parts):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return self._json({"error": "empty body"}, 400)
            data = self.rfile.read(length)
            filename = _q(self.path, "filename") or "upload"
            mime = _q(self.path, "mime") or \
                self.headers.get("Content-Type", "application/octet-stream")
            session_id = _q(self.path, "session_id")
            mission_id = _q(self.path, "mission_id")
            owner = self._resource_principal()
            artifact_id = None
            # When a durable object store is wired, write the payload there
            # (content-addressed by sha256) and keep only metadata in the repo.
            # Otherwise fall back to the repo's in-memory blob stand-in.
            storage_key = ""
            sha256 = ""
            if server.artifact_store is not None:
                session_record = r.get_session(session_id) if session_id else None
                mission_record = r.get_mission(mission_id) if mission_id else None
                if session_id and session_record is None:
                    return self._json({'error': 'session not found'}, 404)
                if mission_id and mission_record is None:
                    return self._json({'error': 'mission not found'}, 404)
                if session_record is not None and mission_record is None:
                    linked = session_record.get('mission_key') or session_record.get('mission_id')
                    mission_record = r.get_mission(linked) if linked else None
                if session_record is not None or mission_record is not None:
                    self._artifact_call('ensure_binding_context',
                                        mission=mission_record, session=session_record)
                artifact_id = self._artifact_call('register', original_filename=filename,
                    mime_type=mime, data=data, created_by=owner,
                    mission_id=mission_id, session_id=session_id)
                durable = self._artifact_call('get_record', artifact_id, principal_id=owner)
                if durable is None:
                    raise RuntimeError('artifact registration was not acknowledged')
                storage_key, sha256 = durable.get('storage_key', ''), durable.get('sha256', '')
            elif server.rustfs is not None:
                storage_key, sha256 = server.rustfs.put(data)
            blob = None if server.rustfs is not None or server.artifact_store is not None else data
            aid = r.register_artifact(session_id=session_id,
                                      artifact_id=artifact_id, created_by=owner,
                                      mission_id=mission_id,
                                      original_filename=filename,
                                      mime_type=mime, size_bytes=len(data),
                                      sha256=sha256, storage_key=storage_key,
                                      data=blob)
            if server.artifact_acl is not None:
                server.artifact_acl.grant(aid, owner, {'read', 'edit', 'review', 'share', 'admin'})
            return self._json(r.get_artifact(aid))

        # ------------------------------------------------------------ SSE
        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = queue.Queue(maxsize=256)
            with server._lock:
                server._subscribers.add(q)
            try:
                # always emit an initial frame so a client gets data immediately
                self.wfile.write(_sse_frame({
                    "event_type": "StreamConnected",
                    "entity_type": "stream", "entity_id": "control-plane",
                    "sequence": 0, "payload": {},
                }))
                self.wfile.flush()
                # replay recent history so a fresh client catches up (Spec §52)
                for ev in reversed(server.repo.recent_events(limit=100)):
                    self.wfile.write(_sse_frame(ev))
                    self.wfile.flush()
                started = time.monotonic()
                # Bounded lifetime frees threads held by half-open clients;
                # EventSource reconnects and replays.
                while time.monotonic() - started < _MAX_SSE_SECONDS:
                    try:
                        ev = q.get(timeout=_SSE_IDLE_SECONDS)
                    except queue.Empty:
                        if q not in server._subscribers:
                            break  # dropped as a slow subscriber: close so the client reconnects
                        # A data frame (not an SSE comment, which EventSource never surfaces)
                        # keeps proxies from timing out AND tells the UI the stream is alive on
                        # a quiet fleet; the client filters it out like StreamConnected.
                        self.wfile.write(_sse_frame({
                            "event_type": "StreamKeepalive",
                            "entity_type": "stream", "entity_id": "control-plane",
                            "sequence": 0, "payload": {},
                        }))
                        self.wfile.flush()
                        continue
                    self.wfile.write(_sse_frame(ev))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                server._subscribers.discard(q)
                self.close_connection = True
            return None

        # ------------------------------------------------------------ static
        def _serve_static(self):
            rel = self.path.lstrip("/") or _INDEX
            if ".." in rel:
                return self._send_json(403, {"error": "forbidden"})
            target = (server.web_root / rel).resolve()
            if not target.is_relative_to(server.web_root.resolve()) or \
                    not target.exists() or target.is_dir():
                return self._send_json(404, {"error": "not found"})
            ctype = {
                ".html": "text/html", ".css": "text/css",
                ".js": "text/javascript", ".svg": "image/svg+xml",
            }.get(target.suffix, "application/octet-stream")
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None

        def _ci_fix_post(self, parts, body):
            """Sandboxed CI fix agent. The browser creates and cancels; the runner (``agent`` role)
            claims, reports state and reports the result. Disabled means every route is a 404."""
            service = server.ci_fix
            if service is None:
                return self._json({"error": "not found"}, 404)
            if len(parts) == 5:
                actor = {"role": self._role(), "via": "api_key" if server._keys else "open"}
                view, created = service.request(
                    server.ci_monitor, body.get("repository"), body.get("number"), body.get("operation_id"),
                    actor=actor,
                    **({"provider_url": body["provider_url"]} if "provider_url" in body else {}))
                return self._json(view, 202 if created else 200)
            if len(parts) == 6 and parts[5] == "claim":
                return self._json(service.claim(body.get("host"), body.get("defer")))
            if len(parts) == 7 and parts[6] == "cancel":
                return self._json(service.cancel(
                    parts[5], actor={"role": self._role(), "via": "api_key" if server._keys else "open"}))
            if len(parts) == 7 and parts[6] == "state":
                return self._json(service.set_state(
                    parts[5], body.get("state"),
                    **{key: body[key] for key in ("reason", "host", "model", "session_id", "branch") if key in body}))
            if len(parts) == 7 and parts[6] == "result":
                return self._json(service.record_result(parts[5], body))
            return self._json({"error": "not found"}, 404)

        # ---------------------------------------------------------- helpers
        def _fabric_services(self):
            """Active fabric leases for the Fabric Services page. Only fields the page shows are passed on."""
            keep = ("id", "external_port", "protocol", "state", "label", "url", "target", "owner", "renewed_at",
                    "expires_at", "health_last_ok_at", "health_last_error")
            if server.fabric_services is None:
                return self._json({"leases": [], "available": False})
            try:
                leases = (server.fabric_services() or {}).get("leases") or []
            except Exception:
                return self._json({"leases": [], "available": False})
            return self._json({"leases": [{k: lz.get(k) for k in keep} for lz in leases], "available": True})

        def _fabric_names(self):
            """Names that mark a URL as the fabric's, for the browser's link detector. Degrades to none."""
            try:
                got = server.fabric_names() if server.fabric_names else None
            except Exception:
                got = None
            names = [n for n in [(got or {}).get("url_base"), *((got or {}).get("aliases") or [])] if isinstance(n, str)]
            return self._json({"names": names[:16], "available": got is not None})

        def _fabric_resolve_urls(self):
            """Canonical fabric URLs for URLs shown in agent output. Read-only; originals are never changed.
            A missing or failing gateway degrades to 'unavailable' so a link is simply left as written."""
            body = self._read_json() or {}
            urls, machine = body.get("urls"), body.get("source_machine")
            namespace = body.get("source_namespace")
            if (not isinstance(urls, list) or len(urls) > 200 or not all(isinstance(u, str) and len(u) <= 2048 for u in urls)
                    or not (machine is None or isinstance(machine, str))
                    or not (namespace is None or (isinstance(namespace, str) and len(namespace) <= 64))):
                return self._json({"error": "urls must be a list of at most 200 strings"}, 400)
            unavailable = {"resolutions": [{"original": u, "canonical": None, "status": "unavailable",
                                            "reason": "fabric gateway not reachable"} for u in urls]}
            if server.fabric_resolver is None:
                return self._json(unavailable)
            try:
                return self._json(server.fabric_resolver(urls, machine, namespace) if namespace
                                  else server.fabric_resolver(urls, machine))
            except Exception:
                return self._json(unavailable)

        def _read_json(self, limit=_MAX_JSON_BODY):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return None
                if length > limit:
                    raise ValueError("request body too large")
                try:
                    return json.loads(self.rfile.read(length))
                except (ValueError, json.JSONDecodeError):
                    raise ValueError("invalid JSON body") from None
            except ValueError:
                # The body may be unread or only partly read: never reuse the connection,
                # or the leftover bytes would be parsed as the next request.
                self.close_connection = True
                raise

        def _json(self, data, status=200, *, etag=None, payload=None):
            if server.ci_fix is not None and payload is None:
                data = server.ci_fix.redact(data)
            payload = json.dumps(data).encode() if payload is None else payload
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            if etag and status == 200:
                self.send_header("ETag", etag)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            return None

        def _artifact_call(self, method, *args, **kwargs):
            store = server.artifact_store
            runner = getattr(store, "sync_call", None) or (store if callable(store) else None)
            if runner is None:
                raise RuntimeError("versioned artifact registry adapter is unavailable")
            return runner(method, *args, **kwargs)

        def _not_modified(self, etag):
            """304: no body (keep-alive safe without Content-Length), same validator."""
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            return None

        def _fresh(self, etag):
            return _if_none_match(self.headers.get("If-None-Match"), etag)

        def _etag_json(self, data, status=200):
            """A read-only body with a content-hash ETag; ``304`` when the client already has it.

            Polled views (hosts, sessions, services, missions) save the transfer
            and the re-render, not the server work: the body is still built.
            """
            if status != 200:
                return self._json(data, status)
            payload, etag = etag_json(data)
            if self._fresh(etag):
                return self._not_modified(etag)
            return self._json(data, etag=etag, payload=payload)

        def _github_list(self, kind, query):
            """``/github``, ``/github/issues`` and ``/github/projects[?project=]`` with ``revision``,
            ``ETag`` and ``since`` deltas.

            The ETag is ``W/"<revision>-<hash>"`` where the hash covers the query and
            the monitor's sync state, taken before the body is built (so it can only
            be older than the body, never claim a newer one). A monitor without a
            trustworthy revision falls back to a content hash.
            """
            monitor = server.github_monitor
            since = query.pop("since", None)
            repository = query.pop("repository", None) if kind == "issues" else None
            project = query.pop("project", None) if kind == "projects" else None
            if kind == "projects":
                if not hasattr(monitor, "projects"):  # a monitor without Projects support: sync is off
                    body = dict(monitor.snapshot(start=False), projects=[],
                                projects_sync={"enabled": False, "status": "disabled", "error": None,
                                               "projects": 0, "deferred": 0, "last_success_at": None})
                    return self._etag_json(body)
                if project is not None:
                    kind = "project_items"
                    monitor.project_items(project, start=False)  # 404 before any ETag for an unknown project
                    repository = project
            tag_of = getattr(monitor, "list_tag", None)
            tag = tag_of() if tag_of else None
            etag = None
            if tag is not None:
                material = json.dumps([kind, repository, since, sorted(query.items()), tag["fingerprint"]],
                                      default=str).encode()
                etag = f'W/"{tag["revision"]}-{hashlib.sha256(material).hexdigest()[:20]}"'
                if self._fresh(etag):
                    return self._not_modified(etag)
            changes = getattr(monitor, "changes", None)
            if since is not None and changes is not None:
                filters = ({"repository_id": repository, "tab": query.get("tab", "all")} if kind == "issues"
                           else {"project_id": project} if kind == "project_items" else {})
                body = changes(kind=kind, since=since, search=query.get("search", ""), **filters)
            else:
                if kind == "projects":
                    body = monitor.projects()
                elif kind == "project_items":
                    body = monitor.project_items(project)
                else:
                    extra = {"repository_id": repository} if kind == "issues" else {}
                    body = (monitor.issues(**extra, **query) if kind == "issues" else monitor.repositories(**query))
                if since is not None:  # a monitor without deltas: always a full resync
                    body = dict(body, resync=True)
                if tag is not None:
                    body = dict(body, revision=tag["revision"])
            if etag is None:
                return self._etag_json(body)
            return self._json(body, etag=etag)

        def _bytes(self, data, mime):
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return None

        def _send_json(self, status, data):
            return self._json(data, status)

    return _Handler


def _defaults() -> dict:
    from .inspector import DEFAULT_LIMITS, DEFAULT_PREFS, HARD_CEILINGS
    return {"prefs": DEFAULT_PREFS, "limits": DEFAULT_LIMITS, "ceilings": HARD_CEILINGS}


def _empty_inspector(store, sid: str) -> dict:
    """A known session that has not (yet) sent inspector telemetry: say so, never imply data."""
    return {"session_id": sid, "view": "all", "epoch": store.epoch, "events": [], "has_more": False, "rev": 0,
            "dropped": 0, "oldest_available_seq": None, "telemetry": "none",
            "summary": None, "capabilities": {"declared": False, "reasoning": "unknown"},
            "prefs": store.get_prefs(sid)}


def _redact_event(event: dict) -> dict:
    ev = dict(event)
    payload = ev.get("payload", {})
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
    ev["payload"] = redact_payload(payload)
    return ev


def _raw_frame(obj: dict) -> bytes:
    """SSE frame for already-sanitised inspector frames (no event-envelope redaction)."""
    return f"data: {json.dumps(obj)}\n\n".encode()


def _sse_frame(event: dict) -> bytes:
    return f"data: {json.dumps(_redact_event(event))}\n\n".encode()


def _ci_view() -> dict[str, Any]:
    """Legacy synchronous CLI view; HTTP clients use the server's shared monitor."""
    from .ci_monitor import CIMonitor
    monitor = CIMonitor()
    monitor.refresh()
    return monitor.snapshot(start=False)


def _tests_view() -> dict[str, Any]:
    """Test-run view: adapters + a coarse per-adapter summary. Fail-closed."""
    from ..tests import TestExplorer
    try:
        explorer = TestExplorer()
        return {"adapters": explorer.adapters()}
    except Exception as exc:
        return {"adapters": [], "error": str(exc)}


_FEED_SERVICE_FIELDS = ("service_id", "service_name", "host_id", "source_port", "session_id",
                        "discovery_source", "verified_origin", "ownership", "listener_identity")


def _ci_reports_query(path):
    from urllib.parse import parse_qs, urlsplit

    from .ci_reports import normalize_provider_url
    from .redact import redact_text

    query = parse_qs(urlsplit(path).query, keep_blank_values=True, max_num_fields=8)
    identity = {'provider_url', 'provider_repo_id', 'repository', 'run_number'}
    if set(query) - (identity | {'compact'}) or any(len(v) != 1 for v in query.values()):
        raise ValueError('Invalid CI report filters')
    values = {key: value[0] for key, value in query.items()}
    if any(not value or len(value) > 500 or not value.isprintable() or redact_text(value) != value
           for value in values.values()):
        raise ValueError('Invalid CI report filter')
    if values.get('compact', '0') not in ('0', '1'):
        raise ValueError('Invalid compact report option')
    run = {key: value for key, value in values.items() if key in identity}
    if run:
        if set(run) != identity or not run['run_number'].isdigit() or int(run['run_number']) < 1:
            raise ValueError('Exact CI report run identity is required')
        run['provider_url'] = normalize_provider_url(run['provider_url'])
        run['run_number'] = int(run['run_number'])
    return {'compact': values.get('compact') == '1', 'run': run or None}


def _ci_observability_query(path, route):
    from urllib.parse import parse_qs, urlsplit

    from .ci_reports import normalize_provider_url
    from .redact import redact_text

    query = parse_qs(urlsplit(path).query, keep_blank_values=True, max_num_fields=16)
    allowed = {'days', 'repository', 'branch', 'provider_url', 'provider_repo_id',
               'scope', 'start', 'end', 'mode', 'run_number'} | ({'test'} if route == 'tests' else set())
    if set(query) - allowed or any(len(values) != 1 for values in query.values()):
        raise ValueError('Invalid CI filters')
    filters = {key: values[0] for key, values in query.items() if values[0] != ''}
    for value in filters.values():
        if len(value) > 500 or not value.isprintable() or redact_text(value) != value:
            raise ValueError('Invalid CI filter')
    days = filters.get('days', '14')
    if days not in ('7', '14', '30'):
        raise ValueError('CI days must be 7, 14 or 30')
    filters['days'] = int(days)
    if filters.get('mode', 'days') not in ('days', 'runs', 'hours'):
        raise ValueError('Invalid CI window mode')
    if 'run_number' in filters:
        number = filters['run_number']
        if (filters.get('mode') != 'runs' or not number.isascii() or not number.isdigit() or
                not 0 < int(number) <= 2**63 - 1 or
                not all(filters.get(key) for key in ('provider_url', 'provider_repo_id', 'repository'))):
            raise ValueError('CI run selection requires a valid run and exact project identity')
        filters['run_number'] = int(number)
    if filters.get('scope', 'all') not in ('all', 'default', 'pr'):
        raise ValueError('CI scope must be all, default or pr')
    if 'start' in filters or 'end' in filters:
        from datetime import datetime

        try:
            bounds = [datetime.fromisoformat(filters[key].replace('Z', '+00:00')) for key in ('start', 'end')]
            if any(value.tzinfo is None for value in bounds) or bounds[0] >= bounds[1]:
                raise ValueError()
        except (KeyError, ValueError):
            raise ValueError('CI bucket requires ordered start and end timestamps with time zones') from None
    if 'provider_url' in filters:
        filters['provider_url'] = normalize_provider_url(filters['provider_url'])
    return filters


def _q(path, key):
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(path).query).get(key, [None])[0]


def _qint(path, key, default):
    v = _q(path, key)
    try:
        return int(v) if v is not None else default
    except ValueError:
        return default


def etag_json(body) -> tuple[bytes, str]:
    """The JSON payload of ``body`` and its strong content-hash ETag.

    The hash covers the serialized bytes, so it saves bandwidth and client
    re-render on an unchanged body, not the work of building it (ruling 1).
    """
    payload = json.dumps(body).encode()
    return payload, '"' + hashlib.sha256(payload).hexdigest()[:32] + '"'


def _if_none_match(header, etag) -> bool:
    """RFC 9110 weak comparison of ``If-None-Match`` (a list, ``*``, ``W/`` prefixes) with ``etag``."""
    if not header or not etag:
        return False
    def opaque(tag):
        tag = tag.strip()
        return tag[2:] if tag.startswith("W/") else tag
    wanted = opaque(etag)
    return any(item.strip() == "*" or opaque(item) == wanted for item in header.split(","))


_MAX_REVISION = 2**63 - 1


def _github_query(path, kind):
    """Bound local dashboard filters; never forward arbitrary upstream paths."""
    values = parse_qs(urlsplit(path).query, keep_blank_values=True, max_num_fields=20)
    allowed = {"repository", "number"} if kind == "issue" else {"page", "per_page", "search", "since"}
    if kind == "issues":
        allowed |= {"repository", "tab"}
    if kind == "projects":
        allowed = {"project", "since"}
    if values.keys() - allowed or any(len(items) != 1 for items in values.values()):
        raise ValueError("invalid GitHub query")
    query = {key: items[0] for key, items in values.items()}
    if kind == "projects":
        if "project" in query and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", query["project"]):
            raise ValueError("invalid GitHub project")
        if "since" in query:
            if not re.fullmatch(r"[0-9]{1,19}", query["since"]) or int(query["since"]) > _MAX_REVISION:
                raise ValueError("invalid GitHub revision")
            query["since"] = int(query["since"])
        return query
    repository = query.get("repository")
    if repository is not None:
        name = repository.removeprefix("github.com/")
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", name)
                or name.split("/")[1] in {".", ".."}):
            raise ValueError("invalid GitHub repository")
        query["repository"] = "github.com/" + name.lower()
    integer_fields = {"number": (None, 2147483647)} if kind == "issue" else {
        "page": (1, 1000000), "per_page": (50, 100)}
    for key, (default, maximum) in integer_fields.items():
        value = query.get(key, default)
        if value is None or not re.fullmatch(r"[0-9]{1,10}", str(value)):
            raise ValueError("invalid GitHub pagination or issue number")
        number = int(value)
        if not 1 <= number <= maximum:
            raise ValueError("invalid GitHub pagination or issue number")
        query[key] = number
    if kind == "issue":
        if not repository:
            raise ValueError("GitHub repository required")
        return query
    if "since" in query:
        if not re.fullmatch(r"[0-9]{1,19}", query["since"]) or int(query["since"]) > _MAX_REVISION:
            raise ValueError("invalid GitHub revision")
        query["since"] = int(query["since"])
    query.setdefault("search", "")
    if len(query["search"]) > 300 or any(ord(char) < 32 or ord(char) == 127 for char in query["search"]):
        raise ValueError("invalid GitHub search")
    if kind == "issues":
        query.setdefault("tab", "all")
        if query["tab"] not in {"all", "autospec", "preparation", "in_progress", "blocked"}:
            raise ValueError("invalid GitHub issue tab")
    return query
