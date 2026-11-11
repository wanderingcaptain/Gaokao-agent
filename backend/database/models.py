"""
SQLAlchemy ORM 模型定义
对应 Schema：score_segments / universities / admission_records
"""
from sqlalchemy import (
    Column, Integer, Text, Float, UniqueConstraint, Index,
    create_engine
)
from sqlalchemy.orm import declarative_base, sessionmaker
from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./database/gaokao.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ──────────────────────────────────────────────
# 表1：一分一段表
# ──────────────────────────────────────────────
class ScoreSegment(Base):
    __tablename__ = "score_segments"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    province         = Column(Text, nullable=False)      # 省份
    year             = Column(Integer, nullable=False)   # 年份
    subject_type     = Column(Text, nullable=False)      # 物理类/历史类/理科/文科
    score            = Column(Integer, nullable=False)   # 分数
    cumulative_count = Column(Integer, nullable=False)   # 该分数及以上累计人数（即位次）

    __table_args__ = (
        UniqueConstraint("province", "year", "subject_type", "score",
                         name="uq_score_segment"),
    )


# ──────────────────────────────────────────────
# 表2：院校基础表
# ──────────────────────────────────────────────
class University(Base):
    __tablename__ = "universities"

    university_id    = Column(Text, primary_key=True)    # 院校代码（教育部标准）
    name             = Column(Text, nullable=False)       # 院校名称
    province         = Column(Text, nullable=False)       # 院校所在省份
    city             = Column(Text, nullable=False)       # 院校所在城市
    university_type  = Column(Text, nullable=False)       # 985/211/双一流/普通本科
    is_sino_foreign  = Column(Integer, nullable=False, default=0)  # 0=否, 1=是
    official_website = Column(Text)
    logo_url         = Column(Text)


# ──────────────────────────────────────────────
# 表3：历年专业录取数据表（核心查询表）
# ──────────────────────────────────────────────
class AdmissionRecord(Base):
    __tablename__ = "admission_records"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    university_id    = Column(Text, nullable=False)       # 外键（不强制约束以提升批量导入速度）
    university_name  = Column(Text, nullable=False)       # 冗余，方便直接查询
    major_name       = Column(Text, nullable=False)       # 专业名称
    major_category   = Column(Text)                       # 专业大类
    enrollment_type  = Column(Text, nullable=False)       # 普通类/中外合作/强基等
    province         = Column(Text, nullable=False)       # 投档省份
    subject_type     = Column(Text, nullable=False)       # 物理类/历史类/理科/文科
    year             = Column(Integer, nullable=False)    # 录取年份
    min_score        = Column(Integer)                    # 最低录取分数
    min_rank         = Column(Integer)                    # 最低录取位次（核心）
    max_score        = Column(Integer)                    # 最高录取分数
    avg_score        = Column(Float)                      # 平均录取分数
    enrollment_count = Column(Integer)                    # 录取人数
    score_diff       = Column(Integer)                    # 线差（min_score - 省控线）

    __table_args__ = (
        UniqueConstraint(
            "university_id", "major_name", "enrollment_type",
            "province", "subject_type", "year",
            name="uq_admission_record"
        ),
        Index("idx_admission_province_subject_year", "province", "subject_type", "year"),
        Index("idx_admission_rank", "min_rank"),
    )


# ──────────────────────────────────────────────
# 表4：系统用户
# ──────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    username      = Column(Text, nullable=False, unique=True)
    password_hash = Column(Text, nullable=False)
    created_at    = Column(Text, nullable=False)
    updated_at    = Column(Text, nullable=False)

    __table_args__ = (
        Index("idx_users_username", "username"),
    )


# ──────────────────────────────────────────────
# 表5：用户画像归属
# ──────────────────────────────────────────────
class UserProfile(Base):
    __tablename__ = "user_profiles"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    user_id      = Column(Integer, nullable=False)
    profile_uuid = Column(Text, nullable=False)
    profile_json = Column(Text, nullable=False)
    created_at   = Column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "profile_uuid", name="uq_user_profile_uuid"),
        Index("idx_user_profiles_user_uuid", "user_id", "profile_uuid"),
    )


# ──────────────────────────────────────────────
# 表6：推荐结果历史
# ──────────────────────────────────────────────
class RecommendationResult(Base):
    __tablename__ = "recommendation_results"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    user_id      = Column(Integer, nullable=False)
    profile_uuid = Column(Text, nullable=False)
    result_json  = Column(Text, nullable=False)
    fallback     = Column(Integer, nullable=False, default=0)
    summary      = Column(Text)
    created_at   = Column(Text, nullable=False)

    __table_args__ = (
        Index("idx_recommendation_user_created", "user_id", "created_at"),
        Index("idx_recommendation_user_profile", "user_id", "profile_uuid"),
    )


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    id = Column(Text, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    profile_uuid = Column(Text, nullable=False)
    state_json = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="ready")
    active_turn_id = Column(Text)
    created_at = Column(Text, nullable=False)


class AgentTurn(Base):
    __tablename__ = "agent_turns"
    id = Column(Text, primary_key=True)
    task_id = Column(Text, nullable=False, index=True)
    request_id = Column(Text, nullable=False)
    status = Column(Text, nullable=False)
    input_json = Column(Text, nullable=False)
    events_json = Column(Text, nullable=False, default="[]")
    created_at = Column(Text, nullable=False)
    __table_args__ = (UniqueConstraint("task_id", "request_id", name="uq_agent_request"),)


class MultiAgentSession(Base):
    __tablename__ = "multi_agent_sessions"
    id = Column(Text, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    state_json = Column(Text, nullable=False)
    active_request_id = Column(Text, nullable=True)


class SchemaMigration(Base):
    __tablename__ = 'schema_migrations'
    version = Column(Integer, primary_key=True)
    applied_at = Column(Float, nullable=False)


class AgentRun(Base):
    __tablename__ = 'agent_runs_v2'
    id = Column(Text, primary_key=True)
    session_id = Column(Text, nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    request_id = Column(Text, nullable=False)
    input_json = Column(Text, nullable=False)
    status = Column(Text, nullable=False, index=True, default='queued')
    checkpoint_json = Column(Text, nullable=False, default='{}')
    result_json = Column(Text)
    owner = Column(Text)
    lease_until = Column(Float, nullable=False, default=0)
    attempts = Column(Integer, nullable=False, default=0)
    cancel_requested = Column(Integer, nullable=False, default=0)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)
    __table_args__ = (UniqueConstraint('session_id', 'request_id', name='uq_run_request_v2'),)


class RunEvent(Base):
    __tablename__ = 'run_events_v2'
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Text, nullable=False, index=True)
    kind = Column(Text, nullable=False)
    data_json = Column(Text, nullable=False)
    created_at = Column(Float, nullable=False)


class RunStep(Base):
    __tablename__ = 'run_steps_v2'
    run_id = Column(Text, primary_key=True)
    step_id = Column(Text, primary_key=True)
    role = Column(Text, nullable=False)
    kind = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    ok = Column(Integer, nullable=False)
    duration_ms = Column(Float, nullable=False, default=0)
    usage_json = Column(Text, nullable=False, default='{}')
    error_code = Column(Text)


class SubtaskRecord(Base):
    __tablename__ = 'agent_subtasks_v2'
    session_id = Column(Text, primary_key=True)
    subtask_id = Column(Text, primary_key=True)
    run_id = Column(Text, nullable=False, index=True)
    status = Column(Text, nullable=False)
    body_json = Column(Text, nullable=False)


class ArtifactRecord(Base):
    __tablename__ = 'agent_artifacts_v2'
    session_id = Column(Text, primary_key=True)
    artifact_id = Column(Text, primary_key=True)
    run_id = Column(Text, nullable=False, index=True)
    kind = Column(Text, nullable=False)
    profile_version = Column(Integer, nullable=False)
    body_json = Column(Text, nullable=False)


class SourceRecord(Base):
    __tablename__ = 'agent_sources_v2'
    session_id = Column(Text, primary_key=True)
    source_id = Column(Text, primary_key=True)
    body_json = Column(Text, nullable=False)
    versions_json = Column(Text, nullable=False, default='[]')


class UserKnowledge(Base):
    __tablename__ = 'user_knowledge_v2'
    user_id = Column(Integer, primary_key=True)
    revision = Column(Integer, nullable=False, default=1)
    profile_json = Column(Text, nullable=False, default='{}')


class UserMemory(Base):
    __tablename__ = 'user_memories_v2'
    user_id = Column(Integer, primary_key=True)
    memory_id = Column(Text, primary_key=True)
    body_json = Column(Text, nullable=False)
    revision = Column(Integer, nullable=False, default=1)
    deleted = Column(Integer, nullable=False, default=0)


class AdmissionPolicy(Base):
    __tablename__ = 'admission_policies_v2'
    id = Column(Text, primary_key=True)
    university_id = Column(Text, nullable=False, index=True)
    major_name = Column(Text, nullable=False)
    enrollment_type = Column(Text, nullable=False)
    province = Column(Text, nullable=False)
    subject_type = Column(Text, nullable=False)
    target_year = Column(Integer, nullable=False, index=True)
    body_json = Column(Text, nullable=False)


def init_db():
    """建表（幂等操作，已存在则跳过）"""
    Base.metadata.create_all(bind=engine)
    from services.run_store import migrate
    with SessionLocal() as db:
        migrate(db)
    print("✅ 数据库表结构初始化完成")


def get_db():
    """FastAPI 依赖注入用"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
