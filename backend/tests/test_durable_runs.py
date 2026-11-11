import asyncio
import copy
import json
import os
import tempfile
import time
import unittest
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace as NS
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import APIConnectionError, RateLimitError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from database.models import (Base, MultiAgentSession, AgentRun, RunStep, ArtifactRecord,
    SourceRecord, RunEvent, UserMemory, UserProfile)
from agents.runtime import MultiAgentRuntime, new_state
from services.run_store import encode, load_state, save_state, migrate
from services.run_worker import RunWorker, LeaseLost, RunCancelled
from services.auth_service import get_current_user
from routers import multi_agent
from test_multi_agent import ScriptClient, SETTINGS


class Crash(BaseException):
    pass


class DurableRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + str(Path(self.folder.name) / 'db.sqlite'),
                                    connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()
        self.folder.cleanup()

    def create_run(self, text='你好', confirmed=()):
        state = new_state()
        state['session_id'] = 's1'
        with self.sessions() as db:
            db.add(MultiAgentSession(id='s1', user_id=1, state_json=encode(state), active_request_id='r1'))
            db.add(AgentRun(id='run1', session_id='s1', user_id=1, request_id='r1',
                input_json=encode({'text': text, 'confirmed_ids': list(confirmed)}), status='queued',
                created_at=time.time(), updated_at=time.time()))
            db.commit()
        return state

    def factory(self, script):
        return lambda state, **kwargs: MultiAgentRuntime(state, settings=SETTINGS,
            client=ScriptClient(script), **kwargs)

    async def test_persisted_pending_tool_resumes_without_repeating_model_or_memory(self):
        self.create_run('我喜欢数学')
        worker = RunWorker(self.sessions)
        self.assertEqual(worker.claim(), 'run1')
        with self.sessions() as db:
            state = load_state(db, db.get(MultiAgentSession, 's1'))
        state['run_id'] = 'run1'
        snapshot = {}
        def persist(state, execution):
            worker.persist('run1', state, execution)
            snapshot.update(copy.deepcopy(execution))
            if execution['phase'] == 'tools':
                raise Crash()
        client = ScriptClient([('propose_memory', {'kind': 'soft', 'field': 'interest',
            'value': '数学', 'quote': '我喜欢数学'})])
        runtime = MultiAgentRuntime(state, settings=SETTINGS, client=client, persist=persist)
        with self.assertRaises(Crash):
            await runtime.run('我喜欢数学')
        self.assertEqual(len(client.requests), 1)
        with self.sessions() as db:
            run = db.get(AgentRun, 'run1')
            run.lease_until = 0
            db.commit()
        resumed_client = ScriptClient([('respond', {'text': '已记录数学兴趣'})])
        other = RunWorker(self.sessions, runtime_factory=lambda state, **kwargs: MultiAgentRuntime(
            state, settings=SETTINGS, client=resumed_client, **kwargs))
        await other.run_once()
        with self.sessions() as db:
            self.assertEqual(db.get(AgentRun, 'run1').status, 'completed')
            state = load_state(db, db.get(MultiAgentSession, 's1'))
            self.assertEqual(len(state['memories']), 1)
            self.assertEqual(len(state['conversation']), 2)
            self.assertEqual(len(list(db.scalars(select(UserMemory)))), 1)
        # Reconstructed provider context has no orphan tool responses or private reasoning.
        messages = resumed_client.requests[0]['messages']
        self.assertFalse(any(m['role'] == 'tool' for m in messages))
        self.assertEqual(len(resumed_client.requests), 1)

    async def test_tool_commit_and_cursor_are_atomic_and_not_replayed(self):
        self.create_run('喜欢数学')
        worker = RunWorker(self.sessions)
        worker.claim()
        with self.sessions() as db:
            state = load_state(db, db.get(MultiAgentSession, 's1'))
        def persist(state, execution):
            worker.persist('run1', state, execution)
            if state['memories']:
                raise Crash()
        runtime = MultiAgentRuntime(state, settings=SETTINGS, client=ScriptClient([
            ('propose_memory', {'kind': 'soft', 'field': 'interest', 'value': '数学', 'quote': '喜欢数学'})]), persist=persist)
        with self.assertRaises(Crash):
            await runtime.run('喜欢数学')
        with self.sessions() as db:
            run = db.get(AgentRun, 'run1')
            self.assertEqual(json.loads(run.checkpoint_json)['phase'], 'model')
            run.lease_until = 0
            db.commit()
        next_worker = RunWorker(self.sessions, self.factory([('respond', {'text': '继续完成'})]))
        await next_worker.run_once()
        with self.sessions() as db:
            state = load_state(db, db.get(MultiAgentSession, 's1'))
            self.assertEqual(len(state['memories']), 1)
            tools = list(db.scalars(select(RunStep).where(RunStep.kind == 'tool')))
            self.assertEqual([s.name for s in tools].count('propose_memory'), 1)

    async def test_claim_lease_fencing_cancel_and_retry_budget(self):
        state = self.create_run()
        first, second = RunWorker(self.sessions), RunWorker(self.sessions)
        self.assertEqual(first.claim(), 'run1')
        self.assertIsNone(second.claim())
        with self.sessions() as db:
            db.get(AgentRun, 'run1').lease_until = 0
            db.commit()
        self.assertEqual(second.claim(), 'run1')
        with self.assertRaises(LeaseLost):
            first.persist('run1', state, {})
        first.finish('run1', {'status': 'completed', 'text': 'stale'})
        with self.sessions() as db:
            self.assertEqual(db.get(AgentRun, 'run1').status, 'running')
            db.get(AgentRun, 'run1').cancel_requested = 1
            db.commit()
        with self.assertRaises(RunCancelled):
            second.persist('run1', state, {})
        await second.execute_claimed('run1')
        with self.sessions() as db:
            self.assertEqual(db.get(AgentRun, 'run1').status, 'cancelled')
            self.assertIsNone(db.get(MultiAgentSession, 's1').active_request_id)

    async def test_migration_is_idempotent_and_snapshots_are_immutable(self):
        state = new_state()
        state['sources'] = {'web1': {'source_id': 'web1', 'text': '事实'}}
        state['source_versions'] = {'web1': [1]}
        state['artifacts'] = {'old': {'artifact_id': 'old', 'role': 'major_research', 'profile_version': 1, 'findings': '历史'}}
        state['requests'] = {'old-request': {'input': {'text': '旧请求'}, 'result': {'status': 'completed', 'text': '旧结果'}}}
        with self.sessions() as db:
            db.add(MultiAgentSession(id='s1', user_id=1, state_json=encode(state)))
            db.commit()
            migrate(db)
            migrate(db)
            session = db.get(MultiAgentSession, 's1')
            projection = json.loads(session.state_json)
            self.assertNotIn('sources', projection)
            loaded = load_state(db, session)
            self.assertEqual(loaded['sources'], state['sources'])
            self.assertEqual(len(list(db.scalars(select(ArtifactRecord)))), 1)
            self.assertEqual(len(list(db.scalars(select(AgentRun)))), 1)
            loaded['sources']['web1']['text'] = '篡改'
            with self.assertRaises(ValueError):
                save_state(db, session, loaded)

    async def test_actual_process_exit_recovers_committed_tool_once(self):
        self.create_run('我喜欢数学')
        script = '''
import asyncio, os
from database.models import SessionLocal
from services.run_worker import RunWorker
from agents.runtime import MultiAgentRuntime
from providers.client import ModelSettings
from scripts.evaluate_agent import ScriptedModel
class CrashingWorker(RunWorker):
    def persist(self, rid, state, execution):
        super().persist(rid, state, execution)
        if state['memories']:
            os._exit(17)
def runtime(state, **kwargs):
    return MultiAgentRuntime(state, settings=ModelSettings('qwen','synthetic','https://example.invalid','synthetic'),
        client=ScriptedModel([('propose_memory', {'kind':'soft','field':'interest','value':'数学','quote':'我喜欢数学'})]), **kwargs)
asyncio.run(CrashingWorker(SessionLocal, runtime).run_once())
'''
        env = {**os.environ, 'DATABASE_URL': str(self.engine.url), 'PYTHONDONTWRITEBYTECODE': '1'}
        child = await asyncio.to_thread(subprocess.run, [sys.executable, '-B', '-c', script],
            env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, 17, child.stderr)
        with self.sessions() as db:
            row = db.get(AgentRun, 'run1')
            self.assertEqual(row.status, 'running')
            row.lease_until = 0
            db.commit()
        await RunWorker(self.sessions, self.factory([('respond', {'text':'从保存的进度继续完成'})])).run_once()
        with self.sessions() as db:
            state = load_state(db, db.get(MultiAgentSession, 's1'))
            self.assertEqual(len(state['memories']), 1)
            self.assertEqual(db.get(AgentRun, 'run1').status, 'completed')
            self.assertEqual(len(list(db.scalars(select(RunStep).where(RunStep.name == 'propose_memory')))), 1)

    async def test_transient_provider_errors_retry_with_persisted_usage_and_finite_budget(self):
        state = self.create_run()
        state['conversation'] = [{'role': 'user', 'content': '此前说过：想研究北京邮电大学'},
                                 {'role': 'assistant', 'content': '可以按历史位次对照。'}]
        with self.sessions.begin() as db:
            save_state(db, db.get(MultiAgentSession, 's1'), state)
        requests = []

        class FailingClient:
            def __init__(self): self.chat=NS(completions=NS(create=self.create))
            async def create(self, **kwargs):
                requests.append(kwargs['messages'])
                raise APIConnectionError(request=httpx.Request('POST', 'https://example.invalid'))
        factory=lambda state,**kwargs:MultiAgentRuntime(state,settings=SETTINGS,client=FailingClient(),**kwargs)
        worker=RunWorker(self.sessions,factory,max_attempts=3)
        for _ in range(3): await worker.run_once()
        self.assertTrue(all(state['conversation'][0] in messages for messages in requests))
        with self.sessions() as db:
            run=db.get(AgentRun,'run1')
            self.assertEqual((run.status,run.attempts),('failed',3))
            self.assertEqual(json.loads(run.result_json)['provider_error'], 'connection')
            steps=list(db.scalars(select(RunStep)))
            self.assertEqual(len(steps),3)
            self.assertTrue(all(s.error_code=='APIConnectionError' for s in steps))
            self.assertEqual(len(load_state(db,db.get(MultiAgentSession,'s1'))['conversation']),4)

    async def test_quota_failure_restores_unstarted_research_without_automatic_retry(self):
        await self.assert_quota_outcome(started=False)

    async def test_quota_failure_after_model_work_keeps_confirmation_consumed(self):
        await self.assert_quota_outcome(started=True)

    async def assert_quota_outcome(self, *, started):
        state = self.create_run('确认调查', ['scope'])
        state['pending']['scope'] = {'kind': 'research', 'text': '调查计算机', 'profile_version': 1}
        with self.sessions.begin() as db:
            save_state(db, db.get(MultiAgentSession, 's1'), state)

        class QuotaClient(ScriptClient):
            async def create(self, **kwargs):
                if started and not self.requests:
                    return await super().create(**kwargs)
                response = httpx.Response(429, request=httpx.Request('POST', 'https://example.invalid'))
                raise RateLimitError('upstream-private-text', response=response, body={'code': 'insufficient_quota'})

        def runtime(state, **kwargs):
            return MultiAgentRuntime(state, settings=SETTINGS,
                                     client=QuotaClient([('get_profile', {})]), **kwargs)

        await RunWorker(self.sessions, runtime).run_once()
        with self.sessions() as db:
            run = db.get(AgentRun, 'run1')
            self.assertEqual((run.status, run.attempts), ('failed', 1))
            result = json.loads(run.result_json)
            self.assertEqual(result['provider_error'], 'quota')
            self.assertNotIn('upstream-private-text', run.result_json)
            state = load_state(db, db.get(MultiAgentSession, 's1'))
            self.assertEqual(len(state['conversation']), 2)
            if started:
                self.assertNotIn('scope', state['pending'])
            else:
                self.assertEqual(state['pending']['scope'], {'kind': 'research', 'text': '调查计算机',
                    'profile_version': 1, 'retry_after_provider_error': True})


class AsyncApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + str(Path(self.folder.name) / 'api.sqlite'), connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.patch = patch('routers.multi_agent.SessionLocal', self.sessions)
        self.patch.start()
        self.env = patch.dict(os.environ, {'AGENT_EMBEDDED_WORKER': 'false'})
        self.env.start()
        app = FastAPI()
        app.include_router(multi_agent.router)
        self.user = NS(id=1)
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.patch.stop()
        self.engine.dispose()
        self.folder.cleanup()

    def test_async_submit_replay_sse_ownership_and_metrics(self):
        sid = self.client.post('/api/v2/agent/sessions', json={}).json()['id']
        url = f'/api/v2/agent/sessions/{sid}/messages'
        payload = {'request_id': 'one', 'text': '你好'}
        queued = self.client.post(url, json=payload)
        self.assertEqual(queued.status_code, 202)
        rid = queued.json()['id']
        self.assertEqual(self.client.post(url, json=payload).json()['id'], rid)
        self.assertEqual(self.client.post(url, json={**payload, 'text': '不同'}).status_code, 409)
        factory = lambda state, **kwargs: MultiAgentRuntime(state, settings=SETTINGS,
            client=ScriptClient([('respond', {'text': '你好'})]), **kwargs)
        asyncio.run(RunWorker(self.sessions, factory).run_once())
        run_url = f'/api/v2/agent/runs/{rid}'
        self.assertEqual(self.client.get(run_url).json()['status'], 'completed')
        events = self.client.get(run_url + '/events').text
        self.assertIn('event: result', events)
        ids = [int(line.split(': ')[1]) for line in events.splitlines() if line.startswith('id: ')]
        self.assertEqual(ids, sorted(set(ids)))
        replay = self.client.get(run_url + '/events', headers={'Last-Event-ID': str(ids[-1])}).text
        self.assertEqual(replay, '')
        metrics = self.client.get(run_url + '/metrics').json()
        self.assertEqual(len(metrics['steps']), 2)
        self.user = NS(id=2)
        for path in (run_url, run_url + '/events', run_url + '/metrics'):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.post(run_url + '/cancel').status_code, 404)

    def test_cancel_queued_and_cross_session_memory_correction_deletion(self):
        sid = self.client.post('/api/v2/agent/sessions', json={}).json()['id']
        url = f'/api/v2/agent/sessions/{sid}/messages'
        run = self.client.post(url, json={'request_id': 'one', 'text': '你好'}).json()
        rid = run['id']
        self.assertEqual(self.client.post(f'/api/v2/agent/runs/{rid}/cancel').json()['status'], 'cancelled')
        cancelled_state = self.client.get(f'/api/v2/agent/sessions/{sid}').json()['state']
        self.assertEqual(cancelled_state['conversation'][0]['content'], '你好')
        self.assertIn('取消', cancelled_state['conversation'][1]['content'])
        with self.sessions() as db:
            db.add(UserMemory(user_id=1, memory_id='m1', revision=2, deleted=0, body_json=encode(
                {'id': 'm1', 'kind': 'soft', 'field': 'interest', 'value': '数学', 'quote': '数学', 'confirmed': True})))
            db.commit()
        self.assertTrue(self.client.patch('/api/v2/agent/knowledge/memories/m1', json={'value': '计算机'}).json()['ok'])
        sid2 = self.client.post('/api/v2/agent/sessions', json={}).json()['id']
        state = self.client.get(f'/api/v2/agent/sessions/{sid2}').json()['state']
        self.assertEqual(state['memories'][0]['value'], '计算机')
        self.assertTrue(self.client.delete('/api/v2/agent/knowledge/memories/m1').json()['ok'])
        self.assertEqual(self.client.get(f'/api/v2/agent/sessions/{sid2}').json()['state']['memories'], [])
        with self.sessions() as db:
            self.assertEqual(db.get(UserMemory, (1, 'm1')).body_json, '{}')

    def test_profile_change_updates_sessions_and_is_blocked_during_active_run(self):
        sid = self.client.post('/api/v2/agent/sessions', json={}).json()['id']
        profile = {'basic_info': {'province': '广东', 'subject_type': '物理类', 'province_rank': 20000,
            'target_year': 2027, 'selected_subjects': ['物理','化学','生物']},
            'constraints': {'excluded_cities': ['广州'], 'annual_budget_max': 25000, 'accept_sino_foreign': False}}
        self.assertEqual(self.client.put('/api/v2/agent/knowledge/profile', json=profile).status_code, 200)
        state = self.client.get(f'/api/v2/agent/sessions/{sid}').json()['state']
        self.assertEqual(state['profile'], profile)
        self.assertEqual(state['profile_version'], 2)
        new_sid = self.client.post('/api/v2/agent/sessions', json={}).json()['id']
        self.assertEqual(self.client.get(f'/api/v2/agent/sessions/{new_sid}').json()['state']['profile'], profile)
        self.client.post(f'/api/v2/agent/sessions/{sid}/messages', json={'request_id':'running','text':'你好'})
        self.assertEqual(self.client.put('/api/v2/agent/knowledge/profile', json=profile).status_code, 409)
        self.user=NS(id=2)
        self.assertEqual(self.client.get('/api/v2/agent/knowledge').json()['profile'], {})

    def test_conversation_creation_retry_archive_and_ownership(self):
        base = '/api/v2/agent/sessions'
        body = {'request_id': 'create-once'}
        sid = self.client.post(base, json=body).json()['id']
        self.assertEqual(self.client.post(base, json=body).json()['id'], sid)
        url = base + '/' + sid
        self.assertEqual(self.client.patch(url, json={'title': ' 院校比较 '}).status_code, 200)
        payload = {'request_id': 'run-once', 'text': '比较学校'}
        run = self.client.post(url + '/messages', json=payload).json()
        self.assertEqual(self.client.get(base).json()['active_run']['id'], run['id'])
        self.assertEqual(self.client.patch(url, json={'archived': True}).status_code, 409)
        self.client.post('/api/v2/agent/runs/' + run['id'] + '/cancel')
        self.assertEqual(self.client.patch(url, json={'archived': True}).status_code, 200)
        item = self.client.get(base).json()['sessions'][0]
        self.assertEqual((item['title'], item['archived']), ('院校比较', True))
        self.assertEqual(self.client.post(url + '/messages', json=payload).json()['id'], run['id'])
        self.assertEqual(self.client.post(url + '/messages', json={**payload, 'request_id': 'new'}).status_code, 409)
        self.user = NS(id=2)
        self.assertEqual(self.client.patch(url, json={'archived': False}).status_code, 404)
        self.assertNotEqual(self.client.post(base, json=body).json()['id'], sid)

    def test_imported_profile_stays_scoped_and_stale_confirmation_is_rejected(self):
        base = '/api/v2/agent'
        imported = {'basic_info': {'province': '浙江', 'subject_type': '物理类', 'province_rank': 12000}}
        with self.sessions.begin() as db:
            db.add(UserProfile(user_id=1, profile_uuid='old', profile_json=encode(imported), created_at='2025'))
        legacy = self.client.post(base + '/sessions', json={'profile_uuid': 'old'}).json()['id']
        sid = self.client.post(base + '/sessions', json={}).json()['id']
        url = base + '/sessions/' + sid
        with self.sessions.begin() as db:
            row = db.get(MultiAgentSession, sid)
            state = load_state(db, row)
            state['pending']['approval'] = {'kind': 'research', 'profile_version': state['profile_version'], 'text': '开始？'}
            save_state(db, row, state)
        profile = {'basic_info': {'province': '广东', 'subject_type': '物理类', 'target_year': 2027}, 'constraints': {}}
        self.assertEqual(self.client.put(base + '/knowledge/profile', json=profile).status_code, 200)
        self.assertEqual(self.client.post(url + '/messages', json={
            'request_id': 'late', 'text': '确认', 'confirmed_ids': ['approval']}).status_code, 422)
        self.assertEqual(self.client.delete(url + '/confirmations/approval').status_code, 200)
        self.assertEqual(self.client.get(url).json()['state']['pending'], {})
        original = self.client.get(base + '/sessions/' + legacy).json()['state']
        self.assertEqual(original['profile'], imported)
        self.assertEqual(original['profile_version'], 1)

    def test_deleted_memory_cannot_be_restored_by_a_stale_snapshot(self):
        from services.knowledge import knowledge, sync_memories
        with self.sessions() as db:
            knowledge(db,1)
            memory={'id':'deleted','kind':'soft','field':'interest','value':'数学','quote':'数学','confirmed':True}
            db.add(UserMemory(user_id=1,memory_id='deleted',body_json='{}',deleted=1,revision=2))
            db.commit()
            state={'memories':[memory]}
            sync_memories(db,1,state)
            db.commit()
            self.assertEqual(db.get(UserMemory,(1,'deleted')).body_json,'{}')
            self.assertEqual(db.get(UserMemory,(1,'deleted')).deleted,1)
