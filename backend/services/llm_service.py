"""详情报告生成；方案处理的旧导入名称保留用于兼容。"""
from __future__ import annotations

import json
import re

from dotenv import load_dotenv


load_dotenv()

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


from tools.recommendation import (
    _MAX_FINAL_CANDIDATES, _merge_llm_selection, _fallback_candidates, _finalize_shortlist,
)


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
   - 学校+专业简介，结合实际历史位次解释可比性，不预测录取概率
   - "优势匹配点"区块，结合考生兴趣、城市偏好等有据可查的匹配点
   - "待核实事项"区块，标明目标年度招生计划、选科、学费等缺少证据的事项
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
- 禁止 chartConfig 中出现 JavaScript 函数（formatter 使用字符串模板）
- 仅基于传入的考生画像和数据库历史记录分析。没有资料支持的事实应明确待核实，不自行补写"""


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
- 最近三个已有记录年份的录取数据：
{json.dumps(university_data['admission_history'], ensure_ascii=False, indent=2)}

请根据以上信息，严格按系统要求输出 JSON。"""


async def generate_detail_ui(profile: dict, university_data: dict) -> dict:
    """
    返回 {"html": "...", "chartConfig": {...}}
    失败时抛出异常
    """
    from providers.client import ModelSettings, create_client, request_options

    settings = ModelSettings.from_env()
    client = create_client(settings)
    try:
        for attempt in range(2):
            response_stream = None
            try:
                response_stream = await client.chat.completions.create(
                    messages=[
                        {"role": "system", "content": _DETAIL_SYSTEM},
                        {"role": "user", "content": _build_detail_user_prompt(profile, university_data)},
                    ],
                    stream=True,
                    **request_options(settings, temperature=0.3, max_tokens=4096),
                )
                raw_content = ""
                async for chunk in response_stream:
                    if chunk.choices:
                        raw_content += chunk.choices[0].delta.content or ""
                result = _safe_parse_json(raw_content)
                if "html" in result and "chartConfig" in result:
                    return result
            except Exception as exc:
                if attempt == 1:
                    # 不把供应商响应或凭据带入用户可见错误。
                    raise ValueError(f"详情生成失败 ({type(exc).__name__})") from exc
            finally:
                if response_stream is not None:
                    await response_stream.close()
        raise ValueError("大模型多次返回无效数据")
    finally:
        await client.close()
