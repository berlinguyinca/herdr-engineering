"""GateService: the review gate's poll loop.

Each ``tick`` walks every enrolled repo (``config.repos``; nothing else is ever
touched) and every open, non-draft PR in it:

1. New head SHA -> commit status ``pending`` FIRST (if that fails nothing else
   happens for the PR this tick), then ``store.on_push``,
   ``queue.cancel(key, except_sha=sha)`` and the author lookup.
2. ``CI_PENDING`` -> ``ci_verdict_for`` over the CI rows for this repo: green ->
   ``store.on_ci_green`` + ``queue.enqueue``; red -> ``store.on_ci_red`` + status
   ``failure``; otherwise wait.
3. When THIS tick moves the PR into ``CI_GREEN`` (``on_ci_green`` returned
   True) -> ``queue.rearm`` (a fresh job even after a done/superseded one for
   the same sha: operator resume without a push, head A -> B -> A). Already
   ``CI_GREEN`` / ``REVIEW_QUEUED`` -> plain ``enqueue`` (dedupe; ``None`` is
   normal). If that returns ``None`` and no live job exists for (key, sha) the
   status says "cannot queue review" instead of "review queued". The store has
   no event that produces ``REVIEW_QUEUED``, so after enqueueing the state
   stays ``CI_GREEN``, which ``on_review_started`` accepts. ``REVIEWING``
   with no live (queued/claimed) job for (key, sha), e.g. a PR closed during
   its review and reopened at the same head, -> ``queue.rearm``.
4. ``APPROVED`` -> ``decide_merge``: ``merge`` -> status ``success``, then
   ``app.merge_pr(repo, number, head_sha)``; only a True result records
   ``store.on_merged``. False (head moved / not mergeable) or a raise leaves
   the state alone, sets status back to ``pending`` ("retrying at <time>") and
   starts a per-(key, sha) exponential backoff on the injected clock (60 s,
   doubling, capped at 900 s). While backing off neither ``success`` nor
   ``merge_pr`` is repeated, so a stuck PR cannot flap statuses (GitHub caps
   statuses per SHA+context). ``app.update_branch`` is tried once per head SHA.
   ``reject`` -> ``failure``; ``wait`` -> ``pending``.
5. ``CHANGES_REQUESTED``, ``CI_RED`` and ``ESCALATED`` hold ``failure`` (the
   description says why); only ``store.resume`` clears an escalation.
6. In those three states the optional ``notifier`` is called on every tick
   (it is idempotent per head SHA and channel); its errors are logged by PR
   key only and never block the gate. Each tick starts with
   ``notifier.begin_tick()``: at most one external command launch per tick.

Decisions and limits:
- No approval carry-over. A correct carry-over compares each head's diff
  against the merge base with the target branch, which needs a base SHA;
  ``PrInfo`` has only ``base_ref`` (a branch name, which ``patch_id`` rejects)
  and comparing ``approved_sha...new_head`` includes base commits after a
  rebase. So every new head SHA is re-reviewed, ``app.patch_id`` is never
  called and ``PipelineStore.carry_over_approval`` is unused (fail-safe).
- "Branch behind base" is not detectable from ``PrInfo``; ``update_branch`` is
  only tried on the "approved but merge returned False" path. A successful
  update creates a new head, which is then fully re-reviewed. General
  behind-base detection is left to a later plan.
- ``enrollment.required_checks`` (GitHub check names) is validated config this
  service does not consume. The merge decision uses one ``CheckRun`` named
  ``ci:<event>`` per ``enrollment.required_events``, built from the Woodpecker
  rows.
- CI rows: ``ci_snapshot()`` (e.g. ``CIMonitor.snapshot(start=False)``) is read
  once per tick. ``pipelines`` is keyed by Woodpecker ``full_name`` (or
  ``"name (host #id)"`` when names collide across forges). A row belongs to
  ``github.com/<owner>/<name>`` when its ``repository`` equals ``<owner>/<name>``
  (case-insensitive) and its ``forge_url`` host is github.com, or it has no
  ``forge_url`` and sits in the undisambiguated group. A missing repo, a
  ``stale`` or unavailable snapshot, or a raising ``ci_snapshot`` -> pending.
- Authors: resolved through ``registry.author_of(key, head_ref)`` and pinned
  when first seen, but only when known (kind != "unknown"): a first-wins pin of
  "unknown" would permanently disable same-model reviewer exclusion.
  ``PrInfo.author_login`` is never used as an identity.
- Retargeting: an approval is for the base the reviewer saw (the verdict's
  ``base_ref``). An APPROVED PR whose current ``base_ref`` differs, or whose
  approval recorded no base (an old or forged event), is never merged:
  ``store.on_base_changed`` sends it back to ``CI_GREEN``, the head is
  re-queued (``queue.rearm``) and the status says "base branch changed,
  re-reviewing". Otherwise a PR reviewed against branch X (holding unreviewed
  commits) and then retargeted to main would merge those commits unreviewed.
  Right before posting ``success`` and merging, the PR is re-read
  (``app.pr_state``): a retarget since the listing takes the same re-review
  path, a moved head skips the merge (the next tick handles it), and a closed
  or draft PR or a failed read skips it with the merge back-off. The remaining
  window is two GitHub round trips: the ``success`` status post and then
  ``merge_pr``, both after that read.
- A PR that leaves the open list (closed, merged elsewhere) has its queued
  review cancelled. Detection uses an in-memory memo of PR numbers per repo,
  so a PR closed while the service was down is not noticed.
- The author is resolved once per (key, head SHA) and cached in memory.
- Statuses are posted only when (state, description) changes for a SHA (kept
  in memory); the first ``pending`` for a new head SHA is always posted.
- Main verification: when a ``main_verifier`` is given, each enrolled repo's
  ``observe(enrollment, rows, stale)`` runs after its PR loop (also when the PR
  listing failed), in its own try/except: a fault there is counted in
  ``main_errors`` and never stops PR handling.
- ``tick`` never raises. Errors are counted per repo / per PR and the loop goes
  on with the next one.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .config import RepoEnrollment, ReviewGateConfig
from .decision import decide_merge
from .github_app import GitHubAppClient, GitHubAppError, PrInfo
from .models import CheckRun, PrKey, State, Verdict
from .queue import ReviewQueue
from .registry import AuthorRegistry
from .store import PipelineStore, PrSnapshot
from .trigger import ci_verdict_for

log = logging.getLogger(__name__)

REPAIR_BRANCH_PREFIX = "herdr-ci-fix/"  # branches the CI fix runner pushes (deploy/control-plane/ci-fix-runner.py)

STATUS_CONTEXT = "herdr/review-gate"
_CHECK_STATUS = {"green": "pass", "red": "fail", "pending": "pending"}
MERGE_BACKOFF_FIRST = 60.0
MERGE_BACKOFF_MAX = 900.0
STOP_TIMEOUT = 5.0
_WEDGED = "review gate: cannot queue review; push a commit"


def _rows_for(snapshot: dict, repo: str) -> list[dict]:
    """Woodpecker rows of every group that belong to ``github.com/<owner>/<name>``."""
    full = repo.split("/", 1)[1].lower()
    out: list[dict] = []
    pipelines = snapshot.get("pipelines") or {}
    if not isinstance(pipelines, dict):
        return out
    for group, rows in pipelines.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or str(row.get("repository", "")).lower() != full:
                continue
            forge = row.get("forge_url")
            if forge:
                try:
                    host = (urlsplit(str(forge)).hostname or "").lower()
                except ValueError:
                    continue
                if host != "github.com":
                    continue
            elif str(group).lower() != full:
                continue
            out.append(row)
    return out


class GateService:
    def __init__(
        self,
        *,
        config: ReviewGateConfig,
        app: GitHubAppClient,
        store: PipelineStore,
        queue: ReviewQueue,
        registry: AuthorRegistry,
        ci_snapshot: Callable[[], dict],
        now: Callable[[], float] = time.time,
        notifier=None,
        main_verifier=None,
        repair_gate=None,
    ):
        self._config = config
        # Optional deterministic policy for PRs from the CI repair flow only (``herdr-ci-fix/`` branches):
        # ``repair_gate(enrollment, pr, checks) -> ("allow"|"wait"|"reject", reasons)``. None (the default) leaves
        # every merge exactly as before. It can only withhold a merge ``decide_merge`` already allowed.
        self._repair_gate = repair_gate
        self._notifier = notifier
        self._main_verifier = main_verifier
        self._app = app
        self._store = store
        self._queue = queue
        self._registry = registry
        self._ci_snapshot = ci_snapshot
        self._posted: dict[tuple[str, str], tuple[str, str]] = {}
        self._update_tried: set[tuple[PrKey, str]] = set()
        self._now = now
        # (key, sha) -> (retry_at, last_delay) after a refused/failed merge.
        self._merge_backoff: dict[tuple[PrKey, str], tuple[float, float]] = {}
        self._authors: dict[tuple[PrKey, str], str | None] = {}
        self._seen: dict[str, set[int]] = {}
        self._stop_timeout = STOP_TIMEOUT
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._last_tick_at: float | None = None
        self._last_counts: dict = {}
        self._last_error: str | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="review-gate", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread is not None:
            thread.join(self._stop_timeout)
            if thread.is_alive():
                log.warning("review gate: tick still running after stop(); thread left as daemon")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a tick error never kills the thread
                log.exception("review gate tick failed")
            self._stop.wait(self._config.poll_seconds)

    # ------------------------------------------------------------------ tick
    def tick(self) -> dict:
        counts = {
            "repos": 0,
            "prs": 0,
            "enqueued": 0,
            "merged": 0,
            "errors": 0,
            "skipped_drafts": 0,
            "main_errors": 0,
        }
        with self._tick_lock:
            try:
                self._tick(counts)
            except Exception as exc:  # noqa: BLE001 - tick never raises
                self._note_error(exc)
                log.exception("review gate tick failed")
                counts["errors"] += 1
            self._last_tick_at = self._now()
            self._last_counts = dict(counts)
        return counts

    def _note_error(self, exc: Exception) -> None:
        """Remember only the GitHubAppError reason (a fixed vocabulary), never message text."""
        reason = getattr(exc, "reason", None)
        if isinstance(exc, GitHubAppError) and isinstance(reason, str):
            self._last_error = reason[:40]

    def status(self) -> dict:
        """Last tick summary for the API: epoch seconds, counts, last GitHub failure reason."""
        return {
            "last_tick_at": self._last_tick_at,
            "counts": dict(self._last_counts),
            "last_error": self._last_error,
        }

    def _tick(self, counts: dict) -> None:
        if not self._config.enabled:
            return
        begin_tick = getattr(self._notifier, "begin_tick", None)
        if callable(begin_tick):
            try:
                begin_tick()  # one external launch per tick
            except Exception:  # noqa: BLE001 - a notifier fault never blocks the gate
                log.warning("review gate: notifier tick reset failed")
        try:
            snapshot = self._ci_snapshot()
            if not isinstance(snapshot, dict):
                raise TypeError("CI snapshot must be a mapping")
            stale = bool(snapshot.get("stale")) or snapshot.get("available") is False
        except Exception:  # noqa: BLE001 - no CI view means nothing is green
            log.warning("CI snapshot unavailable; treating it as stale")
            snapshot, stale = {}, True
        for enrollment in self._config.repos:
            counts["repos"] += 1
            try:
                prs = self._app.open_prs(enrollment.repo)
            except Exception as exc:  # noqa: BLE001 - one repo never stops the others
                self._note_error(exc)
                log.warning("review gate: listing PRs failed for %s", enrollment.repo)
                counts["errors"] += 1
                self._observe_main(enrollment, snapshot, stale, counts)
                continue
            rows = _rows_for(snapshot, enrollment.repo)
            self._forget_closed(enrollment.repo, prs)
            for pr in prs:
                if pr.draft:
                    counts["skipped_drafts"] += 1
                    continue
                counts["prs"] += 1
                try:
                    self._handle(enrollment, pr, rows, stale, counts)
                except Exception as exc:  # noqa: BLE001 - one PR never stops the others
                    self._note_error(exc)
                    log.warning("review gate: %s#%s failed", enrollment.repo, pr.number)
                    counts["errors"] += 1
            self._observe_main(enrollment, snapshot, stale, counts)

    def _observe_main(self, enrollment, snapshot: dict, stale: bool, counts: dict) -> None:
        if self._main_verifier is None:
            return
        try:
            self._main_verifier.observe(enrollment, _rows_for(snapshot, enrollment.repo), stale)
        except Exception:  # noqa: BLE001 - a main-verification fault never stops PR handling
            log.warning("review gate: main verification failed for %s", enrollment.repo)
            counts["main_errors"] += 1

    def _forget_closed(self, repo: str, prs: list[PrInfo]) -> None:
        """Cancel reviews of PRs that left the open list; drop memos of old heads."""
        numbers = {pr.number for pr in prs}
        for number in sorted(self._seen.get(repo, set()) - numbers):
            try:
                self._queue.cancel(PrKey(repo, number))
            except Exception:  # noqa: BLE001 - best effort; retried never, logged
                log.warning("review gate: could not cancel review for %s#%s", repo, number)
        self._seen[repo] = numbers
        live = {(pr.number, pr.head_sha) for pr in prs}
        heads = {sha for _, sha in live}
        self._posted = {k: v for k, v in self._posted.items() if k[0] != repo or k[1] in heads}

        def keep(k: tuple[PrKey, str]) -> bool:
            return k[0].repo != repo or (k[0].number, k[1]) in live

        self._update_tried = {k for k in self._update_tried if keep(k)}
        self._merge_backoff = {k: v for k, v in self._merge_backoff.items() if keep(k)}
        self._authors = {k: v for k, v in self._authors.items() if keep(k)}

    # ---------------------------------------------------------------- per PR
    def _status(self, repo: str, sha: str, state: str, description: str, *, force=False) -> None:
        if not force and self._posted.get((repo, sha)) == (state, description):
            return
        self._app.set_status(repo, sha, state, description, context=STATUS_CONTEXT)
        self._posted[(repo, sha)] = (state, description)

    def _author_model(self, key: PrKey, sha: str, head_ref: str) -> str | None:
        if (key, sha) in self._authors:
            return self._authors[(key, sha)]
        author = self._registry.author_of(key, head_ref)
        if author.kind != "unknown":
            self._registry.pin(key, author)  # first-wins; no-op once pinned
        self._authors[(key, sha)] = author.model
        return author.model

    def _handle(
        self, enrollment: RepoEnrollment, pr: PrInfo, rows: list[dict], stale: bool, counts: dict
    ) -> None:
        repo = enrollment.repo
        key = PrKey(repo, pr.number)
        sha = pr.head_sha
        snap = self._store.snapshot(key)
        if snap.state == State.MERGED:
            return
        if snap.head_sha != sha:
            self._status(repo, sha, "pending", "review gate: new head, waiting for CI", force=True)
            self._store.on_push(key, sha)
            self._queue.cancel(key, except_sha=sha)
            self._author_model(key, sha, pr.head_ref)
            snap = self._store.snapshot(key)
        if snap.head_sha != sha:  # store refused the push (e.g. merged meanwhile)
            return

        state = snap.state
        rearmed = False
        failures: tuple[str, ...] = ()
        if state == State.CI_PENDING:
            verdict = ci_verdict_for(sha, rows, enrollment, snapshot_stale=stale)
            if verdict.result == "green":
                if self._store.on_ci_green(key, sha):
                    self._queue.rearm(key, sha, self._author_model(key, sha, pr.head_ref))
                    counts["enqueued"] += 1
                    rearmed = True
                snap = self._store.snapshot(key)
                state = snap.state
            elif verdict.result == "red":
                self._store.on_ci_red(key, sha, verdict.failures)
                snap = self._store.snapshot(key)
                state = snap.state
                failures = tuple(verdict.failures)
                if state != State.ESCALATED:
                    self._status(repo, sha, "failure", "CI failed: " + ", ".join(verdict.failures))
                    self._notify(key, snap, pr.head_ref, failures)
                    return
            else:
                self._status(repo, sha, "pending", "review gate: waiting for CI")
                return
        if state in (State.CI_GREEN, State.REVIEW_QUEUED):
            if not rearmed and not self._ensure_queued(key, sha, pr.head_ref, counts):
                self._status(repo, sha, "failure", _WEDGED)
                return
            self._status(repo, sha, "pending", "review gate: CI green, review queued")
        elif state == State.REVIEWING:
            if not any(j["key"] == str(key) and j["sha"] == sha for j in self._queue.snapshot()):
                # Reopened after its job was cancelled: re-queue the head. A job
                # enqueued between the snapshot and here is cancelled and replaced.
                self._queue.rearm(key, sha, self._author_model(key, sha, pr.head_ref))
                counts["enqueued"] += 1
            self._status(repo, sha, "pending", "review gate: review in progress")
        elif state == State.CI_RED:
            if self._posted.get((repo, sha), ("",))[0] != "failure":  # keep the run list
                self._status(repo, sha, "failure", "CI failed; push a fix")
        elif state == State.CHANGES_REQUESTED:
            n = len(snap.last_findings)
            self._status(repo, sha, "failure", f"review gate: changes requested ({n} findings)")
        elif state == State.ESCALATED:
            reason = snap.reason or "cap reached"
            self._status(
                repo, sha, "failure", f"review gate: escalated ({reason}); operator resume needed"
            )
        elif state == State.APPROVED:
            self._approved(enrollment, pr, key, snap, rows, stale, counts)
        if state in (State.CHANGES_REQUESTED, State.CI_RED, State.ESCALATED):
            self._notify(key, snap, pr.head_ref, failures)

    def _notify(self, key: PrKey, snap: PrSnapshot, head_ref: str, failures) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier.notify(key, snap, head_ref, failures)
        except Exception:  # noqa: BLE001 - a notification never blocks the gate
            log.warning("review gate: notification failed for %s#%s", key.repo, key.number)

    def _ensure_queued(self, key: PrKey, sha: str, head_ref: str, counts: dict) -> bool:
        """Plain (deduping) enqueue; False when no live job exists for (key, sha)."""
        if self._queue.enqueue(key, sha, self._author_model(key, sha, head_ref)) is not None:
            counts["enqueued"] += 1
            return True
        return any(j["key"] == str(key) and j["sha"] == sha for j in self._queue.snapshot())

    def _approved(
        self,
        enrollment: RepoEnrollment,
        pr: PrInfo,
        key: PrKey,
        snap: PrSnapshot,
        rows: list[dict],
        stale: bool,
        counts: dict,
    ) -> None:
        repo, sha = enrollment.repo, pr.head_sha
        if pr.draft:  # never merge a draft (drafts are skipped earlier too)
            return
        if snap.approved_base_ref is None or pr.base_ref != snap.approved_base_ref:
            # Approved for another base (retargeted) or for an unknown one: fail
            # closed and review this head again against the current base.
            self._rereview_base(key, pr, counts)
            return
        checks = []
        for event in enrollment.required_events:
            one = replace(enrollment, required_events=(event,))
            result = ci_verdict_for(sha, rows, one, snapshot_stale=stale).result
            checks.append(CheckRun(name=f"ci:{event}", status=_CHECK_STATUS[result], sha=sha))
        verdict = None
        if snap.state == State.APPROVED and snap.approved_sha == sha:
            verdict = Verdict("PASS", sha=snap.approved_sha, findings=(), reviewer_model="")
        decision = decide_merge(
            head_sha=sha,
            checks=checks,
            required_checks=tuple(f"ci:{e}" for e in enrollment.required_events),
            verdict=verdict,
        )
        if decision.decision == "reject":
            self._status(repo, sha, "failure", "review gate: " + "; ".join(decision.reasons))
            return
        if decision.decision == "wait":
            self._status(repo, sha, "pending", "review gate: " + "; ".join(decision.reasons))
            return
        if self._repair_gate is not None and pr.head_ref.startswith(REPAIR_BRANCH_PREFIX):
            try:
                gate, why = self._repair_gate(enrollment, pr, checks)
            except Exception:  # noqa: BLE001 - an unavailable policy fails closed, never open
                gate, why = "wait", ("repair policy unavailable",)
            if gate != "allow":
                self._status(repo, sha, "failure" if gate == "reject" else "pending", "repair policy: " + "; ".join(why))
                return
        backoff = self._merge_backoff.get((key, sha))
        if backoff is not None and self._now() < backoff[0]:
            return  # the "retrying at" pending status is already posted
        # Fresh single-PR read right before the merge: the listing may be up to
        # a tick old. Before ``success`` so a refused merge never shows success.
        try:
            fresh = self._app.pr_state(repo, pr.number)
        except Exception:
            self._back_off(key, sha, "PR re-read failed")
            raise
        if fresh is None or fresh.draft:
            self._back_off(key, sha, "PR not open or draft")
            return
        if fresh.head_sha != sha:
            return  # head moved: the next tick handles the new head
        if fresh.base_ref != snap.approved_base_ref:
            self._rereview_base(key, fresh, counts)
            return
        self._status(repo, sha, "success", "review gate: approved, CI green")
        try:
            detail = getattr(self._app, "merge_pr_sha", None)
            if detail is not None:
                merge_sha = detail(repo, pr.number, sha)
                merged = merge_sha is not None
            else:
                merged, merge_sha = self._app.merge_pr(repo, pr.number, sha), None
        except Exception:
            self._back_off(key, sha, "merge failed")
            raise
        if merged:
            self._merge_backoff.pop((key, sha), None)
            if self._store.on_merged(key, sha, merge_sha):
                counts["merged"] += 1
            return
        self._back_off(key, sha, "merge refused (head moved / not mergeable)")
        # Known limitation: merge_pr returns only a bool, so a refusal that
        # update-branch cannot fix (405 not mergeable vs 409 head moved) cannot
        # be told apart; update-branch is attempted once per head on any refusal.
        if (key, sha) not in self._update_tried:
            self._update_tried.add((key, sha))
            self._app.update_branch(repo, pr.number, sha)

    def _rereview_base(self, key: PrKey, pr: PrInfo, counts: dict) -> None:
        sha = pr.head_sha
        if self._store.on_base_changed(key, sha, pr.base_ref):
            self._queue.rearm(key, sha, self._author_model(key, sha, pr.head_ref))
            counts["enqueued"] += 1
        self._status(key.repo, sha, "pending", "review gate: base branch changed, re-reviewing")

    def _back_off(self, key: PrKey, sha: str, why: str) -> None:
        previous = self._merge_backoff.get((key, sha))
        delay = MERGE_BACKOFF_FIRST if previous is None else min(previous[1] * 2, MERGE_BACKOFF_MAX)
        retry_at = self._now() + delay
        self._merge_backoff[(key, sha)] = (retry_at, delay)
        when = datetime.fromtimestamp(retry_at, UTC).strftime("%H:%M:%SZ")
        self._revert_to_pending(key.repo, sha, f"review gate: {why}; retrying at {when}")

    def _revert_to_pending(self, repo: str, sha: str, description: str) -> None:
        try:
            self._status(repo, sha, "pending", description)
        except Exception:  # noqa: BLE001 - the caller's outcome matters more
            log.warning("review gate: could not revert status for %s@%s", repo, sha)
