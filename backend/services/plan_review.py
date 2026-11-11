"""Server-owned review and targeted repair instructions; no model can approve itself."""
from contracts.artifacts import ReviewReport, ReviewIssue
from contracts.storage import digest


def review_plan(state, args, validation, plan_id, review_id):
    issues, repairs = [], []
    for error in validation['errors']:
        issues.append(ReviewIssue(severity='blocking', candidate_id=error.get('candidate_id'),
            description=error['message'], requested_action='修正 ' + error['field'] + ' 后重新预检并提交'))
        repairs.append({'agent': 'planner', 'candidate_id': error.get('candidate_id'),
                        'action': error['message']})
    for row in validation['rows']:
        eligibility = row.get('qualification', {})
        if row.get('eligibility') == 'conflicting':
            issues.append(ReviewIssue(severity='blocking', candidate_id=row['candidate_id'],
                description='目标年度资格资料相互冲突', requested_action='招生研究核对适用年度和来源，导入一致的已核实规则后再提交'))
            repairs.append({'agent': 'admissions', 'candidate_id': row['candidate_id'], 'action': '核对冲突资格来源'})
        for missing in row.get('unverified', []):
            issues.append(ReviewIssue(severity='warning', candidate_id=row['candidate_id'],
                description=missing, requested_action='招生研究补充适用年度官方证据；未核实前保留未知'))
        for sid in row.get('source_ids', []):
            source = state['sources'].get(sid, {})
            if source.get('verification') == 'conflicting':
                issues.append(ReviewIssue(severity='blocking', candidate_id=row['candidate_id'],
                    description='引用来源标记为冲突', requested_action='核对来源后重新提交'))
    if not validation['rows'] and not validation['errors']:
        issues.append(ReviewIssue(severity='warning', description='所有候选均与硬约束不符',
            requested_action='保持用户条件，委派招生研究查找其他候选'))
        repairs.append({'agent': 'admissions', 'action': '保持原有硬约束，查询其他候选'})
    if any(row.get('unverified') for row in validation['rows']):
        repairs.append({'agent': 'admissions', 'action': '补查目标年选科、计划、学费和专业组规则'})
    blocking = any(i.severity == 'blocking' for i in issues)
    verdict = 'revise' if blocking else 'insufficient_data' if issues else 'pass'
    sources = list(dict.fromkeys(sid for row in args['rows'] for sid in row['source_ids']))
    versions = {str(r['candidate_id']): r.get('qualification', {}).get('data_version')
                for r in validation['rows'] + validation['excluded']}
    review = ReviewReport(task_id=state.get('session_id', 'standalone'), profile_version=state['profile_version'],
        data_version=digest({'sources': sources, 'policies': versions}), artifact_id=review_id,
        plan_id=plan_id, verdict=verdict, issues=issues)
    return {'plan_id': plan_id, 'verdict': verdict, 'issues': [i.model_dump() for i in issues],
        'repair_tasks': repairs, 'blocking': blocking, 'review_contract': review.model_dump(),
        'findings': '存在阻断问题，需要修正。' if blocking else '结构检查通过；仍有待核实项。' if issues else '已支持的规则检查通过。',
        'missing': list(dict.fromkeys(i.description for i in issues if i.severity == 'warning')),
        'attempt': args if blocking else None}
