"""Reviewed, scoped annual policy input. Only the local import workflow may write it."""
from pydantic import Field, field_validator
from contracts.artifacts import Contract
from urllib.parse import urlparse


class AdmissionPolicyInput(Contract):
    policy_id: str = Field(min_length=1, max_length=100)
    university_id: str = Field(min_length=1)
    major_name: str = Field(min_length=1)
    enrollment_type: str = Field(min_length=1)
    province: str = Field(min_length=1)
    subject_type: str = Field(min_length=1)
    target_year: int = Field(ge=2000, le=2100)
    group_code: str | None = None
    required_subjects: list[str] | None = None
    enrollment_count: int | None = Field(default=None, ge=0)
    annual_tuition: int | None = Field(default=None, ge=0)
    annual_total_cost: int | None = Field(default=None, ge=0)
    campus: str | None = None
    other_requirements: list[str] = Field(default_factory=list)
    source_url: str
    source_title: str = Field(min_length=1)
    reviewed: bool = False
    reviewed_by: str = Field(min_length=1)
    supersedes: list[str] = Field(default_factory=list)

    @field_validator('source_url')
    @classmethod
    def official_https(cls, value):
        parsed = urlparse(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('来源必须是正常的 HTTPS 官方文档链接')
        return value

    @field_validator('required_subjects')
    @classmethod
    def valid_subjects(cls, value):
        if value is not None and (len(value) != len(set(value)) or any(s not in
            {'物理', '历史', '化学', '生物', '思想政治', '政治', '地理'} for s in value)):
            raise ValueError('选科要求必须是无重复的有效科目')
        return value
