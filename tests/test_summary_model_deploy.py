"""Model provisioning is explicit, checksummed and isolated from normal deploys."""
import hashlib
import importlib.util
import io
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/control-plane/summary-model.py'


@pytest.fixture
def model():
    spec = importlib.util.spec_from_file_location('summary_model_deploy', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verified_cache_never_downloads(model, tmp_path, monkeypatch):
    data = b'verified gguf fixture'
    monkeypatch.setattr(model, 'MODEL_SHA256', hashlib.sha256(data).hexdigest())
    target = tmp_path / model.MODEL_FILE
    target.write_bytes(data)
    monkeypatch.setattr(model.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('download'))
    assert model.prepare_model(tmp_path) == target


def test_bad_download_is_not_published_and_previous_cache_survives(model, tmp_path, monkeypatch):
    target = tmp_path / model.MODEL_FILE
    target.write_bytes(b'previous invalid cache')
    monkeypatch.setattr(model.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(b'bad download'))
    with pytest.raises(ValueError, match='checksum'):
        model.prepare_model(tmp_path)
    assert target.read_bytes() == b'previous invalid cache'
    assert list(tmp_path.iterdir()) == [target]


def test_download_is_atomically_installed_private(model, tmp_path, monkeypatch):
    data = b'verified gguf fixture'
    monkeypatch.setattr(model, 'MODEL_SHA256', hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(model.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(data))
    path = model.prepare_model(tmp_path)
    assert path.read_bytes() == data
    assert path.stat().st_mode & 0o777 == 0o600


def test_up_requires_prepare_without_network_or_docker(model, tmp_path, monkeypatch):
    monkeypatch.setattr(model.subprocess, 'run', lambda *a, **k: pytest.fail('docker'))
    monkeypatch.setattr(model.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('network'))
    assert model.main(['up', '--model-dir', str(tmp_path)]) == 1


def test_up_verifies_image_and_external_network_then_waits_without_pulling(model, tmp_path, monkeypatch):
    target = tmp_path / model.MODEL_FILE
    target.write_bytes(b'model')
    monkeypatch.setattr(model, 'MODEL_SHA256', hashlib.sha256(b'model').hexdigest())
    calls = []
    monkeypatch.setattr(model.subprocess, 'run', lambda args, **kw: calls.append((args, kw)) or subprocess.CompletedProcess(args, 0))
    assert model.main(['up', '--model-dir', str(tmp_path), '--project', 'lab-test']) == 0
    assert calls[0][0] == ['docker', 'image', 'inspect', model.IMAGE]
    assert calls[1][0] == ['docker', 'network', 'inspect', 'lab-test_default']
    args, kwargs = calls[2]
    assert args[-9:] == ['up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '120', 'agent-summary-model']
    assert kwargs['env']['HERDR_SUMMARY_MODEL_DIR'] == str(tmp_path)
    assert kwargs['env']['HERDR_SUMMARY_NETWORK'] == 'lab-test_default'


def test_compose_is_pinned_bounded_private_and_outside_default_deploy(model):
    doc = yaml.safe_load((SCRIPT.parent / 'compose.summary.yaml').read_text())
    service = doc['services']['agent-summary-model']
    assert service['image'] == model.IMAGE
    assert service['cpus'] == 4
    assert service['mem_limit'] == '3g'
    assert not service.get('ports')
    assert service['volumes'][0]['read_only']
    assert service['volumes'][0]['bind']['create_host_path'] is False
    assert service['read_only'] is True
    assert doc['networks']['default']['external'] is True
    args = service['command']
    assert args[args.index('--alias') + 1] == 'qwen2.5-1.5b-instruct-q4_k_m'
    assert '--chat-template-kwargs' not in args
    assert args[args.index('--predict') + 1] == '350'
    assert args[args.index('--model') + 1] == '/models/' + model.MODEL_FILE
    assert 'summary' not in (SCRIPT.parent / 'deploy.sh').read_text()
