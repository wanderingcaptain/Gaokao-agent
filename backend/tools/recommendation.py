"""确定性方案处理，不调用模型。"""
import os
from copy import deepcopy
from collections import defaultdict
_MAX_FINAL_CANDIDATES = int(os.getenv("LLM_FINAL_CANDIDATES", "45"))

def _merge_llm_selection(candidates_by_id: dict[int, dict], rows: list[dict]) -> list[dict]:
    selected = []
    seen = set()
    for row in rows:
        try:
            candidate_id = int(row.get("candidate_id"))
        except (TypeError, ValueError):
            continue
        if candidate_id in seen or candidate_id not in candidates_by_id:
            continue
        original = candidates_by_id[candidate_id]
        merged = deepcopy(original)
        merged["fit_score"] = max(0, min(100, int(row["fit_score"])))
        merged["reason"] = row.get("reason") or original.get("reason") or original.get("tier_reason", "")
        merged["warnings"] = row.get("warnings") or original.get("warnings") or []
        merged["strategy_note"] = row.get("strategy_note") or original.get("strategy_note") or ""
        merged["enriched_data"] = row.get("enriched_data") or original.get("enriched_data") or {}
        selected.append(merged)
        seen.add(candidate_id)
    return selected


def _fallback_candidates(candidates: list[dict], limit: int = _MAX_FINAL_CANDIDATES) -> list[dict]:
    if limit <= 0:
        return []
    candidates = deepcopy(candidates)
    targets = {"冲": 0.15, "稳": 0.60, "保": 0.25}
    grouped = defaultdict(list)
    for item in candidates:
        grouped[item.get("tier", "稳")].append(item)

    result = []
    for tier in ["冲", "稳", "保"]:
        tier_limit = max(1, round(limit * targets[tier]))
        ranked = sorted(
            grouped[tier],
            key=lambda x: (
                abs((x.get("risk_ratio") or 1) - 1),
                -x.get("risk_score", 0),
            ),
        )
        result.extend(ranked[:tier_limit])

    if len(result) < min(limit, len(candidates)):
        selected_ids = {c["candidate_id"] for c in result}
        extras = [c for c in candidates if c["candidate_id"] not in selected_ids]
        extras.sort(key=lambda x: ({"稳": 0, "保": 1, "冲": 2}.get(x.get("tier"), 9), abs((x.get("risk_ratio") or 1) - 1)))
        result.extend(extras[:limit - len(result)])

    for item in result[:limit]:
        if item.get("fit_score") is None:
            item["fit_score"] = item.get("risk_score", 70)
        item["reason"] = item.get("reason") or item.get("tier_reason") or "根据历史录取位次与画像偏好生成的规则备选结果"
        item["warnings"] = item.get("warnings") or ["AI筛选暂不可用，建议人工复核专业与招生章程"]
        item["strategy_note"] = item.get("strategy_note") or "当前为规则备选结果，可作为临时参考"
        item.setdefault("enriched_data", {})
    return result[:limit]


def _finalize_shortlist(shortlist: list[dict], limit: int = _MAX_FINAL_CANDIDATES) -> list[dict]:
    """后端汇总排序：保留批筛生成的解释字段，不再二次调用 LLM。"""
    if not shortlist:
        return []

    targets = {"冲": 0.15, "稳": 0.60, "保": 0.25}
    grouped = defaultdict(list)
    for item in shortlist:
        grouped[item.get("tier", "稳")].append(item)

    for tier_items in grouped.values():
        tier_items.sort(key=lambda x: (
            -int(x.get("fit_score") or 0),
            -int(x.get("risk_score") or 0),
            abs((x.get("risk_ratio") or 1) - 1),
        ))

    selected = []
    selected_ids = set()
    for tier in ["冲", "稳", "保"]:
        tier_limit = max(1, round(limit * targets[tier]))
        for item in grouped[tier][:tier_limit]:
            selected.append(item)
            selected_ids.add(item["candidate_id"])

    if len(selected) < min(limit, len(shortlist)):
        extras = [item for item in shortlist if item["candidate_id"] not in selected_ids]
        extras.sort(key=lambda x: (
            -int(x.get("fit_score") or 0),
            {"稳": 0, "保": 1, "冲": 2}.get(x.get("tier"), 9),
            abs((x.get("risk_ratio") or 1) - 1),
        ))
        selected.extend(extras[:limit - len(selected)])

    selected = selected[:limit]
    selected.sort(key=lambda x: (
        {"冲": 0, "稳": 1, "保": 2}.get(x.get("tier"), 9),
        -int(x.get("fit_score") or 0),
        abs((x.get("risk_ratio") or 1) - 1),
    ))
    return selected
