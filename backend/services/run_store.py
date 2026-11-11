"""Transactional storage for sessions, immutable artifacts, steps and replayable events."""
import json
import time
from copy import deepcopy
from sqlalchemy import select
from database.models import (MultiAgentSession, ArtifactRecord, SourceRecord, RunEvent,
    RunStep, AgentRun, SchemaMigration, SubtaskRecord)
from contracts.storage import ArtifactEnvelope
from contracts.artifacts import Subtask

TERMINAL = {'completed', 'waiting', 'degraded', 'failed', 'cancelled', 'insufficient_data'}


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def load_state(db, session):
    state = json.loads(session.state_json)
    state['session_id'] = session.id
    state.setdefault('sources', {})
    state.setdefault('source_versions', {})
    state.setdefault('artifacts', {})
    for source in db.scalars(select(SourceRecord).where(SourceRecord.session_id == session.id)):
        state['sources'][source.source_id] = json.loads(source.body_json)
        state['source_versions'][source.source_id] = json.loads(source.versions_json)
    for artifact in db.scalars(select(ArtifactRecord).where(ArtifactRecord.session_id == session.id)):
        state['artifacts'][artifact.artifact_id] = json.loads(artifact.body_json)
    state.setdefault('requests', {})
    state.setdefault('trace', [])
    state.setdefault('subtasks', {})
    for row in db.scalars(select(SubtaskRecord).where(SubtaskRecord.session_id == session.id)):
        state['subtasks'][row.subtask_id] = json.loads(row.body_json)
    return state


def save_state(db, session, state, run_id='legacy'):
    for sid, body in state.get('subtasks', {}).items():
        Subtask.model_validate(body)
        row = db.get(SubtaskRecord, (session.id, sid))
        if row is None:
            row = SubtaskRecord(session_id=session.id, subtask_id=sid, run_id=run_id)
            db.add(row)
        row.status, row.body_json = body['status'], encode(body)
    for sid, source in state.get('sources', {}).items():
        row = db.get(SourceRecord, (session.id, sid))
        if row is None:
            row = SourceRecord(session_id=session.id, source_id=sid, body_json=encode(source))
            db.add(row)
        elif json.loads(row.body_json) != source:
            raise ValueError('来源快照不可覆盖')
        row.versions_json = encode(state.get('source_versions', {}).get(sid, []))
    for aid, body in state.get('artifacts', {}).items():
        if body.get('contract'):
            contract = ArtifactEnvelope.model_validate(body['contract'])
            if contract.task_id not in (session.id, 'standalone'):
                raise ValueError('产物会话不匹配')
            if contract.artifact_id != aid or contract.profile_version != body['profile_version']:
                raise ValueError('产物外层编号或版本与协议不匹配')
        row = db.get(ArtifactRecord, (session.id, aid))
        if row is None:
            db.add(ArtifactRecord(session_id=session.id, artifact_id=aid, run_id=run_id,
                kind=body.get('contract', {}).get('kind', 'legacy'),
                profile_version=body['profile_version'], body_json=encode(body)))
        elif json.loads(row.body_json) != body:
            raise ValueError('已提交产物不可覆盖')
    projection = {k: v for k, v in state.items() if k not in ('sources', 'artifacts', 'source_versions', 'trace', 'requests', 'subtasks')}
    session.state_json = encode(projection)


def event(db, run_id, kind, data):
    db.add(RunEvent(run_id=run_id, kind=kind, data_json=encode(data), created_at=time.time()))


def public_run(run):
    payload = json.loads(run.input_json)
    return {'id': run.id, 'session_id': run.session_id, 'request_id': run.request_id,
        'input_text': payload.get('text', ''), 'confirmed': bool(payload.get('confirmed_ids')),
        'status': run.status, 'attempts': run.attempts, 'created_at': run.created_at,
        'updated_at': run.updated_at, 'cancel_requested': bool(run.cancel_requested),
        'result': json.loads(run.result_json) if run.result_json else None}


def record_outcome(db, run, session, result):
    cursor = json.loads(run.checkpoint_json)
    if not cursor.get('conversation_saved'):
        state = load_state(db, session)
        state['conversation'].extend([{'role': 'user', 'content': json.loads(run.input_json).get('text', '')},
                                      {'role': 'assistant', 'content': result['text']}])
        save_state(db, session, state, run.id)
        cursor.update(conversation_saved=True, phase='done', reply=result)
        run.checkpoint_json = encode(cursor)


def migrate(db):
    """Additive, idempotent migration of legacy session blobs; no table is dropped."""
    if db.get(SchemaMigration, 1):
        return
    for session in db.scalars(select(MultiAgentSession)):
        state = load_state(db, session)
        for request_id, request in state.get('requests', {}).items():
            rid = 'legacy_' + session.id + '_' + request_id
            if db.get(AgentRun, rid) is None:
                result = request.get('result', {'status': 'failed', 'text': '旧版活动请求缺少执行检查点，请重新发起。'})
                db.add(AgentRun(id=rid, session_id=session.id, user_id=session.user_id,
                    request_id=request_id, input_json=encode(request.get('input', {})),
                    status=result['status'], result_json=encode(result), created_at=time.time(), updated_at=time.time()))
        # Legacy engines did not persist execution cursors, so they cannot be resumed.
        session.active_request_id = None
        from services.knowledge import knowledge, sync_memories
        account = knowledge(db, session.user_id)
        if account.profile_json == '{}' and state.get('profile'):
            account.profile_json = encode(state['profile'])
        state.setdefault('uses_account_profile', not bool(state.get('profile')))
        sync_memories(db, session.user_id, state)
        save_state(db, session, state)
        db.flush()
    db.add(SchemaMigration(version=1, applied_at=time.time()))
    db.commit()
