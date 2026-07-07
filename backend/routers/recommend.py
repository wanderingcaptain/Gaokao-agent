"""路由：推荐流程（规则引擎硬筛 + LLM 筛选），使用 SSE 流式推送"""
import json
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.models import RecommendationResult, User, UserProfile, get_db
from services.auth_service import get_current_user, now_iso
from services.profile_service import get_profile
from services.rule_engine import run_rule_engine
from services.llm_service import enrich_and_classify

router = APIRouter(prefix="/api/v1", tags=["recommend"])


class RecommendRequest(BaseModel):
    uuid: str


def _profile_summary(profile: dict, candidate_count: int) -> dict:
    basic = profile.get("basic_info", {})
    return {
        "province": basic.get("province"),
        "subject_type": basic.get("subject_type"),
        "gaokao_score": basic.get("gaokao_score"),
        "province_rank": basic.get("province_rank"),
        "candidate_count": candidate_count,
    }


def _ensure_profile_owner(db: Session, user_id: int, profile_uuid: str):
    owned = (
        db.query(UserProfile)
        .filter(UserProfile.user_id == user_id, UserProfile.profile_uuid == profile_uuid)
        .first()
    )
    if not owned:
        raise HTTPException(status_code=404, detail="用户画像不存在")
    return owned


def _save_recommendation_result(
    db: Session,
    user_id: int,
    profile_uuid: str,
    profile: dict,
    payload: dict,
) -> int:
    candidates = payload.get("candidates", [])
    row = RecommendationResult(
        user_id=user_id,
        profile_uuid=profile_uuid,
        result_json=json.dumps(payload, ensure_ascii=False),
        fallback=1 if payload.get("fallback") else 0,
        summary=json.dumps(_profile_summary(profile, len(candidates)), ensure_ascii=False),
        created_at=now_iso(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


@router.post("/preliminary")
async def get_preliminary(
    body: RecommendRequest,
    current_user: User = Depends(get_current_user),
):
    """返回规则引擎硬筛结果，主要用于调试和兜底。"""
    db_gen = get_db()
    db = next(db_gen)
    try:
        _ensure_profile_owner(db, current_user.id, body.uuid)
        profile = get_profile(body.uuid)
        if not profile:
            raise HTTPException(status_code=404, detail="用户画像不存在")
        candidates = run_rule_engine(db, profile)
        return {"code": 200, "message": "success", "data": {"candidates": candidates}}
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass


async def _sse_stream(uuid: str, user_id: int, request: Request):
    """异步生成器：规则硬筛进度 + LLM 分批筛选结果。"""
    def fmt(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    db_gen = get_db()
    db = next(db_gen)
    try:
        try:
            _ensure_profile_owner(db, user_id, uuid)
        except HTTPException:
            yield fmt("error", {"message": "用户画像不存在，请重新填写信息"})
            return
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass

    profile = get_profile(uuid)
    if not profile:
        yield fmt("error", {"message": "用户画像不存在，请重新填写信息"})
        return

    if await request.is_disconnected():
        return

    yield fmt("progress", {"stage": "rule_screening", "message": "正在根据位次、城市、招生类型和偏好生成硬筛候选池..."})

    db_gen = get_db()
    db = next(db_gen)
    try:
        candidates = run_rule_engine(db, profile)
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass

    if not candidates:
        empty_payload = {"candidates": [], "fallback": False, "message": "未找到符合条件的候选，结果已保存"}
        save_gen = get_db()
        save_db = next(save_gen)
        try:
            result_id = _save_recommendation_result(save_db, user_id, uuid, profile, empty_payload)
            empty_payload["result_id"] = result_id
        finally:
            try:
                next(save_gen)
            except StopIteration:
                pass
        yield fmt("result", empty_payload)
        return

    if await request.is_disconnected():
        return

    yield fmt("progress", {
        "stage": "rule_screening",
        "message": f"硬筛完成，共 {len(candidates)} 个候选，开始交给 AI 分批筛选..."
    })

    async for event_dict in enrich_and_classify(profile, candidates):
        if await request.is_disconnected():
            print(f"[Recommend] 用户已断开连接，停止推荐流 uuid={uuid}", flush=True)
            return
        if event_dict["event"] == "result":
            save_gen = get_db()
            save_db = next(save_gen)
            try:
                payload = dict(event_dict["data"])
                result_id = _save_recommendation_result(save_db, user_id, uuid, profile, payload)
                payload["result_id"] = result_id
                payload["message"] = payload.get("message") or "推荐结果已保存到历史记录"
                event_dict = {"event": "result", "data": payload}
            finally:
                try:
                    next(save_gen)
                except StopIteration:
                    pass
        yield fmt(event_dict["event"], event_dict["data"])


@router.post("/recommend")
async def recommend(
    body: RecommendRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return StreamingResponse(
        _sse_stream(body.uuid, current_user.id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/recommend/history")
async def recommend_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(RecommendationResult)
        .filter(RecommendationResult.user_id == current_user.id)
        .order_by(RecommendationResult.id.desc())
        .limit(50)
        .all()
    )
    items = []
    for row in rows:
        summary = json.loads(row.summary or "{}")
        items.append({
            "id": row.id,
            "profile_uuid": row.profile_uuid,
            "fallback": bool(row.fallback),
            "created_at": row.created_at,
            "summary": summary,
        })
    return {"code": 200, "message": "success", "data": {"items": items}}


@router.get("/recommend/history/{result_id}")
async def recommend_history_detail(
    result_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = (
        db.query(RecommendationResult)
        .filter(RecommendationResult.id == result_id, RecommendationResult.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="推荐结果不存在")
    return {
        "code": 200,
        "message": "success",
        "data": {
            "id": row.id,
            "profile_uuid": row.profile_uuid,
            "fallback": bool(row.fallback),
            "created_at": row.created_at,
            "summary": json.loads(row.summary or "{}"),
            "result": json.loads(row.result_json),
        },
    }
