"""Annual qualification checks. Missing evidence and conflicting policies stay explicit."""
import json
from sqlalchemy import select
from database.models import AdmissionPolicy, SessionLocal
from contracts.admissions import AdmissionPolicyInput
from contracts.storage import digest

SCOPE_KEYS = ('university_id', 'major_name', 'enrollment_type', 'province', 'subject_type', 'target_year')


def import_policies(db, payloads):
    policies = [AdmissionPolicyInput.model_validate(p) for p in payloads]
    by_id = {p.policy_id: p for p in policies}
    if len(by_id) != len(policies):
        raise ValueError('规则编号重复')
    for policy in policies:
        body = policy.model_dump()
        existing = db.get(AdmissionPolicy, policy.policy_id)
        if existing and json.loads(existing.body_json) != body:
            raise ValueError('规则编号已存在且内容不同，请使用新编号和 supersedes')
        for old_id in policy.supersedes:
            old = by_id.get(old_id)
            if old is None:
                row = db.get(AdmissionPolicy, old_id)
                old = AdmissionPolicyInput.model_validate_json(row.body_json) if row else None
            if old is None or old_id == policy.policy_id or any(getattr(old, k) != getattr(policy, k) for k in SCOPE_KEYS):
                raise ValueError('替代规则不存在、引用自身或适用范围不同')
    # Reject cycles across both newly imported and existing policy versions.
    def visit(pid, stack):
        if pid in stack:
            raise ValueError('替代规则存在循环')
        policy = by_id.get(pid)
        if policy is None:
            row = db.get(AdmissionPolicy, pid)
            policy = AdmissionPolicyInput.model_validate_json(row.body_json) if row else None
        for parent in policy.supersedes if policy else []:
            visit(parent, stack | {pid})
    for pid in by_id:
        visit(pid, set())
    for policy in policies:
        if db.get(AdmissionPolicy, policy.policy_id) is None:
            body = policy.model_dump()
            db.add(AdmissionPolicy(id=policy.policy_id, **{k: body[k] for k in SCOPE_KEYS},
                body_json=json.dumps(body, ensure_ascii=False)))
    return len(policies)


def lookup_policies(candidate, target_year):
    if not target_year:
        return []
    with SessionLocal() as db:
        return [json.loads(row.body_json) for row in db.scalars(select(AdmissionPolicy).where(
            AdmissionPolicy.university_id == candidate['university_id'],
            AdmissionPolicy.major_name == candidate['major_name'],
            AdmissionPolicy.enrollment_type == candidate['enrollment_type'],
            AdmissionPolicy.province == candidate.get('province', '广东'),
            AdmissionPolicy.subject_type == candidate['subject_type'],
            AdmissionPolicy.target_year == target_year))]


def assess(candidate, profile, target_year, policies):
    basic, constraints = profile.get('basic_info', {}), profile.get('constraints', {})
    target_year = target_year or basic.get('target_year')
    expected = {**candidate, 'province': candidate.get('province', '广东'), 'target_year': target_year}
    matched = [p for p in policies if p.get('reviewed') and all(p.get(k) == expected.get(k) for k in SCOPE_KEYS)]
    superseded = {pid for p in matched for pid in p.get('supersedes', [])}
    active = [p for p in matched if p['policy_id'] not in superseded]
    result = {'status': 'unknown', 'target_year': target_year, 'policy_ids': [p['policy_id'] for p in active],
        'sources': [{'policy_id': p['policy_id'], 'url': p['source_url'], 'title': p['source_title'],
                     'reviewed_by': p['reviewed_by']} for p in active],
        'data_version': digest(active), 'checks': [], 'missing': [], 'differences': [],
        'rule_snapshots': active,
        'scope': '仅检查已建模条件，不能代替官方报考资格审核'}
    if not target_year or not active:
        result['missing'] = ['目标年度未明确' if not target_year else '缺少适用年度且经过核实的招生规则']
        return result
    # Multiple groups need an explicit enrollment-unit mapping; don't choose one by name.
    group_codes = {p.get('group_code') for p in active}
    if len(group_codes) > 1:
        result['missing'] = ['同专业有多个专业组，需明确目标招生单位']
        return result
    substantive = ('group_code', 'required_subjects', 'enrollment_count', 'annual_tuition',
                   'annual_total_cost', 'campus', 'other_requirements')
    if len({digest({k: p.get(k) for k in substantive}) for p in active}) > 1:
        result.update(status='conflicting', missing=['同一招生单位的已核实规则存在冲突'])
        return result
    rule = active[0]
    for key in ('province', 'subject_type'):
        if not basic.get(key):
            result['missing'].append('缺少考生' + key)
        elif basic[key] != rule[key]:
            result['differences'].append('考生省份或科类与招生规则不符')
    required = rule.get('required_subjects')
    selected = basic.get('selected_subjects')
    if required is None:
        result['missing'].append('具体选科要求尚未核实')
    elif required and not selected:
        result['missing'].append('缺少考生实际选科')
    elif required and not {s.replace('思想政治', '政治') for s in required}.issubset(
            {s.replace('思想政治', '政治') for s in selected}):
        result['differences'].append('考生选科不满足必选科目')
    else:
        result['checks'].append('已核对必选科目')
    if not rule.get('group_code'):
        result['missing'].append('缺少专业组代码')
    if rule.get('enrollment_count') is None:
        result['missing'].append('目标年度招生计划未核实')
    elif rule['enrollment_count'] == 0:
        result['differences'].append('该招生单位当年计划为零')
    else:
        result['checks'].append('目标年度存在招生计划')
    budget = constraints.get('annual_budget_max')
    total, tuition = rule.get('annual_total_cost'), rule.get('annual_tuition')
    if budget is not None and (total is not None and total > budget or tuition is not None and tuition > budget):
        result['differences'].append('已核实费用超过年度预算')
    elif budget is not None and total is None:
        result['missing'].append('学费之外费用未核实，无法确认年度总预算')
    elif budget is None:
        result['missing'].append('未明确年度预算')
    else:
        result['checks'].append('已核实年度总费用不超过预算')
    if rule.get('other_requirements'):
        result['missing'].extend('需人工核实：' + r for r in rule['other_requirements'])
    result.update(group_code=rule.get('group_code'), annual_tuition=tuition, annual_total_cost=total,
        enrollment_count=rule.get('enrollment_count'), campus=rule.get('campus'))
    result['status'] = 'ineligible' if result['differences'] else 'unknown' if result['missing'] else 'eligible'
    return result
