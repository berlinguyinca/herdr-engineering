"""Evidence-based project and commit links for provider projections.

Forge repository identity is stable across discovered service ports. Explicit
project overrides may group several repositories without conflating their runs.
"""
from __future__ import annotations

import copy
import json
import os
import re
from collections import defaultdict
from urllib.parse import unquote, urlsplit

from .redact import redact_text

_FULL_COMMIT = re.compile(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})\Z")
_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")


def repository_language(value):
    """Accept one bounded provider-reported label, never infer from names."""
    if not isinstance(value, str) or len(value) > 100 or not value.isprintable():
        return None
    value = value.strip()
    if not value or "[REDACTED]" in value or redact_text(value) != value:
        return None
    return value


def cached_github_languages(monitor):
    """Optional cache enrichment must not make CI depend on GitHub availability."""
    lookup = getattr(monitor, "repository_languages", None)
    if not callable(lookup):
        return {}
    try:
        languages = lookup()
        return languages if isinstance(languages, dict) else {}
    except Exception:
        return {}


def canonical_repository(value, *, forge_host=None):
    """Normalize an explicit forge repository URL/identity, never a cwd/name."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        return None
    value = value.strip()
    if value.startswith("/") or any(ord(char) < 32 for char in value):
        return None
    # Git's scp transport is not a URL, but carries an explicit forge host.
    scp = re.fullmatch(r"[^/@:]+@([^/:]+):(.+)", value)
    if scp:
        value = f"ssh://{scp.group(1)}/{scp.group(2)}"
    if "://" not in value:
        first = value.split("/", 1)[0]
        if "." in first and "/" in value:
            value = "https://" + value
        elif isinstance(forge_host, str) and re.fullmatch(r"[A-Za-z0-9.-]+", forge_host):
            value = f"https://{forge_host}/{value}"
        else:
            return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http", "ssh", "git"} or not parsed.hostname:
            return None
        host = parsed.hostname.lower().rstrip(".")
        # Distinct hosted forge origins must not collide. Transport-default ports
        # disappear; a nonstandard forge port is retained as part of its origin.
        port = parsed.port
        defaults = {"https": 443, "http": 80, "ssh": 22, "git": 9418}
        if port and port != defaults[parsed.scheme]:
            host += f":{port}"
    except ValueError:
        return None
    path = unquote(parsed.path).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) < 2 or any(not part or part in {".", ".."} for part in parts):
        return None
    if any(char.isspace() or ord(char) < 32 or char in "?#\\" for char in path):
        return None
    if host == "github.com":
        path = path.lower()  # GitHub owner/repository names are case insensitive.
    return f"{host}/{path}"


def _identities(record):
    return {
        identity for field in ("repository_id", "forge_url", "repository_url", "repo_url", "clone_url", "repository")
        if (identity := canonical_repository(record.get(field), forge_host=record.get("forge_host")))
    }


def _identity(record):
    identities = _identities(record)
    # Conflicting advertised origins must not silently pick one association.
    return next(iter(identities)) if len(identities) == 1 else None


def _commit(record):
    commits = {value.lower() for field in ("commit_sha", "commit", "revision")
               if isinstance(value := record.get(field), str) and _FULL_COMMIT.fullmatch(value)}
    return next(iter(commits)) if len(commits) == 1 else None


def _project_mapping():
    path = os.environ.get("HERDR_ENGINEERING_PROJECT_REPOS_FILE")
    if not path:
        return {}, "default", None
    try:
        with open(os.path.expanduser(path)) as stream:
            text = stream.read(65537)
        if len(text) > 65536:
            raise ValueError("mapping too large")
        raw = json.loads(text)
        if not isinstance(raw, dict) or len(raw) > 1000:
            raise ValueError("mapping must be an object")
        mapping = {}
        for key, project in raw.items():
            identity = canonical_repository(key)
            if not identity or not isinstance(project, str) or not _PROJECT_ID.fullmatch(project):
                raise ValueError("invalid project mapping")
            if identity in mapping and mapping[identity] != project:
                raise ValueError("conflicting project mapping")
            mapping[identity] = project
        return mapping, "configured", None
    except (OSError, ValueError, UnicodeError):
        return {}, "error", "Project repository mapping could not be read or is invalid."


def project_for_repository(repository):
    """Apply the bounded shared-project mapping, defaulting to forge identity."""
    identity = canonical_repository(repository)
    if identity is None:
        return None
    mapping, _, _ = _project_mapping()
    return mapping.get(identity, identity)


def _mission_ref(record):
    mission_id, source = record.get("mission_id"), record.get("source", "herdr")
    if (not isinstance(mission_id, str) or not mission_id or ":" in mission_id
            or not isinstance(source, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", source)):
        return None
    key = mission_id if source == "herdr" else f"{source}:{mission_id}"
    if record.get("mission_key", key) != key:
        return None
    return {"mission_id": mission_id, "source": source, "mission_key": key}


def _session_mission(repo, session):
    """Require the session's persisted relationship, never legacy reverse links."""
    lookup = getattr(repo, "get_mission", None)
    key, source = session.get("mission_key"), session.get("mission_source")
    if (not callable(lookup) or not isinstance(key, str) or not key
            or not isinstance(source, str) or not source or not session.get("mission_id")):
        return None
    resolved = lookup(key)
    if not isinstance(resolved, dict):
        return None
    ref = _mission_ref(resolved)
    if (not ref or ref["mission_key"] != key or ref["source"] != source
            or session["mission_id"] not in {ref["mission_id"], ref["mission_key"]}):
        return None
    return ref


def _related_records(repo):
    indexes = {"sessions": defaultdict(list), "missions": defaultdict(list)}
    missions = repo.list_missions()
    for kind, records in (("sessions", repo.list_sessions()), ("missions", missions)):
        for record in records:
            identity = _identity(record)
            commit = _commit({"commit_sha": record.get("commit_sha")})
            id_field = "session_id" if kind == "sessions" else "mission_id"
            if not identity or not commit or not record.get(id_field):
                continue
            key = (identity, commit)
            ref = _mission_ref(record) if kind == "missions" else {id_field: record[id_field]}
            if ref and ref not in indexes[kind][key]:
                indexes[kind][key].append(ref)
            if kind == "sessions":
                mission = _session_mission(repo, record)
                if mission and mission not in indexes["missions"][key]:
                    indexes["missions"][key].append(mission)
    return indexes


def annotate_ci_history(view, repo, *, repository_languages=None):
    """Apply the same exact repository/commit associations to flat history."""
    result = annotate_ci({**view, "pipelines": {"history": view.get("pipelines", [])}}, repo,
                         repository_languages=repository_languages)
    result["pipelines"] = result["pipelines"].get("history", [])
    return result


def annotate_ci(view, repo, *, repository_languages=None):
    """Return a copied CI snapshot with project metadata and exact commit links.

    Related records need an explicit forge repository and full SHA-1/SHA-256
    commit. Session sequence revisions, branch names, cwd and ports are not
    evidence of a revision relationship. Missions can also be reached through an
    exact-matching session and its recorded, namespace-verified membership. No
    control-plane records are mutated.
    """
    result = copy.deepcopy(view)
    mapping, status, error = _project_mapping()
    result.update(project_mapping_status=status, project_mapping_error=error)
    related = _related_records(repo)
    repository_languages = repository_languages or {}
    by_id, by_name = defaultdict(list), defaultdict(list)
    projects = defaultdict(set)
    for record in result.get("repos", []):
        identity = _identity(record)
        language = repository_language(record.get("language"))
        language_source = "provider_repository" if language else "unknown"
        if language is None and identity and identity.startswith("github.com/"):
            language = repository_language(repository_languages.get(identity))
            if language:
                language_source = "github_primary_language"
        record.update(language=language, language_source=language_source)
        provider_id = str(record["id"]) if record.get("id") is not None else None
        record.update(repository_id=identity, provider_repo_id=provider_id,
                      forge_host=identity.split("/", 1)[0] if identity else None,
                      project_id=mapping.get(identity, identity))
        if provider_id:
            by_id[provider_id].append(record)
        if record.get("repository"):
            by_name[record["repository"]].append(record)
        if identity:
            projects[record["project_id"]].add(identity)
    for group, rows in result.get("pipelines", {}).items():
        for row in rows:
            provider_id = row.get("provider_repo_id", row.get("repo_id"))
            if provider_id is not None:
                candidates = by_id.get(str(provider_id), [])
            elif str(group) in by_id:
                candidates = by_id[str(group)]
            else:
                candidates = by_name.get(row.get("repository") or group, [])
            record = candidates[0] if len(candidates) == 1 else {}
            identity = record.get("repository_id")
            # Woodpecker pipeline forge_url links to the PR or commit, while
            # repository inventory forge_url identifies the repository itself.
            forge_identity = canonical_repository(row.get("forge_url"), forge_host=row.get("forge_host"))
            context = re.fullmatch(r"(github\.com/[^/]+/[^/]+)/(?:pull/[1-9][0-9]*|commit/(?:[a-f0-9]{40}|[a-f0-9]{64}))", forge_identity or "")
            row_identities = _identities({**row, "forge_url": context.group(1)} if context else row)
            language_record = record if not row_identities or row_identities == {identity} else {}
            row.update(language=language_record.get("language"),
                       language_source=language_record.get("language_source", "unknown"))
            if record:
                row.update(repository_id=identity, provider_repo_id=record.get("provider_repo_id"),
                           forge_host=record.get("forge_host"), project_id=record.get("project_id"))
            else:
                # Current inventory may not include a retained historical run's
                # repository. Enrichment must not erase its stored identifiers.
                for field in ("repository_id", "provider_repo_id", "forge_host", "project_id"):
                    row.setdefault(field, None)
            key = (identity, _commit(row))
            row["related_sessions"] = related["sessions"].get(key, [])
            row["related_missions"] = related["missions"].get(key, [])
    result["projects"] = [{"project_id": project, "repositories": sorted(identities)}
                          for project, identities in sorted(projects.items())]
    return result


_PULL_REF = re.compile(r"^refs/pull/(\d+)/")


def mark_merged_pull_requests(view, gate_store):
    """Set ``pr_merged`` on each CI run whose pull request the review gate recorded as merged.

    The link is exact: the run's ``refs/pull/<n>/...`` ref and repository against the gate's (repository, number) key. A run
    without such a ref, or with no gate record, is left unmarked, so the flag never claims a merge that was not recorded.
    """
    if gate_store is None:
        return view
    try:
        merged = {(snap.key.repo, snap.key.number) for snap in gate_store.snapshots() if getattr(snap.state, "name", "") == "MERGED"}
    except Exception:  # noqa: BLE001 - an unreadable gate store must not break the CI page
        return view
    if not merged:
        return view
    for rows in view.get("pipelines", {}).values():
        for row in rows:
            match = _PULL_REF.match(str(row.get("ref") or ""))
            if match and (row.get("repository"), int(match.group(1))) in merged:
                row["pr_merged"] = True
    return view
