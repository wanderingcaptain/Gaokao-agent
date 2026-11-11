"""Authenticated v2 conversations, durable submissions and replayable event streams."""
import asyncio
import json
import os
import time
from uuid import NAMESPACE_URL, uuid4, uuid5
from fastapi import APIRouter, Depends, HTTPException, Query, Header, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Literal
from sqlalchemy import select, update, func
from database.models import (AdmissionRecord, ConversationSettings, MultiAgentSession,
    SessionLocal, User, UserProfile, AgentRun, RunEvent, RunStep, UserKnowledge)
from services.auth_service import get_current_user
from services.run_store import encode, load_state, save_state, public_run, event, record_outcome, TERMINAL
from services.run_worker import RunWorker
from services import knowledge
from agents.runtime import MultiAgentRuntime, new_state
from agents.research import query_history
from tools.risk import city_name

router = APIRouter(prefix='/api/v2/agent', tags=['multi-agent'])
_workers = {}


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)


class Create(Input):
    profile_uuid: str | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=100)


class ConversationEdit(Input):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    archived: bool | None = None


class Message(Input):
    request_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=4000)
    confirmed_ids: list[str] = Field(default_factory=list, max_length=10)


class MemoryEdit(Input):
    value: str = Field(min_length=1, max_length=300)


class Basic(Input):
    province: str = Field(min_length=1, max_length=20)
    subject_type: Literal['物理类', '历史类', '理科', '文科']
    province_rank: int | None = Field(default=None, ge=1, le=10000000)
    gaokao_score: int | None = Field(default=None, ge=0, le=900)
    target_year: int = Field(ge=2000, le=2100)
    selected_subjects: list[str] = Field(default_factory=list, max_length=6)

    @field_validator('selected_subjects')
    @classmethod
    def subjects(cls, value):
        allowed = {'物理', '历史', '化学', '生物', '地理', '政治', '思想政治'}
        normalized = [s.replace('思想政治', '政治') for s in value]
        if len(normalized) != len(set(normalized)) or any(s not in allowed for s in value):
            raise ValueError('选科必须是无重复的有效科目')
        return normalized


class Constraints(Input):
    excluded_cities: list[str] = Field(default_factory=list, max_length=50)
    annual_budget_max: int | None = Field(default=None, ge=0, le=10000000)
    accept_sino_foreign: bool = False
    preferred_cities: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode='after')
    def consistent_cities(self):
        if {city_name(city) for city in self.preferred_cities} & {city_name(city) for city in self.excluded_cities}:
            raise ValueError('偏好城市与排除城市不能重叠')
        return self


class ProfileEdit(Input):
    basic_info: Basic
    constraints: Constraints


def owned(db, sid, uid):
    row = db.get(MultiAgentSession, sid)
    if not row or row.user_id != uid:
        raise HTTPException(404, '会话不存在')
    return row


def owned_run(db, rid, uid):
    row = db.get(AgentRun, rid)
    if not row or row.user_id != uid:
        raise HTTPException(404, '任务不存在')
    return row


def user_write_lock(db, uid):
    knowledge.knowledge(db, uid)
    db.execute(update(UserKnowledge).where(UserKnowledge.user_id == uid).values(revision=UserKnowledge.revision))


def require_idle(db, uid):
    if db.scalar(select(AgentRun.id).where(AgentRun.user_id == uid, AgentRun.status.in_(['queued', 'running'])).limit(1)):
        raise HTTPException(409, '当前有任务执行中，请完成或取消后再修改条件或发起新任务')


def ensure_worker():
    if os.getenv('AGENT_EMBEDDED_WORKER', 'true').lower() != 'true':
        return
    loop = asyncio.get_running_loop()
    current = _workers.get(loop)
    if current is None or current[1].done():
        worker = RunWorker(SessionLocal, runtime_factory=lambda state, **kwargs: MultiAgentRuntime(state, **kwargs))
        _workers[loop] = (worker, asyncio.create_task(worker.serve()))


async def shutdown_worker():
    current = _workers.pop(asyncio.get_running_loop(), None)
    if current:
        worker, task = current
        worker.stopping = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@router.post('/sessions')
def create(body: Create, user: User = Depends(get_current_user)):
    sid = (uuid5(NAMESPACE_URL, f'gaokao-agent/conversations/{user.id}/{body.request_id}').hex
           if body.request_id else uuid4().hex)
    state = new_state()
    state['session_id'] = sid
    state['uses_account_profile'] = not bool(body.profile_uuid)
    state['imported_profile_uuid'] = body.profile_uuid
    result = {'id': sid, 'opening': '我们先聊聊：有哪些课、活动或事情让你愿意主动花时间？暂时没有方向也没关系。'}
    with SessionLocal() as db:
        if body.request_id:
            user_write_lock(db, user.id)
            existing = db.get(MultiAgentSession, sid)
            if existing:
                if json.loads(existing.state_json).get('imported_profile_uuid') != body.profile_uuid:
                    raise HTTPException(409, '同一对话创建请求不能使用不同画像')
                return result
        knowledge.refresh(db, user.id, state)
        if body.profile_uuid:
            profile = db.query(UserProfile).filter_by(user_id=user.id, profile_uuid=body.profile_uuid).first()
            if not profile:
                raise HTTPException(404, '画像不存在')
            state['profile'] = json.loads(profile.profile_json)
        db.add(MultiAgentSession(id=sid, user_id=user.id, state_json=encode(state)))
        db.add(ConversationSettings(session_id=sid, created_at=time.time()))
        db.commit()
    return result


@router.get('/sessions')
def sessions(user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        latest = (select(AgentRun.session_id, func.max(AgentRun.created_at).label('last_run'))
                  .where(AgentRun.user_id == user.id).group_by(AgentRun.session_id).subquery())
        rows = db.execute(select(MultiAgentSession, ConversationSettings, latest.c.last_run)
            .outerjoin(ConversationSettings, ConversationSettings.session_id == MultiAgentSession.id)
            .outerjoin(latest, latest.c.session_id == MultiAgentSession.id)
            .where(MultiAgentSession.user_id == user.id)
            .order_by(func.coalesce(latest.c.last_run, ConversationSettings.created_at, 0).desc(),
                      MultiAgentSession.id))
        result = []
        for row, settings, last_run in rows:
            first = next((m['content'] for m in json.loads(row.state_json).get('conversation', [])
                          if m['role'] == 'user'), '')
            result.append({'id': row.id, 'running': bool(row.active_request_id),
                'title': (settings.title if settings else None) or ' '.join(first.split())[:40],
                'archived': bool(settings and settings.archived),
                'updated_at': last_run or (settings.created_at if settings else 0)})
        active = db.scalar(select(AgentRun).where(AgentRun.user_id == user.id,
            AgentRun.status.in_(['queued', 'running'])).order_by(AgentRun.created_at.desc()).limit(1))
        return {'sessions': result, 'active_run': public_run(active) if active else None}


@router.patch('/sessions/{sid}')
def edit_session(sid: str, body: ConversationEdit, user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        user_write_lock(db, user.id)
        row = owned(db, sid, user.id)
        if body.archived and row.active_request_id:
            raise HTTPException(409, '请在当前研究结束后归档')
        settings = db.get(ConversationSettings, sid)
        if settings is None:
            settings = ConversationSettings(session_id=sid)
            db.add(settings)
        if 'title' in body.model_fields_set:
            settings.title = body.title
        if body.archived is not None:
            settings.archived = int(body.archived)
        db.commit()
        return {'ok': True}


@router.get('/sessions/{sid}')
def read(sid: str, user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        row = owned(db, sid, user.id)
        state = load_state(db, row)
        if not row.active_request_id:
            knowledge.refresh(db, user.id, state)
            db.commit()  # Do not write a session projection from a read request.
        state.pop('requests', None)
        runs = list(db.scalars(select(AgentRun).where(AgentRun.session_id == sid).order_by(AgentRun.created_at.desc()).limit(30)))
        state['trace'] = [{'role': step.role, 'tool': step.name, 'ok': bool(step.ok)}
            for step in db.scalars(select(RunStep).join(AgentRun, AgentRun.id == RunStep.run_id)
                .where(AgentRun.session_id == sid, RunStep.kind == 'tool').limit(300))]
        active = next((run for run in runs if run.request_id == row.active_request_id), None)
        input_saved = bool(active and json.loads(active.checkpoint_json).get('conversation_saved'))
        return {'id': sid, 'running': row.active_request_id is not None, 'input_saved': input_saved, 'state': state,
                'runs': [public_run(run) for run in runs]}


@router.post('/sessions/{sid}/messages')
async def message(sid: str, body: Message, response: Response, wait: bool = False,
                  user: User = Depends(get_current_user)):
    payload = body.model_dump()
    if len(set(body.confirmed_ids)) != len(body.confirmed_ids):
        raise HTTPException(422, '确认编号重复')
    with SessionLocal() as db:
        user_write_lock(db, user.id)
        row = owned(db, sid, user.id)
        previous = db.scalar(select(AgentRun).where(AgentRun.session_id == sid, AgentRun.request_id == body.request_id))
        if previous:
            if json.loads(previous.input_json) != payload:
                raise HTTPException(409, '同一请求编号内容不同')
            rid = previous.id
        else:
            require_idle(db, user.id)
            settings = db.get(ConversationSettings, sid)
            if settings and settings.archived:
                raise HTTPException(409, '恢复归档对话后可继续研究')
            state = load_state(db, row)
            knowledge.refresh(db, user.id, state)
            if any(state['pending'].get(cid, {}).get('profile_version') != state['profile_version']
                   for cid in body.confirmed_ids):
                raise HTTPException(422, '确认项已使用或过期，请查看当前确认项')
            if row.active_request_id:
                raise HTTPException(409, '会话已有活动请求')
            rid = uuid4().hex
            row.active_request_id = body.request_id
            save_state(db, row, state)
            db.add(AgentRun(id=rid, session_id=sid, user_id=user.id, request_id=body.request_id,
                input_json=encode(payload), status='queued', created_at=time.time(), updated_at=time.time()))
            event(db, rid, 'queued', {'status': 'queued'})
        db.commit()
    ensure_worker()
    if wait:  # Explicit compatibility/testing option; the UI always uses asynchronous submission.
        deadline = time.monotonic() + 310
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                run = owned_run(db, rid, user.id)
                if run.status in TERMINAL:
                    return json.loads(run.result_json)
            await asyncio.sleep(.05)
    with SessionLocal() as db:
        result = public_run(owned_run(db, rid, user.id))
    response.status_code = 202 if result['status'] not in TERMINAL else 200
    return result


@router.delete('/sessions/{sid}/confirmations/{confirmation_id}')
def dismiss_confirmation(sid: str, confirmation_id: str, user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        user_write_lock(db, user.id)
        row = owned(db, sid, user.id)
        require_idle(db, user.id)
        state = load_state(db, row)
        state['pending'].pop(confirmation_id, None)
        save_state(db, row, state)
        db.commit()
        return {'ok': True}


@router.get('/runs/{rid}')
def run_status(rid: str, user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        return public_run(owned_run(db, rid, user.id))


@router.post('/runs/{rid}/cancel')
def cancel(rid: str, user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        user_write_lock(db, user.id)
        run = owned_run(db, rid, user.id)
        if run.status not in TERMINAL and not run.cancel_requested:
            run.cancel_requested = 1
            run.updated_at = time.time()
            if run.status == 'queued':
                run.status = 'cancelled'
                run.result_json = encode({'status': 'cancelled', 'text': '任务已取消。'})
                session = owned(db, run.session_id, user.id)
                session.active_request_id = None
                record_outcome(db, run, session, json.loads(run.result_json))
                event(db, rid, 'result', json.loads(run.result_json))
            else:
                event(db, rid, 'cancelling', {'status': 'cancelling'})
            db.commit()
        return public_run(run)


@router.get('/runs/{rid}/events')
async def events(rid: str, request: Request, after: int = Query(default=0, ge=0),
                 last_event_id: str | None = Header(default=None), user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        owned_run(db, rid, user.id)
    try:
        cursor = max(after, int(last_event_id or 0))
    except ValueError:
        raise HTTPException(422, '无效事件游标')
    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            with SessionLocal() as db:
                run = owned_run(db, rid, user.id)
                done = run.status in TERMINAL
                rows = list(db.scalars(select(RunEvent).where(RunEvent.run_id == rid, RunEvent.id > cursor)
                    .order_by(RunEvent.id).limit(100)))
                chunks = [(e.id, e.kind, e.data_json) for e in rows]
            for eid, kind, data in chunks:
                cursor = eid
                yield f'id: {eid}\nevent: {kind}\ndata: {data}\n\n'
            if done and len(chunks) < 100:
                break
            if not chunks:
                yield ': heartbeat\n\n'
            await asyncio.sleep(.3)
    return StreamingResponse(stream(), media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.get('/knowledge')
def get_knowledge(user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        account = knowledge.knowledge(db, user.id)
        result = {'revision': account.revision, 'profile': json.loads(account.profile_json),
                  'memories': knowledge.memories(db, user.id)}
        db.commit()
        return result


@router.get('/coverage')
def get_coverage(user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        rows = db.execute(select(AdmissionRecord.province, AdmissionRecord.subject_type, AdmissionRecord.year)
                          .distinct().order_by(AdmissionRecord.province, AdmissionRecord.subject_type,
                                               AdmissionRecord.year))
        coverage = {}
        for province, subject_type, year in rows:
            coverage.setdefault((province, subject_type), []).append(year)
        return {'admissions': [{'province': p, 'subject_type': s, 'years': years}
                               for (p, s), years in coverage.items()]}


@router.get('/history')
def history(province: str = Query(min_length=1, max_length=20),
            subject_type: Literal['物理类', '历史类', '理科', '文科'] = '物理类',
            school: str = Query(default='', max_length=80),
            major: str = Query(default='', max_length=80),
            offset: int = Query(default=0, ge=0, le=100000),
            near_rank: int | None = Query(default=None, ge=1, le=10000000),
            target_year: int | None = Query(default=None, ge=2000, le=2100),
            user: User = Depends(get_current_user)):
    return query_history(province=province.strip(), subject_type=subject_type,
                         school=school.strip(), major=major.strip(), offset=offset, limit=10,
                         near_rank=near_rank, target_year=target_year)


@router.put('/knowledge/profile')
def set_profile(body: ProfileEdit, user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        user_write_lock(db, user.id)
        require_idle(db, user.id)
        account = knowledge.knowledge(db, user.id)
        profile = json.loads(account.profile_json)
        changes = body.model_dump()
        if 'gaokao_score' not in body.basic_info.model_fields_set:
            changes['basic_info'].pop('gaokao_score')
        if 'preferred_cities' not in body.constraints.model_fields_set:
            changes['constraints'].pop('preferred_cities')
        for group, values in changes.items():
            profile.setdefault(group, {}).update(values)
        if json.loads(account.profile_json) != profile:
            account.profile_json = encode(profile)
            account.revision += 1
        db.commit()
        return {'ok': True, 'revision': account.revision}


@router.patch('/knowledge/memories/{mid}')
def edit_memory(mid: str, body: MemoryEdit, user: User = Depends(get_current_user)):
    return change_memory(mid, user.id, value=body.value)


@router.delete('/knowledge/memories/{mid}')
def delete_memory(mid: str, user: User = Depends(get_current_user)):
    return change_memory(mid, user.id, delete=True)


def change_memory(mid, uid, **kwargs):
    with SessionLocal() as db:
        user_write_lock(db, uid)
        require_idle(db, uid)
        try:
            revision = knowledge.edit_memory(db, uid, mid, **kwargs)
        except KeyError:
            raise HTTPException(404, '记忆不存在')
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        db.commit()
        return {'ok': True, 'revision': revision}


@router.get('/runs/{rid}/metrics')
def metrics(rid: str, user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        run = owned_run(db, rid, user.id)
        steps = [{'step_id': s.step_id, 'role': s.role, 'kind': s.kind, 'name': s.name, 'ok': bool(s.ok),
                  'duration_ms': s.duration_ms, 'usage': json.loads(s.usage_json), 'error_code': s.error_code}
                 for s in db.scalars(select(RunStep).where(RunStep.run_id == rid))]
        return {'run': public_run(run), 'steps': steps,
            'total_tokens': sum(s['usage'].get('total_tokens', 0) for s in steps),
            'duration_ms': sum(s['duration_ms'] for s in steps),
            'failed_steps': sum(not s['ok'] for s in steps)}


def recover_interrupted():
    # An expired job is queued for a new lease; live workers retain ownership.
    with SessionLocal() as db:
        db.execute(update(AgentRun).where(AgentRun.status == 'running', AgentRun.lease_until < time.time())
            .values(status='queued', owner=None, lease_until=0))
        db.commit()
