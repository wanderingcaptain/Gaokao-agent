"""
用户画像服务：
- 生成 UUID
- 将画像持久化为 JSON 文件
- 从 JSON 文件读取画像
"""
import json
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

PROFILES_DIR = Path(os.getenv("PROFILES_DIR", "./profiles"))
PROFILES_DIR.mkdir(parents=True, exist_ok=True)

CST = timezone(timedelta(hours=8))


def create_profile(data: dict) -> str:
    """接收原始请求数据，构建标准画像结构，写入 JSON，返回 UUID"""
    uid = str(uuid.uuid4())
    now = datetime.now(CST).isoformat()

    profile = {
        "uuid": uid,
        "created_at": now,
        "basic_info": {
            "province":     data["province"],
            "subject_type": data["subject_type"],
            "gaokao_score": data["gaokao_score"],
            "province_rank": data["province_rank"],
        },
        "academic_preferences": {
            "liked_subjects":    data.get("liked_subjects", []),
            "disliked_subjects": data.get("disliked_subjects", []),
            "math_level":        data.get("math_level", "中"),
            "english_level":     data.get("english_level", "中"),
            "physics_level":     data.get("physics_level", "中"),
        },
        "personality": {
            "traits":         data.get("personality_traits", []),
            "career_vision":  data.get("career_vision", ""),
        },
        "constraints": {
            "annual_budget_min":   data.get("annual_budget_min", 0),
            "annual_budget_max":   data.get("annual_budget_max", 100000),
            "preferred_cities":    data.get("preferred_cities", []),
            "excluded_cities":     data.get("excluded_cities", []),
            "accept_postgrad":     data.get("accept_postgrad", True),
            "accept_sino_foreign": data.get("accept_sino_foreign", False),
        },
        "priority_factors": data.get("priority_factors", []),
        "extra_notes":      data.get("extra_notes", ""),
    }

    file_path = PROFILES_DIR / f"{uid}_用户画像.json"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    return uid


def get_profile(uid: str) -> dict | None:
    """根据 UUID 读取画像，不存在则返回 None"""
    file_path = PROFILES_DIR / f"{uid}_用户画像.json"
    if not file_path.exists():
        return None
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)
