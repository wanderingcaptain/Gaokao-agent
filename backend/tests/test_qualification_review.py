import asyncio
import copy
import unittest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch
from database.models import Base, AdmissionPolicy
from services.qualification import assess, import_policies, lookup_policies
from services.plan_review import review_plan
from agents.runtime import MultiAgentRuntime, new_state
from contracts.artifacts import ReviewReport, validate_handoff
from contracts.storage import ArtifactEnvelope
from test_multi_agent import ScriptClient, SETTINGS
from test_research_tools import fact, plan


def policy(pid='p1', **kwargs):
    return {'policy_id': pid, 'university_id': 'u1', 'major_name': '专业1', 'enrollment_type': '普通类',
        'province': '广东', 'subject_type': '物理类', 'target_year': 2027, 'group_code': '201',
        'required_subjects': ['物理','化学'], 'enrollment_count': 10, 'annual_tuition': 6000,
        'annual_total_cost': 20000, 'campus': '主校区', 'source_url': 'https://example.edu.cn/policy',
        'source_title': '测试规则（模拟）', 'reviewed': True, 'reviewed_by': 'synthetic-reviewer', **kwargs}


def profile():
    return {'basic_info': {'province': '广东', 'subject_type': '物理类', 'province_rank': 20000,
        'target_year': 2027, 'selected_subjects': ['物理','化学','生物']},
        'constraints': {'annual_budget_max': 30000, 'accept_sino_foreign': False}}


class QualificationTests(unittest.TestCase):
    def test_eligible_ineligible_unknown_and_conflicting(self):
        candidate, user = fact(), profile()
        self.assertEqual(assess(candidate, user, 2027, [policy()])['status'], 'eligible')
        self.assertEqual(assess(candidate, user, 2027, [])['status'], 'unknown')
        self.assertEqual(assess(candidate, user, 2027, [policy(target_year=2026)])['status'], 'unknown')
        self.assertEqual(assess(candidate, user, 2027, [policy(reviewed=False)])['status'], 'unknown')
        self.assertEqual(assess(candidate, user, 2027, [policy(), policy('p2', annual_tuition=9000)])['status'], 'conflicting')
        self.assertEqual(assess(candidate, user, 2027, [policy(), policy('p2', group_code='202')])['status'], 'unknown')
        self.assertEqual(assess(candidate, user, 2027, [policy(enrollment_count=0)])['status'], 'ineligible')
        self.assertEqual(assess(candidate, user, 2027, [policy(annual_tuition=40000)])['status'], 'ineligible')
        user['basic_info']['selected_subjects']=['物理','生物','地理']
        self.assertEqual(assess(candidate, user, 2027, [policy()])['status'], 'ineligible')

    def test_incomplete_requirements_and_budget_are_never_assumed(self):
        for changes in ({'required_subjects': None}, {'enrollment_count': None},
                        {'group_code': None}, {'annual_total_cost': None}, {'other_requirements':['色觉要求']}):
            with self.subTest(changes=changes):
                self.assertEqual(assess(fact(), profile(), 2027, [policy(**changes)])['status'], 'unknown')
        result = assess(fact(), profile(), 2027, [policy(), policy('p2', annual_total_cost=24000, supersedes=['p1'])])
        self.assertEqual(result['status'], 'eligible')
        self.assertEqual(result['policy_ids'], ['p2'])

    def test_import_validation_idempotency_immutability_and_cycles(self):
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        sessions=sessionmaker(bind=engine)
        with sessions() as db:
            import_policies(db,[policy()]);db.commit()
            import_policies(db,[policy()]);db.commit()
            self.assertEqual(len(list(db.scalars(select(AdmissionPolicy)))),1)
            with self.assertRaises(ValueError): import_policies(db,[policy(annual_tuition=1)])
            with self.assertRaises(ValueError): import_policies(db,[policy('p2',supersedes=['missing'])])
            with self.assertRaises(ValueError): import_policies(db,[policy('p2',supersedes=['p3']),policy('p3',supersedes=['p2'])])
            with self.assertRaises(ValueError): import_policies(db,[policy('p2',province='浙江',supersedes=['p1'])])
        with patch('services.qualification.SessionLocal',sessions):
            self.assertEqual(lookup_policies(fact(),2027)[0]['group_code'],'201')
            self.assertEqual(lookup_policies(fact(),2026),[])
        engine.dispose()


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    async def prepare(self, policies):
        state=new_state();state['profile']=profile()
        rt=MultiAgentRuntime(state,settings=SETTINGS,client=ScriptClient([]),policies=lambda c,y: policies)
        rt.authorized=True
        rt.query=lambda **kwargs:{'ok':True,'candidates':[fact()]}
        await rt.execute('admissions','query_candidates',{})
        report=await rt.execute('admissions','submit_report',{'findings':'历史资料','source_ids':['db1']})
        await rt.execute('planner','read_artifact',{'artifact_id':report['artifact_id']})
        return rt

    async def test_blocking_review_is_stored_and_repair_produces_linked_review(self):
        rt=await self.prepare([policy()])
        bad=plan();bad['rows'][0]['source_ids']=['forged']
        rejected=await rt.execute('planner','submit_plan',bad)
        self.assertFalse(rejected['ok'])
        review=rt.state['artifacts'][rejected['review_id']]
        self.assertEqual(review['verdict'],'revise')
        self.assertEqual(review['repair_tasks'][0]['agent'],'planner')
        self.assertEqual(rt.state['plans'],[])
        accepted=await rt.execute('planner','submit_plan',plan())
        artifact=rt.state['artifacts'][accepted['artifact_id']]
        self.assertEqual(artifact['rows'][0]['eligibility'],'eligible')
        self.assertFalse(artifact['ready_for_submission'])
        contract=ArtifactEnvelope.model_validate(artifact['contract'])
        report=ReviewReport.model_validate(rt.state['artifacts'][artifact['review_id']]['review_contract'])
        validate_handoff(contract.plan,contract.evidence_pack,{1},report)
        self.assertEqual(report.plan_id,artifact['artifact_id'])

    async def test_policy_conflict_blocks_and_rule_revision_invalidates_plan(self):
        policies=[policy(),policy('p2',annual_tuition=9000)]
        rt=await self.prepare(policies)
        rejected=await rt.execute('planner','submit_plan',plan())
        self.assertFalse(rejected['ok'])
        self.assertEqual(rejected['repair_tasks'][0]['agent'],'admissions')
        policies.pop()
        result=await rt.execute('planner','submit_plan',plan())
        await rt.execute('planner','read_artifact',{'artifact_id':result['artifact_id']})
        policies.append(policy('p2',annual_total_cost=50000,supersedes=['p1']))
        with self.assertRaises(ValueError):
            await rt.execute('planner','read_artifact',{'artifact_id':result['artifact_id']})

    async def test_targeted_repair_routes_only_requested_role_and_is_bounded(self):
        rt=await self.prepare([policy(),policy('p2',annual_tuition=9000)])
        result=await rt.execute('planner','submit_plan',plan())
        args={'agent':'admissions','goal':'核对冲突资格','review_id':result['review_id']}
        for _ in range(2):
            task=await rt.execute('coordinator','delegate_task',args)
            self.assertTrue(task['repair_tasks'])
            self.assertIn(task['subtask_id'],rt.state['subtasks'])
        with self.assertRaises(ValueError):
            await rt.execute('coordinator','delegate_task',args)
        with self.assertRaises(ValueError):
            await rt.execute('coordinator','delegate_task',{**args,'agent':'major_research'})
