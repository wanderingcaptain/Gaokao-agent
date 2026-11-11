"""
高考志愿规划 Agent。

Agent 负责决定下一步使用什么工具；工具负责访问数据和执行确定性校验。
规则引擎是不可绕过的安全护栏，Agent 只能从硬筛候选池中提交方案。
"""
from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field

from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from tools.recommendation import (
    _MAX_FINAL_CANDIDATES,
    _fallback_candidates,
    _finalize_shortlist,
    _merge_llm_selection,
)
from tools.risk import run_rule_engine


AGENT_PAGE_SIZE = int(os.getenv("AGENT_PAGE_SIZE", "60"))


@dataclass
class AgentState:
    """一次推荐任务的状态，不跨用户和请求共享。"""

    profile: dict
    candidates: list[dict] = field(default_factory=list)
    shortlist: list[dict] = field(default_factory=list)
    final_candidates: list[dict] | None = None
    validation: dict | None = None
    fallback: bool = False
    step_count: int = 0
    tools_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    viewed_ids: set[int] = field(default_factory=set)
    waiting: dict | None = None
    answer: dict | None = None

    @property
    def candidates_by_id(self) -> dict[int, dict]:
        return {int(item["candidate_id"]): item for item in self.candidates}


def _candidate_view(candidate: dict) -> dict:
    """给模型的候选摘要，不暴露 ORM 对象或无关内部字段。"""
    history = [
        [row.get("year"), row.get("min_rank"), row.get("avg_score"), row.get("enrollment_count")]
        for row in candidate.get("admission_history", [])
    ]
    return {
        "candidate_id": candidate.get("candidate_id"),
        "school": candidate.get("university_name"),
        "major": candidate.get("major_name"),
        "city": candidate.get("city"),
        "school_type": candidate.get("university_type"),
        "enrollment_type": candidate.get("enrollment_type"),
        "tier": candidate.get("tier"),
        "risk_ratio": candidate.get("risk_ratio"),
        "risk_score": candidate.get("risk_score"),
        "tier_reason": candidate.get("tier_reason"),
        "rank_trend": candidate.get("rank_trend"),
        "rank_volatility": candidate.get("rank_volatility"),
        "history": history,
    }


def _candidate_detail(candidate: dict) -> dict:
    detail = _candidate_view(candidate)
    detail.update({
        "university_id": candidate.get("university_id"),
        "stable_reference_rank": candidate.get("stable_reference_rank"),
        "avg_score_rank": candidate.get("avg_score_rank"),
        "risk_basis": candidate.get("risk_basis"),
        "admission_history": candidate.get("admission_history", []),
    })
    return detail


def _tier_counts(candidates: list[dict]) -> dict[str, int]:
    counts = Counter(item.get("tier", "未知") for item in candidates)
    return {tier: counts.get(tier, 0) for tier in ("冲", "稳", "保")}


def _candidate_page(state: AgentState, args: dict) -> dict:
    tier = args.get("tier")
    offset = max(0, int(args.get("offset", 0) or 0))
    limit = min(max(1, int(args.get("limit", AGENT_PAGE_SIZE) or AGENT_PAGE_SIZE)), AGENT_PAGE_SIZE)

    pool = state.candidates
    if tier in {"冲", "稳", "保"}:
        pool = [item for item in pool if item.get("tier") == tier]
    if args.get("city"):
        pool = [item for item in pool if item.get("city") == args["city"]]
    if args.get("major"):
        pool = [item for item in pool if args["major"] in item.get("major_name", "")]

    page = pool[offset:offset + limit]
    state.viewed_ids.update(item["candidate_id"] for item in page)
    next_offset = offset + len(page)
    return {
        "ok": True,
        "tier": tier or "all",
        "offset": offset,
        "returned": len(page),
        "total_in_view": len(pool),
        "next_offset": next_offset if next_offset < len(pool) else None,
        "remaining_in_view": max(0, len(pool) - next_offset),
        "candidates": [_candidate_view(item) for item in page],
    }


def _candidate_details(state: AgentState, args: dict) -> dict:
    raw_ids = args.get("candidate_ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return {"ok": False, "error": "candidate_ids 不能为空"}
    if len(raw_ids) > 10:
        return {"ok": False, "error": "一次最多查看 10 个候选详情"}
    try:
        candidate_ids = list(dict.fromkeys(int(value) for value in raw_ids))
    except (TypeError, ValueError):
        return {"ok": False, "error": "candidate_ids 必须是整数数组"}

    by_id = state.candidates_by_id
    missing = [value for value in candidate_ids if value not in by_id]
    if missing:
        return {"ok": False, "error": f"候选不存在: {missing[:10]}"}
    state.viewed_ids.update(candidate_ids)
    return {
        "ok": True,
        "candidates": [_candidate_detail(by_id[value]) for value in candidate_ids],
    }


def _submit_recommendation(state: AgentState, args: dict) -> dict:
    rows = args.get("candidates") or []
    if not isinstance(rows, list) or not rows:
        return {"ok": False, "error": "candidates 不能为空"}
    if len(rows) > _MAX_FINAL_CANDIDATES:
        return {"ok": False, "error": f"最多提交 {_MAX_FINAL_CANDIDATES} 个候选"}

    candidate_ids = []
    invalid_rows = []
    for row in rows:
        if not isinstance(row, dict):
            invalid_rows.append("候选项必须是对象")
            continue
        try:
            candidate_ids.append(int(row.get("candidate_id")))
        except (TypeError, ValueError):
            invalid_rows.append("candidate_id 必须是整数")
            continue
        for field in ("fit_score", "reason", "warnings", "strategy_note"):
            if field not in row:
                invalid_rows.append(f"候选 {row.get('candidate_id')} 缺少 {field}")

    if invalid_rows:
        return {"ok": False, "error": "; ".join(invalid_rows[:5])}
    if len(candidate_ids) != len(set(candidate_ids)):
        return {"ok": False, "error": "提交结果包含重复 candidate_id"}
    outside_pool = [value for value in candidate_ids if value not in state.candidates_by_id]
    if outside_pool:
        return {"ok": False, "error": f"候选不在硬筛池中: {outside_pool[:10]}"}
    unseen = set(candidate_ids) - state.viewed_ids
    if unseen:
        return {"ok": False, "error": "先查询候选资料再提交", "unseen_ids": sorted(unseen)}

    # 只合并模型对解释字段的补充，学校、专业、tier、风险数据全部来自规则候选池。
    try:
        selected = _merge_llm_selection(state.candidates_by_id, rows)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"候选解释字段格式无效: {str(exc)[:120]}"}
    if len(selected) != len(rows):
        return {"ok": False, "error": "部分候选无法从硬筛池中解析"}
    state.shortlist = selected
    state.final_candidates = _finalize_shortlist(selected)
    state.fallback = False
    state.validation = None
    return {
        "ok": True,
        "candidate_count": len(state.final_candidates),
        "tier_counts": _tier_counts(state.final_candidates),
        "message": "推荐方案已提交，下一步必须调用 validate_recommendation",
    }


def _validate_state(state: AgentState) -> dict:
    result = state.final_candidates or []
    errors: list[str] = []
    candidate_ids = [item.get("candidate_id") for item in result]
    allowed_ids = set(state.candidates_by_id)

    if not result:
        errors.append("推荐结果为空")
    if len(candidate_ids) != len(set(candidate_ids)):
        errors.append("推荐结果包含重复候选")
    if any(candidate_id not in allowed_ids for candidate_id in candidate_ids):
        errors.append("推荐结果包含硬筛候选池之外的学校或专业")
    if any(item.get("tier") not in {"冲", "稳", "保"} for item in result):
        errors.append("存在无效的冲稳保标签")
    if len(result) > _MAX_FINAL_CANDIDATES:
        errors.append(f"推荐结果超过 {_MAX_FINAL_CANDIDATES} 个候选")

    available_tiers = {item.get("tier") for item in state.candidates}
    selected_tiers = {item.get("tier") for item in result}
    missing_tiers = sorted((available_tiers & {"冲", "稳", "保"}) - selected_tiers)
    if missing_tiers:
        errors.append(f"缺少可用的风险层级: {'、'.join(missing_tiers)}")

    required_fields = ("fit_score", "reason", "warnings", "strategy_note")
    for item in result:
        original = state.candidates_by_id.get(item.get("candidate_id"))
        if original is not None:
            explanation_fields = {"fit_score", "reason", "warnings", "strategy_note", "enriched_data"}
            if any(item.get(key) != value for key, value in original.items() if key not in explanation_fields):
                errors.append(f"候选 {item.get('candidate_id')} 的事实或风险数据与候选池不一致")
        missing = [field for field in required_fields if field not in item]
        if missing:
            errors.append(f"候选 {item.get('candidate_id')} 缺少解释字段: {','.join(missing)}")

    state.validation = {
        "valid": not errors,
        "errors": errors,
        "checked_candidate_count": len(result),
        "unverified": ["学费与年度预算", "具体选科资格", "当年招生章程及计划"],
    }
    return {"ok": True, **state.validation}


async def _execute_tool(name: str, args: dict, state: AgentState, db: Session | None = None) -> dict:
    state.tools_used.append(name)
    tool = next((t["function"] for t in AGENT_TOOLS if t["function"]["name"] == name), None)
    if tool is None:
        return {"ok": False, "error": "未知工具"}
    errors = list(Draft202012Validator(tool["parameters"]).iter_errors(args))
    if errors:
        return {"ok": False, "error": "工具参数不合法: " + errors[0].message[:300]}
    if name == "ask_user":
        state.waiting = args
        return {"ok": True, "status": "waiting"}
    if name == "get_profile":
        return {"ok": True, "profile": state.profile,
                "coverage": "仅本地历史数据；学费、具体选科要求、当年章程尚未核实"}

    if name == "run_rule_screening":
        if not state.candidates:
            if db is None:
                return {"ok": False, "error": "数据库会话不可用"}
            state.candidates = run_rule_engine(db, state.profile)
        return {
            "ok": True,
            "candidate_count": len(state.candidates),
            "tier_counts": _tier_counts(state.candidates),
            "next_action": "调用 get_candidate_page 浏览候选",
        }
    if name == "get_candidate_page":
        return _candidate_page(state, args)
    if name == "answer_comparison":
        if not set(args["candidate_ids"]).issubset(state.viewed_ids & set(state.candidates_by_id)):
            return {"ok": False, "error": "必须先查询比较对象"}
        state.answer = args
        return {"ok": True}
    if name == "get_candidate_details":
        return _candidate_details(state, args)
    if name == "submit_recommendation":
        return _submit_recommendation(state, args)
    if name == "validate_recommendation":
        return _validate_state(state)
    return {"ok": False, "error": f"未知工具: {name}"}


AGENT_TOOLS = [
    {"type": "function", "function": {
        "name": "answer_comparison", "description": "结束候选比较。仅总结已查询资料，引用候选编号，不生成新志愿方案。",
        "parameters": {"type": "object", "properties": {
            "candidate_ids": {"type": "array", "items": {"type": "integer"},
                              "minItems": 1, "maxItems": 10, "uniqueItems": True},
            "summary": {"type": "string", "minLength": 1, "maxLength": 2000}},
            "required": ["candidate_ids", "summary"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_profile", "description": "读取画像和数据覆盖限制",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "ask_user", "description": "追问缺失信息或冲突；涉及硬约束修改时等待用户在画像编辑区明确修改。",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": 800}},
            "required": ["question"], "additionalProperties": False}}},
    {
        "type": "function",
        "function": {
            "name": "run_rule_screening",
            "description": "运行硬约束筛选，生成不可修改的候选池和冲稳保标签。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_candidate_page",
            "description": "分页浏览硬筛候选。若 next_offset 不为空，应继续浏览该层级的下一页。",
            "parameters": {
                "type": "object",
                "properties": {
                    "tier": {"type": "string", "enum": ["冲", "稳", "保"]},
                    "city": {"type": "string", "maxLength": 50},
                    "major": {"type": "string", "maxLength": 100},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": AGENT_PAGE_SIZE},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_candidate_details",
            "description": "查看最多 10 个硬筛候选的完整历史录取数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                        "maxItems": 10,
                    },
                },
                "required": ["candidate_ids"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_recommendation",
            "description": "提交最终推荐候选及其个性化解释。候选必须来自硬筛池。",
            "parameters": {
                "type": "object",
                "properties": {
                    "candidates": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": _MAX_FINAL_CANDIDATES,
                        "items": {
                            "type": "object",
                            "properties": {
                                "candidate_id": {"type": "integer"},
                                "fit_score": {"type": "integer", "minimum": 0, "maximum": 100},
                                "reason": {"type": "string", "minLength": 1, "maxLength": 300},
                                "warnings": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 300}},
                                "strategy_note": {"type": "string", "minLength": 1, "maxLength": 300},
                            },
                            "required": ["candidate_id", "fit_score", "reason", "warnings", "strategy_note"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["candidates"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_recommendation",
            "description": "确定性验证推荐方案的来源、冲稳保覆盖、重复项和解释字段。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]
