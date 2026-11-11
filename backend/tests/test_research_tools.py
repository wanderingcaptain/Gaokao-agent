"""V2 tool behavior: real SQL/HTTP adapter paths with isolated data and scripted models."""
import asyncio
import copy
import json
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import httpx
from jsonschema import Draft202012Validator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agents.research import query_history, search_web, public_query
from agents.runtime import MultiAgentRuntime, new_state, BudgetExceeded
from agents.schemas import SCHEMAS, PERMISSIONS, tool_definitions
from database.models import Base, University, AdmissionRecord
from tools.planning import historical_risk
from tools.recommendation import _fallback_candidates
from test_multi_agent import ScriptClient, SETTINGS, candidate


def fact(cid=1, **changes):
    return {**candidate(), 'candidate_id': cid, 'source_id': f'db{cid}',
        'major_name': f'专业{cid}', 'province': '广东',
        'admission_history': [{'year': 2023, 'min_rank': 18000},
                              {'year': 2024, 'min_rank': 20000},
                              {'year': 2025, 'min_rank': 22000}], **changes}


def plan(*ids, **kwargs):
    return {'strategy': '按兴趣比较，资格待核实', 'rows': [
        {'candidate_id': cid, 'reason': '课程匹配', 'tradeoffs': '资格待核实',
         'source_ids': [f'db{cid}']} for cid in (ids or (1,))], **kwargs}


class ResearchToolTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, script=(), **kwargs):
        state = new_state()
        state['profile'] = {'basic_info': {'province': '广东', 'subject_type': '物理类',
            'province_rank': 20000}, 'constraints': {'accept_sino_foreign': False}}
        return MultiAgentRuntime(state, client=ScriptClient(script), settings=SETTINGS, **kwargs)

    async def researched(self, rt, candidates=None):
        candidates = candidates or [fact(), fact(2, city='深圳')]
        rt.query = lambda **kwargs: {'ok': True, 'candidates': candidates}
        rt.authorized = True
        await rt.execute('admissions', 'query_candidates', {})
        report = await rt.execute('admissions', 'submit_report', {'findings': '历史数据',
            'source_ids': [c['source_id'] for c in candidates], 'missing': ['当年资格']})
        rt.read_ids, rt.read_source_ids = set(), set()
        return report['artifact_id']

    async def test_all_declared_tools_have_success_paths(self):
        rt = self.runtime()
        exercised = set()
        async def call(role, name, args):
            result = await rt.execute(role, name, args)
            self.assertNotEqual(result.get('ok'), False, (name, result))
            exercised.add(name)
            return result
        await call('coordinator', 'get_profile', {})
        rt.user_text = '喜欢数学'
        await call('coordinator', 'propose_memory', {'kind': 'soft', 'field': 'interest',
            'value': '数学', 'quote': '喜欢数学'})
        approval = await call('coordinator', 'request_confirmation', {'text': '开始研究院校？'})
        self.assertIn(approval['confirmation_id'], rt.state['pending'])
        rt.authorized = True
        await call('coordinator', 'delegate_task', {'agent': 'admissions', 'goal': '调查专业'})
        async def search(query):
            return {'ok': True, 'sources': [{'source_id': 'web1', 'text': '培养课程'}]}
        rt.search = search
        await call('major_research', 'search_sources', {'query': '学校课程'})
        rt.query = lambda **kwargs: {'ok': True, 'candidates': [fact(), fact(2)]}
        await call('admissions', 'query_candidates', {})
        report = await call('admissions', 'submit_report', {'findings': '历史参考',
            'source_ids': ['db1', 'db2'], 'missing': ['资格']})
        await call('planner', 'list_artifacts', {'role': 'admissions'})
        content = await call('planner', 'read_artifact', {'artifact_id': report['artifact_id']})
        self.assertEqual(len(content['candidates']), 2)
        await call('planner', 'read_sources', {'source_ids': ['web1']})
        await call('coordinator', 'get_candidate_details', {'candidate_ids': [1]})
        await call('planner', 'analyze_historical_risk', {'candidate_ids': [1]})
        await call('planner', 'compare_candidates', {'candidate_ids': [2, 1]})
        validated = await call('planner', 'validate_plan', plan(1, 2))
        self.assertTrue(validated['valid'])
        self.assertEqual(rt.state['plans'], [])
        await call('planner', 'submit_plan', plan(1, 2))
        await call('coordinator', 'respond', {'text': '测试草案已保存'})
        self.assertEqual(exercised, set(SCHEMAS))

    async def test_every_schema_and_role_permission_is_enforced(self):
        rt = self.runtime()
        for role, permissions in PERMISSIONS.items():
            definitions = tool_definitions(role)
            self.assertEqual({d['function']['name'] for d in definitions}, permissions)
            for definition in definitions:
                Draft202012Validator.check_schema(definition['function']['parameters'])
            for name in SCHEMAS:
                with self.subTest(role=role, name=name):
                    with self.assertRaises(ValueError):
                        await rt.execute(role, name, {'unexpected': 'reject'})
        with self.assertRaises(ValueError):
            await rt.execute('not-a-role', 'respond', {'text': 'no'})
        for name, args in [('respond', {'text': '  '}),
                           ('query_candidates', {'offset': True}),
                           ('query_candidates', {'years': [2025, 2025]}),
                           ('query_candidates', {'years': [1900]}),
                           ('get_candidate_details', {'candidate_ids': [0]}),
                           ('get_candidate_details', {'candidate_ids': [1, 1]}),
                           ('compare_candidates', {'candidate_ids': [1]})]:
            role = next(role for role, perms in PERMISSIONS.items() if name in perms)
            with self.assertRaises(ValueError):
                await rt.execute(role, name, args)

    async def test_handoff_contains_real_facts_and_requires_current_admissions_report(self):
        rt = self.runtime()
        report = await self.researched(rt)
        before = await rt.execute('planner', 'validate_plan', plan())
        self.assertFalse(before['valid'])
        item = await rt.execute('planner', 'read_artifact', {'artifact_id': report})
        self.assertEqual(item['candidates'][0]['admission_history'][0]['min_rank'], 18000)
        self.assertTrue((await rt.execute('planner', 'validate_plan', plan()))['valid'])
        item['candidates'][0]['major_name'] = 'tampered'
        self.assertEqual(rt.state['candidates']['1']['major_name'], '专业1')

    async def test_plan_invalid_then_repair_and_revision_preserves_prior_order(self):
        rt = self.runtime()
        report = await self.researched(rt)
        await rt.execute('planner', 'read_artifact', {'artifact_id': report})
        bad = plan()
        bad['rows'][0]['source_ids'] = ['db2']
        self.assertFalse((await rt.execute('planner', 'submit_plan', bad))['ok'])
        self.assertEqual(rt.state['plans'], [])
        self.assertFalse((await rt.execute('planner', 'validate_plan', plan(1, 1)))['valid'])
        first = await rt.execute('planner', 'submit_plan', plan(1, 2))
        snapshot = copy.deepcopy(rt.state['artifacts'][first['artifact_id']])
        self.assertFalse((await rt.execute('planner', 'submit_plan', plan(2, 1)))['ok'])
        await rt.execute('planner', 'read_artifact', {'artifact_id': first['artifact_id']})
        second = await rt.execute('planner', 'submit_plan', plan(2, 1, base_plan_id=first['artifact_id']))
        self.assertEqual(rt.state['artifacts'][first['artifact_id']], snapshot)
        self.assertEqual(rt.state['artifacts'][second['artifact_id']]['plan_version'], 2)
        self.assertEqual(rt.state['artifacts'][second['artifact_id']]['rows'][0]['candidate_id'], 2)
        self.assertFalse((await rt.execute('planner', 'submit_plan', plan(base_plan_id=first['artifact_id'])))['ok'])

    async def test_excludes_province_subject_city_and_sino_foreign_but_budget_stays_unknown(self):
        rt = self.runtime()
        rt.state['profile']['constraints'].update(excluded_cities=['深圳'], annual_budget_max=0)
        report = await self.researched(rt, [fact(), fact(2, city='深圳'),
            fact(3, subject_type='历史类'), fact(4, is_sino_foreign=True), fact(5, province='浙江')])
        await rt.execute('planner', 'read_artifact', {'artifact_id': report})
        result = await rt.execute('planner', 'validate_plan', plan(1, 2, 3, 4, 5))
        self.assertTrue(result['valid'])
        self.assertEqual([r['candidate_id'] for r in result['rows']], [1])
        self.assertEqual(len(result['excluded']), 4)
        self.assertIn('学费与预算', result['rows'][0]['unverified'])
        self.assertFalse(result['ready_for_submission'])

    async def test_new_snapshot_does_not_overwrite_old_evidence_or_reuse_old_report(self):
        rt = self.runtime()
        report = await self.researched(rt)
        old_source = copy.deepcopy(rt.state['sources']['db1'])
        rt.query = lambda **kwargs: {'ok': True, 'candidates': [fact(source_id='db1_new', min_rank=10000)]}
        await rt.execute('admissions', 'query_candidates', {})
        self.assertEqual(rt.state['sources']['db1'], old_source)
        with self.assertRaises(ValueError):
            await rt.execute('planner', 'read_artifact', {'artifact_id': report})
        with self.assertRaises(ValueError):
            await rt.execute('planner', 'read_sources', {'source_ids': ['db1']})
        self.assertEqual((await rt.execute('planner', 'list_artifacts', {}))['total'], 0)

    async def test_source_collision_and_failed_search_are_atomic(self):
        rt = self.runtime()
        rt.store_sources([{'source_id': 'web1', 'text': 'original'}])
        with self.assertRaises(ValueError):
            rt.store_sources([{'source_id': 'web2', 'text': 'new'}, {'source_id': 'web1', 'text': 'overwrite'}])
        self.assertNotIn('web2', rt.state['sources'])
        async def failed(query):
            return {'ok': False, 'sources': [{'source_id': 'forged', 'text': 'no'}]}
        rt.search = failed
        await rt.execute('major_research', 'search_sources', {'query': '学校资料'})
        self.assertNotIn('forged', rt.state['sources'])

    async def test_changed_data_allows_new_plan_without_reusing_stale_base(self):
        rt = self.runtime()
        report = await self.researched(rt)
        await rt.execute('planner', 'read_artifact', {'artifact_id': report})
        first = await rt.execute('planner', 'submit_plan', plan())
        report = await self.researched(rt, [fact(source_id='db1_new', min_rank=10000)])
        await rt.execute('planner', 'read_artifact', {'artifact_id': report})
        fresh = plan()
        fresh['rows'][0]['source_ids'] = ['db1_new']
        second = await rt.execute('planner', 'submit_plan', fresh)
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(rt.state['artifacts'][second['artifact_id']]['supersedes_plan_id'], first['artifact_id'])
        self.assertEqual(len(rt.state['plans']), 2)

    async def test_stale_hard_memory_can_be_proposed_again(self):
        rt = self.runtime()
        rt.user_text = '不去广州，喜欢数学'
        args = {'kind': 'hard', 'field': 'excluded_city', 'value': '广州', 'quote': '不去广州'}
        old = await rt.execute('coordinator', 'propose_memory', args)
        await rt.execute('coordinator', 'propose_memory', {'kind': 'soft', 'field': 'interest',
            'value': '数学', 'quote': '喜欢数学'})
        with self.assertRaises(ValueError):
            await rt.run('确认旧提案', [old['memory']['id']])
        rt.user_text = '不去广州'
        new = await rt.execute('coordinator', 'propose_memory', args)
        self.assertNotEqual(new['memory']['id'], old['memory']['id'])
        self.assertEqual(new['memory']['profile_version'], 2)

    async def test_memory_dedup_effective_constraints_and_stale_evidence(self):
        rt = self.runtime([('respond', {'text': '已确认'})])
        await self.researched(rt)
        rt.user_text = '喜欢数学，不去广州，预算30000'
        soft = {'kind': 'soft', 'field': 'interest', 'value': '数学', 'quote': '喜欢数学'}
        await rt.execute('coordinator', 'propose_memory', soft)
        await rt.execute('coordinator', 'propose_memory', soft)
        self.assertEqual(rt.state['profile_version'], 2)
        self.assertFalse(rt.authorized)
        with self.assertRaises(ValueError):
            await rt.execute('planner', 'get_candidate_details', {'candidate_ids': [1]})
        hard = await rt.execute('coordinator', 'propose_memory', {'kind': 'hard', 'field': 'annual_budget_max',
            'value': '30000', 'quote': '预算30000'})
        await rt.run('确认', [hard['memory']['id']])
        profile = await rt.execute('coordinator', 'get_profile', {})
        self.assertEqual(profile['profile']['constraints']['annual_budget_max'], 30000)
        self.assertEqual(rt.state['profile_version'], 3)

    async def test_confirmation_validation_is_atomic_and_authorization_does_not_leak(self):
        rt = self.runtime([('respond', {'text': '已确认'}), ('respond', {'text': '继续'})])
        rt.state['pending']['go'] = {'kind': 'research', 'profile_version': 1}
        before = copy.deepcopy(rt.state)
        with self.assertRaises(ValueError):
            await rt.run('重复', ['go', 'go'])
        self.assertEqual(rt.state, before)
        with self.assertRaises(ValueError):
            await rt.run('无效', ['go', 'missing'])
        self.assertEqual(rt.state, before)
        await rt.run('确认', ['go'])
        self.assertTrue(rt.authorized)
        await rt.run('下轮')
        self.assertFalse(rt.authorized)

    async def test_search_budget_privacy_and_unknown_reports(self):
        rt = self.runtime(max_searches=0)
        for query in ('姓名：张三 课程', '电话13800138000', 'a@example.com 学校'):
            with self.assertRaises(ValueError):
                await rt.execute('major_research', 'search_sources', {'query': query})
        self.assertEqual(rt.searches, 0)
        with self.assertRaises(BudgetExceeded):
            await rt.execute('major_research', 'search_sources', {'query': '学校课程'})
        with self.assertRaises(ValueError):
            await rt.execute('major_research', 'submit_report', {'findings': '没有来源还说完成'})
        result = await rt.execute('major_research', 'submit_report', {'findings': '资料未核实', 'missing': ['搜索未配置']})
        self.assertIn(result['artifact_id'], rt.state['artifacts'])

    async def test_terminal_batch_skips_later_mutations_and_keeps_message_pairing(self):
        rt = self.runtime()
        calls = [NS(id=str(i), function=NS(name=name, arguments=json.dumps(args)))
                 for i, (name, args) in enumerate([('respond', {'text': '完成'}),
                                                   ('request_confirmation', {'text': '不该执行'})])]
        messages = []
        self.assertEqual(await rt.tools('coordinator', NS(tool_calls=calls), messages), 'end')
        self.assertEqual(rt.state['pending'], {})
        self.assertEqual([m['tool_call_id'] for m in messages], ['0', '1'])
        self.assertFalse(json.loads(messages[1]['content'])['ok'])


class HistoricalRiskTests(unittest.TestCase):
    profile = {'basic_info': {'province': '广东', 'subject_type': '物理类', 'province_rank': 20000}}

    def test_actual_year_window_and_target_year_cutoff(self):
        c = fact(admission_history=[{'year': y, 'min_rank': rank}
            for y, rank in [(2022, 10000), (2024, 18000), (2025, 22000), (2026, 24000)]])
        result = historical_risk(c, self.profile)
        self.assertEqual(result['years'], [2024, 2025, 2026])
        self.assertEqual(result['reference_rank'], 22000)
        self.assertEqual(result['rank_margin'], 2000)
        prior = historical_risk(c, self.profile, 2026)
        self.assertEqual(prior['reference_rank'], 18000)
        self.assertNotIn(2026, prior['years'])
        self.assertIn('历史年份不连续', prior['warnings'])
        self.assertNotIn('probability', result)

    def test_missing_invalid_rank_and_subject_mismatch_stay_unknown(self):
        for rank in (0, -1, None, True):
            result = historical_risk(fact(admission_history=[{'year': 2026, 'min_rank': rank}]), self.profile)
            self.assertEqual(result['status'], 'insufficient_data')
            self.assertIsNone(result['reference_rank'])
        result = historical_risk(fact(subject_type='历史类'), self.profile)
        self.assertEqual(result['status'], 'insufficient_data')
        self.assertIsNone(result['rank_margin'])
        self.assertIsNone(historical_risk(fact(), {})['reference_to_user_ratio'])

    def test_fallback_does_not_mutate_candidate_pool_or_replace_zero_score(self):
        candidates = [{'candidate_id': 0, 'tier': '稳', 'fit_score': 0, 'risk_score': 70, 'warnings': []}]
        before = copy.deepcopy(candidates)
        result = _fallback_candidates(candidates)
        self.assertEqual(candidates, before)
        self.assertEqual(result[0]['fit_score'], 0)
        self.assertEqual(_fallback_candidates(candidates, 0), [])


class HistoryConnectorTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.patch = patch('agents.research.SessionLocal', self.sessions)
        self.patch.start()
        with self.sessions() as db:
            db.add(University(university_id='u1', name='测试大学', province='广东', city='广州', university_type='普通'))
            for i in range(23):
                for year in (2022, 2026):
                    db.add(AdmissionRecord(university_id='u1', university_name='测试大学',
                        major_name=f'专业{i:02}', enrollment_type='普通类', province='广东',
                        subject_type='物理类', year=year, min_rank=20000 + i, avg_score=600))
            db.add(AdmissionRecord(university_id='missing', university_name='无院校资料',
                major_name='孤立数据', enrollment_type='普通类', province='广东', subject_type='物理类', year=2026))
            db.commit()

    def tearDown(self):
        self.patch.stop()
        self.engine.dispose()

    def test_complete_pagination_filters_and_no_orphan_page_gaps(self):
        first = query_history()
        self.assertEqual((first['total'], first['returned'], first['next_offset']), (23, 20, 20))
        second = query_history(offset=first['next_offset'])
        self.assertEqual((second['returned'], second['next_offset'], second['remaining']), (3, None, 0))
        self.assertFalse(set(c['candidate_id'] for c in first['candidates']) & set(c['candidate_id'] for c in second['candidates']))
        filtered = query_history(major='专业01', school='测试', city='广州', subject_type='物理类', enrollment_type='普通类')
        self.assertEqual(filtered['total'], 1)
        self.assertEqual(query_history(subject_type='历史类')['total'], 0)
        self.assertEqual(query_history(major='%')['total'], 0)
        self.assertEqual(query_history(offset=1000)['next_offset'], None)

    def test_year_filter_keeps_identity_and_full_history_and_hash_tracks_changes(self):
        one = query_history('专业00')['candidates'][0]
        two = query_history('专业00', years=[2026])['candidates'][0]
        self.assertEqual(one, two)
        self.assertEqual([r['year'] for r in two['admission_history']], [2022, 2026])
        with self.sessions() as db:
            row = db.query(AdmissionRecord).filter_by(major_name='专业00', year=2026).one()
            row.min_rank = 19000
            db.commit()
        updated = query_history('专业00')['candidates'][0]
        self.assertEqual(one['candidate_id'], updated['candidate_id'])
        self.assertNotEqual(one['source_id'], updated['source_id'])
        self.assertEqual(query_history(years=[2025])['total'], 0)

    def test_empty_database_and_invalid_pagination(self):
        with self.assertRaises(ValueError):
            query_history(offset=-1)
        with self.assertRaises(ValueError):
            query_history(limit=1000)
        with self.sessions() as db:
            db.query(AdmissionRecord).delete()
            db.commit()
        self.assertEqual(query_history()['covered_years'], [])
        self.assertEqual(query_history()['candidates'], [])


class SearchConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, handler):
        real_client = httpx.AsyncClient
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'synthetic-key', 'RESEARCH_OFFICIAL_DOMAINS': 'eea.gd.gov.cn'}), \
             patch('agents.research.httpx.AsyncClient', side_effect=lambda **kwargs:
                   real_client(transport=httpx.MockTransport(handler), **kwargs)):
            return await search_web('学校招生课程')

    async def test_request_domain_allowlist_and_unverified_excerpt(self):
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={'results': [
                {'url': 'https://eea.gd.gov.cn/notice', 'title': '官方', 'content': '资格摘要'},
                {'url': 'https://eea.gd.gov.cn.evil.example/page', 'content': '非官方'},
                {'url': 'http://eea.gd.gov.cn/old', 'content': '无 https'},
                {'url': 'https://user:secret@eea.gd.gov.cn/page', 'content': '凭据'},
                {'url': 'https://example.com', 'content': None}]})
        result = await self.call(handler)
        self.assertEqual(len(result['sources']), 2)
        self.assertEqual(result['sources'][0]['authority'], 'official_domain')
        self.assertEqual(result['sources'][1]['authority'], 'supplementary_unverified')
        self.assertTrue(all(s['verification'] == 'unverified' for s in result['sources']))
        self.assertFalse(seen[0]['include_answer'])
        self.assertEqual(seen[0]['query'], '学校招生课程')

    async def test_provider_failure_malformed_results_and_no_error_secret_leaks(self):
        for status, body in [(429, {'secret': 'do-not-expose'}), (401, {}), (500, {}), (200, {'results': 'bad'})]:
            with self.subTest(status=status):
                result = await self.call(lambda request: httpx.Response(status, json=body))
                self.assertFalse(result['ok'])
                self.assertNotIn('do-not-expose', json.dumps(result))
        def timeout(request):
            raise httpx.ReadTimeout('secret detail', request=request)
        self.assertEqual((await self.call(timeout))['error'], 'search_unavailable')
        self.assertTrue((await self.call(lambda request: httpx.Response(200, json={'results': []})))['ok'])

    async def test_stable_content_hash_dedup_and_content_revision(self):
        async def source(content):
            return await self.call(lambda request: httpx.Response(200, json={'results': [
                {'url': 'https://example.com', 'content': content}] * 2}))
        first, repeat, changed = await source('课程1'), await source('课程1'), await source('课程2')
        self.assertEqual(len(first['sources']), 1)
        self.assertEqual(first, repeat)
        self.assertNotEqual(first['sources'][0]['source_id'], changed['sources'][0]['source_id'])
