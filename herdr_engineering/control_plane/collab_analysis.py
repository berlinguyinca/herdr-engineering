"""Durable chat-analysis coordination; execution belongs to the registered harness.

Input/output references resolve only to available files in the authorized room.
No object URLs, paths, bytes or execution callbacks cross this boundary. Tool,
parameters and runtime are harness-reported provenance, not execution attestation.
The injected registry supplies live identity validation and publication policy;
its connection must be used exclusively on its owning event loop.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import uuid4

from .collab import CollabError, Principal
from .collab_store import LIFECYCLE_LOCK
from .skill_registry import SkillDraft, SkillRegistry, _canonical


@dataclass(frozen=True)
class AnalysisRequest:
    request_id: str
    room_id: str
    owner_id: str
    requested_by: str
    agent_id: str
    prompt: str
    files: list[dict]
    parent_artifact_id: str | None
    created_at: str
    execution: str = 'harness_only'
    request_key: str | None = None  # Historical requests have no caller key.


@dataclass(frozen=True)
class AnalysisArtifact:
    artifact_id: str
    request_id: str
    room_id: str
    outputs: list[dict]
    provenance: dict
    created_at: str
    execution: str = 'harness_only'
    trust: str = 'untrusted'


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise CollabError(400, 'invalid identifier')
    return value


def _file_ids(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise CollabError(400, 'select between 1 and 100 files')
    for item in value:
        _identifier(item)
    if len(set(value)) != len(value):
        raise CollabError(400, 'duplicate files')
    return list(value)


class CollabAnalysis:
    def __init__(self, registry: SkillRegistry):
        self.registry, self.conn = registry, registry.conn

    async def _authorize(self, principal, room_id, authorize=None):
        await self.registry._principal(principal)
        await self.registry._room(principal, room_id)
        if authorize is not None:
            await authorize(room_id)

    async def _lock(self):
        await self.conn.execute('SELECT pg_advisory_xact_lock_shared($1)', LIFECYCLE_LOCK)

    async def _executor(self, owner_id, agent_id, room_id):
        # A human may assign only their own active agent. No synthesized credential.
        row = await self.conn.fetchrow(
            'SELECT owner_id,status FROM collab_agents WHERE agent_id=$1 FOR SHARE', agent_id)
        grant = await self.conn.fetchrow(
            'SELECT status,actions,expires_at FROM collab_agent_room_attachments '
            'WHERE room_id=$1 AND agent_id=$2 FOR SHARE', room_id, agent_id)
        if (row is None or row['owner_id'] != owner_id or row['status'] != 'active'
                or grant is None or grant['status'] != 'active'
                or not {'read', 'execute'} <= set(grant['actions'])
                or (grant['expires_at'] is not None and grant['expires_at'] <= datetime.now(UTC).timestamp())):
            raise CollabError(404, 'not found')

    async def _files(self, room_id, file_ids):
        rows = await self.conn.fetch(
            "SELECT f.file_id,f.sha256,f.size_bytes FROM collab_room_files f "
            "WHERE f.room_id=$1 AND f.file_id=ANY($2::text[]) AND f.status='available' "
            "AND (f.upload_id IS NULL OR EXISTS (SELECT 1 FROM collab_room_uploads u "
            "WHERE u.room_id=f.room_id AND u.upload_id=f.upload_id AND u.status='available')) FOR SHARE",
            room_id, file_ids)
        by_id = {r['file_id']: dict(r) for r in rows}
        if len(by_id) != len(file_ids):
            raise CollabError(404, 'not found')
        return [by_id[fid] for fid in file_ids]

    async def _check_files(self, room_id, snapshots):
        current = await self._files(room_id, [f['file_id'] for f in snapshots])
        expected = [{k: f[k] for k in ('file_id', 'sha256', 'size_bytes')} for f in snapshots]
        if current != expected:
            raise CollabError(409, 'analysis file content changed')

    async def _audit(self, principal, action, entity_id):
        await self.conn.execute(
            'INSERT INTO collab_audit_events(event_id,actor_id,action,entity_type,entity_id) '
            "VALUES ($1,$2,$3,'analysis',$4)", str(uuid4()), principal.owner, action, entity_id)

    async def create_analysis_request(self, principal: Principal, room_id: str, prompt: str,
                                      file_ids: list[str], request_key=None, *, target_agent_id=None,
                                      parent_artifact_id=None, authorize=None) -> AnalysisRequest:
        """Create once per authenticated creator/key; replay only under current authority."""
        _identifier(request_key)
        _identifier(room_id)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
            raise CollabError(400, 'prompt must contain 1 to 8000 characters')
        selected = _file_ids(file_ids)
        agent_id = _identifier(target_agent_id or (principal.id if principal.is_agent else None))
        if principal.is_agent and agent_id != principal.id:
            raise CollabError(403, 'analysis delegation is not permitted')
        if parent_artifact_id is not None:
            _identifier(parent_artifact_id)
        fingerprint = hashlib.sha256(_canonical({
            'room_id': room_id, 'prompt': prompt, 'file_ids': selected,
            'agent_id': agent_id, 'parent_artifact_id': parent_artifact_id,
        })).hexdigest()
        async with self.conn.transaction():
            await self._lock()
            await self.registry._principal(principal)
            # Serialize absent-row races across processes, not just committed replays.
            await self.conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',
                                    _canonical(['analysis-request', principal.key, request_key]).decode())
            await self._authorize(principal, room_id, authorize)
            await self._executor(principal.owner, agent_id, room_id)
            previous = await self.conn.fetchrow(
                'SELECT request_id,request_fingerprint FROM collab_analysis_requests '
                'WHERE creator_key=$1 AND request_key=$2', principal.key, request_key)
            if previous and previous['request_fingerprint'] != fingerprint:
                raise CollabError(409, 'analysis request key conflicts with original input')
            if parent_artifact_id is not None:
                parent = await self._artifact(principal, parent_artifact_id, authorize)
                if parent.room_id != room_id:
                    raise CollabError(404, 'not found')
            if previous:
                return await self._request(principal, previous['request_id'], authorize)
            files = await self._files(room_id, selected)
            result = AnalysisRequest(str(uuid4()), room_id, principal.owner, principal.key,
                                     agent_id, prompt, files, parent_artifact_id, datetime.now(UTC).isoformat(),
                                     request_key=request_key)
            await self._authorize(principal, room_id, authorize)
            await self.conn.execute(
                'INSERT INTO collab_analysis_requests '
                '(request_id,room_id,owner_id,agent_id,record,creator_key,request_key,request_fingerprint) '
                'VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8)', result.request_id, room_id, principal.owner,
                agent_id, _canonical(asdict(result)).decode(), principal.key, request_key, fingerprint)
            await self._audit(principal, 'analysis.requested', result.request_id)
            return result

    async def _request(self, principal, request_id, authorize=None):
        _identifier(request_id)
        await self.registry._principal(principal)
        row = await self.conn.fetchrow('SELECT * FROM collab_analysis_requests WHERE request_id=$1', request_id)
        if (row is None or row['owner_id'] != principal.owner
                or (principal.is_agent and row['agent_id'] != principal.id)):
            raise CollabError(404, 'not found')
        result = AnalysisRequest(**_decode(row['record']))
        await self._authorize(principal, result.room_id, authorize)
        await self._check_files(result.room_id, result.files)
        if principal.is_agent:
            await self._executor(principal.owner, principal.id, result.room_id)
        await self._authorize(principal, result.room_id, authorize)
        return result

    async def get_analysis_request(self, principal, request_id, *, authorize=None):
        async with self.conn.transaction():
            await self._lock()
            return await self._request(principal, request_id, authorize)

    async def _artifact(self, principal, artifact_id, authorize=None):
        _identifier(artifact_id)
        await self.registry._principal(principal)
        row = await self.conn.fetchrow('SELECT * FROM collab_analysis_artifacts WHERE artifact_id=$1', artifact_id)
        if row is None:
            raise CollabError(404, 'not found')
        request = await self._request(principal, row['request_id'], authorize)
        result = AnalysisArtifact(**_decode(row['record']))
        await self._check_files(request.room_id, result.outputs)
        await self._authorize(principal, request.room_id, authorize)
        return result

    async def get_analysis_artifact(self, principal, artifact_id, *, authorize=None):
        async with self.conn.transaction():
            await self._lock()
            return await self._artifact(principal, artifact_id, authorize)

    @staticmethod
    def _manifest(value):
        try:
            if not isinstance(value, dict) or set(value) != {'outputs', 'tool', 'skill', 'parameters', 'runtime'}:
                raise ValueError()
            encoded = _canonical(value)
            if len(encoded) > 48 * 1024:
                raise ValueError()
            value = json.loads(encoded)
            outputs = value['outputs']
            if not isinstance(outputs, list) or any(
                    not isinstance(o, dict) or set(o) != {'file_id', 'kind'}
                    or o['kind'] not in ('chart', 'table', 'report') for o in outputs):
                raise ValueError()
            _file_ids([o['file_id'] for o in outputs])
            tool, runtime, skill = value['tool'], value['runtime'], value['skill']
            for obj, fields in ((tool, {'name', 'version', 'sha256'}), (runtime, {'name', 'version'})):
                if not isinstance(obj, dict) or set(obj) != fields:
                    raise ValueError()
                for field in fields:
                    _identifier(obj[field])
            if not re.fullmatch('[0-9a-f]{64}', tool['sha256']) or not isinstance(value['parameters'], dict):
                raise ValueError()
            if skill is not None:
                if not isinstance(skill, dict) or set(skill) != {'skill_id', 'version'}:
                    raise ValueError()
                for field in skill:
                    _identifier(skill[field])
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise CollabError(400, 'invalid analysis output manifest') from None

    async def record_analysis_output(self, request_id: str, output_manifest: dict, *,
                                     principal: Principal, authorize=None) -> AnalysisArtifact:
        """The authenticated executing agent is mandatory; an ID alone is no authority."""
        if not principal.is_agent:
            raise CollabError(403, 'only the assigned harness may record output')
        manifest = self._manifest(output_manifest)
        async with self.conn.transaction():
            await self._lock()
            await self.conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',
                                    'analysis-output:' + _identifier(request_id))
            request = await self._request(principal, request_id, authorize)
            if set(o['file_id'] for o in manifest['outputs']) & {f['file_id'] for f in request.files}:
                raise CollabError(400, 'derived outputs must be new files')
            files = await self._files(request.room_id, [o['file_id'] for o in manifest['outputs']])
            # Derived artifacts must have been uploaded by this harness's owner.
            if await self.conn.fetchval(
                'SELECT EXISTS(SELECT 1 FROM collab_room_files WHERE file_id=ANY($1::text[]) '
                'AND uploaded_by IS DISTINCT FROM $2)', [f['file_id'] for f in files], principal.owner):
                raise CollabError(403, 'output files must belong to the executing owner')
            skill = manifest['skill']
            if skill is not None:
                package = await self.registry.get_skill_version(skill['skill_id'], skill['version'], principal=principal)
                skill = {**skill, 'sha256': package.sha256}
            previous = await self.conn.fetchrow(
                'SELECT * FROM collab_analysis_artifacts WHERE request_id=$1', request_id)
            if previous:
                if _decode(previous['output_manifest']) != manifest:
                    raise CollabError(409, 'analysis output already recorded with different content')
                return await self._artifact(principal, previous['artifact_id'], authorize)
            created_at = datetime.now(UTC).isoformat()
            outputs = [{**f, 'kind': o['kind']} for f, o in zip(files, manifest['outputs'], strict=True)]
            provenance = {'request_id': request_id, 'room_id': request.room_id,
                          'requested_by': request.requested_by, 'agent_id': request.agent_id,
                          'prompt': request.prompt, 'parent_artifact_id': request.parent_artifact_id,
                          'file_ids': [f['file_id'] for f in request.files], 'source_files': request.files,
                          'tool': manifest['tool'], 'skill': skill, 'parameters': manifest['parameters'],
                          'runtime': manifest['runtime'], 'attestation': 'harness_reported',
                          'requested_at': request.created_at, 'recorded_at': created_at}
            result = AnalysisArtifact(str(uuid4()), request_id, request.room_id, outputs, provenance, created_at)
            await self._authorize(principal, request.room_id, authorize)
            await self.conn.execute(
                'INSERT INTO collab_analysis_artifacts(artifact_id,request_id,record,output_manifest) '
                'VALUES ($1,$2,$3::jsonb,$4::jsonb)', result.artifact_id, request_id,
                _canonical(asdict(result)).decode(), _canonical(manifest).decode())
            await self._audit(principal, 'analysis.recorded', result.artifact_id)
            return result

    async def promote_analysis_to_skill(self, principal: Principal, artifact_id: str, scope: str,
                                        metadata: dict, *, authorize=None) -> SkillDraft:
        if not isinstance(scope, str) or scope not in {'private', 'room', 'shared'}:
            raise CollabError(400, 'invalid scope')
        if not isinstance(metadata, dict):
            raise CollabError(400, 'invalid skill metadata')
        data = copy.deepcopy(metadata)
        async with self.conn.transaction():
            await self._lock()
            await self.registry._principal(principal, human=True)
            artifact = await self._artifact(principal, artifact_id, authorize)
            await self.registry._publication(principal, scope, artifact.room_id if scope == 'room' else None)
            data['provenance'] = {**artifact.provenance, 'artifact_id': artifact_id,
                                  'outputs': artifact.outputs, 'requested_scope': scope}
            data['room_id'] = artifact.room_id if scope == 'room' else None
            draft = await self.registry.create_skill_draft(principal, data)
            await self._authorize(principal, artifact.room_id, authorize)
            inserted = await self.conn.fetchval(
                'INSERT INTO collab_analysis_promotions(artifact_id,draft_id) VALUES ($1,$2) '
                'ON CONFLICT DO NOTHING RETURNING draft_id', artifact_id, draft.draft_id)
            if inserted:
                await self._audit(principal, 'analysis.promoted', artifact_id)
            return draft
