"""Bounded synthetic evaluation: --mode offline (default) or --mode live with local credentials."""
import argparse
import asyncio
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace as NS
from dotenv import load_dotenv
from agents.runtime import MultiAgentRuntime, new_state
from providers.client import ModelSettings


class ScriptedModel:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.chat = NS(completions=NS(create=self.create))
    async def create(self, **kwargs):
        action = next(self.steps)
        name, args = action() if callable(action) else action
        call = NS(id=str(time.monotonic_ns()), function=NS(name=name, arguments=json.dumps(args)))
        return NS(choices=[NS(message=NS(content='', tool_calls=[call]))])


def candidate():
    return {'candidate_id':1, 'university_id':'synthetic-university', 'university_name':'合成样本大学（非真实院校）',
        'major_name':'计算机科学与技术', 'enrollment_type':'普通类', 'subject_type':'物理类', 'province':'广东',
        'city':'广州', 'source_id':'synthetic_db_1', 'admission_history':[{'year':2025,'min_rank':20000},
        {'year':2026,'min_rank':21000}], 'eligibility':'unknown'}


def script(case, state):
    if case['expect'] == 'conversation':
        return [('propose_memory', {'kind':'soft','field':'interest','value':'数学与动手解决问题',
            'quote':'我喜欢数学和动手解决问题'}), ('respond', {'text':'已记录兴趣。我们可以从课程与学习活动开始比较专业方向。'})]
    if case['expect'] == 'confirmation':
        return [('request_confirmation', {'text':'是否按现有条件开始院校调查？'})]
    def report():
        return next(aid for aid,a in state['artifacts'].items() if a['role']=='admissions')
    return [('delegate_task', {'agent':'admissions','goal':'调查计算机历史'}),
        ('query_candidates', {'major':'计算机'}),
        ('submit_report', {'findings':'合成历史资料，资格尚未核实','source_ids':['synthetic_db_1'],'missing':['当年资格']}),
        lambda: ('delegate_task', {'agent':'planner','goal':'形成研究草案','artifact_ids':[report()]}),
        lambda: ('read_artifact', {'artifact_id':report()}),
        ('analyze_historical_risk', {'candidate_ids':[1], 'target_year':2027}),
        ('submit_plan', {'strategy':'按兴趣与历史资料形成测试草案','rows':[{'candidate_id':1,
            'reason':'符合计算机探索方向','tradeoffs':'当年资格仍需核实','source_ids':['synthetic_db_1']}]}),
        ('respond', {'text':'研究草案已保存；所有资格未知项和审核记录可查看。'})]


async def evaluate(case, mode='offline', max_calls=16):
    state=new_state()
    state['session_id']='eval_'+case['id']
    state['profile']={'basic_info':{'province':'广东','subject_type':'物理类','province_rank':20000,'target_year':2027},
                      'constraints':{'annual_budget_max':30000,'accept_sino_foreign':False}}
    confirmed=[]
    if case['authorized']:
        state['pending']['approval']={'kind':'research','profile_version':1}
        confirmed=['approval']
    async def search(query):
        return {'ok':False,'error':'evaluation_search_disabled','sources':[]}
    settings=ModelSettings.from_env() if mode=='live' else ModelSettings('qwen','scripted','https://example.invalid','synthetic')
    runtime=MultiAgentRuntime(state, settings=settings, max_calls=max_calls,
        client=ScriptedModel(script(case,state)) if mode=='offline' else None,
        search=search, query=lambda **kwargs:{'ok':True,'candidates':[candidate()],'total':1,'next_offset':None},
        policies=lambda c,y:[])
    start=time.monotonic()
    result=await runtime.run(case['text'],confirmed)
    checks={'bounded':runtime.calls<=max_calls, 'valid_status':result['status'] in {'completed','waiting'}}
    if case['expect']=='conversation':
        checks['no_school_research']=not any(t['tool']=='query_candidates' and t['ok'] for t in state['trace'])
    elif case['expect']=='confirmation':
        checks['asks_confirmation']=result['status']=='waiting' and bool(result.get('confirmation_id'))
        checks['no_unauthorized_plan']=not state['plans']
    else:
        checks['plan_saved']=bool(state['plans'])
        checks['review_saved']=bool(state.get('reviews'))
        checks['preserves_unknown']=all(row['eligibility']=='unknown' and row['source_id']=='synthetic_db_1'
            for aid in state['plans'] for row in state['artifacts'][aid]['rows'])
    metrics=runtime.execution.get('metrics',[])
    return {'case_id':case['id'],'mode':mode,'passed':all(checks.values()),'checks':checks,
        'status':result['status'],'provider_error':result.get('provider_error'),
        'duration_seconds':round(time.monotonic()-start,3),
        'model_calls':runtime.calls,'tool_calls':runtime.tool_calls,
        'tokens':sum(m.get('usage',{}).get('total_tokens',0) for m in metrics),
        'failed_steps':sum(not m['ok'] for m in metrics),
        'scope':'真实模型+合成数据工具' if mode=='live' else '脚本模型+合成数据工具'}


async def main():
    load_dotenv(Path(__file__).resolve().parents[1]/'.env')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['offline','live'],default='offline')
    parser.add_argument('--case')
    parser.add_argument('--max-calls',type=int,default=16)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if not 1<=args.max_calls<=24:
        parser.error('--max-calls 必须在 1–24 之间')
    cases=json.loads((Path(__file__).resolve().parents[1]/'evals/cases.json').read_text())
    if args.case:
        cases=[case for case in cases if case['id']==args.case]
        if not cases: parser.error('评测案例不存在')
    results=[await evaluate(case,args.mode,args.max_calls) for case in cases]
    report={'mode':args.mode,'passed':sum(r['passed'] for r in results),'total':len(results),'results':results}
    output=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(output+'\n')
    print(output)
    if not all(r['passed'] for r in results):
        raise SystemExit(1)


if __name__=='__main__':
    asyncio.run(main())
