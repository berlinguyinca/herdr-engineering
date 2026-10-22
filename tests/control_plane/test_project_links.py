"""Repository correlation requires forge identity and exact commit evidence."""
import copy
import json
from types import SimpleNamespace

from herdr_engineering.control_plane.project_links import annotate_ci, canonical_repository

SHA = "abcdef0123456789" * 2 + "abcdef01"


def records(sessions=(), missions=()):
    return SimpleNamespace(list_sessions=lambda: list(sessions), list_missions=lambda: list(missions))


def snapshot():
    return {
        "repos": [{"id": 17, "repository": "org/frontend", "forge_url": "https://github.com/org/frontend",
                   "clone_url": "https://github.com/org/frontend.git"},
                  {"id": 18, "repository": "org/backend", "forge_url": "https://github.com/org/backend"}],
        "pipelines": {"org/frontend": [{"repository": "org/frontend", "provider_repo_id": 17,
                                        "revision": SHA, "run_number": 1}],
                      "org/backend": [{"repository": "org/backend", "provider_repo_id": 18,
                                       "revision": SHA, "run_number": 2}]},
    }


def test_normalizes_git_transports_without_using_service_ports_or_cwd():
    assert canonical_repository("git@github.com:org/frontend.git") == "github.com/org/frontend"
    assert canonical_repository("ssh://git@github.com/org/frontend.git") == "github.com/org/frontend"
    assert canonical_repository("https://GITHUB.COM/org/frontend/") == "github.com/org/frontend"
    assert canonical_repository("github.com/org/frontend") == "github.com/org/frontend"
    assert canonical_repository("org/frontend") is None
    assert canonical_repository("/home/user/org/frontend") is None
    assert canonical_repository("http://localhost:1234") is None


def test_defaults_to_forge_identity_and_preserves_snapshot(monkeypatch):
    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    view = snapshot()
    original = copy.deepcopy(view)
    result = annotate_ci(view, records())
    assert result["repos"][0]["project_id"] == "github.com/org/frontend"
    assert result["repos"][0]["provider_repo_id"] == "17"
    assert result["pipelines"]["org/frontend"][0]["repository_id"] == "github.com/org/frontend"
    assert result["pipelines"]["org/frontend"][0]["related_sessions"] == []
    assert view == original


def test_shared_project_override_groups_separate_repos_without_merging_runs(tmp_path, monkeypatch):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"github.com/org/frontend": "my-project",
                                "github.com/org/backend": "my-project"}))
    monkeypatch.setenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", str(path))
    result = annotate_ci(snapshot(), records())
    assert {row["project_id"] for row in result["repos"]} == {"my-project"}
    assert len(result["pipelines"]) == 2
    assert result["projects"] == [{"project_id": "my-project", "repositories": [
        "github.com/org/backend", "github.com/org/frontend"]}]


def test_same_repo_and_commit_on_different_forges_do_not_cross_link(monkeypatch):
    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    view = snapshot()
    view["repos"].append({"id": 19, "repository": "org/frontend", "forge_url": "https://gitlab.com/org/frontend"})
    view["pipelines"]["org/frontend (gitlab.com #19)"] = [
        {"provider_repo_id": 19, "repository": "org/frontend", "revision": SHA}]
    sessions = [{"session_id": "s1", "repository": "https://github.com/org/frontend.git", "commit_sha": SHA},
                {"session_id": "s2", "repository": "org/frontend", "forge_host": "gitlab.com", "commit_sha": SHA}]
    result = annotate_ci(view, records(sessions))
    assert result["pipelines"]["org/frontend"][0]["related_sessions"] == [{"session_id": "s1"}]
    gitlab = result["pipelines"]["org/frontend (gitlab.com #19)"][0]
    assert gitlab["related_sessions"] == [{"session_id": "s2"}]
    assert gitlab["project_id"] == "gitlab.com/org/frontend"


def test_only_exact_full_commit_and_explicit_repository_link(monkeypatch):
    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    sessions = [{"session_id": "good", "repository_id": "github.com/org/frontend", "commit_sha": SHA},
                {"session_id": "short", "repository_id": "github.com/org/frontend", "revision": SHA[:12]},
                {"session_id": "sequence", "repository_id": "github.com/org/frontend", "revision": 40},
                {"session_id": "cwd", "cwd": "/home/user/frontend", "commit_sha": SHA},
                {"session_id": "different", "repository_id": "github.com/org/frontend", "commit_sha": "f"*40},
                {"session_id": "branch", "repository_id": "github.com/org/frontend", "branch": "main"}]
    missions = [{"mission_id": "m1", "source": "pi", "mission_key": "pi:m1",
                 "repository_id": "github.com/org/frontend", "commit_sha": SHA}]
    result = annotate_ci(snapshot(), records(sessions, missions))
    row = result["pipelines"]["org/frontend"][0]
    assert row["related_sessions"] == [{"session_id": "good"}]
    assert row["related_missions"] == [{"mission_id": "m1", "source": "pi", "mission_key": "pi:m1"}]


def test_ambiguous_same_name_without_provider_id_fails_closed(monkeypatch):
    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    view = snapshot()
    view["repos"].append({"id": 19, "repository": "org/frontend", "forge_url": "https://gitlab.com/org/frontend"})
    view["pipelines"]["org/frontend"][0].pop("provider_repo_id")
    row = annotate_ci(view, records())["pipelines"]["org/frontend"][0]
    assert row["repository_id"] is None
    assert row["project_id"] is None
    assert row["related_sessions"] == []


def test_malformed_mapping_reports_safe_error_and_uses_default_identity(tmp_path, monkeypatch):
    path = tmp_path / "private.json"
    path.write_text('{"secret":"DO_NOT_EXPOSE"')
    monkeypatch.setenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", str(path))
    result = annotate_ci(snapshot(), records())
    assert result["project_mapping_status"] == "error"
    assert "DO_NOT_EXPOSE" not in json.dumps(result)
    assert result["repos"][0]["project_id"] == "github.com/org/frontend"


def test_missing_forge_is_unresolved_even_with_matching_bare_names(monkeypatch):
    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    view = snapshot()
    view["repos"][0].pop("forge_url")
    view["repos"][0].pop("clone_url")
    result = annotate_ci(view, records([{"session_id": "bare", "repository": "org/frontend", "commit_sha": SHA}]))
    assert result["repos"][0]["project_id"] is None
    assert result["pipelines"]["org/frontend"][0]["related_sessions"] == []


def test_conflicting_repository_or_commit_metadata_fails_closed(monkeypatch):
    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    sessions = [
        {"session_id": "conflicting-forge", "repository_id": "github.com/org/frontend",
         "repository": "https://gitlab.com/org/frontend", "commit_sha": SHA},
        {"session_id": "conflicting-commit", "repository_id": "github.com/org/frontend",
         "revision": SHA, "commit_sha": "f" * 40},
    ]
    row = annotate_ci(snapshot(), records(sessions))["pipelines"]["org/frontend"][0]
    assert row["related_sessions"] == []


def test_project_group_does_not_imply_same_commit_relationship(tmp_path, monkeypatch):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"github.com/org/frontend": "app", "github.com/org/backend": "app"}))
    monkeypatch.setenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", str(path))
    result = annotate_ci(snapshot(), records([
        {"session_id": "frontend", "repository_id": "github.com/org/frontend", "commit_sha": SHA}]))
    assert result["pipelines"]["org/frontend"][0]["related_sessions"] == [{"session_id": "frontend"}]
    assert result["pipelines"]["org/backend"][0]["related_sessions"] == []


def test_ingested_session_commit_links_its_resolved_namespaced_mission(monkeypatch):
    from herdr_engineering.control_plane.ingest import IngestService
    from herdr_engineering.control_plane.repo import ControlPlaneRepo

    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    repo = ControlPlaneRepo()
    ingest = IngestService(repo)
    for source in ("herdr", "pi"):
        ingest.ingest({"entity_type": "mission", "entity_id": "MSN-1", "event_type": "mission.started",
                       "source": source, "payload": {"title": source}})
    ingest.ingest({"entity_type": "session", "entity_id": "pi-session", "event_type": "session.started",
                   "source": "pi", "payload": {"mission_id": "MSN-1", "host_id": "lab",
                   "repository_id": "github.com/org/frontend", "commit_sha": SHA}})
    row = annotate_ci(snapshot(), repo)["pipelines"]["org/frontend"][0]
    assert row["related_sessions"] == [{"session_id": "pi-session"}]
    assert row["related_missions"] == [{"mission_id": "MSN-1", "source": "pi", "mission_key": "pi:MSN-1"}]


def test_orphan_session_does_not_infer_mission_from_same_raw_id(monkeypatch):
    from herdr_engineering.control_plane.ingest import IngestService
    from herdr_engineering.control_plane.repo import ControlPlaneRepo

    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    repo = ControlPlaneRepo()
    ingest = IngestService(repo)
    ingest.ingest({"entity_type": "mission", "entity_id": "MSN-1", "event_type": "mission.started",
                   "source": "herdr", "payload": {"title": "unrelated"}})
    ingest.ingest({"entity_type": "session", "entity_id": "pi-orphan", "event_type": "session.started",
                   "source": "pi", "payload": {"mission_id": "MSN-1",
                   "repository_id": "github.com/org/frontend", "commit_sha": SHA}})
    row = annotate_ci(snapshot(), repo)["pipelines"]["org/frontend"][0]
    assert row["related_sessions"] == [{"session_id": "pi-orphan"}]
    assert row["related_missions"] == []


def test_ambiguous_reverse_membership_cannot_choose_mission_namespace(monkeypatch):
    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    missions = [{"mission_id": "m", "source": source, "mission_key": f"{source}:m", "sessions": ["s"]}
                for source in ("pi", "other")]
    repo = records([{"session_id": "s", "mission_id": "m",
                     "repository_id": "github.com/org/frontend", "commit_sha": SHA}], missions)
    repo.get_mission = lambda key: next((m for m in missions if m["mission_key"] == key), None)
    row = annotate_ci(snapshot(), repo)["pipelines"]["org/frontend"][0]
    assert row["related_missions"] == []


def test_derived_mission_requires_exact_namespace_lookup(monkeypatch):
    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    mission = {"mission_id": "m", "source": "pi", "mission_key": "pi:m", "sessions": ["s"]}
    repo = records([{"session_id": "s", "mission_id": "m", "mission_key": "pi:m", "mission_source": "pi",
                     "repository_id": "github.com/org/frontend", "commit_sha": SHA}], [mission])
    repo.get_mission = lambda key: dict(mission, source="other", mission_key="other:m")
    row = annotate_ci(snapshot(), repo)["pipelines"]["org/frontend"][0]
    assert row["related_missions"] == []


def test_persisted_session_namespace_wins_over_legacy_reverse_membership(monkeypatch):
    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    missions = [{"mission_id": "m", "source": source, "mission_key": f"{source}:m", "sessions": ["s"]}
                for source in ("pi", "other")]
    repo = records([{"session_id": "s", "mission_id": "m", "mission_key": "pi:m", "mission_source": "pi",
                     "repository_id": "github.com/org/frontend", "commit_sha": SHA}], missions)
    repo.get_mission = lambda key: next((m for m in missions if m["mission_key"] == key), None)
    row = annotate_ci(snapshot(), repo)["pipelines"]["org/frontend"][0]
    assert row["related_missions"] == [{"mission_id": "m", "source": "pi", "mission_key": "pi:m"}]


def test_legacy_reverse_membership_alone_is_not_namespace_proof(monkeypatch):
    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    mission = {"mission_id": "m", "source": "pi", "mission_key": "pi:m", "sessions": ["s"]}
    repo = records([{"session_id": "s", "mission_id": "m",
                     "repository_id": "github.com/org/frontend", "commit_sha": SHA}], [mission])
    repo.get_mission = lambda key: mission
    row = annotate_ci(snapshot(), repo)["pipelines"]["org/frontend"][0]
    assert row["related_missions"] == []


def test_flat_ci_history_keeps_exact_context_links_without_mutating_coverage():
    from herdr_engineering.control_plane.project_links import annotate_ci_history
    view = snapshot()
    view['pipelines'] = [row for rows in view['pipelines'].values() for row in rows]
    view['coverage'] = {'17': {'exhausted': True}}
    original = copy.deepcopy(view)
    result = annotate_ci_history(view, records(
        sessions=[{'session_id': 's1', 'repository_id': 'github.com/org/frontend', 'commit_sha': SHA}],
        missions=[{'mission_id': 'm1', 'source': 'pi', 'mission_key': 'pi:m1',
                   'repository_id': 'github.com/org/frontend', 'commit_sha': SHA}]))
    assert result['repos'][0]['repository_id'] == 'github.com/org/frontend'
    assert result['pipelines'][0]['related_sessions'] == [{'session_id': 's1'}]
    assert result['pipelines'][0]['related_missions'][0]['mission_key'] == 'pi:m1'
    assert result['pipelines'][1]['related_sessions'] == []
    assert result['pipelines'][1]['related_missions'] == []
    assert result['coverage'] == original['coverage']
    assert view == original
