"""Project-intelligence board proposals on the Rakazo phone: pending ones become ``ask`` cards, answers go through the
dashboard's own decision path.

The board (:class:`.intel_assistant.ActionBoard`) and the executor stay the only owners of a proposal. This module
only (1) decides whether this bridge account may see and decide proposals, (2) turns each pending proposal into one
card for the account's first bot thread, and (3) turns a ``threads/answer`` into
:meth:`.project_intelligence_api.IntelligenceRoutes.decide_proposal`, the function the HTTP route uses too.

Fail closed. Nothing is shown unless project intelligence is on with the ``suggest`` flag and no kill switch, the
account has an ``assistant_viewers["<tenant>/<principal>"]`` scope (the scope its own assistant files proposals
under; ``default_org_id`` is for the open listener and is never used here), and the account may decide: the server is
in single-operator mode, or the principal is listed in ``approvers``. The reason is logged once per account, with no
ids, titles or payload text.

Card text is built ONLY from payload fields that pass the executor's own validation (repository, run number, a title
of bounded length). A proposal whose fields fail validation gets a generic line; ``propose_issue`` bodies and
free-form payload keys are never shown.
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from . import project_intelligence as pi
from .assistant import redact_secrets
from .intel_assistant import Proposal
from .intel_executor import _OWNER_NAME, _REF
from .rakazo_events import snippet

log = logging.getLogger(__name__)

_SOURCE = re.compile(r"[a-z_]{1,40}")
_PREDICATE = re.compile(r"[a-z_]{1,40}")
_TITLE = re.compile(r"[^\x00-\x1f]{1,200}")


_SECRETISH = re.compile(r"(?i)gh[pousr]_|github_pat_|\bsk-|\bakia[0-9a-z]{8}|xox[abprs]-|bearer\s|-----begin|[A-Za-z0-9+/_\-]{32,}")


def _looks_secret(text: str) -> bool:
    """Free text is shown only when it carries nothing token-shaped (or keyword-marked) that could be a credential."""
    return bool(_SECRETISH.search(text)) or redact_secrets(text) != text


def _field(payload: dict, key: str, pattern: re.Pattern) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _run(payload: dict) -> int | None:
    run = payload.get("run")
    return run if isinstance(run, int) and not isinstance(run, bool) and run >= 1 else None


def proposal_title(p: Proposal) -> str:
    """One human-readable line from validated fields only."""
    pl = p.payload if isinstance(p.payload, dict) else {}
    repo = _field(pl, "repo", _OWNER_NAME if p.action == "ci_rerun" else _REF)
    if repo is not None and p.action == "ci_rerun" and any(part in (".", "..") for part in repo.split("/")):
        repo = None
    run = _run(pl)
    text = None
    if p.action == "ci_rerun" and repo and run:
        text = f"CI failed? Re-run {repo} run {run}"
    elif p.action == "request_ci_repair" and repo and run:
        text = f"CI failed? Ask for a repair of {repo} run {run}"
    elif p.action == "propose_issue" and repo and (title := _field(pl, "title", _TITLE)) and not _looks_secret(title):
        text = f"Open an issue in {repo}: {snippet(title, 100)}"
    elif p.action == "link_entities":
        a, b, rel = _field(pl, "from_ref", _REF), _field(pl, "to_ref", _REF), _field(pl, "predicate", _PREDICATE)
        if a and b and rel:
            text = f"Link {a} to {b} ({rel})"
    elif p.action == "pause_ingestion" and (source := _field(pl, "source", _SOURCE)):
        text = f"Pause ingestion from {source}"
    return redact_secrets(text) if text else f"Approve a {p.action.replace('_', ' ')} request"


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%H:%M UTC")


class Busy(Exception):
    """Too many approved proposals are executing at once; nothing was decided."""


MAX_EXECUTING = 4


class ProposalDesk:
    def __init__(self, *, monotonic: Callable[[], float], poll_seconds: float = 5.0, min_interval: float = 1.0,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.routes = None                      # IntelligenceRoutes; set by the API glue, None = surface nothing
        self.poll_seconds = poll_seconds          # between reads while a stream is open
        self.min_interval = min_interval          # floor for every read, forced or not: no client can trigger a scan loop
        self._monotonic, self._clock = monotonic, clock
        self._warned: set[tuple[str, str]] = set()
        self._polled: dict[str, float] = {}
        self._decide_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_EXECUTING)    # executions in flight; no queue behind them
        self._workers: set[threading.Thread] = set()
        self._closed = False

    # access ----------------------------------------------------------------------------------------------------
    def _warn_once(self, user: str, reason: str) -> None:
        if (user, reason) not in self._warned:
            self._warned.add((user, reason))
            log.warning("rakazo bridge: proposals are not shown to %s: %s", user, reason)

    def access(self, tenant: str, user: str):
        """``(viewer, single, execute_on)`` when this account may see and decide proposals, else None (fail closed)."""
        routes = self.routes
        if routes is None:
            return None
        cfg, raw = routes._cfg()
        if not cfg["enabled"] or not cfg["suggest"] or cfg["kill_switch"]:
            return None
        viewer = routes.assistant_viewer(tenant, user)
        if viewer is None:
            self._warn_once(user, "no project-intelligence scope (assistant_viewers) is configured for this account")
            return None
        single = bool(routes.single_operator_fn())
        approvers = raw.get("approvers")
        if not single and not (isinstance(approvers, list) and user in [a for a in approvers if isinstance(a, str)]):
            self._warn_once(user, "this account is not in project_intelligence.approvers and the server is not "
                                  "in single-operator mode")
            return None
        return viewer, single, bool(cfg["execute"])

    # cards -----------------------------------------------------------------------------------------------------
    def _card(self, p: Proposal, user: str, execute_on: bool, now: datetime) -> dict[str, Any]:
        state = {"awaiting_approval": "pending", "approved": "approved", "executing": "approved", "executed": "executed", "failed": "failed",
                 "refused": "rejected", "expired": "expired"}.get(p.state)
        state = state or "expired"        # approved/executing with execute on = being run now (see ``running``)
        last = redact_secrets(snippet(p.history[-1], 200)) if p.history else ""
        if p.state == "failed":      # an exception message can carry provider text: only its type reaches the phone
            kind = re.match(r"execution failed: (\w{1,60})", last)
            last = f"Failed ({kind.group(1)}); see the dashboard." if kind else "Failed; see the dashboard."
        title = proposal_title(p)
        outcome = {
            "executed": f"Done: {title}. {last}".strip(),
            "failed": f"Approved, but it did not run: {title}. {last}".strip(),
            "approved": f"Approved: {title}. " + ("Running now." if execute_on else "Execution is off, so nothing was run."),
            "rejected": f"Rejected: {title}.",
            "expired": f"Expired without a decision: {title}.",
            "pending": "",
        }[state]
        detail = (f"Proposal {p.id[:12]}\nExpires {_stamp(p.expires_at)}\n"
                  + ("Approving runs it now." if execute_on else "Execution is off: approving only records permission."))
        note = (f"A request needs your decision: {title}. It expires at {_stamp(p.expires_at)}. "
                "Open the card below to approve or reject it.")
        return {"id": p.id, "sha256": p.payload_sha256, "version": p.resource_version, "state": state,
                "title": title, "detail": detail, "note": note, "outcome": snippet(outcome, 400), "user_id": user,
                "created_at": now, "expires_at": p.expires_at,
                "running": state == "approved" and execute_on}

    def refresh(self, tenant: str, user: str, journal, *, force: bool, bypass: bool = False) -> None:
        """Bring ``journal`` up to date with the board. Call with the journal registry lock held. ``force`` (a request
        that wants it current) waits only ``min_interval``; a stream tick waits ``poll_seconds``; ``bypass`` is for the
        read right after this server recorded a decision."""
        if not bypass:
            now_m = self._monotonic()
            if now_m - self._polled.get(journal.thread_id, -1e9) < (self.min_interval if force else self.poll_seconds):
                return
            self._polled[journal.thread_id] = now_m
        access = self.access(tenant, user)
        if access is None:
            self._withdraw(journal)
            return
        viewer, _single, execute_on = access
        now = self._clock()
        cards, seen = [], set()
        for p in self.routes.visible_proposals(viewer):         # also marks expired ones
            seen.add(p.id)
            cards.append(self._card(p, user, execute_on, now))
        for pid, old in list(journal.proposals.items()):
            if pid in seen or not (old["state"] == "pending" or old.get("running")):      # a running one may have finished
                continue
            try:
                p = self.routes.board.get(pid, viewer.org_id)
            except pi.NotFound:
                p = None
            if p is None or not self._visible(viewer, p):
                cards.append({**old, "state": "expired", "running": False, "outcome": snippet(
                    f"Expired without a decision: {old['title']}.", 400)})
            else:
                cards.append(self._card(p, user, execute_on, now))
        journal.sync_proposals(cards, now)

    def _withdraw(self, journal) -> None:
        """The account lost its standing (kill switch, suggest off, scope removed): cards already shown stop looking
        answerable and their waiting runs end."""
        gone = []
        for old in journal.proposals.values():
            if old["state"] == "pending":
                gone.append({**old, "state": "withdrawn",
                             "outcome": snippet(f"No longer available: {old['title']}.", 400)})
            elif old.get("running"):      # approved and executing: its result can no longer be followed from here
                gone.append({**old, "running": False, "outcome": snippet(
                    f"Approved: {old['title']}. No longer available here; see the dashboard for the result.", 400)})
        if gone:
            journal.sync_proposals(gone, self._clock())

    @staticmethod
    def _visible(viewer, p) -> bool:
        from .project_intelligence_api import proposal_visible
        return proposal_visible(viewer, p)

    # answers ---------------------------------------------------------------------------------------------------
    def answer(self, tenant: str, user: str, journal, message_id: str, run_id: Any, decision: str,
               on_done: Callable[[], None] | None = None) -> dict[str, Any]:
        """``decision`` is "approve" or "reject". Raises ``pi.NotFound``, ``pi.Conflict``, ``ApprovalRefused``,
        ``pi.IntelligenceError`` for the bridge to map. The hash and version come from the card this server showed,
        never from the request."""
        from .project_intelligence_api import ApprovalRefused
        from .rakazo_events import PROPOSAL_PREFIX, proposal_run_id
        pid = message_id[len(PROPOSAL_PREFIX):]
        card = journal.proposals.get(pid)
        if card is None or run_id != proposal_run_id(card):
            raise pi.NotFound("proposal")                         # never shown here, or a tampered/stale run id
        access = self.access(tenant, user)
        if access is None:
            raise ApprovalRefused("this account may not decide project-intelligence proposals")
        viewer, single, execute_on = access
        run_exec = decision == "approve" and execute_on
        if run_exec:           # a repeat answer for a settled proposal is idempotent, so only a new decision needs a slot
            try:
                awaiting = self.routes.board.get(pid, viewer.org_id).state == "awaiting_approval"
            except pi.NotFound:
                awaiting = False
            run_exec = awaiting
        if run_exec and not self._slots.acquire(blocking=False):
            raise Busy("Several approved actions are still running. Try again in a moment.")
        started = False
        try:
            with self._decide_lock:
                try:
                    p = self.routes.decide_proposal(pid, decision, viewer=viewer, actor=user, single=single,
                                                    open_listener=False, payload_sha256=card["sha256"],
                                                    resource_version=card["version"], execute=False)
                except pi.Conflict as exc:
                    current = self.routes.board.get(pid, viewer.org_id).state
                    if (decision == "approve" and current in ("approved", "executing", "executed", "failed")) or \
                            (decision == "reject" and current == "refused"):
                        return {"ok": True, "message": f"Already {'approved' if decision == 'approve' else 'rejected'}; "
                                                       "nothing more was done."}
                    if current in ("approved", "executing", "executed", "failed", "refused", "expired"):
                        raise pi.Conflict(f"This request was already settled ({current.replace('_', ' ')}).") from None
                    raise exc
            decided = p.state          # before the worker can claim it
            if run_exec and decided == "approved":
                started = self._start(pid, viewer.org_id, on_done)
                if not started:
                    log.warning("rakazo bridge: an approved proposal could not be started")
                    self.routes.mark_failed(pid, viewer.org_id, "StartError: could not start", only=("approved",))
                    if on_done is not None:
                        on_done()
                    return {"ok": True, "state": "failed",
                            "message": "Approved, but it could not be started; see the dashboard."}
        finally:
            if run_exec and not started:
                self._slots.release()
        return {"ok": True, "state": decided}

    def _start(self, pid: str, org_id: str, on_done: Callable[[], None] | None) -> bool:
        """Run the executor off the request thread. The decision is already recorded and the executor claims the
        proposal itself, so it runs once. A daemon thread per job, at most ``MAX_EXECUTING``, no queue. When the
        job ends (and the request thread is long gone) ``on_done`` refreshes the journal so the card turns
        Executed/Failed and the outcome is posted even if no stream is open."""
        def work() -> None:
            try:
                self.routes.executor.execute(pid, org_id)
            except Exception as exc:  # noqa: BLE001 - state is on the proposal; log the type only
                log.warning("rakazo bridge: an approved proposal ended with %s", type(exc).__name__)
            finally:
                try:      # never leave it "executing": whatever ended the worker, the proposal gets a final state
                    self.routes.mark_failed(pid, org_id, "WorkerAborted: ended without a result", only=("executing",))
                except Exception as exc:  # noqa: BLE001
                    log.warning("rakazo bridge: could not close out a proposal (%s)", type(exc).__name__)
                self._slots.release()
                try:
                    if on_done is not None:
                        on_done()
                except Exception as exc:  # noqa: BLE001
                    log.warning("rakazo bridge: refreshing after an execution failed with %s", type(exc).__name__)
                with self._decide_lock:
                    self._workers.discard(threading.current_thread())

        worker = threading.Thread(target=work, name="rakazo-exec", daemon=True)
        with self._decide_lock:
            if self._closed:
                return False
            self._workers.add(worker)
        try:
            worker.start()
        except RuntimeError:
            with self._decide_lock:
                self._workers.discard(worker)
            return False
        return True

    def close(self, timeout: float = 5.0) -> None:
        """Stop accepting work and give running executions a moment to finish (they are daemons)."""
        with self._decide_lock:
            self._closed = True
            workers = list(self._workers)
        for w in workers:
            w.join(timeout)
