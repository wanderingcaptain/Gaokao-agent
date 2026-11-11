"""Read-only research connectors; never fetch arbitrary URLs on the application host."""
import hashlib
import json
import os
import re
from urllib.parse import urlparse

import httpx
from sqlalchemy import select, func, tuple_
from database.models import AdmissionRecord, SessionLocal, University
from agents.schemas import Query, Search


def public_query(query):
    """Reject common contact/identity fields before sending a query to a third party."""
    query = Search(query=query).query.strip()
    if (re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', query)
            or re.search(r'(?<!\d)(?:\d[ -]?){10,17}[\dXx](?!\d)', query)
            or re.search(r'(?:身份证|手机号|联系电话|考生号|准考证|姓名)\s*[:：]', query)):
        raise ValueError('搜索关键词含疑似身份或联系方式，请仅使用公开学校、专业与政策关键词')
    return query


async def search_web(query):
    query = public_query(query)
    key = os.getenv('TAVILY_API_KEY', '').strip()
    if not key:
        return {'ok': False, 'error': 'search_not_configured', 'sources': []}
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            response = await client.post('https://api.tavily.com/search',
                headers={'Authorization': f'Bearer {key}'}, json={
                    'query': query, 'max_results': 5, 'search_depth': 'basic',
                    'include_answer': False, 'include_raw_content': False})
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
            raise ValueError('invalid search response')
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        error = 'search_rate_limited' if status == 429 else 'search_http_error'
        return {'ok': False, 'error': error, 'sources': []}
    except (httpx.RequestError, ValueError):
        return {'ok': False, 'error': 'search_unavailable', 'sources': []}
    official = [x.strip().lower().rstrip('.') for x in os.getenv('RESEARCH_OFFICIAL_DOMAINS',
                'eea.gd.gov.cn').split(',') if x.strip()]
    sources, seen = [], set()
    for row in payload['results'][:5]:
        if not isinstance(row, dict) or not isinstance(row.get('url'), str):
            continue
        url = row['url']
        try:
            parsed = urlparse(url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                    or parsed.password or parsed.port not in (None, 443)):
                continue
        except ValueError:
            continue
        hostname = parsed.hostname.lower().rstrip('.')
        known = any(hostname == d or hostname.endswith('.' + d) for d in official)
        excerpt = row.get('content')
        if not isinstance(excerpt, str) or not excerpt.strip():
            continue
        excerpt = excerpt[:6000]
        title = row.get('title') if isinstance(row.get('title'), str) else ''
        source = {'url': url, 'title': title[:500], 'text': excerpt,
            'authority': 'official_domain' if known else 'supplementary_unverified',
            'coverage': 'search_excerpt_not_full_document', 'verification': 'unverified'}
        digest = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
        source['source_id'] = 'web_' + digest
        if source['source_id'] not in seen:
            sources.append(source)
            seen.add(source['source_id'])
    return {'ok': True, 'sources': sources, 'returned': len(sources)}


def query_history(major='', offset=0, *, school='', city='', subject_type='',
                  enrollment_type='', years=None, limit=20):
    """Page program groups, then fetch their complete history as immutable snapshots.

    Year filters select groups; histories retain all recorded years so a narrower
    search never overwrites a candidate with an incomplete snapshot.
    """
    args = Query(major=major, offset=offset, school=school, city=city,
                 subject_type=subject_type, enrollment_type=enrollment_type,
                 years=years or [], limit=limit)
    with SessionLocal() as db:
        covered_years = list(db.scalars(select(AdmissionRecord.year).where(
            AdmissionRecord.province == '广东').distinct().order_by(AdmissionRecord.year)))
        keys = (AdmissionRecord.university_id, AdmissionRecord.major_name,
                AdmissionRecord.enrollment_type, AdmissionRecord.subject_type)
        base = select(*keys, func.min(AdmissionRecord.id).label('candidate_id')).join(
            University, University.university_id == AdmissionRecord.university_id).where(
            AdmissionRecord.province == '广东')
        for column, value in ((AdmissionRecord.major_name, args.major), (University.name, args.school)):
            if value:
                base = base.where(column.contains(value, autoescape=True))
        for column, value in ((University.city, args.city), (AdmissionRecord.subject_type, args.subject_type),
                              (AdmissionRecord.enrollment_type, args.enrollment_type)):
            if value:
                base = base.where(column == value)
        # HAVING preserves a stable group ID even if only selected years match.
        if args.years:
            base = base.having(func.max(AdmissionRecord.year.in_(args.years)) == 1)
        grouped = base.group_by(*keys)
        total = db.scalar(select(func.count()).select_from(grouped.subquery()))
        groups = db.execute(grouped.order_by(func.min(AdmissionRecord.id)).offset(args.offset).limit(args.limit)).all()
        candidates = []
        histories = {}
        universities = {}
        if groups:
            rows = db.scalars(select(AdmissionRecord).where(AdmissionRecord.province == '广东',
                tuple_(*keys).in_([tuple(group[:4]) for group in groups])).order_by(AdmissionRecord.year)).all()
            for row in rows:
                key = (row.university_id, row.major_name, row.enrollment_type, row.subject_type)
                histories.setdefault(key, []).append(row)
            universities = {u.university_id: u for u in db.scalars(select(University).where(
                University.university_id.in_({group[0] for group in groups})))}
        for group in groups:
            rows = histories[tuple(group[:4])]
            row = rows[-1]
            university = universities[row.university_id]
            item = {key: getattr(row, key) for key in ('id', 'university_id', 'major_name',
                'province', 'subject_type', 'year', 'min_score', 'min_rank', 'enrollment_count', 'enrollment_type')}
            item.update(candidate_id=group.candidate_id, university_name=university.name,
                city=university.city, university_type=university.university_type,
                is_sino_foreign=bool(university.is_sino_foreign), eligibility='unknown',
                admission_history=[{k: getattr(r, k) for k in ('year', 'min_score', 'min_rank',
                    'max_score', 'avg_score', 'enrollment_count')} for r in rows])
            digest = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
            item['source_id'] = f'db_{group.candidate_id}_{digest}'
            candidates.append(item)
    next_offset = args.offset + len(candidates)
    return {'ok': True, 'province': '广东', 'covered_years': covered_years, 'candidates': candidates,
        'offset': args.offset, 'returned': len(candidates), 'total': total,
        'next_offset': next_offset if next_offset < total else None,
        'remaining': max(0, total - next_offset),
        'warning': '历史记录不是目标年度资格；年份过滤仅筛选候选，历史保留全部年份；跨年同专业不重复入表'}
