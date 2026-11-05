import json
import os

import pytest

from herdr_engineering.control_plane import projection_compatibility as compatibility
from tests.control_plane.test_projection_checkpoint_e2e import durable  # noqa: F401


def write_policy(path, current='c' * 64, producers=None):
    policy={'format_version':1,'checkpoint_schema':1,'current_code_fingerprint':current,
            'compatible_code_fingerprints':producers if producers is not None else ['a' * 64],
            'reason':'Operational error handling only; stored reducers unchanged'}
    path.write_text(json.dumps(policy))
    return policy


def test_compatibility_is_bound_to_exact_current_build_and_valid_producers(tmp_path, monkeypatch):
    path=tmp_path/'compatibility.json'
    monkeypatch.setattr(compatibility,'MANIFEST_PATH',path)
    write_policy(path)
    assert compatibility.compatible_fingerprints('c' * 64)==('a' * 64,)
    assert compatibility.compatible_fingerprints('d' * 64)==()
    write_policy(path,producers=['not-a-fingerprint'])
    assert compatibility.compatible_fingerprints('c' * 64)==()
    write_policy(path,producers=['c' * 64,'a' * 64,'a' * 64])
    assert compatibility.compatible_fingerprints('c' * 64)==('a' * 64,)


def test_compatibility_policy_is_hashed_except_its_self_reference(tmp_path, monkeypatch):
    path=tmp_path/'compatibility.json'
    monkeypatch.setattr(compatibility,'MANIFEST_PATH',path)
    write_policy(path)
    before=compatibility.fingerprint_policy()
    write_policy(path,current='d' * 64)
    assert compatibility.fingerprint_policy()==before
    write_policy(path,producers=['b' * 64])
    assert compatibility.fingerprint_policy()!=before


def test_malformed_missing_oversized_or_unknown_format_fails_closed(tmp_path, monkeypatch):
    path=tmp_path/'compatibility.json'
    monkeypatch.setattr(compatibility,'MANIFEST_PATH',path)
    assert compatibility.compatible_fingerprints('c' * 64)==()
    for content in ('not-json','[]','{}','x' * (64 * 1024+1),'{"invalid":NaN}'):
        path.write_text(content)
        assert compatibility.compatible_fingerprints('c' * 64)==()
        compatibility.fingerprint_policy()
    policy=write_policy(path)
    policy['checkpoint_schema']=2
    path.write_text(json.dumps(policy))
    assert compatibility.compatible_fingerprints('c' * 64)==()


@pytest.mark.skipif(not os.environ.get('HERDR_PERF_PG_DSN'), reason='dedicated isolated Postgres required')
def test_reviewed_old_producer_is_reused_but_build_and_policy_mismatch_fall_back(
        durable, tmp_path, monkeypatch):  # noqa: F811 - imported pytest fixture
    from herdr_engineering.control_plane.projection_checkpoint import code_fingerprint
    from tests.control_plane.test_projection_checkpoint_e2e import insert

    path=tmp_path/'compatibility.json'
    monkeypatch.setattr(compatibility,'MANIFEST_PATH',path)
    conn,run,recover=durable
    for n in range(12):
        run(insert(conn,f'prefix-{n}',n,{'service_name':'App','host_id':'h','description':'Keep this'}))
    _,_,_=recover(publish=True)
    legacy='9d2aa47a5b170b1ad0740e6769d8998c4040f30a1fbc324ff57c4bc11ef422d6'
    run(conn.execute('UPDATE projection_checkpoints SET code_fingerprint=$1',legacy))
    policy=write_policy(path,producers=[legacy])
    active=code_fingerprint()
    policy['current_code_fingerprint']=active
    path.write_text(json.dumps(policy))
    assert code_fingerprint()==active
    restored,source,_=recover()
    assert source.recovery_info['mode']=='checkpoint'
    assert source.recovery_info['event_rows_scanned']==0
    assert restored.get_service('service-one')['description']=='Keep this'
    run(conn.execute('UPDATE projection_checkpoints SET code_fingerprint=$1','0' * 64))
    _,source,_=recover()
    assert source.recovery_info['mode']=='full_replay'
    run(conn.execute('UPDATE projection_checkpoints SET code_fingerprint=$1',legacy))
    policy['current_code_fingerprint']='f' * 64
    path.write_text(json.dumps(policy))
    _,source,_=recover()
    assert source.recovery_info['mode']=='full_replay'
    policy['current_code_fingerprint']=active
    path.write_text(json.dumps(policy))
    run(conn.execute("UPDATE projection_checkpoints SET policy_fingerprint='other-policy'"))
    _,source,_=recover()
    assert source.recovery_info['mode']=='full_replay'
    assert run(conn.fetchval('SELECT count(*) FROM fabric_events'))==12
