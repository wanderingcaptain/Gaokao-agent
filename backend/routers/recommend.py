"""路由：推荐流程（规则引擎 + LLM 分级），使用 SSE 流式推送"""
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from database.models import get_db
from services.profile_service import get_profile
from services.rule_engine import run_rule_engine
from services.llm_service import enrich_and_classify

router = APIRouter(prefix="/api/v1", tags=["recommend"])


class RecommendRequest(BaseModel):
    uuid: str


@router.post("/preliminary")
async def get_preliminary(body: RecommendRequest):
    """
    直接返回规则引擎的初筛结果（普通 JSON 接口，不使用流式）
    """
    profile = get_profile(body.uuid)
    if not profile:
        raise HTTPException(status_code=404, detail="用户画像不存在")
    db_gen = get_db()
    db = next(db_gen)
    try:
        candidates = run_rule_engine(db, profile)
        return {"code": 200, "message": "success", "data": {"candidates": candidates}}
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass


async def _sse_stream(uuid: str):
    """异步生成器，仅负责 LLM 增强的流式推送"""
    def fmt(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    profile = get_profile(uuid)
    if not profile:
        yield fmt("error", {"message": "用户画像不存在，请重新填写信息"})
        return

    # 先做一次规则引擎拿到同样的候选列表（速度很快<0.1s，重复做也没事）
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
        yield fmt("result", {"candidates": []})
        return

    # 直接开始 LLM 补全 + 分级
    async for event_dict in enrich_and_classify(profile, candidates):
        yield fmt(event_dict["event"], event_dict["data"])


@router.post("/recommend")
async def recommend(body: RecommendRequest):
    return StreamingResponse(
        _sse_stream(body.uuid),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )
