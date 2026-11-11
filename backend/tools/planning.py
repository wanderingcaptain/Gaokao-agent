"""Deterministic v2 profile, comparison and plan analysis. No model or network calls."""
from copy import deepcopy
from statistics import median, pstdev
from tools.risk import _rank_trend, city_name

UNVERIFIED = ['目标年度报考资格与具体选科', '学费与预算', '专业组与正式填报规则']


def effective_profile(state):
    profile = deepcopy(state.get('profile', {}))
    constraints = profile.setdefault('constraints', {})
    for memory in state['memories']:
        if not memory.get('confirmed'):
            continue
        if memory['field'] == 'excluded_city':
            cities = constraints.setdefault('excluded_cities', [])
            if city_name(memory['value']) not in {city_name(city) for city in cities}:
                cities.append(memory['value'])
        elif memory['field'] == 'annual_budget_max':
            constraints['annual_budget_max'] = int(memory['value'])
    return profile


def constraint_differences(candidate, profile):
    differences = []
    constraints = profile.get('constraints', {})
    basic = profile.get('basic_info', {})
    if city_name(candidate.get('city')) in {city_name(city) for city in constraints.get('excluded_cities', [])}:
        differences.append('画像已排除该城市')
    if basic.get('province') and basic['province'] != candidate.get('province', '广东'):
        differences.append('历史招生省份与考生不一致')
    if basic.get('subject_type') and basic['subject_type'] != candidate.get('subject_type'):
        differences.append('历史科类与考生不一致')
    if constraints.get('accept_sino_foreign') is False and (
            candidate.get('is_sino_foreign') or '中外合作' in candidate.get('enrollment_type', '')):
        differences.append('画像不接受中外合作')
    return differences


def historical_risk(candidate, profile, target_year=None):
    """Describe recent observed ranks, without pretending to predict admission."""
    basic = profile.get('basic_info', {})
    target_year = target_year or basic.get('target_year')
    history = sorted(candidate.get('admission_history', []), key=lambda row: row['year'])
    if target_year:
        history = [row for row in history if row['year'] < target_year]
    window = history[-3:]
    valid = [(row['year'], row['min_rank']) for row in window
             if type(row.get('min_rank')) is int and row['min_rank'] > 0]
    ranks = [rank for _, rank in valid]
    user_rank = basic.get('province_rank')
    comparable = (type(user_rank) is int and user_rank > 0
        and basic.get('province') == candidate.get('province', '广东')
        and basic.get('subject_type') == candidate.get('subject_type'))
    warnings = ['历史位次跨年仅作启发式比较，不是录取概率，不证明当年资格']
    if not comparable:
        warnings.append('缺少有效考生位次/省份/科类，或与候选不一致，不能计算位次差额')
    if len(valid) < 3:
        warnings.append('最近三个记录年份的有效位次不足三年')
    if len(window) > 1 and any(b['year'] - a['year'] != 1 for a, b in zip(window, window[1:])):
        warnings.append('历史年份不连续')
    if target_year and (not window or window[-1]['year'] < target_year - 1):
        warnings.append('缺少目标年度前一年的记录')
    reference = median(ranks) if ranks else None
    return {'candidate_id': candidate['candidate_id'], 'source_id': candidate['source_id'],
        'status': 'ok' if ranks and comparable else 'insufficient_data',
        'method': 'median_of_last_three_recorded_years', 'target_year': target_year,
        'years': [row['year'] for row in window], 'valid_rank_years': [year for year, _ in valid],
        'reference_rank': reference, 'min_rank': min(ranks) if ranks else None,
        'max_rank': max(ranks) if ranks else None,
        'rank_volatility': round(pstdev(ranks), 2) if len(ranks) > 1 else None,
        'rank_trend': _rank_trend(valid),
        'user_rank': user_rank if comparable else None,
        'rank_margin': reference - user_rank if reference is not None and comparable else None,
        'reference_to_user_ratio': round(reference / user_rank, 4) if reference is not None and comparable else None,
        'annual_comparisons': [{'year': year, 'historical_min_rank': rank,
            'rank_margin': rank - user_rank if comparable else None} for year, rank in valid],
        'warnings': warnings}


def candidate_detail(candidate, profile, target_year=None):
    return {**deepcopy(candidate), 'differences': constraint_differences(candidate, profile),
        'unverified': list(UNVERIFIED), 'historical_risk': historical_risk(candidate, profile, target_year)}
