"""院校历史录取和基于本地数据的个性化报告。"""
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database.models import AdmissionRecord, AgentTask, University, User, UserProfile, get_db
from services.auth_service import get_current_user
from services.llm_service import generate_detail_ui

router = APIRouter(prefix="/api/v1", tags=["detail"])


def _history(db, university_id, major_name, enrollment_type, province, subject_type):
    rows = db.query(AdmissionRecord).filter_by(
        university_id=university_id, major_name=major_name, enrollment_type=enrollment_type,
        province=province, subject_type=subject_type,
    ).order_by(AdmissionRecord.year.asc())
    fields = ("year", "min_rank", "min_score", "max_score", "avg_score", "enrollment_count", "score_diff")
    return [{field: getattr(row, field) for field in fields} for row in rows]


@router.get("/detail/history")
async def get_history(
    university_id:   str = Query(...),
    major_name:      str = Query(...),
    enrollment_type: str = Query(...),
    province:        str = Query(...),
    subject_type:    str = Query(...),
    db: Session = Depends(get_db),
):
    history = _history(db, university_id, major_name, enrollment_type, province, subject_type)
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
    db: Session = Depends(get_db),
):
    owned = db.query(UserProfile).filter_by(user_id=current_user.id, profile_uuid=body.uuid).first()
    if not owned:
        raise HTTPException(status_code=404, detail="用户画像不存在")
    profile = json.loads(owned.profile_json)
    if body.task_id:
        task = db.get(AgentTask, body.task_id)
        if not task or task.user_id != current_user.id or task.profile_uuid != body.uuid:
            raise HTTPException(status_code=404, detail="任务不存在")
        profiles = json.loads(task.state_json)["profiles"]
        version = body.profile_version if body.profile_version is not None else len(profiles)
        if not 1 <= version <= len(profiles):
            raise HTTPException(status_code=422, detail="画像版本不存在")
        profile = profiles[version - 1]

    enrollment_type = body.university_data.get("enrollment_type")
    if not isinstance(enrollment_type, str) or not enrollment_type.strip():
        raise HTTPException(status_code=422, detail="请指定候选的招生类型")
    basic = profile["basic_info"]
    school = db.get(University, body.university_id)
    history = _history(db, body.university_id, body.major_name, enrollment_type,
                       basic["province"], basic["subject_type"])
    if not school or not history:
        raise HTTPException(status_code=404, detail="本省份和科类暂无该候选的录取记录")
    candidate = {"university_name": school.name, "city": school.city, "university_type": school.university_type,
                 "major_name": body.major_name, "enrollment_type": enrollment_type, "admission_history": history[-3:]}
    db.close()
    try:
        result = await generate_detail_ui(profile, candidate)
    except Exception:
        raise HTTPException(status_code=500, detail="模型服务暂时无法生成报告，请稍后重试") from None
    return {"code": 200, "message": "success", "data": result}
