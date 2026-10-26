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


@pytest.mark.parametrize('kind', ['custom_model', 'llm_key'])
def test_generic_ingestion_cannot_forge_fabric_policy_or_credentials(kind):
    repo = ControlPlaneRepo()
    with pytest.raises(ValueError, match='not accepted'):
        IngestService(repo).ingest({'entity_type': kind, 'entity_id': 'summaries',
                                   'event_type': f'{kind}.upserted', 'payload': {}})
    assert repo.stream(kind, 'summaries') == []


def test_generic_ingestion_rejects_empty_token_usage():
    repo = ControlPlaneRepo()
    with pytest.raises(ValueError, match='not accepted without usage fields'):
        IngestService(repo).ingest({'entity_type': 'token_usage', 'entity_id': 'summaries',
                                   'event_type': 'token_usage.recorded', 'payload': {}})
    assert repo.stream('token_usage', 'summaries') == []
    assert repo.usage_analytics()['tokens']['total'] == {}


def test_token_usage_ingestion_cannot_change_fabric_policy_or_credentials():
    repo = ControlPlaneRepo()
    repo.upsert_custom_model('summaries', 'http://cpu:8080', 'qwen-small', 'summaries',
                             fabric_model='qwen-small', context_window=8192)
    repo.create_project_key('summaries')
    config = repo.llm_config()
    result = IngestService(repo).ingest({
        'entity_type': 'token_usage', 'entity_id': 'summary-request',
        'event_type': 'token_usage.recorded', 'payload': {
            'request_id': 'summary-request', 'model': 'qwen-small',
            'input_tokens': 7, 'output_tokens': 3, 'cache_write_tokens': 2,
            'base_url': 'http://attacker.invalid', 'fabric_model': 'forged',
            'context_window': 1, 'key_hash': 'forged',
        },
    })
    assert result['entity_type'] == 'token_usage'
    assert result['entity_id'] == 'summary-request'
    usage = result['usage']
    assert (usage['input_tokens'], usage['output_tokens'], usage['cache_write_tokens']) == (7, 3, 2)
    assert not {'base_url', 'fabric_model', 'context_window', 'key_hash'} & usage.keys()
    assert repo.llm_config() == config
    events = repo.stream('token_usage', 'summary-request')
    assert len(events) == 1 and events[0]['payload'] == usage
    assert repo.usage_analytics()['tokens']['by_model']['qwen-small']['cache_write_tokens'] == 2
