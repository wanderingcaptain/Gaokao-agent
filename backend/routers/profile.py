"""路由：用户画像的创建与读取"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from services.profile_service import create_profile, get_profile

router = APIRouter(prefix="/api/v1", tags=["profile"])


class PriorityFactor(BaseModel):
    factor: str
    rank: int


class ProfileCreateRequest(BaseModel):
    province: str
    subject_type: str
    gaokao_score: int
    province_rank: int

    liked_subjects: list[str] = []
    disliked_subjects: list[str] = []
    math_level: str = "中"
    english_level: str = "中"
    physics_level: str = "中"

    personality_traits: list[str] = []
    career_vision: str = ""

    annual_budget_min: int = 0
    annual_budget_max: int = 100000
    preferred_cities: list[str] = []
    excluded_cities: list[str] = []
    accept_postgrad: bool = True
    accept_sino_foreign: bool = False

    priority_factors: list[PriorityFactor] = []
    extra_notes: str = ""


@router.post("/profile")
async def create_user_profile(body: ProfileCreateRequest):
    uid = create_profile(body.model_dump())
    return {"code": 200, "message": "success", "data": {"uuid": uid}}


@router.get("/profile/{uuid}")
async def get_user_profile(uuid: str):
    profile = get_profile(uuid)
    if not profile:
        raise HTTPException(status_code=404, detail="用户画像不存在")
    return {"code": 200, "message": "success", "data": profile}
