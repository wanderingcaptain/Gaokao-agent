"""
LLM 服务：封装 Qwen API 调用。

包含：
  1. enrich_and_classify() — 分批筛选硬筛候选，并汇总成最终冲稳保榜单。
  2. generate_detail_ui()  — 生成详情页 HTML + ECharts 配置。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import defaultdict

from dotenv import load_dotenv
from openai import OpenAI


# 禁用代理，防止本地代理导致 API 请求极慢或超时
os.environ["HTTP_PROXY"] = ""
os.environ["HTTPS_PROXY"] = ""
os.environ["ALL_PROXY"] = ""

load_dotenv()

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """
    延迟初始化 OpenAI 客户端（使用 openai 标准库）。
    api_key 和 base_url 均从 .env 环境变量读取：
      QWEN_API_KEY  — 阿里云百炼平台 API Key
      QWEN_BASE_URL — 兼容 OpenAI 格式的接入点（默认 DashScope）
    """
    global _client
    if _client is None:
        api_key = os.getenv("QWEN_API_KEY")
        base_url = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        if not api_key:
            raise ValueError(
                "未找到 QWEN_API_KEY，请在 backend/.env 中配置：\n"
                "  QWEN_API_KEY=your_key_here\n"
                "  QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1"
            )
        _client = OpenAI(api_key=api_key, base_url=base_url, timeout=120.0)
    return _client


MODEL = os.getenv("QWEN_MODEL", "qwen-plus")
_BATCH_SIZE = int(os.getenv("LLM_FILTER_BATCH_SIZE", "60"))
_MAX_FINAL_CANDIDATES = int(os.getenv("LLM_FINAL_CANDIDATES", "45"))
_MAX_BATCH_SELECTED = int(os.getenv("LLM_BATCH_SELECTED", "12"))


# ──────────────────────────────────────────────────────────────────────
# Prompt：批次初筛；最终汇总由后端排序完成
# ──────────────────────────────────────────────────────────────────────
_BATCH_FILTER_SYSTEM = """你是一个专业的高考志愿填报顾问。系统已经用真实录取数据完成了硬筛和冲稳保风险分层。

你的任务：根据考生画像，从本批候选中筛出最值得进入最终评估的学校+专业。

重要规则：
1. tier 是规则层给出的录取风险标签，只能原样使用，不能改成其他梯度。
2. 你负责个性化适配：专业兴趣、能力结构、城市、预算、职业方向、性格、优先级。
3. 不要只迷信学校名气；专业匹配、城市接受度、风险结构都要纳入。
4. 每批最多选择 12 个候选。若本批质量一般，可以少选。
5. 输出必须非常精简，只输出 candidate_id、fit_score、reason、warnings、strategy_note。
6. 输出必须是合法 JSON，不要 Markdown，不要代码块，不要补充解释。

输出结构：
{
  "selected_candidates": [
    {
      "candidate_id": 0,
      "fit_score": 88,
      "reason": "不超过60字的个性化推荐理由",
      "warnings": ["最多2条简短风险或注意事项"],
      "strategy_note": "一句填报策略建议"
    }
  ]
}"""


_FINAL_SELECT_SYSTEM = """你是一个高考志愿方案总筛选专家。你会收到各批次入围候选，请生成最终展示给考生的冲稳保推荐榜单。

硬性要求：
1. tier 是规则层录取风险标签，必须原样保留，不能重新分层。
2. 目标比例：冲 10%-20%，稳 50%-70%，保 15%-30%。候选不足时优先保证稳和保。
3. 最多输出 45 个；如果高质量候选不足，可以少于 45 个。
4. 同一学校同一专业只保留一次；同一学校过多时要控制重复，避免榜单单调。
5. 每个候选必须有 fit_score、reason、warnings、strategy_note。
6. 输出必须非常精简，只输出 candidate_id、fit_score、reason、warnings、strategy_note。
7. 输出必须是合法 JSON，不要 Markdown，不要代码块，不要补充解释。

输出结构：
{
  "candidates": [
    {
      "candidate_id": 0,
      "fit_score": 90,
      "reason": "不超过60字的最终推荐理由",
      "warnings": ["最多2条简短风险或注意事项"],
      "strategy_note": "一句填报策略建议"
    }
  ]
}"""


def _preference_tree_summary(profile: dict) -> str:
    items = profile.get("preference_tree", [])
    if not items:
        return "未填写"

    explicit = []
    fit_signals = []
    for item in items[:30]:
        label_path = " / ".join(item.get("path") or [item.get("category", ""), item.get("label", "")]).strip(" /")
        if not label_path:
            continue
        if item.get("signal_type") == "fit_signal":
            fit_signals.append(label_path)
        else:
            explicit.append(label_path)

    parts = []
    if explicit:
        parts.append("显式偏好：" + "、".join(explicit[:18]))
    if fit_signals:
        parts.append("适合信号：" + "、".join(fit_signals[:12]))
    return "；".join(parts) or "未填写"


def _profile_prompt(profile: dict) -> str:
    basic = profile["basic_info"]
    academic = profile.get("academic_preferences", {})
    personality = profile.get("personality", {})
    constraints = profile.get("constraints", {})
    priority_factors = profile.get("priority_factors", [])
    preference_summary = _preference_tree_summary(profile)
    pf_str = "、".join(
        f"{p['factor']}(第{p['rank']}位)"
        for p in sorted(priority_factors, key=lambda x: x.get("rank", 99))
    ) or "未填写"

    return f"""## 考生画像
- 省份/科类：{basic['province']} / {basic['subject_type']}
- 高考分数/位次：{basic['gaokao_score']}分 / 第{basic['province_rank']}名
- 喜欢专业方向：{', '.join(academic.get('liked_subjects', [])) or '未填写'}
- 不喜欢专业方向：{', '.join(academic.get('disliked_subjects', [])) or '无'}
- 数学/英语/物理水平：{academic.get('math_level', '中')} / {academic.get('english_level', '中')} / {academic.get('physics_level', '中')}
- 性格特点：{', '.join(personality.get('traits', [])) or '未填写'}
- 职业方向：{personality.get('career_vision') or '未填写'}
- 预算：{constraints.get('annual_budget_min', 0)}~{constraints.get('annual_budget_max', 150000)}元/年
- 偏好城市：{', '.join(constraints.get('preferred_cities', [])) or '不限'}
- 排除城市：{', '.join(constraints.get('excluded_cities', [])) or '无'}
- 接受中外合作：{'是' if constraints.get('accept_sino_foreign') else '否'}
- 接受未来读研：{'是' if constraints.get('accept_postgrad', True) else '否'}
- 核心看重因素：{pf_str}
- 层级气泡选择：{preference_summary}
- 个人补充：{profile.get('extra_notes') or '无'}"""


def _compact_candidate(candidate: dict) -> dict:
    history = []
    for row in candidate.get("admission_history", []):
        history.append([
            row.get("year"),
            row.get("min_rank"),
            row.get("min_score"),
            row.get("enrollment_count"),
        ])
    return {
        "candidate_id": candidate["candidate_id"],
        "school": candidate["university_name"],
        "city": candidate["city"],
        "school_type": candidate["university_type"],
        "major": candidate["major_name"],
        "enrollment_type": candidate["enrollment_type"],
        "tier": candidate["tier"],
        "risk_ratio": candidate.get("risk_ratio"),
        "risk_score": candidate.get("risk_score"),
        "tier_reason": candidate.get("tier_reason"),
        "reference_rank": candidate.get("stable_reference_rank"),
        "rank_trend": candidate.get("rank_trend"),
        "rank_volatility": candidate.get("rank_volatility"),
        "history": history,
    }


def _build_batch_prompt(profile: dict, batch: list[dict], batch_num: int, num_batches: int) -> str:
    compact = [_compact_candidate(candidate) for candidate in batch]
    return f"""{_profile_prompt(profile)}

## 本批候选
- 批次：{batch_num}/{num_batches}
- 本批候选数：{len(compact)}
- 字段说明：history=[年份,最低位次,最低分,招生人数]；risk_ratio=规则参考位次/考生位次。

{json.dumps(compact, ensure_ascii=False, separators=(',', ':'))}

请筛选本批最值得保留的候选，严格按 JSON 结构输出。"""


def _build_final_prompt(profile: dict, shortlist: list[dict]) -> str:
    compact = [_compact_candidate(candidate) | {
        "fit_score": candidate.get("fit_score"),
        "reason": candidate.get("reason"),
            "warnings": candidate.get("warnings", [])[:2],
            "strategy_note": candidate.get("strategy_note", ""),
    } for candidate in shortlist]
    counts = {tier: sum(1 for c in compact if c.get("tier") == tier) for tier in ["冲", "稳", "保"]}
    return f"""{_profile_prompt(profile)}

## 入围候选汇总
- 入围候选数：{len(compact)}
- 当前分布：冲 {counts['冲']} / 稳 {counts['稳']} / 保 {counts['保']}
- 最终最多输出：{_MAX_FINAL_CANDIDATES}
- 目标比例：冲10%-20%，稳50%-70%，保15%-30%。候选不足时优先保证稳和保。

{json.dumps(compact, ensure_ascii=False, separators=(',', ':'))}

请生成最终展示给考生的推荐榜单，严格按 JSON 结构输出。"""


# ──────────────────────────────────────────────────────────────────────
# JSON 调用与兜底
# ──────────────────────────────────────────────────────────────────────
def _safe_parse_json(text: str) -> dict:
    """尝试解析 JSON，若带有 markdown 包裹或前后废话则清洗。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", text)
        open_braces = repaired.count("{") - repaired.count("}")
        open_brackets = repaired.count("[") - repaired.count("]")
        if open_brackets > 0:
            repaired += "]" * open_brackets
        if open_braces > 0:
            repaired += "}" * open_braces
        return json.loads(repaired)


def _chat_json(system_prompt: str, user_prompt: str, max_tokens: int) -> dict:
    t0 = time.time()
    stream = _get_client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.25,
        max_tokens=max_tokens,
        stream=True,
        extra_body={"enable_search": False},
    )

    chunks = []
    for chunk in stream:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta and delta.content:
            chunks.append(delta.content)
    raw = "".join(chunks)
    print(f"[LLM] JSON 响应完成，耗时 {time.time() - t0:.1f}s，字符数={len(raw)}", flush=True)
    return _safe_parse_json(raw)


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
        merged = dict(original)
        merged["fit_score"] = max(0, min(100, int(row.get("fit_score") or original.get("fit_score") or 70)))
        merged["reason"] = row.get("reason") or original.get("reason") or original.get("tier_reason", "")
        merged["warnings"] = row.get("warnings") or original.get("warnings") or []
        merged["strategy_note"] = row.get("strategy_note") or original.get("strategy_note") or ""
        merged["enriched_data"] = row.get("enriched_data") or original.get("enriched_data") or {}
        selected.append(merged)
        seen.add(candidate_id)
    return selected


def _fallback_candidates(candidates: list[dict], limit: int = _MAX_FINAL_CANDIDATES) -> list[dict]:
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
        item["fit_score"] = item.get("fit_score") or item.get("risk_score") or 70
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


async def enrich_and_classify(profile: dict, candidates: list[dict]):
    """
    异步生成器，yield SSE 事件字典。
    事件类型：progress | result
    """
    total = len(candidates)
    if not total:
        yield {"event": "result", "data": {"candidates": [], "fallback": False}}
        return

    yield {"event": "progress", "data": {
        "stage": "llm_batch_filtering",
        "message": f"大模型开始分批筛选 {total} 个硬筛候选..."
    }}

    candidates_by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
    batches = [candidates[i:i + _BATCH_SIZE] for i in range(0, total, _BATCH_SIZE)]
    shortlist = []
    llm_errors = []

    for batch_idx, batch in enumerate(batches):
        batch_num = batch_idx + 1
        yield {"event": "progress", "data": {
            "stage": "llm_batch_filtering",
            "message": f"AI 正在筛选第 {batch_num}/{len(batches)} 批候选（{len(batch)} 个，每批最多入围 {_MAX_BATCH_SELECTED} 个）..."
        }}
        try:
            parsed = await asyncio.to_thread(
                _chat_json,
                _BATCH_FILTER_SYSTEM,
                _build_batch_prompt(profile, batch, batch_num, len(batches)),
                2048,
            )
            rows = parsed.get("selected_candidates") or parsed.get("candidates") or []
            selected = _merge_llm_selection(candidates_by_id, rows)
            shortlist.extend(selected[:_MAX_BATCH_SELECTED])
            print(f"[LLM] 第 {batch_num}/{len(batches)} 批入围 {len(selected)} 个", flush=True)
        except Exception as exc:
            msg = str(exc)[:160]
            llm_errors.append(msg)
            print(f"[LLM] 第 {batch_num}/{len(batches)} 批筛选失败: {msg}", flush=True)
            yield {"event": "progress", "data": {
                "stage": "llm_retry",
                "message": f"第 {batch_num} 批 AI 筛选失败，稍后使用规则兜底补齐..."
            }}

    if not shortlist:
        fallback = _fallback_candidates(candidates)
        yield {"event": "result", "data": {
            "candidates": fallback,
            "fallback": True,
            "message": f"AI筛选失败，已返回规则备选结果：{'; '.join(llm_errors[:2])}"
        }}
        return

    # 去重，避免同一候选在异常合并时重复出现。
    unique_shortlist = []
    seen = set()
    for item in shortlist:
        if item["candidate_id"] in seen:
            continue
        unique_shortlist.append(item)
        seen.add(item["candidate_id"])

    yield {"event": "progress", "data": {
        "stage": "llm_final_selecting",
        "message": f"正在汇总 {len(unique_shortlist)} 个 AI 入围候选，按冲稳保比例生成最终榜单..."
    }}

    final_candidates = _finalize_shortlist(unique_shortlist)
    yield {"event": "result", "data": {
        "candidates": final_candidates,
        "fallback": False,
        "message": f"AI 分批筛选完成，已从 {len(unique_shortlist)} 个入围候选中生成最终榜单"
    }}


# ──────────────────────────────────────────────────────────────────────
# Prompt：详情页动态 UI 生成
# ──────────────────────────────────────────────────────────────────────
_DETAIL_SYSTEM = """你是一个专业的高考志愿填报顾问助手。
你的任务是根据考生画像和指定院校数据，生成一段结构化的 JSON 分析报告。

## 严格输出要求
你必须且只能输出一个合法的 JSON 对象，不得包含任何 Markdown 代码块标记（如 ```json）、注释或 JSON 之外的任何内容。

## 输出 JSON 结构
必须包含且仅包含以下两个顶级字段：

1. "html"：字符串类型。一段基于 Tailwind CSS 编写的 HTML 片段。须包含：
   - 学校+专业简介（结合考生位次给出录取可能性判断，语气专业亲切）
   - "优势匹配点"区块（用绿色/blue标签展示，结合画像中的喜好、城市、预算等，至少3条）
   - "潜在风险/注意事项"区块（用橙色/红色标签展示，实事求是，至少2条）
   - 一个空的占位 div（必须存在）：<div id="dynamic-chart-container" style="width:100%;height:350px;"></div>

2. "chartConfig"：对象类型。一个合法的 Apache ECharts option 配置对象（可直接传入 echarts.setOption()）。
   须包含该校近三年最低录取位次趋势折线图，并标注考生自身位次。
   要求：
   - Y 轴必须设置 inverse: true（位次越小越靠上）
   - 使用 markLine 标注考生位次
   - 颜色使用 #6366f1（考生线）和 #f59e0b（录取线）
   - 图表背景透明（backgroundColor: 'transparent'）

## 禁止行为
- 禁止输出 ```json 代码块标记
- 禁止在 JSON 之外输出任何文字
- 禁止 HTML 中使用 <script> 或 <style> 标签
- 禁止 chartConfig 中出现 JavaScript 函数（formatter 使用字符串模板）"""


def _build_detail_user_prompt(profile: dict, university_data: dict) -> str:
    basic = profile["basic_info"]
    pf = profile["priority_factors"]
    pf_str = "、".join(f"{p['factor']}" for p in sorted(pf, key=lambda x: x["rank"]))
    preference_summary = _preference_tree_summary(profile)

    return f"""## 考生画像
- 省份：{basic['province']}，科类：{basic['subject_type']}
- 高考分数：{basic['gaokao_score']}分，全省位次：{basic['province_rank']}
- 喜欢的专业方向：{', '.join(profile['academic_preferences']['liked_subjects'])}
- 核心看重因素（优先级）：{pf_str}
- 预算：{profile['constraints']['annual_budget_min']}~{profile['constraints']['annual_budget_max']}元/年
- 偏好城市：{', '.join(profile['constraints']['preferred_cities']) or '不限'}
- 性格特点：{', '.join(profile['personality']['traits'])}
- 职业方向：{profile['personality']['career_vision']}
- 层级气泡选择：{preference_summary}
- 个人补充：{profile.get('extra_notes', '无')}

## 目标院校数据
- 院校：{university_data['university_name']}（{university_data['city']}，{university_data['university_type']}）
- 专业：{university_data['major_name']}，招生类型：{university_data['enrollment_type']}
- 冲稳保判定：{university_data.get('tier', '稳')}
- 录取风险说明：{university_data.get('tier_reason', '')}
- 个性化匹配分：{university_data.get('fit_score', '')}
- 推荐理由：{university_data.get('reason', '')}
- 注意事项：{json.dumps(university_data.get('warnings', []), ensure_ascii=False)}
- 近三年录取数据：
{json.dumps(university_data['admission_history'], ensure_ascii=False, indent=2)}
- 软性数据：
{json.dumps(university_data.get('enriched_data', {}), ensure_ascii=False, indent=2)}

请根据以上信息，严格按系统要求输出 JSON。"""


async def generate_detail_ui(profile: dict, university_data: dict) -> dict:
    """
    返回 {"html": "...", "chartConfig": {...}}
    失败时抛出异常
    """
    from openai import AsyncOpenAI

    api_key = os.environ.get("QWEN_API_KEY")
    base_url = os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    model_name = os.environ.get("QWEN_MODEL", "qwen-plus")

    client = AsyncOpenAI(
        api_key=api_key or "dummy",
        base_url=base_url,
    )

    max_retries = 2
    for attempt in range(max_retries):
        try:
            response_stream = await client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": _DETAIL_SYSTEM},
                    {"role": "user", "content": _build_detail_user_prompt(profile, university_data)},
                ],
                temperature=0.3,
                max_tokens=4096,
                stream=True,
                extra_body={"enable_search": False},
            )

            raw_content = ""
            async for chunk in response_stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        raw_content += delta

            result = _safe_parse_json(raw_content)
            if "html" in result and "chartConfig" in result:
                return result
        except Exception as e:
            if attempt == max_retries - 1:
                raise ValueError(f"大模型报错或返回无效 JSON ({type(e).__name__}): {str(e)[:200]}")

    raise ValueError("大模型多次返回无效数据")
