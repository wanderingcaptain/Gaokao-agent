"""Persisted envelopes. Domain bodies remain inspectable and versioned."""
import hashlib
import json
from typing import Literal
from pydantic import Field, model_validator
from contracts.artifacts import Scope, Evidence, EvidencePack, PlanDraft, validate_handoff


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ArtifactEnvelope(Scope):
    artifact_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    role: Literal['coordinator', 'major_research', 'admissions', 'planner', 'reviewer']
    kind: Literal['report', 'plan', 'review']
    payload: dict
    evidence_pack: EvidencePack
    plan: PlanDraft | None = None

    @model_validator(mode='after')
    def scope_and_refs(self):
        pack = self.evidence_pack
        if any(getattr(pack, key) != getattr(self, key) for key in ('task_id', 'profile_version', 'data_version')):
            raise ValueError('产物与证据范围不匹配')
        if self.plan is not None:
            if self.kind != 'plan' or self.plan.artifact_id != self.artifact_id:
                raise ValueError('方案产物编号不匹配')
            validate_handoff(self.plan, pack, {row.candidate_id for row in self.plan.items})
        return self


def envelope(state, aid, role, body):
    source_ids = body.get('source_ids', [])
    scope = {'task_id': state.get('session_id', 'standalone'),
             'profile_version': state['profile_version'],
             'data_version': digest({'sources': source_ids, 'policies': body.get('policy_versions', {})})}
    facts = []
    candidate_ids = [r['candidate_id'] for r in body.get('rows', []) + body.get('excluded', [])]
    for sid in source_ids:
        source = state['sources'][sid]
        facts.append(Evidence(evidence_id=sid, source=source.get('url') or sid,
            year=source.get('year'), province=source.get('province') or '未明确',
            candidate_ids=[source['candidate_id']] if source.get('kind') == 'database' else candidate_ids,
            claim=(source.get('text') or '本地历史记录，仅证明历史数据来源')[:6000],
            verification='verified' if source.get('kind') == 'database' else source.get('verification', 'unverified')))
    pack = EvidencePack(**scope, artifact_id=aid + '_evidence', evidence=facts, missing=body.get('missing', []))
    plan = None
    kind = 'review' if role == 'reviewer' else 'plan' if 'rows' in body else 'report'
    submitted = body.get('rows', []) + body.get('excluded', [])
    if kind == 'plan' and submitted:
        plan = PlanDraft(**scope, artifact_id=aid, evidence_pack_id=pack.artifact_id,
            strategy=body['strategy'], items=[{'candidate_id': r['candidate_id'], 'reason': r['reason'],
                'evidence_ids': r['source_ids'], 'warnings': r.get('differences', [])} for r in submitted])
        validate_handoff(plan, pack, {int(cid) for cid in state['candidates']})
    return ArtifactEnvelope(**scope, artifact_id=aid, run_id=state.get('run_id', 'standalone'),
        role=role, kind=kind, payload=body, evidence_pack=pack, plan=plan).model_dump()
