"""
路由：院校详情
  - GET  /detail/history   — 查本地 DB，返回历年录取数据（无 AI，极快）
  - POST /detail/ai-report — 调用 LLM 生成深度分析报告（原 /detail 端点）
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import json
from sqlalchemy.orm import Session
from database.models import AdmissionRecord, User, UserProfile, AgentTask, get_db
from services.auth_service import get_current_user
from services.profile_service import get_profile
from services.llm_service import generate_detail_ui

router = APIRouter(prefix="/api/v1", tags=["detail"])


# ─── GET /detail/history ─────────────────────────────────────────────────────

@router.get("/detail/history")
async def get_history(
    university_id:   str = Query(...),
    major_name:      str = Query(...),
    enrollment_type: str = Query(...),
    province:        str = Query(...),
    subject_type:    str = Query(...),
):
    """查询本地 DB 中该学校+专业+省份的近五年录取数据，无 AI 调用，响应极快。"""
    db_gen = get_db()
    db: Session = next(db_gen)
    try:
        rows = (
            db.query(AdmissionRecord)
            .filter(
                AdmissionRecord.university_id   == university_id,
                AdmissionRecord.major_name      == major_name,
                AdmissionRecord.enrollment_type == enrollment_type,
                AdmissionRecord.province        == province,
                AdmissionRecord.subject_type    == subject_type,
            )
            .order_by(AdmissionRecord.year.asc())
            .all()
        )
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass

    history = [
        {
            "year":             r.year,
            "min_rank":         r.min_rank,
            "min_score":        r.min_score,
            "max_score":        r.max_score,
            "avg_score":        r.avg_score,
            "enrollment_count": r.enrollment_count,
            "score_diff":       r.score_diff,
        }
        for r in rows
    ]
    return {"code": 200, "data": {"history": history}}


# ─── POST /detail/ai-report ───────────────────────────────────────────────────

class AIReportRequest(BaseModel):
    uuid:            str
    university_id:   str
    major_name:      str
    university_data: dict
    task_id: str | None = None
    profile_version: int | None = None


@router.post("/detail/ai-report")
async def get_ai_report(
    body: AIReportRequest,
    current_user: User = Depends(get_current_user),
):
    """调用 LLM 生成院校专业深度分析报告（耗时较长）"""
    db_gen = get_db()
    db = next(db_gen)
    task_profile = None
    try:
        if body.task_id:
            task = db.get(AgentTask, body.task_id)
            if not task or task.user_id != current_user.id or task.profile_uuid != body.uuid:
                raise HTTPException(status_code=404, detail="任务不存在")
            profiles = json.loads(task.state_json)["profiles"]
            version = body.profile_version or len(profiles)
            if not 1 <= version <= len(profiles):
                raise HTTPException(status_code=422, detail="画像版本不存在")
            task_profile = profiles[version - 1]
        owned = (
            db.query(UserProfile)
            .filter(UserProfile.user_id == current_user.id, UserProfile.profile_uuid == body.uuid)
            .first()
        )
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass
    if not owned:
        raise HTTPException(status_code=404, detail="用户画像不存在")
    profile = task_profile or get_profile(body.uuid)
    if not profile:
        raise HTTPException(status_code=404, detail="用户画像不存在")
    try:
        result = await generate_detail_ui(profile, body.university_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 分析生成失败: {str(e)}")
    return {"code": 200, "message": "success", "data": result}
