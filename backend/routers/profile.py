"""路由：用户画像的创建与读取"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from database.models import User, UserProfile, get_db
from services.auth_service import get_current_user
from services.profile_service import build_profile, get_profile, save_profile
from tools.risk import city_name

router = APIRouter(prefix="/api/v1", tags=["profile"])


class PriorityFactor(BaseModel):
    factor: str
    rank: int


class PreferenceTreeItem(BaseModel):
    id: str
    label: str
    path: list[str]
    depth: int
    category: str
    signal_type: str


class ProfileCreateRequest(BaseModel):
    province: str
    subject_type: str
    gaokao_score: int = Field(ge=0, le=900)
    province_rank: int = Field(gt=0)

    liked_subjects: list[str] = []
    disliked_subjects: list[str] = []
    math_level: str = "中"
    english_level: str = "中"
    physics_level: str = "中"

    personality_traits: list[str] = []
    career_vision: str = ""

    annual_budget_min: int = Field(default=0, ge=0)
    annual_budget_max: int = Field(default=150000, ge=0)
    preferred_cities: list[str] = []
    excluded_cities: list[str] = []
    accept_postgrad: bool = True
    accept_sino_foreign: bool = False

    priority_factors: list[PriorityFactor] = []
    preference_tree: list[PreferenceTreeItem] = []
    extra_notes: str = ""

    @model_validator(mode="after")
    def consistent_constraints(self):
        if self.annual_budget_min > self.annual_budget_max:
            raise ValueError("预算下限不能高于上限")
        if {city_name(city) for city in self.preferred_cities} & {city_name(city) for city in self.excluded_cities}:
            raise ValueError("偏好城市与排除城市不能重叠")
        return self


@router.post("/profile")
async def create_user_profile(
    body: ProfileCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = build_profile(body.model_dump())
    uid = save_profile(profile)
    db.add(UserProfile(
        user_id=current_user.id,
        profile_uuid=uid,
        profile_json=json.dumps(profile, ensure_ascii=False),
        created_at=profile["created_at"],
    ))
    db.commit()
    return {"code": 200, "message": "success", "data": {"uuid": uid}}


@router.get("/profile/{uuid}")
async def get_user_profile(
    uuid: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owned = (
        db.query(UserProfile)
        .filter(UserProfile.user_id == current_user.id, UserProfile.profile_uuid == uuid)
        .first()
    )
    if not owned:
        raise HTTPException(status_code=404, detail="用户画像不存在")
    profile = get_profile(uuid)
    if not profile:
        raise HTTPException(status_code=404, detail="用户画像不存在")
    return {"code": 200, "message": "success", "data": profile}
