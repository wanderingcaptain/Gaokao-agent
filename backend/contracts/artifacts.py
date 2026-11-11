"""多 Agent 的版本化交接协议；尚未替代现有持久化/API格式。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1


class Scope(Contract):
    task_id: str = Field(min_length=1)
    profile_version: int = Field(ge=1)
    data_version: str = Field(min_length=1)


class Evidence(Contract):
    evidence_id: str = Field(min_length=1)
    source: str = Field(min_length=1, description="官方URL或可追溯的本地记录标识")
    year: int | None = Field(default=None, ge=2000, le=2100)
    province: str = Field(min_length=1)
    candidate_ids: list[int] = Field(default_factory=list)
    claim: str = Field(min_length=1)
    verification: Literal["verified", "unverified", "conflicting"]


class EvidencePack(Scope):
    artifact_id: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [x.evidence_id for x in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("证据编号重复")
        return self


class PlanItem(Contract):
    candidate_id: int = Field(ge=1)
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)


class PlanDraft(Scope):
    artifact_id: str = Field(min_length=1)
    evidence_pack_id: str = Field(min_length=1)
    items: list[PlanItem] = Field(min_length=1, max_length=45)
    strategy: str = Field(min_length=1)

    @model_validator(mode="after")
    def unique_candidates(self):
        ids = [x.candidate_id for x in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("方案候选重复")
        return self


class ReviewIssue(Contract):
    severity: Literal["blocking", "warning"]
    candidate_id: int | None = None
    description: str = Field(min_length=1)
    requested_action: str = Field(min_length=1)


class ReviewReport(Scope):
    artifact_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    verdict: Literal["pass", "revise", "insufficient_data"]
    issues: list[ReviewIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_verdict(self):
        if self.verdict == "pass" and any(i.severity == "blocking" for i in self.issues):
            raise ValueError("存在阻断问题不能通过审核")
        if self.verdict != "pass" and not self.issues:
            raise ValueError("未通过审核必须说明问题")
        return self


class Subtask(Scope):
    subtask_id: str = Field(min_length=1)
    agent: Literal["coordinator", "major_research", "admissions", "planner"]
    goal: str = Field(min_length=1)
    dependencies: list[str] = Field(default_factory=list)
    input_artifact_ids: list[str] = Field(default_factory=list)
    output_artifact_ids: list[str] = Field(default_factory=list)
    status: Literal["pending", "running", "waiting", "completed", "failed", "cancelled"] = "pending"
    max_model_calls: int = Field(default=8, ge=1, le=24)
    timeout_seconds: int = Field(default=120, ge=1, le=300)
    error: str | None = None

    @model_validator(mode="after")
    def no_self_dependency(self):
        if self.subtask_id in self.dependencies:
            raise ValueError("子任务不能依赖自身")
        return self


def validate_handoff(plan: PlanDraft, evidence: EvidencePack,
                     allowed_candidate_ids: set[int], review: ReviewReport | None = None):
    """确定性交接检查，不等同于事实核实或最终发布授权。"""
    scopes = [evidence] + ([review] if review is not None else [])
    for item in scopes:
        if any(getattr(item, key) != getattr(plan, key)
               for key in ("task_id", "profile_version", "data_version")):
            raise ValueError("交接产物任务或版本不一致")
    if plan.evidence_pack_id != evidence.artifact_id:
        raise ValueError("证据包引用不匹配")
    facts = {x.evidence_id: x for x in evidence.evidence}
    for item in plan.items:
        if item.candidate_id not in allowed_candidate_ids:
            raise ValueError("候选不在授权候选池")
        for ref in item.evidence_ids:
            if ref not in facts or item.candidate_id not in facts[ref].candidate_ids:
                raise ValueError("证据引用不存在或不属于该候选")
    if review is not None and review.plan_id != plan.artifact_id:
        raise ValueError("审核未引用当前方案")
