"""Session-scoped v2 tools shared by the bounded multi-role runtime."""
import asyncio
import time
from copy import deepcopy
from uuid import uuid4

from agents.schemas import SCHEMAS, PERMISSIONS
from agents.research import public_query
from tools.planning import effective_profile, candidate_detail, historical_risk
from contracts.storage import envelope, digest
from services.plan_review import review_plan
from services.qualification import assess
from contracts.artifacts import Subtask


class ResearchTools:
    def artifact(self, role, body, artifact_id=None):
        aid = artifact_id or 'artifact_' + uuid4().hex
        self.state['artifacts'][aid] = {'artifact_id': aid, 'role': role,
            'profile_version': self.state['profile_version'], 'created_at': time.time(), **deepcopy(body),
            'contract': envelope(self.state, aid, role, body)}
        result = {'artifact_id': aid, 'status': 'completed', 'role': role,
            'summary': body.get('findings', body.get('strategy', '测试草案'))[:1200],
            'missing': body.get('missing', [])}
        if role == 'admissions':
            result['candidate_count'] = len(body.get('candidate_sources', {}))
            if result['candidate_count']:
                result['next_action'] = '若用户请求志愿草案，将此 artifact_id 传给 planner。'
        return result

    def current_artifact(self, aid):
        item = self.state['artifacts'].get(aid)
        if not item or item['profile_version'] != self.state['profile_version']:
            raise ValueError('产物不存在或画像版本已过期')
        self.check_sources(item.get('source_ids', []))
        # A report/plan must not silently change when the same candidate gets new data.
        for cid, sid in item.get('candidate_sources', {}).items():
            candidate = self.state['candidates'].get(cid)
            if not candidate or candidate['source_id'] != sid:
                raise ValueError('产物引用的候选数据已变化，请重新调查')
        for cid, version in item.get('policy_versions', {}).items():
            detail = self.qualified_detail(self.state['candidates'][cid], effective_profile(self.state), item.get('target_year'))
            if detail['qualification'].get('data_version') != version:
                raise ValueError('目标年度资格规则已变化，请重新审核方案')
        return item

    def check_sources(self, ids, *, read=False):
        for sid in ids:
            if sid not in self.state['sources']:
                raise ValueError('引用了不存在的来源')
            if self.state['profile_version'] not in self.state['source_versions'].get(sid, []):
                raise ValueError('来源属于旧画像版本，请重新查询')
            source = self.state['sources'][sid]
            if source.get('kind') == 'database':
                candidate = self.state['candidates'].get(str(source['candidate_id']))
                if not candidate or candidate['source_id'] != sid:
                    raise ValueError('候选数据已变化，旧来源不能用于新产物')
        if read and not set(ids).issubset(self.read_source_ids):
            raise ValueError('先读取所引用的来源或研究产物')

    def store_sources(self, sources):
        staged = {}
        for source in sources:
            sid = source.get('source_id')
            if not isinstance(sid, str) or not sid:
                raise ValueError('来源缺少编号')
            existing = staged.get(sid, self.state['sources'].get(sid))
            if existing is not None and existing != source:
                raise ValueError('来源编号冲突，不能覆盖已保存证据')
            staged[sid] = deepcopy(source)
        for sid, source in staged.items():
            self.state['sources'][sid] = source
            versions = self.state['source_versions'].setdefault(sid, [])
            if self.state['profile_version'] not in versions:
                versions.append(self.state['profile_version'])
        self.read_source_ids.update(staged)

    def known_candidates(self, ids):
        result = []
        for cid in ids:
            candidate = self.state['candidates'].get(str(cid))
            if not candidate:
                raise ValueError('候选未在本会话查询，请委派招生研究后读取')
            self.check_sources([candidate['source_id']])
            result.append(candidate)
        return result

    def qualified_detail(self, candidate, profile, target_year=None):
        detail = candidate_detail(candidate, profile, target_year)
        year = target_year or profile.get('basic_info', {}).get('target_year')
        try:
            policies = self.policies(candidate, year) if year else []
            qualification = assess(candidate, profile, year, policies)
        except Exception:
            qualification = {'status': 'unknown', 'missing': ['资格规则服务暂不可用'],
                             'differences': [], 'sources': [], 'policy_ids': []}
        detail['qualification'] = qualification
        detail['eligibility'] = qualification['status']
        detail['differences'].extend(qualification.get('differences', []))
        detail['unverified'] = list(dict.fromkeys(qualification['missing'] + ['专业组与正式填报规则']))
        if qualification['status'] == 'unknown':
            detail['unverified'] = list(dict.fromkeys(detail['unverified'] + ['学费与预算']))
        return detail

    def validate_plan(self, args):
        errors, rows, excluded, seen = [], [], [], set()
        try:
            if not self.authorized:
                raise ValueError('生成/更新表格未授权')
            reports = [self.current_artifact(aid) for aid in self.read_ids]
            if not any(a['role'] == 'admissions' for a in reports):
                raise ValueError('先读取当前版本的招生研究产物')
            base_id = args.get('base_plan_id')
            if base_id:
                base = self.current_artifact(base_id)
                if base_id not in self.state['plans'] or base_id not in self.read_ids or 'rows' not in base:
                    raise ValueError('修订前必须读取本会话已有方案')
                if base_id != self.state['plans'][-1]:
                    raise ValueError('只能基于最新方案修订，避免覆盖后续版本')
            elif self.state['plans']:
                try:
                    self.current_artifact(self.state['plans'][-1])
                except ValueError:
                    pass  # Invalidated data/profile requires a fresh plan, not stale evidence.
                else:
                    raise ValueError('已有方案，请通过 base_plan_id 明确引用最新版本')
        except ValueError as exc:
            errors.append({'field': 'plan', 'message': str(exc)})
        profile = effective_profile(self.state)
        for index, item in enumerate(args['rows']):
            try:
                self.check_sources(item['source_ids'], read=True)
                candidate = self.known_candidates([item['candidate_id']])[0]
                if candidate['source_id'] not in item['source_ids']:
                    raise ValueError('候选必须引用当前查询的历史记录')
                for sid in item['source_ids']:
                    source = self.state['sources'][sid]
                    if source.get('kind') == 'database' and source['candidate_id'] != candidate['candidate_id']:
                        raise ValueError('不能把其他候选的历史记录作为本行证据')
                key = tuple(candidate.get(k) for k in ('university_id', 'major_name', 'enrollment_type', 'subject_type'))
                if key in seen:
                    raise ValueError('不能重复选择同一院校专业科类的不同年份记录')
                seen.add(key)
                row = {**self.qualified_detail(candidate, profile, args.get('target_year')), **item}
                (excluded if row['differences'] else rows).append(row)
            except ValueError as exc:
                errors.append({'field': f'rows[{index}]', 'candidate_id': item['candidate_id'], 'message': str(exc)})
        return {'ok': True, 'valid': not errors, 'errors': errors, 'rows': rows, 'excluded': excluded,
            'test_only': True, 'ready_for_submission': False,
            'missing': ['全部候选仍需核实目标年度资格，不可直接正式填报'] +
                (['所有提交候选均与硬约束不符，当前没有可排序候选'] if not rows else [])}

    async def execute(self, role, name, raw):
        if name not in PERMISSIONS.get(role, set()):
            raise ValueError('该角色无权调用此工具')
        args = SCHEMAS[name].model_validate(raw).model_dump()
        if name == 'get_profile':
            profile = effective_profile(self.state)
            missing = [key for key in ('province', 'subject_type', 'province_rank', 'target_year')
                       if not profile.get('basic_info', {}).get(key)]
            return {'ok': True, 'profile': profile, 'memories': deepcopy(self.state['memories']),
                'profile_version': self.state['profile_version'], 'research_authorized': self.authorized,
                'missing': missing, 'unverified': ['具体选科资格', '当年计划与章程', '学费预算']}
        if name == 'list_artifacts':
            items = []
            for item in reversed(list(self.state['artifacts'].values())):
                if args['role'] and item['role'] != args['role']:
                    continue
                try:
                    self.current_artifact(item['artifact_id'])
                except ValueError:
                    continue
                items.append({k: item.get(k) for k in ('artifact_id', 'role', 'profile_version', 'missing')})
            page = items[args['offset']:args['offset'] + args['limit']]
            next_offset = args['offset'] + len(page)
            return {'ok': True, 'artifacts': page, 'total': len(items),
                'next_offset': next_offset if next_offset < len(items) else None}
        if name == 'read_artifact':
            item = self.current_artifact(args['artifact_id'])
            self.read_ids.add(args['artifact_id'])
            # Full candidate facts are returned, so the planner never has to guess IDs from prose.
            candidates = [deepcopy(self.state['candidates'][cid]) for cid in item.get('candidate_sources', {})]
            self.read_source_ids.update(c['source_id'] for c in candidates)
            return {**deepcopy(item), 'candidates': candidates,
                'source_index': [{k: self.state['sources'][sid].get(k)
                    for k in ('source_id', 'kind', 'title', 'url', 'verification', 'coverage')}
                    for sid in item.get('source_ids', [])]}
        if name == 'read_sources':
            self.check_sources(args['source_ids'])
            self.read_source_ids.update(args['source_ids'])
            return {'ok': True, 'sources': [deepcopy(self.state['sources'][sid]) for sid in args['source_ids']]}
        if name in {'get_candidate_details', 'analyze_historical_risk', 'compare_candidates'}:
            candidates = self.known_candidates(args['candidate_ids'])
            self.read_source_ids.update(c['source_id'] for c in candidates)
            profile = effective_profile(self.state)
            if name == 'analyze_historical_risk':
                return {'ok': True, 'analyses': [historical_risk(c, profile, args['target_year']) for c in candidates]}
            return {'ok': True, 'candidates': [self.qualified_detail(c, profile, args.get('target_year')) for c in candidates],
                'ordering': 'input_order', 'warning': '比较不代表推荐顺序或当年报考资格'}
        if name == 'request_confirmation':
            if self.authorized:
                raise ValueError('本轮已有调查授权。请按已确认范围执行，新增范围需另起一轮。')
            cid = 'confirm_' + uuid4().hex
            self.state['pending'][cid] = {'kind': 'research', 'text': args['text'],
                'user_request': self.user_text,
                'profile_version': self.state['profile_version']}
            if self.history_candidate_id:
                self.state['pending'][cid]['history_candidate_id'] = self.history_candidate_id
                if self.history_plan:
                    self.state['pending'][cid]['history_plan'] = True
            self.reply = {'status': 'waiting', 'text': args['text'], 'confirmation_id': cid}
            return self.reply
        if name == 'respond':
            self.reply = {'status': 'completed', **args}
            return self.reply
        if name == 'propose_memory':
            if args['quote'] not in self.user_text:
                raise ValueError('记忆必须引用本轮用户原话')
            hard = args['field'] in {'excluded_city', 'annual_budget_max'}
            if hard != (args['kind'] == 'hard'):
                raise ValueError('硬约束字段不能通过软偏好写入')
            if args['field'] == 'annual_budget_max' and (
                    not args['value'].isascii() or not args['value'].isdigit()):
                raise ValueError('预算需要非负整数金额')
            for entry in [*self.state['memories'], *self.state['pending'].values()]:
                if not entry.get('confirmed') and entry.get('profile_version') != self.state['profile_version']:
                    continue
                if all(entry.get(key) == args[key] for key in ('kind', 'field', 'value')):
                    return {'recorded': bool(entry.get('confirmed')), 'pending_confirmation': not entry.get('confirmed'),
                            'memory': deepcopy(entry), 'unchanged': True}
            mid = 'memory_' + uuid4().hex
            entry = {'id': mid, **args, 'confirmed': not hard, 'profile_version': self.state['profile_version']}
            if hard:
                self.state['pending'][mid] = entry
            else:
                self.state['memories'].append(entry)
                self.state['profile_version'] += 1
                self.authorized = False
            return {'recorded': not hard, 'pending_confirmation': hard, 'memory': deepcopy(entry),
                    'profile_version': self.state['profile_version'], 'research_authorized': self.authorized}
        if name == 'delegate_task':
            if args['agent'] in {'admissions', 'planner'} and not self.authorized:
                raise ValueError('本轮调查/表格更新尚未经用户确认')
            artifacts = [self.current_artifact(aid) for aid in args['artifact_ids']]
            if args['agent'] == 'planner' and not any(
                    item['role'] == 'admissions' and item.get('candidate_sources') for item in artifacts):
                raise ValueError('先委派 admissions 查询历史录取，再把包含候选的招生报告编号放入 artifact_ids')
            if args.get('review_id'):
                review = self.current_artifact(args['review_id'])
                if review['role'] != 'reviewer':
                    raise ValueError('修正委派必须引用审核报告')
                repairs = [r for r in review.get('repair_tasks', []) if r['agent'] == args['agent']]
                if not repairs:
                    raise ValueError('审核未要求该角色修正')
                key = digest([review['review_contract']['data_version'], self.state['profile_version'], repairs])
                attempts = self.state.setdefault('repair_attempts', {})
                if attempts.get(key, 0) >= 2:
                    raise ValueError('该问题已补查两轮，请说明未解决项并等待新的资料')
                attempts[key] = attempts.get(key, 0) + 1
                args['repair_tasks'] = repairs
            subtask_id = 'subtask_' + uuid4().hex
            subtask = Subtask(task_id=self.state.get('session_id', 'standalone'),
                profile_version=self.state['profile_version'], data_version=digest(args['artifact_ids']),
                subtask_id=subtask_id, agent=args['agent'], goal=args['goal'],
                input_artifact_ids=args['artifact_ids'], status='running')
            self.state.setdefault('subtasks', {})[subtask_id] = subtask.model_dump()
            args['subtask_id'] = subtask_id
            return args
        if name == 'search_sources':
            query = public_query(args['query'])
            if role == 'admissions' and not self.authorized:
                raise ValueError('院校调查未授权')
            self.consume_search()
            result = await self.search(query)
            if result.get('ok'):
                self.store_sources(result.get('sources', []))
            return result
        if name == 'query_candidates':
            if not self.authorized:
                raise ValueError('院校调查未授权')
            basic = effective_profile(self.state).get('basic_info', {})
            if basic.get('province'):
                args = {**args, 'province': basic['province']}
            if basic.get('subject_type'):
                args = {**args, 'subject_type': basic['subject_type']}
            anchor = self.history_candidate_id and not self.execution.get('history_candidate_queried')
            if anchor:
                result = await asyncio.to_thread(self.query, history_candidate_id=self.history_candidate_id,
                    province=basic.get('province'), subject_type=basic.get('subject_type', ''), limit=1)
            else:
                result = await asyncio.to_thread(self.query, **args)
            if result.get('ok'):
                if not anchor:
                    result['filters'] = {key: args[key] for key in
                        ('school', 'major', 'city', 'subject_type', 'enrollment_type', 'years') if args[key]}
                candidates = result['candidates']
                if len({c['candidate_id'] for c in candidates}) != len(candidates):
                    raise ValueError('查询返回重复候选')
                self.store_sources([{'kind': 'database', **c} for c in candidates])
                for candidate in candidates:
                    self.state['candidates'][str(candidate['candidate_id'])] = deepcopy(candidate)
                if anchor and candidates:
                    selected = candidates[0]
                    self.execution['history_candidate_queried'] = True
                    result['selected_candidate'] = selected['candidate_id']
                    result['history_report'] = self.artifact(role, {
                        'findings': f"已查到所选候选 {selected['university_name']} · {selected['major_name']} 的历史录取记录。",
                        'source_ids': [selected['source_id']],
                        'candidate_sources': {str(selected['candidate_id']): selected['source_id']},
                        'missing': ['目标年度的招生计划、专业组、选科和费用待核实'],
                    })
                if result.get('total') == 0 and result.get('covered_years'):
                    result['next_step'] = ('所选筛选项没有匹配记录。保留学校和考生科类，试用专业简称；'
                                           '如果目标年份尚无历史数据，移除 years 后查询实际覆盖年份。')
            return result
        if name == 'submit_report':
            self.check_sources(args['source_ids'], read=True)
            if not args['source_ids'] and not args['missing']:
                raise ValueError('无来源的报告必须说明资料缺失')
            if any(self.state['sources'][sid].get('verification') == 'unverified' for sid in args['source_ids']):
                args['missing'] = list(dict.fromkeys([*args['missing'], '网页摘要的适用年度与正式原文待核实']))
            candidate_sources = {str(self.state['sources'][sid]['candidate_id']): sid
                for sid in args['source_ids'] if self.state['sources'][sid].get('kind') == 'database'}
            return self.artifact(role, {**args, 'candidate_sources': candidate_sources})
        if name in {'submit_plan', 'validate_plan'}:
            validation = self.validate_plan(args)
            if name == 'validate_plan':
                return validation
            plan_id, review_id = 'artifact_' + uuid4().hex, 'artifact_' + uuid4().hex
            review = review_plan(self.state, args, validation, plan_id, review_id)
            self.artifact('reviewer', review, review_id)
            self.state.setdefault('reviews', []).append(review_id)
            if review['blocking']:
                return {'ok': False, 'error': '方案审核未通过，请按修正任务处理后重试',
                    'validation': validation, 'review_id': review_id, 'repair_tasks': review['repair_tasks']}
            source_ids = list(dict.fromkeys(sid for row in args['rows'] for sid in row['source_ids']))
            payload = {**args, **{k: v for k, v in validation.items() if k not in ('ok', 'valid', 'errors')},
                'source_ids': source_ids,
                'candidate_sources': {str(row['candidate_id']): self.state['candidates'][str(row['candidate_id'])]['source_id']
                                      for row in args['rows']},
                'supersedes_plan_id': self.state['plans'][-1] if self.state['plans'] else None,
                'plan_version': len(self.state['plans']) + 1}
            payload['review_id'] = review_id
            payload['policy_versions'] = {str(r['candidate_id']): r.get('qualification', {}).get('data_version')
                for r in validation['rows'] + validation['excluded']}
            result = self.artifact(role, payload, plan_id)
            result.update(review_id=review_id, review_verdict=review['verdict'], repair_tasks=review['repair_tasks'])
            self.state['plans'].append(result['artifact_id'])
            return result
        raise ValueError('未知工具')
