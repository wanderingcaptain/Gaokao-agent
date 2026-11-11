import asyncio
import copy
import json
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.runtime import MultiAgentRuntime, new_state
from agents.research import search_web, query_history
from agents.schemas import tool_definitions
from providers.client import ModelSettings
from database.models import Base, AdmissionRecord, University, MultiAgentSession
from routers import multi_agent
from services.auth_service import get_current_user

SETTINGS = ModelSettings('qwen', 'test', 'https://example.invalid', 'test')

class ScriptClient:
    def __init__(self, script):
        self.script = iter(script)
        self.requests = []
        self.chat = NS(completions=NS(create=self.create))
    async def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        entry = next(self.script)
        name, args = entry() if callable(entry) else entry
        call = NS(id=f'call{len(self.requests)}', function=NS(name=name, arguments=json.dumps(args)))
        return NS(choices=[NS(message=NS(content='', tool_calls=[call]))])

def candidate():
    return {'candidate_id': 1, 'university_id': 'u1', 'university_name': '测试大学',
            'major_name': '计算机', 'enrollment_type': '普通类', 'city': '广州',
            'subject_type': '物理类', 'source_id': 'db1', 'min_rank': 20000}

class RuntimeTests(unittest.TestCase):
    def runtime(self, state, script=(), **kwargs):
        return MultiAgentRuntime(state, client=ScriptClient(script), settings=SETTINGS, **kwargs)

    def test_four_agents_and_artifact_return_loop(self):
        state = new_state()
        state['pending']['approval'] = {'kind': 'research', 'profile_version': 1}
        def latest(role):
            return next(aid for aid, a in state['artifacts'].items() if a['role'] == role)
        async def search(query):
            return {'ok': True, 'sources': [{'source_id': 'web1', 'text': '模拟课程资料'}]}
        def query(**kwargs):
            return {'ok': True, 'candidates': [candidate()], 'covered_years': [2025]}
        script = [
            ('delegate_task', {'agent': 'major_research', 'goal': '研究计算机课程'}),
            ('search_sources', {'query': '测试大学计算机课程'}),
            ('submit_report', {'findings': '课程研究', 'source_ids': ['web1'], 'missing': []}),
            lambda: ('read_artifact', {'artifact_id': latest('major_research')}),
            ('delegate_task', {'agent': 'admissions', 'goal': '查询广东历史录取'}),
            ('query_candidates', {'major': '计算机'}),
            ('submit_report', {'findings': '有历史数据，资格未知', 'source_ids': ['db1'], 'missing': ['资格']}),
            lambda: ('delegate_task', {'agent': 'planner', 'goal': '生成测试表',
                'artifact_ids': [latest('major_research'), latest('admissions')]}),
            lambda: ('read_artifact', {'artifact_id': latest('major_research')}),
            lambda: ('read_artifact', {'artifact_id': latest('admissions')}),
            ('read_sources', {'source_ids': ['web1']}),
            ('submit_plan', {'strategy': '优先兴趣，再参考历史位次', 'rows': [{
                'candidate_id': 1, 'reason': '匹配兴趣', 'tradeoffs': '资格待核实', 'source_ids': ['db1', 'web1']}]}),
            ('respond', {'text': '已生成测试草案，资格仍待核实。'})]
        runtime = self.runtime(state, script, search=search, query=query)
        reply = asyncio.run(runtime.run('开始调查', ['approval']))
        self.assertEqual(reply['status'], 'completed')
        self.assertEqual({a['role'] for a in state['artifacts'].values()}, {'major_research', 'admissions', 'planner', 'reviewer'})
        plan = state['artifacts'][state['plans'][0]]
        self.assertTrue(plan['test_only'])
        self.assertEqual(plan['rows'][0]['eligibility'], 'unknown')
        # Coordinator receives the actual persisted artifact reference, not a repeated investigation.
        last = runtime.client.requests[-1]['messages']
        self.assertTrue(any(m.get('role') == 'tool' and state['plans'][0] in m.get('content', '') for m in last))

    def test_confirmation_not_model_grant(self):
        state = new_state()
        runtime = self.runtime(state, [
            ('delegate_task', {'agent': 'admissions', 'goal': '未经确认查学校'}),
            ('request_confirmation', {'text': '是否开始选校调查？'})])
        reply = asyncio.run(runtime.run('我喜欢计算机'))
        self.assertEqual(reply['status'], 'waiting')
        self.assertFalse(state['trace'][0]['ok'])
        self.assertEqual(len(state['artifacts']), 0)
        followup = self.runtime(state, [('respond', {'text': '收到确认。'})])
        asyncio.run(followup.run('确认', [reply['confirmation_id']]))
        self.assertTrue(followup.authorized)
        self.assertNotIn(reply['confirmation_id'], state['pending'])

    def test_permissions_and_fabricated_sources(self):
        runtime = self.runtime(new_state())
        async def check():
            with self.assertRaises(ValueError):
                await runtime.execute('major_research', 'submit_plan', {})
            with self.assertRaises(ValueError):
                await runtime.execute('admissions', 'submit_report', {'findings': '假来源', 'source_ids': ['fake']})
            with self.assertRaises(ValueError):
                await runtime.execute('coordinator', 'read_artifact', {'artifact_id': 'another-session'})
        asyncio.run(check())

    def test_memory_quote_hard_constraint_and_staleness(self):
        state = new_state()
        runtime = self.runtime(state)
        runtime.user_text = '不想去广州'
        async def check():
            with self.assertRaises(ValueError):
                await runtime.execute('coordinator', 'propose_memory', {'kind': 'soft',
                    'field': 'excluded_city', 'value': '广州', 'quote': '不想去广州'})
            item = await runtime.execute('coordinator', 'propose_memory', {'kind': 'hard',
                'field': 'excluded_city', 'value': '广州', 'quote': '不想去广州'})
            self.assertEqual(state['memories'], [])
            return item['memory']['id']
        mid = asyncio.run(check())
        old = runtime.artifact('major_research', {'findings': '旧报告'})['artifact_id']
        followup = self.runtime(state, [('respond', {'text': '已确认排除广州。'})])
        asyncio.run(followup.run('确认', [mid]))
        self.assertEqual(state['profile_version'], 2)
        with self.assertRaises(ValueError):
            asyncio.run(followup.execute('coordinator', 'read_artifact', {'artifact_id': old}))

    def test_budget_and_search_not_configured(self):
        runtime = self.runtime(new_state(), max_calls=0)
        self.assertEqual(asyncio.run(runtime.run('你好'))['status'], 'degraded')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(asyncio.run(search_web('学校课程'))['error'], 'search_not_configured')

    def test_excluded_candidate_is_not_in_ordered_rows(self):
        state = new_state()
        state['candidates']['1'] = candidate()
        state['sources']['db1'] = {'kind': 'database', **candidate()}
        state['source_versions']['db1'] = [1]
        state['memories'] = [{'confirmed': True, 'field': 'excluded_city', 'value': '广州'}]
        runtime = self.runtime(state)
        runtime.authorized = True
        report = runtime.artifact('admissions', {'findings': '城市不符合',
            'source_ids': ['db1'], 'candidate_sources': {'1': 'db1'}})['artifact_id']
        asyncio.run(runtime.execute('planner', 'read_artifact', {'artifact_id': report}))
        result = asyncio.run(runtime.execute('planner', 'submit_plan', {
            'strategy': '测试', 'rows': [{'candidate_id': 1, 'reason': '匹配', 'tradeoffs': '城市不符', 'source_ids': ['db1']}]}))
        plan = state['artifacts'][result['artifact_id']]
        self.assertEqual(plan['rows'], [])
        self.assertEqual(len(plan['excluded']), 1)

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.patch = patch('routers.multi_agent.SessionLocal', self.sessions)
        self.patch.start()
        self.app = FastAPI()
        self.app.include_router(multi_agent.router)
        self.user = NS(id=1)
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(self.app)
    def tearDown(self):
        self.client.close()
        self.patch.stop()
        self.engine.dispose()
    def test_session_ownership_idempotency_and_persistence(self):
        sid = self.client.post('/api/v2/agent/sessions', json={}).json()['id']
        original = MultiAgentRuntime
        def runtime(state, **kwargs):
            return original(state, settings=SETTINGS,
                client=ScriptClient([('respond', {'text': '你好，我们先探索兴趣。'})]), **kwargs)
        url = f'/api/v2/agent/sessions/{sid}'
        payload = {'request_id': 'r1', 'text': '你好'}
        with patch('routers.multi_agent.MultiAgentRuntime', side_effect=runtime):
            response = self.client.post(url + '/messages?wait=true', json=payload)
        self.assertEqual(response.json()['status'], 'completed')
        self.assertEqual(self.client.post(url + '/messages?wait=true', json=payload).json(), response.json())
        self.assertEqual(self.client.post(url + '/messages?wait=true', json={**payload, 'text': '其他'}).status_code, 409)
        self.assertEqual(len(self.client.get(url).json()['state']['conversation']), 2)
        self.user = NS(id=2)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_active_request_and_restart_recovery(self):
        sid = self.client.post('/api/v2/agent/sessions', json={}).json()['id']
        with self.sessions() as db:
            row = db.get(MultiAgentSession, sid)
            state = json.loads(row.state_json)
            state['requests']['r1'] = {'input': {}}
            row.state_json = json.dumps(state)
            row.active_request_id = 'r1'
            db.commit()
        url = f'/api/v2/agent/sessions/{sid}'
        self.assertEqual(self.client.post(url + '/messages?wait=true', json={'request_id': 'r2', 'text': '你好'}).status_code, 409)
        from services.run_store import migrate
        with self.sessions() as db:
            migrate(db)
        self.assertFalse(self.client.get(url).json()['running'])

    def test_confirmation_research_validation_repair_and_plan_persist_through_api(self):
        with self.sessions() as db:
            db.add(University(university_id='u1', name='测试大学', province='广东', city='广州', university_type='普通'))
            for year in (2024, 2025, 2026):
                db.add(AdmissionRecord(university_id='u1', university_name='测试大学', major_name='计算机',
                    enrollment_type='普通类', province='广东', subject_type='物理类', year=year, min_rank=20000))
            db.commit()
        sid = self.client.post('/api/v2/agent/sessions', json={}).json()['id']
        url = f'/api/v2/agent/sessions/{sid}'
        original = MultiAgentRuntime
        scripts = iter(['confirmation', 'research'])
        def runtime(state, **kwargs):
            stage = next(scripts)
            def report_id():
                return next(aid for aid, a in state['artifacts'].items() if a['role'] == 'admissions')
            def plan_args(valid=True):
                c = next(iter(state['candidates'].values()))
                return {'strategy': '参考历史，资格未知', 'rows': [{'candidate_id': c['candidate_id'],
                    'reason': '兴趣匹配', 'tradeoffs': '未核实当年资格',
                    'source_ids': [c['source_id'] if valid else 'fabricated']}]}
            script = [('request_confirmation', {'text': '开始院校调查？'})] if stage == 'confirmation' else [
                ('delegate_task', {'agent': 'admissions', 'goal': '调查计算机'}),
                ('query_candidates', {'major': '计算机', 'limit': 1}),
                lambda: ('submit_report', {'findings': '发现一所学校，资格待核实',
                    'source_ids': [c['source_id'] for c in state['candidates'].values()], 'missing': ['当年资格']}),
                lambda: ('delegate_task', {'agent': 'planner', 'goal': '生成测试方案', 'artifact_ids': [report_id()]}),
                lambda: ('read_artifact', {'artifact_id': report_id()}),
                lambda: ('validate_plan', plan_args(False)),
                lambda: ('submit_plan', plan_args(False)),
                lambda: ('submit_plan', plan_args()),
                ('respond', {'text': '草案已保存，资格需要继续核实。'})]
            return original(state, settings=SETTINGS, client=ScriptClient(script), **kwargs)
        with patch('routers.multi_agent.MultiAgentRuntime', side_effect=runtime), \
             patch('agents.research.SessionLocal', self.sessions):
            waiting = self.client.post(url + '/messages?wait=true', json={'request_id': 'one', 'text': '研究计算机'}).json()
            self.assertEqual(waiting['status'], 'waiting')
            body = {'request_id': 'two', 'text': '确认', 'confirmed_ids': [waiting['confirmation_id']]}
            completed = self.client.post(url + '/messages?wait=true', json=body)
        self.assertEqual(completed.json()['status'], 'completed')
        restored = self.client.get(url).json()
        state = restored['state']
        self.assertFalse(restored['running'])
        self.assertEqual(len(state['plans']), 1)
        plan = state['artifacts'][state['plans'][0]]
        self.assertEqual(plan['rows'][0]['university_name'], '测试大学')
        self.assertTrue(plan['test_only'])
        self.assertFalse(plan['ready_for_submission'])
        self.assertTrue(any(t['tool'] == 'submit_plan' and not t['ok'] for t in state['trace']))
        self.assertEqual(self.client.post(url + '/messages?wait=true', json=body).json(), completed.json())

    def test_history_uses_actual_database_years(self):
        with self.sessions() as db:
            db.add(University(university_id='u', name='测试', province='广东', city='广州', university_type='普通'))
            for year in (2022, 2026):
                db.add(AdmissionRecord(university_id='u', university_name='测试', major_name='计算机',
                    enrollment_type='普通', province='广东', subject_type='物理类', year=year, min_rank=20000))
            db.commit()
        with patch('agents.research.SessionLocal', self.sessions):
            result = query_history('计算机')
        self.assertEqual(result['covered_years'], [2022, 2026])
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(len(result['candidates'][0]['admission_history']), 2)
