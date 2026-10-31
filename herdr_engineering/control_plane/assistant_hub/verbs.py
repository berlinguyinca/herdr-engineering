"""Verb catalog and canonical action identity for the assistant hub policy core.

An ``ActionKey`` is the normalized description of one proposed action. Its canonical JSON (NFKC-normalized, lowercased
tool and scope, sorted keys, no whitespace) is hashed into a stable ``key_id`` so grants and vetoes match on identity
rather than on whatever spelling a worker happened to use. ``unknown`` verbs are always OUTWARD (fail closed).
"""

from __future__ import annotations

import enum
import hashlib
import json
import unicodedata
from dataclasses import dataclass


class RiskClass(enum.StrEnum):
    LOCAL_REVERSIBLE = "local_reversible"
    REMOTE_REVERSIBLE = "remote_reversible"
    OUTWARD = "outward"


class FlagClass(enum.StrEnum):
    NONE = "none"
    FORCE = "force"
    HARD = "hard"
    RECURSIVE = "recursive"
    NO_VERIFY = "no_verify"
    MULTIPLE = "multiple"


class Verb(enum.StrEnum):
    EMAIL_DRAFT = "email.draft"
    EMAIL_SEND = "email.send"
    PR_COMMENT = "pr.comment"
    PR_MERGE = "pr.merge"
    DEPLOY_CANARY = "deploy.canary"
    DEPLOY_PROD = "deploy.prod"
    GIT_PUSH = "git.push"
    GIT_FORCE_PUSH = "git.force_push"
    ARTIFACT_CREATE = "artifact.create"
    SHELL_READ_ONLY = "shell.read_only"
    SHELL_MUTATING = "shell.mutating"
    CLUSTER_READ_ONLY = "cluster.read_only"
    CLUSTER_MUTATING = "cluster.mutating"
    FS_DELETE = "fs.delete"
    UNKNOWN = "unknown"


_LOCAL = (Verb.EMAIL_DRAFT, Verb.ARTIFACT_CREATE, Verb.SHELL_READ_ONLY, Verb.CLUSTER_READ_ONLY)
_REMOTE = (Verb.PR_COMMENT, Verb.GIT_PUSH)
VERB_RISK: dict[Verb, RiskClass] = {
    verb: (RiskClass.LOCAL_REVERSIBLE if verb in _LOCAL
           else RiskClass.REMOTE_REVERSIBLE if verb in _REMOTE else RiskClass.OUTWARD)
    for verb in Verb
}

# Ascending severity; STRICTER compares by position here (not by enum declaration order).
_RISK_ORDER = (RiskClass.LOCAL_REVERSIBLE, RiskClass.REMOTE_REVERSIBLE, RiskClass.OUTWARD)
_FLAG_ORDER = (FlagClass.NONE, FlagClass.MULTIPLE, FlagClass.NO_VERIFY, FlagClass.RECURSIVE, FlagClass.FORCE,
               FlagClass.HARD)


def _norm(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


@dataclass(frozen=True)
class ActionKey:
    tool: str
    verb: Verb
    resource_scope: str
    target: str
    flag_class: FlagClass
    risk_class: RiskClass

    def canonical(self) -> str:
        return json.dumps({
            "tool": _norm(self.tool).lower(),
            "verb": _norm(str(self.verb.value)),
            "resource_scope": _norm(self.resource_scope).lower(),
            "target": _norm(self.target).strip(),
            "flag_class": _norm(str(self.flag_class.value)),
            "risk_class": _norm(str(self.risk_class.value)),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def key_id(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()


def STRICTER(a: ActionKey, b: ActionKey) -> ActionKey:  # noqa: N802 - policy-vocabulary name
    """Return the more restrictive key: higher risk class first, then more severe flag; ties keep ``a``."""
    rank_a = (_RISK_ORDER.index(a.risk_class), _FLAG_ORDER.index(a.flag_class))
    rank_b = (_RISK_ORDER.index(b.risk_class), _FLAG_ORDER.index(b.flag_class))
    return b if rank_b > rank_a else a
