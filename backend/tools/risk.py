"""
规则引擎：硬性过滤 + 录取风险分层。

职责边界：
  1. 只做可解释的硬筛和冲/稳/保风险计算。
  2. 返回尽量完整的候选池给 LLM 做个性化筛选、排序、推荐理由。
  3. 不在规则层固定凑满某个展示数量。
"""
from __future__ import annotations

from statistics import pstdev

from sqlalchemy.orm import Session

from database.models import AdmissionRecord, ScoreSegment, University


RECENT_YEARS = [2023, 2024, 2025]
YEAR_WEIGHTS = {2025: 0.5, 2024: 0.3, 2023: 0.2}

# 位次比例：历史录取位次 / 考生位次。位次数值越小代表排名越靠前。
RATIO_MIN = 0.85
RATIO_CHONG_MAX = 0.95
RATIO_WEN_CORE_MAX = 1.05
RATIO_WEN_MAX = 1.10
RATIO_BAO_PREFERRED_MAX = 1.30
RATIO_BAO_EXTENDED_MAX = 1.40

MAJOR_DIRECTION_KEYWORDS = {
    "计算机类": ["计算机", "软件", "网络工程", "物联网", "大数据", "人工智能", "数据科学", "智能科学"],
    "电子信息类": ["电子", "通信", "微电子", "集成电路", "光电", "信息工程"],
    "电气类": ["电气", "智能电网"],
    "自动化类": ["自动化", "机器人工程", "智能制造", "测控"],
    "机械类": ["机械", "车辆工程", "工业设计"],
    "土木建筑类": ["土木", "建筑", "城乡规划", "风景园林"],
    "能源与材料类": ["新能源", "能源", "储能", "材料"],
    "化工环境类": ["化学工程", "化工", "制药", "环境", "安全工程"],
    "数学物理类": ["数学", "统计", "物理", "应用数学", "信息与计算科学"],
    "生物医学类": ["生物科学", "生物工程", "生物医学", "药学"],
    "医学类": ["临床医学", "口腔", "中医学", "护理", "医学影像", "麻醉", "医学"],
    "农林食品类": ["农学", "动物医学", "园艺", "林学", "食品", "水产"],
    "金融经济类": ["金融", "经济", "财政", "国际经济与贸易", "数字经济"],
    "会计管理类": ["会计", "财务管理", "工商管理", "人力资源", "物流管理"],
    "法学公安类": ["法学", "知识产权", "公安", "侦查", "监狱"],
    "教育师范类": ["教育", "学前教育", "小学教育", "特殊教育", "体育教育", "师范"],
    "语言文学类": ["汉语言", "英语", "日语", "翻译", "商务英语", "小语种", "外语", "文学"],
    "新闻传媒类": ["新闻", "传播", "广告", "网络与新媒体", "编辑出版"],
    "艺术设计类": ["美术", "视觉传达", "数字媒体艺术", "动画", "产品设计", "环境设计", "艺术"],
    "公共服务类": ["公共管理", "行政管理", "社会工作", "旅游管理", "酒店管理", "图书情报"],
}


def _weighted_average_rank(history: list[dict], year_weights=None) -> int | None:
    weighted_sum = 0.0
    weight_sum = 0.0
    for row in history:
        rank = row.get("min_rank")
        year = row.get("year")
        if not rank or rank < 0:
            continue
        weight = (year_weights or YEAR_WEIGHTS).get(year, 0.1)
        weighted_sum += rank * weight
        weight_sum += weight
    if weight_sum == 0:
        return None
    return round(weighted_sum / weight_sum)


def _score_to_rank(db: Session, province: str, subject_type: str, year: int, score: float | None) -> int | None:
    """用一分一段表把平均分近似换成位次；找不着就返回 None。"""
    if score is None:
        return None
    row = (
        db.query(ScoreSegment)
        .filter(
            ScoreSegment.province == province,
            ScoreSegment.subject_type == subject_type,
            ScoreSegment.year == year,
            ScoreSegment.score <= int(round(score)),
        )
        .order_by(ScoreSegment.score.desc())
        .first()
    )
    return row.cumulative_count if row else None


def _weighted_average_score_rank(
    db: Session,
    province: str,
    subject_type: str,
    history: list[dict],
    year_weights=None,
) -> int | None:
    weighted_sum = 0.0
    weight_sum = 0.0
    for row in history:
        avg_rank = _score_to_rank(db, province, subject_type, row.get("year"), row.get("avg_score"))
        if not avg_rank:
            continue
        weight = (year_weights or YEAR_WEIGHTS).get(row.get("year"), 0.1)
        weighted_sum += avg_rank * weight
        weight_sum += weight
    if weight_sum == 0:
        return None
    return round(weighted_sum / weight_sum)


def _rank_trend(ranks_by_year: list[tuple[int, int]]) -> str:
    if len(ranks_by_year) < 2:
        return "insufficient_data"
    first_rank = ranks_by_year[0][1]
    last_rank = ranks_by_year[-1][1]
    if last_rank < first_rank * 0.97:
        return "harder"
    if last_rank > first_rank * 1.03:
        return "easier"
    return "stable"


def _classify_tier(risk_ratio: float) -> tuple[str, str, int]:
    """
    返回 (tier, tier_reason, risk_score)。
    risk_score 越高代表录取把握越高。
    """
    if risk_ratio < RATIO_CHONG_MAX:
        if risk_ratio >= RATIO_MIN:
            return "冲", "往年参考位次比考生高约5%-15%，属于跳一跳够得着", 35
        return "冲", "往年参考位次明显高于考生，风险偏高，仅作为心仪冲刺项", 20
    if risk_ratio <= RATIO_WEN_MAX:
        if risk_ratio <= RATIO_WEN_CORE_MAX:
            return "稳", "往年参考位次与考生位次非常接近，属于核心稳定区间", 70
        return "稳", "往年参考位次略低于考生，录取把握较稳", 78
    if risk_ratio <= RATIO_BAO_PREFERRED_MAX:
        return "保", "往年参考位次明显低于考生，适合作为可靠保底", 90
    return "保", "往年参考位次低于考生较多，保底属性更强", 96


def _passes_disliked_subject_filter(major_name: str, disliked_subjects: list[str]) -> bool:
    if not disliked_subjects:
        return True
    for subject in disliked_subjects:
        keywords = [subject]
        keywords.extend(MAJOR_DIRECTION_KEYWORDS.get(subject, []))
        if subject == "英语":
            keywords.extend(["外语", "翻译"])
        if any(keyword in major_name for keyword in keywords):
            return False
    return True


def city_name(value: str | None) -> str:
    return (value or "").strip().removesuffix("特别行政区").removesuffix("市")


def _fallback_sort_key(item: dict, preferred_cities: set[str]) -> tuple:
    tier_order = {"冲": 0, "稳": 1, "保": 2}
    city_bonus = 0 if city_name(item["city"]) in preferred_cities else 1
    ratio = item.get("risk_ratio") or 9
    return (tier_order.get(item.get("tier"), 9), city_bonus, abs(ratio - 1), -item.get("risk_score", 0))


def run_rule_engine(db: Session, profile: dict) -> list[dict]:
    """
    返回经过硬筛、带风险指标的候选池。
    LLM 会在此基础上做最终筛选和排序。
    """
    basic = profile["basic_info"]
    constraints = profile["constraints"]

    province = basic["province"]
    subject_type = basic["subject_type"]
    user_rank = basic["province_rank"]
    if type(user_rank) is not int or user_rank <= 0:
        raise ValueError("考生位次必须为正整数")
    preferred_cities = {city_name(city) for city in constraints.get("preferred_cities", [])}
    excluded_cities = {city_name(city) for city in constraints.get("excluded_cities", [])}
    accept_sino_foreign = constraints.get("accept_sino_foreign", False)
    disliked_subjects = profile.get("academic_preferences", {}).get("disliked_subjects", [])
    years_query = db.query(AdmissionRecord.year).filter(
        AdmissionRecord.province == province, AdmissionRecord.subject_type == subject_type)
    if basic.get("target_year"):
        years_query = years_query.filter(AdmissionRecord.year < basic["target_year"])
    recent_years = [row.year for row in years_query.distinct().order_by(
        AdmissionRecord.year.desc()).limit(3)]
    year_weights = dict(zip(recent_years, (0.5, 0.3, 0.2)))

    rank_high = max(1, round(user_rank * RATIO_MIN))
    rank_low = round(user_rank * RATIO_BAO_EXTENDED_MAX)
    print(f"[RuleEngine] 考生位次={user_rank}, 百分比筛选区间=[{rank_high}, {rank_low}]", flush=True)

    qualifying_combos = (
        db.query(
            AdmissionRecord.university_id,
            AdmissionRecord.major_name,
            AdmissionRecord.enrollment_type,
        )
        .filter(
            AdmissionRecord.province == province,
            AdmissionRecord.subject_type == subject_type,
            AdmissionRecord.year.in_(recent_years),
            AdmissionRecord.min_rank >= rank_high,
            AdmissionRecord.min_rank <= rank_low,
        )
        .distinct()
        .all()
    )

    if not qualifying_combos:
        print("[RuleEngine] 未找到符合条件的学校专业组合", flush=True)
        return []

    univ_ids = list({combo.university_id for combo in qualifying_combos})
    universities = {
        u.university_id: u
        for u in db.query(University).filter(University.university_id.in_(univ_ids)).all()
    }

    candidates = []
    for combo in qualifying_combos:
        univ = universities.get(combo.university_id)
        if univ is None:
            continue
        if city_name(univ.city) in excluded_cities:
            continue
        if (univ.is_sino_foreign or "中外合作" in combo.enrollment_type) and not accept_sino_foreign:
            continue
        if not _passes_disliked_subject_filter(combo.major_name, disliked_subjects):
            continue

        history_rows = (
            db.query(AdmissionRecord)
            .filter(
                AdmissionRecord.university_id == combo.university_id,
                AdmissionRecord.major_name == combo.major_name,
                AdmissionRecord.enrollment_type == combo.enrollment_type,
                AdmissionRecord.province == province,
                AdmissionRecord.subject_type == subject_type,
                AdmissionRecord.year.in_(recent_years),
            )
            .order_by(AdmissionRecord.year.asc())
            .all()
        )

        admission_history = [
            {
                "year": r.year,
                "major_category": r.major_category,
                "min_score": r.min_score,
                "min_rank": r.min_rank,
                "max_score": r.max_score,
                "avg_score": r.avg_score,
                "enrollment_count": r.enrollment_count,
                "score_diff": r.score_diff,
            }
            for r in history_rows
        ]
        reference_rank = _weighted_average_rank(admission_history, year_weights)
        if not reference_rank:
            continue

        avg_score_rank = _weighted_average_score_rank(db, province, subject_type, admission_history, year_weights)
        risk_basis = "min_rank"
        stable_reference_rank = reference_rank
        if avg_score_rank:
            stable_reference_rank = round((reference_rank + avg_score_rank) / 2)
            risk_basis = "min_rank+avg_score_rank"

        risk_ratio = stable_reference_rank / user_rank
        tier, tier_reason, risk_score = _classify_tier(risk_ratio)

        ranks_by_year = [(h["year"], h["min_rank"]) for h in admission_history if h.get("min_rank")]
        ranks_by_year.sort(key=lambda x: x[0])
        rank_values = [rank for _, rank in ranks_by_year]
        volatility = round(pstdev(rank_values)) if len(rank_values) > 1 else 0

        candidates.append({
            "candidate_id": len(candidates),
            "university_id": univ.university_id,
            "university_name": univ.name,
            "province": univ.province,
            "city": univ.city,
            "university_type": univ.university_type,
            "is_sino_foreign": bool(univ.is_sino_foreign),
            "major_name": combo.major_name,
            "enrollment_type": combo.enrollment_type,
            "admission_history": admission_history,
            "tier": tier,
            "tier_reason": tier_reason,
            "risk_ratio": round(risk_ratio, 3),
            "risk_score": risk_score,
            "reference_rank": reference_rank,
            "stable_reference_rank": stable_reference_rank,
            "avg_score_rank": avg_score_rank,
            "risk_basis": risk_basis,
            "rank_trend": _rank_trend(ranks_by_year),
            "rank_volatility": volatility,
            "reason": None,
            "fit_score": None,
            "warnings": [],
            "strategy_note": "",
            "enriched_data": {},
        })

    tier_counts = {tier: sum(1 for c in candidates if c["tier"] == tier) for tier in ["冲", "稳", "保"]}
    print(
        f"[RuleEngine] 硬筛候选={len(candidates)} / 冲={tier_counts['冲']} / 稳={tier_counts['稳']} / 保={tier_counts['保']}",
        flush=True,
    )

    candidates.sort(key=lambda item: _fallback_sort_key(item, preferred_cities))
    for idx, item in enumerate(candidates):
        item["candidate_id"] = idx
    return candidates
