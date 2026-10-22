import pytest

from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def test_fabric_preference_registration_survives_restore():
    repo = ControlPlaneRepo()
    repo.upsert_custom_model('summaries', 'http://cpu:8080', 'qwen-small', 'summaries',
                             fabric_model='qwen-small', context_window=8192)
    restored = ControlPlaneRepo()
    restored.import_state(repo.export_state())
    model = restored.llm_config()['custom_models'][0]
    assert model['fabric_model'] == 'qwen-small'
    assert model['context_window'] == 8192
    assert model['base_url'] == 'http://cpu:8080'
    assert model['project'] == 'summaries'


@pytest.mark.parametrize('model,window', [('', 8192), (123, 8192), ('bad\nname', 8192),
    ('x' * 257, 8192), ('qwen', True), ('qwen', '8192'), ('qwen', 0), ('qwen', 1048577)])
def test_fabric_policy_rejects_invalid_identity_or_context(model, window):
    repo = ControlPlaneRepo()
    with pytest.raises(ValueError):
        repo.upsert_custom_model('summaries', 'http://cpu:8080',
                                 fabric_model=model, context_window=window)
    assert not repo.list_custom_models()


@pytest.mark.parametrize('kind', ['custom_model', 'llm_key', 'token_usage'])
def test_generic_ingestion_cannot_forge_fabric_policy_or_credentials(kind):
    repo = ControlPlaneRepo()
    with pytest.raises(ValueError, match='not accepted'):
        IngestService(repo).ingest({'entity_type': kind, 'entity_id': 'summaries',
                                   'event_type': f'{kind}.upserted', 'payload': {}})
    assert repo.stream(kind, 'summaries') == []
