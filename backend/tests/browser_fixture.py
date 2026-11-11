"""Isolated browser fixture. Runs the real HTTP API and worker with a scripted model."""
import asyncio

from agents.runtime import MultiAgentRuntime
from database.models import AdmissionRecord, SessionLocal, University
from main import app
from routers import multi_agent
from test_multi_agent import ScriptClient, SETTINGS


class BrowserRuntime(MultiAgentRuntime):
    def __init__(self, state, **kwargs):
        super().__init__(state, settings=SETTINGS, **kwargs)

    async def run(self, text, confirmed_ids=()):
        def artifact_id(role):
            return next(key for key, value in self.state['artifacts'].items() if value['role'] == role)

        def report():
            return 'submit_report', {'findings': '浏览器验收资料', 'missing': ['目标年度资格'],
                'source_ids': [c['source_id'] for c in self.state['candidates'].values()]}

        def plan():
            candidate = next(iter(self.state['candidates'].values()))
            return 'submit_plan', {'strategy': '参考历史，核实当年资格', 'rows': [{
                'candidate_id': candidate['candidate_id'], 'source_ids': [candidate['source_id']],
                'reason': '匹配测试兴趣', 'tradeoffs': '资格待核实',
            }]}

        if confirmed_ids:
            script = [
                ('delegate_task', {'agent': 'admissions', 'goal': '查历史资料'}),
                ('query_candidates', {'major': '计算机', 'limit': 1}),
                report,
                lambda: ('delegate_task', {'agent': 'planner', 'goal': '生成方案',
                                            'artifact_ids': [artifact_id('admissions')]}),
                lambda: ('read_artifact', {'artifact_id': artifact_id('admissions')}),
                plan,
                ('respond', {'text': '已保存浏览器验收方案。'}),
            ]
        elif text == '记录数学兴趣':
            script = [
                ('propose_memory', {'kind': 'soft', 'field': 'interest', 'quote': text, 'value': '数学'}),
                ('respond', {'text': '已记录数学兴趣。'}),
            ]
        else:
            script = [('request_confirmation', {'text': '开始院校调查？'})]
        self.client = SlowClient(script) if text == '测试停止研究' else ScriptClient(script)
        return await super().run(text, confirmed_ids)


class SlowClient(ScriptClient):
    async def create(self, **kwargs):
        await asyncio.sleep(20)
        return await super().create(**kwargs)


multi_agent.MultiAgentRuntime = BrowserRuntime


@app.on_event('startup')
async def seed():
    with SessionLocal.begin() as db:
        for index in range(1, 13):
            school = f'浏览器验收大学{index:02d}'
            db.add(University(university_id=str(index), name=school, province='广东', city='广州', university_type='普通'))
            for year in (2024, 2025):
                db.add(AdmissionRecord(university_id=str(index), university_name=school, major_name='计算机',
                    enrollment_type='普通类', province='广东', subject_type='物理类', year=year,
                    min_score=600, min_rank=20000 + index))
