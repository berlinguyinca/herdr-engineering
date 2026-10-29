"""Deployment failure escalation and the repair ledger (deploy-4 Task 8, spec 7.3-7.7, 8, 9).

Every coordinator tick hands its actions to ``DeploymentEscalation.run``. For each
component a ``Halt`` names, the escalation:

1. **Diagnoses** the component's decisive failed slot for the halted release:
   ``deployment_diagnosis.diagnose`` over that event (redacted on entry there; nothing
   here reads unredacted host text). Model calls are clamped to the tick's budget.
2. **Decides one repair** with ``next_repair`` and records it in the REPAIR LEDGER
   (persisted per ``component:release``, durably BEFORE it is applied: a ledger row that
   cannot be persisted means no repair is exposed). A repair round is one decision for
   the whole fleet of that component: a failure starts a new round (and a new attempt)
   only when herdr received it AFTER the last round was decided, so 20 nodes failing in
   one tick are one attempt, not 20, and the ``ATTEMPT_CAP`` holds. The attempt number is
   the count of recorded repairs (retry/restart/rollback) plus one; a round whose
   decision is ``none`` does not advance it. Rounds per release are capped; beyond that,
   failures fold into the last round with no new diagnosis.
3. **Reaches the host** through the permit answer: ``repair_for`` gives
   ``{id, action, attempt, reason, restart?, rollback_to?}`` while the round's failure is
   still the component's current decisive failed slot (a recovered or restarted
   component sees none). The id (``component:release:round``) lets the host act once.
   Hosts read ``repair`` independently of ``permitted`` (a halt withholds permission from
   its own stage). ``rollback_to`` is herdr's own record of the previous good commit (the
   newest commit in the plan's past on which this component had an ``ok`` slot), never
   model output; with none known, a rollback becomes ``none``. The host must confirm it
   holds that version locally (it keeps the last five) and refuse otherwise.
4. **Escalates**, once per ``component:release`` (idempotent across ticks and restarts):

   * software finding: a GitHub issue through an injectable ``create_issue(repo, title,
     body, key, *, timeout=None)`` sink (the signature ``intel_executor`` expects), only
     with ``control_plane.deployment_escalation.issues: true`` (default off). The claim
     is persisted durably before GitHub is called; a claim found without a result after a
     restart is ``unknown`` and is never filed again. The body is redacted, mentions are
     neutralised, and a model-produced summary is labelled as model output. A pull
     request is not generated: ``pr`` stays null (a fix goes through normal CI/review);
   * host finding: a full report (what changed, evidence, what was tried, current state,
     suggested next step) stored on the record, and a Personal Assistant notification
     with ``dedupe_key = component:release`` through an injectable ``assistant_sink``,
     only with ``assistant: true`` (default off). The assistant has no notification API
     yet (its contracts are proposed: ``AssistantNotification{..., dedupe_key}``), so the
     default is the fallback;
   * always (the fallback, internal only): a herdr notification - an attention-inbox item
     derived from the open escalations (``alerts``) - and one shared-feed event per key
     carrying validated fields only (component, release, kind, source, attempt).

Nothing here executes a repair, releases a GPU, or holds a production credential.
"""
from __future__ import annotations

import copy
import dataclasses
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from .deployment_actions import PERMIT_STALE_S
from .deployment_coordinator import _order, _state_of, same_commit
from .deployment_diagnosis import (
    ATTEMPT_CAP,
    LADDER,
    DiagnosisConfig,
    Finding,
    diagnose,
    next_repair,
)
from .deployments_monitor import redact_evidence

log = logging.getLogger("herdr.deployment_escalation")

CONFIG_KEY = "deployment_escalation"
ENTITY_TYPE = "deployment_escalation"
RECORD_EVENT = "DeploymentEscalationRecorded"
#: The shared-feed herdr event (validated fields only); its own entity, not the private stream.
FEED_ENTITY = "deployment_alert"
FEED_EVENT = "DeploymentEscalated"

#: Escalation records kept (LRU by component:release).
MAX_RECORDS = 256
#: Repair rounds per release; later failures fold into the last round.
MAX_ROUNDS = ATTEMPT_CAP + 3
#: Failure keys kept per round (one per instance that failed in it).
MAX_KEYS = 64
MAX_REPORT_EVIDENCE = 2000
MAX_ISSUE_BODY = 6000
MAX_REASON = 200
#: The largest per-tick budget. The next tick starts after the escalation plus the loop's
#: interval, so this plus the interval must stay under the permit's staleness limit.
MAX_BUDGET_S = 45
#: The coordinator loop's interval (``api.DEPLOYMENTS_TICK_S`` asserts it is not larger).
TICK_INTERVAL_S = 30
assert MAX_BUDGET_S + TICK_INTERVAL_S + 10 < PERMIT_STALE_S

_COMPONENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_COMMIT = re.compile(r"[A-Za-z0-9._-]{7,128}")
#: owner/name; each part starts with a letter or digit, so ``..`` can never form a path.
_REPO_PART = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
_REPO = re.compile(_REPO_PART + "/" + _REPO_PART)
_SOURCE = re.compile(r"pattern:[a-z0-9-]{1,40}|model|fallback")
_ISSUE_STATES = ("claimed", "filed", "failed", "unknown")
_ASSIST_STATES = ("claimed", "delivered", "failed", "unknown")


def issue_url_ok(url, repo) -> bool:
    """Only ``https://github.com/<repo>/issues/<n>`` is ever stored or rendered."""
    if not isinstance(url, str) or not isinstance(repo, str):
        return False
    m = re.fullmatch(r"https://github\.com/(" + _REPO_PART + "/" + _REPO_PART + r")/issues/([1-9][0-9]{0,9})", url)
    return bool(m) and m.group(1).lower() == repo.lower()


# ------------------------------------------------------------------- config
@dataclass(frozen=True)
class EscalationConfig:
    """``control_plane.deployment_escalation`` (YAML). Everything with an external side
    effect is off by default."""
    issues: bool = False
    assistant: bool = False
    #: Wall-clock budget for one tick's escalation (model and GitHub calls are clamped to it);
    #: at most ``MAX_BUDGET_S``, so permits stay fresh.
    budget_s: float = 40
    issue_timeout_s: float = 15

    def __post_init__(self):
        for name in ("issues", "assistant"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"deployment_escalation.{name} must be a boolean")
        for name, lo, hi in (("budget_s", 5, MAX_BUDGET_S), ("issue_timeout_s", 1, 30)):
            v = getattr(self, name)
            if type(v) not in (int, float) or not lo <= v <= hi:
                raise ValueError(f"deployment_escalation.{name} must be a number in {lo}..{hi}")

    @classmethod
    def from_mapping(cls, mapping):
        if mapping is None:
            return cls()
        if not isinstance(mapping, dict):
            raise ValueError("deployment_escalation must be a mapping")
        unknown = set(mapping) - {f.name for f in dataclasses.fields(cls)}
        if unknown:
            raise ValueError(f"deployment_escalation has unknown keys: {sorted(unknown)[:5]}")
        return cls(**mapping)


# ------------------------------------------------------------------- helpers
def _bounded(fn, timeout):
    """Run ``fn()`` with a wall-clock bound; raises TimeoutError, or RuntimeError on failure.
    A late answer is dropped (the worker is a daemon thread)."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except IssueRefused as exc:      # our own bounded reason, safe to keep
            box["refused"] = exc
        except Exception:  # noqa: BLE001 - one generic failure; the message may carry server text
            box["error"] = True

    worker = threading.Thread(target=run, name="deploy-escalation", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError("timed out")
    if "refused" in box:
        raise IssueRefused(box["refused"].reason)
    if "error" in box:
        raise RuntimeError("failed")
    return box.get("value")


def _line(text, cap):
    text = "".join(c if c.isprintable() else " " for c in str(text))
    return " ".join(text.split())[:cap]


def _no_mentions(text):
    """GitHub must not notify anyone or link anything from host or model text."""
    return text.replace("@", "@​").replace("#", "#​")


def _tail(text, cap):
    text = redact_evidence(text) if isinstance(text, str) else ""
    return text if len(text) <= cap else "[earlier evidence dropped]\n" + text[-cap:]


def _finding_dict(f: Finding):
    return {"kind": f.kind, "summary": f.summary, "suggested_action": f.suggested_action,
            "source": f.source}


# -------------------------------------------------------------------- ledger
class EscalationLedger:
    """The repair ledger and escalation records, per ``component:release``.

    Each change persists the WHOLE record (latest by sequence wins on replay) through the
    repo's private sink. ``persist_required`` (durable, raises) guards what must not be
    lost: a new repair round and an issue/assistant claim."""

    def __init__(self, *, persist=None, persist_required=None):
        self._persist = persist
        self._persist_required = persist_required
        self._lock = threading.Lock()
        self._records: OrderedDict[str, dict] = OrderedDict()
        self._open: set = set()
        self._seq = 0

    @staticmethod
    def key(component, release):
        return f"{component}:{release}"

    # ---------------------------------------------------------- reads
    def get(self, component, release):
        with self._lock:
            rec = self._find(component, release)
            return copy.deepcopy(rec) if rec is not None else None

    def _find(self, component, release):
        rec = self._records.get(self.key(component, release))
        if rec is not None:
            return rec
        for r in reversed(self._records.values()):
            if r["component"] == component and same_commit(r["release"], release):
                return r
        return None

    def attempts(self, component, release) -> int:
        """Repairs recorded for this release: the attempt count advances only here."""
        rec = self.get(component, release)
        return sum(1 for r in (rec or {}).get("rounds", ()) if r["repair"]["action"] in LADDER)

    def snapshot(self) -> list:
        with self._lock:
            return [{**copy.deepcopy(r), "open": k in self._open} for k, r in self._records.items()]

    def set_open(self, keys):
        """The escalations whose component is still failing (in memory; the tick sets it)."""
        with self._lock:
            self._open = set(keys) & set(self._records)

    def alerts(self) -> list:
        with self._lock:
            return [copy.deepcopy(self._records[k]) for k in self._records if k in self._open]

    # --------------------------------------------------------- writes
    def put(self, record, *, required=False):
        """Store ``record`` (persisted first when ``required``: raising leaves nothing applied)."""
        key = self.key(record["component"], record["release"])
        with self._lock:
            self._seq += 1
            row = {"entity_type": ENTITY_TYPE, "entity_id": key[:200], "event_type": RECORD_EVENT,
                   "sequence": self._seq,
                   "payload": {"kind": "escalation", "seq": self._seq, "record": copy.deepcopy(record)}}
            if required and self._persist_required is not None:
                self._persist_required(copy.deepcopy(row))
            self._records[key] = copy.deepcopy(record)
            self._records.move_to_end(key)
            while len(self._records) > MAX_RECORDS:
                self._records.popitem(last=False)
        if not required and self._persist is not None:
            try:
                self._persist(copy.deepcopy(row))
            except Exception:  # noqa: BLE001 - best effort; the required rows guard the caps
                pass

    # ------------------------------------------------------- replay
    def hydrate(self, rows) -> int:
        def seq_of(r):
            p = r.get("payload") if isinstance(r, dict) else None
            s = p.get("seq") if isinstance(p, dict) else None
            return s if isinstance(s, int) and not isinstance(s, bool) else 0

        applied = 0
        with self._lock:
            for row in sorted((r for r in rows if isinstance(r, dict)), key=seq_of):
                p = row.get("payload")
                if not isinstance(p, dict) or p.get("kind") != "escalation":
                    continue
                self._seq = max(self._seq, seq_of(row))
                rec = _valid_record(p.get("record"))
                if rec is None:
                    continue
                # A claim without its result: the call may or may not have happened. Never again.
                for name in ("issue", "assistant"):
                    if (rec.get(name) or {}).get("state") == "claimed":
                        rec[name] = {**rec[name], "state": "unknown",
                                     "reason": "interrupted by a restart; check before filing by hand"}
                key = self.key(rec["component"], rec["release"])
                self._records[key] = rec
                self._records.move_to_end(key)
                while len(self._records) > MAX_RECORDS:
                    self._records.popitem(last=False)
                applied += 1
        return applied


def _str(v, cap, rx=None):
    return isinstance(v, str) and 0 < len(v) <= cap and (rx is None or rx.fullmatch(v) is not None)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e12


def _valid_round(r):
    if not isinstance(r, dict):
        return None
    f, rep, keys = r.get("finding"), r.get("repair"), r.get("keys")
    if not isinstance(f, dict) or not isinstance(rep, dict) or not isinstance(keys, list):
        return None
    try:
        finding = Finding(kind=f["kind"], summary=_line(redact_evidence(f["summary"]), 400),
                          suggested_action=f["suggested_action"], source=f["source"])
    except (KeyError, TypeError, ValueError):
        return None
    if not _SOURCE.fullmatch(finding.source or "") or rep.get("action") not in (*LADDER, "none"):
        return None
    attempt = r.get("attempt")
    if attempt is not None and (not isinstance(attempt, int) or isinstance(attempt, bool)
                                or not 1 <= attempt <= MAX_ROUNDS):
        return None
    rollback_to = rep.get("rollback_to")
    if rollback_to is not None and not _str(rollback_to, 128, _COMMIT):
        return None
    restart = rep.get("restart")
    if restart is not None and not _str(restart, 64, re.compile(r"[a-z0-9-]{1,64}")):
        return None
    if not _num(r.get("at")) or not all(_str(k, 300) for k in keys):
        return None
    return {"index": r.get("index") if isinstance(r.get("index"), int) else 0,
            "attempt": attempt, "at": float(r["at"]), "keys": keys[:MAX_KEYS],
            "finding": _finding_dict(finding),
            "repair": {"action": rep["action"], "reason": _line(rep.get("reason") or "", MAX_REASON),
                       "restart": restart, "rollback_to": rollback_to}}


def _valid_record(rec):
    if not isinstance(rec, dict):
        return None
    comp, release, repo = rec.get("component"), rec.get("release"), rec.get("repo")
    if not _str(comp, 64, _COMPONENT) or not _str(release, 128, _COMMIT):
        return None
    if repo is not None and not _str(repo, 201, _REPO):
        return None
    rounds = rec.get("rounds")
    if not isinstance(rounds, list) or not rounds or len(rounds) > MAX_ROUNDS:
        return None
    rounds = [_valid_round(r) for r in rounds]
    if any(r is None for r in rounds):
        return None
    out = {"component": comp, "release": release, "repo": repo, "rounds": rounds,
           "stage": rec.get("stage") if isinstance(rec.get("stage"), int) else None,
           "host": rec.get("host") if _str(rec.get("host"), 64) else None,
           "report": None, "issue": None, "assistant": None, "pr": None,
           "alerted": rec.get("alerted") is True}
    report = rec.get("report")
    if isinstance(report, dict):
        clean = {}
        for k in _REPORT_KEYS:
            v = report.get(k)
            if k == "tried" and isinstance(v, list):
                clean[k] = [_line(redact_evidence(t), 200) for t in v[:MAX_ROUNDS] if isinstance(t, str)]
            elif isinstance(v, str):
                clean[k] = redact_evidence(v)[:MAX_REPORT_EVIDENCE + 64]
        out["report"] = clean or None
    issue = rec.get("issue")
    if isinstance(issue, dict) and issue.get("state") in _ISSUE_STATES:
        url = issue.get("url")
        out["issue"] = {"state": issue["state"],
                        "url": url if issue_url_ok(url, repo) else None,
                        "number": issue.get("number") if isinstance(issue.get("number"), int) else None,
                        "reason": _line(issue.get("reason") or "", MAX_REASON) or None}
    assistant = rec.get("assistant")
    if isinstance(assistant, dict) and assistant.get("state") in _ASSIST_STATES:
        out["assistant"] = {"state": assistant["state"],
                            "reason": _line(assistant.get("reason") or "", MAX_REASON) or None}
    return out


_REPORT_KEYS = ("what_changed", "evidence", "tried", "current_state", "next_step", "finding_source")


# ------------------------------------------------------------------- driver
def _previous_good(state, track, component, definition, target):
    """Herdr's own record of the component's previous good commit, or None."""
    for commit in state.plan(track).past:
        if same_commit(commit, target):
            continue
        view = state.slots_for(commit, {component: definition}).get(component) or {}
        if any(s["outcome"] == "ok" for s in (view.get("instances") or {}).values()):
            return commit
    return None


def _failed_slots(state, component, definition, target):
    view = state.slots_for(target, {component: definition}).get(component) or {}
    slots = [(inst, s) for inst, s in (view.get("instances") or {}).items()
             if _state_of(s["outcome"]) == "failed"]
    return sorted(slots, key=lambda kv: (_order(kv[1]), kv[0]))


def _fkey(instance, slot):
    return f"{instance}@{slot['host_ts']}@{slot['outcome']}"[:300]


def _evidence_event(components, component, definition, instance, slot):
    from .deployment_coordinator import _instance_of
    row = (components or {}).get(component) if isinstance(components, dict) else None
    for e in reversed((row or {}).get("timeline") or []):
        if (isinstance(e, dict) and e.get("reported_by") == definition.host
                and e.get("outcome") == slot["outcome"] and e.get("host_ts") == slot["host_ts"]
                and _instance_of(e) == instance):
            return e
    return {"component": component, "outcome": slot["outcome"],
            "evidence": "(the failure event is no longer in the deployment timeline)"}


class DeploymentEscalation:
    """Runs after each coordinator tick; see the module docstring.

    ``ledger``, ``state`` and ``monitor`` are callables returning the repo's current
    objects (a re-import replaces them). ``config`` / ``diagnosis_config`` are read per
    run and fail closed to the defaults (everything off)."""

    def __init__(self, *, ledger, state, monitor, config=None, diagnosis_config=None,
                 diagnose_fn=None, issue_sink=None, assistant_sink=None, alert=None,
                 now=None, clock=None):
        self._ledger = ledger
        self._state = state
        self._monitor = monitor
        self._config = config or EscalationConfig
        self._diagnosis_config = diagnosis_config or DiagnosisConfig
        self._diagnose = diagnose_fn or diagnose
        self.issue_sink = issue_sink
        self.assistant_sink = assistant_sink
        self._alert = alert
        self._now = now or time.time
        self._clock = clock or time.monotonic

    def _configs(self):
        try:
            cfg = self._config()
        except Exception:  # noqa: BLE001 - fail closed: nothing external
            cfg = EscalationConfig()
        try:
            dcfg = self._diagnosis_config()
        except Exception:  # noqa: BLE001 - fail closed: no model call
            dcfg = DiagnosisConfig()
        return (cfg if isinstance(cfg, EscalationConfig) else EscalationConfig(),
                dcfg if isinstance(dcfg, DiagnosisConfig) else DiagnosisConfig())

    # -------------------------------------------------------------- run
    def run(self, results, definitions) -> dict:
        """Escalate every halted component of one tick's ``results`` ({track: [actions]})."""
        from .deployment_coordinator import Halt
        cfg, dcfg = self._configs()
        deadline = self._clock() + cfg.budget_s
        ledger, state = self._ledger(), self._state()
        try:
            components = self._monitor().snapshot()["components"]
        except Exception:  # noqa: BLE001 - evidence then falls back to the slot alone
            components = {}
        stats = {"rounds": 0, "folded": 0, "deferred": 0, "issues": 0, "assistant": 0}
        open_keys = set()
        for track, actions in sorted((results or {}).items()):
            last = actions[-1] if actions else None
            if not isinstance(last, Halt) or last.stage is None or not last.target_commit:
                continue
            for name in last.components:
                d = definitions.get(name)
                if d is None:
                    continue
                try:
                    key = self._component(track, d, last, ledger, state, components, cfg, dcfg,
                                          deadline, stats)
                except Exception as exc:  # noqa: BLE001 - one component never stops the others
                    log.warning("deployment escalation for %s failed: %s", name, type(exc).__name__)
                    key = None
                if key:
                    open_keys.add(key)
        ledger.set_open(open_keys)
        return stats

    def _remaining(self, deadline):
        return deadline - self._clock()

    def _component(self, track, d, halt, ledger, state, components, cfg, dcfg, deadline, stats):
        name, target = d.component, halt.target_commit
        failed = _failed_slots(state, name, d, target)
        if not failed:
            return None                      # failure-evicted only: the operator ack path
        key = EscalationLedger.key(name, target)
        rec = ledger.get(name, target)
        seen = {k for r in (rec or {}).get("rounds", ()) for k in r["keys"]}
        fresh = [(i, s) for i, s in failed if _fkey(i, s) not in seen]
        if rec is not None:
            key = EscalationLedger.key(rec["component"], rec["release"])
        if not fresh:
            self._escalate(rec, d, cfg, deadline, stats, ledger)
            return key
        rounds = (rec or {}).get("rounds", [])
        last_at = rounds[-1]["at"] if rounds else None
        new = [(i, s) for i, s in fresh if last_at is None or s["rx"] > last_at]
        if not new or len(rounds) >= MAX_ROUNDS:
            # this round's other instances (or the round cap): fold, no new diagnosis
            rec = copy.deepcopy(rec)
            r = rec["rounds"][-1]
            r["keys"] = (r["keys"] + [_fkey(i, s) for i, s in fresh])[-MAX_KEYS:]
            ledger.put(rec)
            stats["folded"] += len(fresh)
            self._escalate(ledger.get(name, target), d, cfg, deadline, stats, ledger)
            return key
        remaining = self._remaining(deadline)
        if remaining < 1:
            stats["deferred"] += 1
            return key if rec is not None else None
        inst, slot = new[-1]                 # the newest failure is the round's evidence
        evidence = _evidence_event(components, name, d, inst, slot)
        if dcfg.enabled:
            dcfg = dataclasses.replace(dcfg, timeout_s=max(0.1, min(dcfg.timeout_s, remaining - 0.5)))
        finding = self._diagnose(evidence, config=dcfg)
        attempts = sum(1 for r in rounds if r["repair"]["action"] in LADDER)
        rolled_back = (any(s["outcome"] == "rolled-back" for _, s in failed)
                       or any(r["repair"]["action"] == "rollback" for r in rounds))
        repair = next_repair(finding, d, attempts=attempts + 1, rolled_back=rolled_back)
        rollback_to = None
        if repair.action == "rollback":
            rollback_to = _previous_good(state, track, name, d, target)
            if rollback_to is None:
                repair = dataclasses.replace(repair, action="none",
                                             reason="rollback needed but herdr has no previous good commit")
        advancing = repair.action in LADDER
        round_ = {"index": len(rounds) + 1, "attempt": attempts + 1 if advancing else None,
                  "at": float(self._now()),
                  "keys": [_fkey(i, s) for i, s in fresh][-MAX_KEYS:],
                  "finding": _finding_dict(finding),
                  "repair": {"action": repair.action, "reason": _line(repair.reason, MAX_REASON),
                             "restart": repair.restart if repair.action == "restart" else None,
                             "rollback_to": rollback_to}}
        new_rec = copy.deepcopy(rec) if rec is not None else {
            "component": name, "release": target, "repo": d.track[0] if _REPO.fullmatch(d.track[0]) else None,
            "stage": d.stage, "host": d.host, "rounds": [], "report": None, "issue": None,
            "assistant": None, "pr": None, "alerted": False}
        new_rec["rounds"] = rounds + [round_]
        new_rec["report"] = self._report(new_rec, d, evidence, halt)
        ledger.put(new_rec, required=True)   # raises: no round, no repair exposed
        stats["rounds"] += 1
        self._escalate(ledger.get(name, target), d, cfg, deadline, stats, ledger)
        return key

    # ---------------------------------------------------------- report
    @staticmethod
    def _report(rec, d, evidence, halt):
        last = rec["rounds"][-1]
        frm = evidence.get("from") if isinstance(evidence.get("from"), str) else None
        tried = [f"round {r['index']}: {r['finding']['source']} -> {r['repair']['action']}"
                 + (f" (attempt {r['attempt']})" if r["attempt"] else "") for r in rec["rounds"]]
        return {
            "what_changed": f"{d.component} (stage {d.stage}, host {d.host}): "
                            f"{_line(frm or 'unknown', 128)} -> {rec['release']}",
            "evidence": _tail(evidence.get("evidence"), MAX_REPORT_EVIDENCE),
            "tried": tried,
            "current_state": f"stage {halt.stage} halted; {d.component} failed on this release",
            "next_step": _line(f"{last['finding']['summary']} Repair decided: "
                               f"{last['repair']['action']} ({last['repair']['reason']}).", 700),
            "finding_source": last["finding"]["source"],
        }

    # -------------------------------------------------------- escalate
    def _escalate(self, rec, d, cfg, deadline, stats, ledger):
        if rec is None or not rec["rounds"]:
            return
        kind = rec["rounds"][-1]["finding"]["kind"]
        if not rec.get("alerted"):
            self._feed_alert(rec, ledger)
        if kind == "software" and rec.get("issue") is None and cfg.issues and self.issue_sink is not None:
            self._file_issue(rec, cfg, deadline, stats, ledger)
        if kind == "host" and rec.get("assistant") is None and cfg.assistant and self.assistant_sink is not None:
            self._notify_assistant(rec, cfg, deadline, stats, ledger)

    def _feed_alert(self, rec, ledger):
        rec = copy.deepcopy(rec)
        rec["alerted"] = True
        ledger.put(rec)
        if self._alert is None:
            return
        last = rec["rounds"][-1]
        try:
            self._alert(rec["component"], rec["release"], {
                "component": rec["component"], "release": rec["release"],
                "kind": last["finding"]["kind"], "source": last["finding"]["source"],
                "repair": last["repair"]["action"], "attempt": last["attempt"]})
        except Exception:  # noqa: BLE001 - the attention item is derived from the ledger anyway
            log.warning("deployment escalation feed event could not be recorded")

    def _file_issue(self, rec, cfg, deadline, stats, ledger):
        repo = rec.get("repo")
        remaining = self._remaining(deadline)
        if not repo or remaining < 1:
            return
        key = EscalationLedger.key(rec["component"], rec["release"])
        title, body = issue_text(rec)
        rec = copy.deepcopy(rec)
        rec["issue"] = {"state": "claimed", "url": None, "number": None, "reason": None}
        ledger.put(rec, required=True)       # raises: not claimed, not filed (next tick retries)
        timeout = min(cfg.issue_timeout_s, max(1.0, remaining - 0.5))
        try:
            out = _bounded(lambda: self.issue_sink(repo, title, body, key, timeout=timeout), timeout + 1)
            url, number = (out or {}).get("url"), (out or {}).get("number")
            if not issue_url_ok(url, repo) or not isinstance(number, int) or isinstance(number, bool):
                raise ValueError("bad issue answer")
            rec["issue"] = {"state": "filed", "url": url, "number": number, "reason": None}
        except TimeoutError:
            rec["issue"] = {"state": "unknown", "url": None, "number": None,
                            "reason": "GitHub did not answer in time; check before filing by hand"}
        except IssueRefused as exc:
            rec["issue"] = {"state": "failed", "url": None, "number": None, "reason": exc.reason}
        except Exception:  # noqa: BLE001 - never retried automatically: no duplicate issues
            rec["issue"] = {"state": "unknown", "url": None, "number": None,
                            "reason": "the issue call failed; check before filing by hand"}
        stats["issues"] += 1
        ledger.put(rec)

    def _notify_assistant(self, rec, cfg, deadline, stats, ledger):
        remaining = self._remaining(deadline)
        if remaining < 1:
            return
        dedupe_key = EscalationLedger.key(rec["component"], rec["release"])
        rec = copy.deepcopy(rec)
        rec["assistant"] = {"state": "claimed", "reason": None}
        ledger.put(rec, required=True)
        report = copy.deepcopy(rec["report"])
        try:
            _bounded(lambda: self.assistant_sink(dedupe_key=dedupe_key, report=report),
                     min(15.0, max(1.0, remaining - 0.5)))
            rec["assistant"] = {"state": "delivered", "reason": None}
        except TimeoutError:
            rec["assistant"] = {"state": "unknown", "reason": "the assistant did not answer in time"}
        except Exception:  # noqa: BLE001
            rec["assistant"] = {"state": "failed", "reason": "the assistant refused or failed"}
        stats["assistant"] += 1
        ledger.put(rec)

    # ---------------------------------------------------------- permit
    def repair_for(self, component, target, definition):
        """The repair for the permit answer, or None (see the module docstring, item 3)."""
        if not target:
            return None
        rec = self._ledger().get(component, target)
        if rec is None or not rec["rounds"]:
            return None
        last = rec["rounds"][-1]
        rep = last["repair"]
        if rep["action"] not in LADDER:
            return None
        current = {_fkey(i, s) for i, s in _failed_slots(self._state(), component, definition, target)}
        if not current & set(last["keys"]):
            return None
        out = {"id": f"{component}:{rec['release']}:{last['index']}", "action": rep["action"],
               "attempt": last["attempt"], "reason": rep["reason"]}
        if rep["action"] == "restart":
            out["restart"] = rep["restart"]
        if rep["action"] == "rollback":
            out["rollback_to"] = rep["rollback_to"]
        return out


# ----------------------------------------------------------------- issues
def issue_text(rec):
    """(title, body) for a software finding: only validated fields in the title; the body
    is redacted again, mentions neutralised, a model summary labelled as model output."""
    last = rec["rounds"][-1]
    f = last["finding"]
    src = f["source"]
    label = src.split(":", 1)[1] if src.startswith("pattern:") else (
        "AI diagnosis" if src == "model" else "unclassified")
    title = f"Deployment of {rec['component']} failed on {rec['release'][:12]}: {label}"
    summary = _no_mentions(_line(redact_evidence(f["summary"]), 400))
    if src == "model":
        summary = ("**Model output (AI-generated, unverified; it read redacted evidence only):** "
                   + summary)
    report = rec.get("report") or {}
    evidence = _no_mentions(redact_evidence(report.get("evidence") or "")).replace("~~~", "~ ~ ~")
    tried = "\n".join(f"- {_no_mentions(_line(t, 200))}" for t in report.get("tried") or ())
    body = "\n".join([
        f"<!-- herdr-deployment-escalation {rec['component']}:{rec['release']} -->",
        "Automated report from the herdr deployment coordinator.",
        "",
        f"- Component: `{rec['component']}` (stage {rec.get('stage')}, host `{rec.get('host')}`)",
        f"- Release: `{rec['release']}`",
        f"- Diagnosis source: `{src}`",
        f"- Repair decided: `{last['repair']['action']}` - {_no_mentions(last['repair']['reason'])}",
        "",
        "### Finding",
        summary,
        "",
        "### What was tried",
        tried or "- nothing yet",
        "",
        "### Evidence (redacted, tail)",
        "~~~text",
        evidence or "(none)",
        "~~~",
        "",
        "A fix goes through normal CI and review; herdr deploys nothing from outside `main`.",
    ])
    body = redact_evidence(body)
    if len(body) > MAX_ISSUE_BODY:
        body = body[:MAX_ISSUE_BODY - 20] + "\n~~~\n[truncated]"
    return title, body


class IssueRefused(Exception):
    """GitHub answered and refused (e.g. 403 for a read-only token): recorded, not retried."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class GitHubIssueSink:
    """``create_issue(repo, title, body, key, *, timeout)`` over the GitHub REST API with the
    server-held ``github.token`` (no gh CLI). No redirects, bounded read, the token never in
    an error. Wired only with ``control_plane.deployment_escalation.issues: true``."""

    MAX_RESPONSE = 256 * 1024

    def __init__(self, *, token=None, opener=None):
        import urllib.request
        self._token = token
        from ..github import _NoRedirect
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    def _token_value(self):
        if self._token is not None:
            value = self._token() if callable(self._token) else self._token
        else:
            from ..host_config import host_value
            value = host_value("github.token")
        if not isinstance(value, str) or not value or len(value) > 16384 or not all(32 < ord(c) < 127 for c in value):
            raise IssueRefused("no GitHub token configured")
        return value

    def __call__(self, repo, title, body, key, *, timeout=None):
        import json
        import urllib.error
        import urllib.request
        if not isinstance(repo, str) or not _REPO.fullmatch(repo):
            raise IssueRefused("invalid repository")
        token = self._token_value()
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues", method="POST",
            data=json.dumps({"title": title[:256], "body": body}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/vnd.github+json", "User-Agent": "herdr-engineering"})
        try:
            with self._opener.open(request, timeout=timeout or 15) as response:
                raw = response.read(self.MAX_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (401, 403, 404, 410, 422):
                raise IssueRefused(f"GitHub refused the issue (HTTP {code})") from None
            raise RuntimeError("issue request failed") from None
        except Exception:
            raise RuntimeError("issue request failed") from None
        if len(raw) > self.MAX_RESPONSE:
            raise RuntimeError("issue response too large")
        data = json.loads(raw)
        return {"url": data.get("html_url"), "number": data.get("number")}


# -------------------------------------------------------------- attention
def attention_items(ledger, item):
    """Attention-inbox items (the herdr notification) for the open escalations. ``item`` is
    ``attention_inbox._item``; only validated ledger fields become text."""
    out = []
    for rec in ledger.alerts():
        last = rec["rounds"][-1]
        host = last["finding"]["kind"] == "host"
        issue = rec.get("issue") or {}
        detail = (f"{'Host' if host else 'Software'} cause ({last['finding']['source']}); "
                  f"repair: {last['repair']['action']}"
                  + (f", attempt {last['attempt']} of {ATTEMPT_CAP}" if last["attempt"] else "")
                  + (f"; issue #{issue['number']}" if issue.get("number") else "")
                  + ("; full report on the Deployments page" if host else ""))
        out.append(item(f"deploy:{rec['component']}:{rec['release'][:40]}", "deployment_escalation",
                        "critical" if host else "warning",
                        f"Deployment needs attention · {rec['component']} {rec['release'][:12]}",
                        detail, "#/deployments", "deployments",
                        f"{len(rec['rounds'])}:{issue.get('state') or '-'}", seen=last["at"],
                        action="Open deployments"))
    return out
