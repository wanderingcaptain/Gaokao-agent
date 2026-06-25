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


def init_db():
    """建表（幂等操作，已存在则跳过）"""
    Base.metadata.create_all(bind=engine)
    print("✅ 数据库表结构初始化完成")


def get_db():
    """FastAPI 依赖注入用"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
