"""V2 model-facing contracts. IDs, authorization and versions are server-owned."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal['coordinator', 'major_research', 'admissions', 'planner']

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @field_validator('*', mode='after')
    @classmethod
    def meaningful_values(cls, value):
        if isinstance(value, str) and value and not value.strip():
            raise ValueError('文本不能只包含空白')
        if isinstance(value, list):
            if any(isinstance(item, str) and not item.strip() for item in value):
                raise ValueError('列表不能包含空白项')
            if all(isinstance(item, (str, int)) for item in value) and len(value) != len(set(value)):
                raise ValueError('列表不能包含重复项')
        return value

class Search(Input):
    query: str = Field(min_length=2, max_length=300)

class Query(Input):
    major: str = Field(default='', max_length=80)
    school: str = Field(default='', max_length=100)
    city: str = Field(default='', max_length=50)
    subject_type: str = Field(default='', max_length=20)
    enrollment_type: str = Field(default='', max_length=50)
    years: list[int] = Field(default_factory=list, max_length=20)
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=20, ge=1, le=50)

    @field_validator('years')
    @classmethod
    def valid_years(cls, value):
        if any(year < 2000 or year > 2100 for year in value):
            raise ValueError('年份必须在 2000 至 2100 之间')
        return value

class ListArtifacts(Input):
    role: Literal['coordinator', 'major_research', 'admissions', 'planner', 'reviewer'] | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)

class Sources(Input):
    source_ids: list[str] = Field(min_length=1, max_length=20)

class Candidates(Input):
    candidate_ids: list[int] = Field(min_length=1, max_length=10)

    @field_validator('candidate_ids')
    @classmethod
    def positive_ids(cls, value):
        if any(cid < 1 for cid in value):
            raise ValueError('候选编号必须为正整数')
        return value

class Analysis(Candidates):
    target_year: int | None = Field(default=None, ge=2000, le=2100)

class Comparison(Analysis):
    candidate_ids: list[int] = Field(min_length=2, max_length=10)

class Read(Input):
    artifact_id: str = Field(min_length=1)

class Delegate(Input):
    agent: Literal['major_research', 'admissions', 'planner']
    goal: str = Field(min_length=1, max_length=2000)
    artifact_ids: list[str] = Field(default_factory=list, max_length=10)
    review_id: str | None = Field(default=None, min_length=1, max_length=100)

class Report(Input):
    findings: str = Field(min_length=1, max_length=12000)
    source_ids: list[str] = Field(default_factory=list, max_length=60)
    missing: list[str] = Field(default_factory=list, max_length=30)

class Row(Input):
    candidate_id: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    tradeoffs: str = Field(min_length=1, max_length=2000)
    source_ids: list[str] = Field(min_length=1, max_length=20)

class Plan(Input):
    rows: list[Row] = Field(min_length=1, max_length=45)
    strategy: str = Field(min_length=1, max_length=4000)
    target_year: int | None = Field(default=None, ge=2000, le=2100)
    base_plan_id: str | None = Field(default=None, min_length=1, max_length=100)

class Respond(Input):
    text: str = Field(min_length=1, max_length=12000)

class Memory(Input):
    kind: Literal['soft', 'hard']
    field: Literal['interest', 'career_goal', 'learning_preference', 'excluded_city', 'annual_budget_max']
    value: str = Field(min_length=1, max_length=300)
    quote: str = Field(min_length=1, max_length=500)

SCHEMAS = {'get_profile': Input, 'list_artifacts': ListArtifacts,
    'read_sources': Sources, 'get_candidate_details': Candidates,
    'analyze_historical_risk': Analysis, 'compare_candidates': Comparison, 'validate_plan': Plan,
    'search_sources': Search, 'query_candidates': Query,
    'read_artifact': Read, 'delegate_task': Delegate, 'submit_report': Report,
    'submit_plan': Plan, 'respond': Respond, 'request_confirmation': Respond,
    'propose_memory': Memory}
PERMISSIONS = {
    'coordinator': {'delegate_task', 'read_artifact', 'respond', 'request_confirmation', 'propose_memory'},
    'major_research': {'search_sources', 'read_artifact', 'submit_report'},
    'admissions': {'search_sources', 'query_candidates', 'read_artifact', 'submit_report'},
    'planner': {'read_artifact', 'submit_report', 'submit_plan'},
}
for _role, _permissions in PERMISSIONS.items():
    _permissions.update({'get_profile', 'list_artifacts', 'read_sources'})
    if _role != 'major_research':
        _permissions.update({'get_candidate_details', 'analyze_historical_risk', 'compare_candidates'})
PERMISSIONS['planner'].add('validate_plan')

DESCRIPTIONS = {
    'get_profile': '读取当前画像、已确认记忆、版本、调查授权状态与缺失信息。',
    'list_artifacts': '分页列出当前画像版本的研究产物及测试表；支持按角色过滤。',
    'read_sources': '读取已查询且适用于当前画像版本的来源原始摘要或本地历史记录。',
    'get_candidate_details': '读取本会话已经查询的候选事实、历史数据和硬约束差异；不发起新调查。',
    'analyze_historical_risk': '按当前画像位次分析最近三个有数据年份的历史位次、中位数、波动及差额。不是概率；可指定目标年排除该年及之后数据。',
    'compare_candidates': '对比已查询候选的学校专业、城市、约束差异和历史位次；保持输入顺序，不替用户选定。',
    'validate_plan': '预检草案的候选、已读来源、版本、重复项、硬约束和修订引用，返回可修正错误，不保存方案。',
    'search_sources': '检索公开资料并返回来源编号、摘要和可信度类别；未配置时明确失败。不得发送个人身份信息。',
    'query_candidates': '查询广东数据库实际覆盖年份的专业历史数据；不会证明当年报考资格。',
    'read_artifact': '按编号读取本会话当前画像版本的完整研究报告或测试表。',
    'delegate_task': '委派一个专业子 Agent，等待并获得其产物编号和摘要。院校调查与方案需要用户授权。',
    'submit_report': '结束本次专业研究，保存结构化结论、实际来源引用和未知项；不能编造来源编号。',
    'submit_plan': '按输入顺序保存测试志愿草稿；校验候选及引用，隔离明确不符合条件者，不代表正式填报资格。',
    'respond': '结束本轮并向用户回答；不要展示内部思考。',
    'request_confirmation': '创建本轮选校调查/表格更新的待确认事项并暂停，不能自行授权。',
    'propose_memory': '引用本轮原话记录明确软偏好并提示；硬约束只创建待用户确认的提案。',
}

def tool_definitions(role: Role):
    return [{'type': 'function', 'function': {'name': name,
        'description': DESCRIPTIONS[name], 'parameters': SCHEMAS[name].model_json_schema()}}
        for name in sorted(PERMISSIONS[role])]
