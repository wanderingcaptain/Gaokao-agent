"""
LLM 服务：封装 Qwen API 调用
包含：
  1. enrich_and_classify() — 补全软性数据 + 冲稳保分级（SSE 分阶段 yield 进度）
  2. generate_detail_ui()  — 生成详情页 HTML + ECharts 配置（普通调用）
"""
import json
import os
import re
import time
from openai import OpenAI
from dotenv import load_dotenv

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
    timeout 设置为 120 秒，避免 nginx 504 超时。
    """
    global _client
    if _client is None:
        api_key  = os.getenv("QWEN_API_KEY")
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


# ──────────────────────────────────────────────────────────────────────
# Prompt：阶段3 — 补全软性数据 + 冲稳保分级
# ──────────────────────────────────────────────────────────────────────
_ENRICH_SYSTEM = """你是一个专业的高考志愿填报数据分析师。
你的任务有两步：
【步骤1】针对候选院校专业列表，补全以下软性数据：
  - annual_tuition（学费/年，整数，元）
  - dormitory_info（宿舍条件，简短描述，如"4人间，空调独卫"）
  - postgrad_rate（保研率/升学率，如"约30%"）
  - campus_location（校区地址，如"北京市海淀区学院路"）
  - key_features（该专业/学院的2~3个亮点，字符串数组）

【步骤2】结合考生画像，将每个候选项判定为"冲"、"稳"、"保"三个梯度，并给出一句推荐理由（reason字段，不超过60字）。

判定规则参考：
- 冲：考生位次 ≥ 该校近三年最低录取位次均值（位次数值上，考生位次大于录取位次，说明有风险）
- 稳：考生位次在该校近三年最低录取位次均值 ±15% 范围内
- 保：考生位次 < 该校近三年最低录取位次均值（考生位次数值更小，说明把握较大）

## 严格输出要求
只输出一个合法 JSON 对象，结构为：
{
  "candidates": [ /* 与输入顺序一致，每项补全 enriched_data、tier、reason 字段 */ ]
}
不得输出任何 JSON 之外的内容，不得使用 ```json 代码块。"""


def _build_enrich_user_prompt(profile: dict, candidates: list) -> str:
    # 精简候选列表（仅传必要字段，节省 Token）
    slim = []
    for c in candidates:
        slim.append({
            "university_id":   c["university_id"],
            "university_name": c["university_name"],
            "city":            c["city"],
            "university_type": c["university_type"],
            "major_name":      c["major_name"],
            "enrollment_type": c["enrollment_type"],
            "admission_history": c["admission_history"],
        })

    basic = profile["basic_info"]
    pf    = profile["priority_factors"]
    pf_str = "、".join(f"{p['factor']}(第{p['rank']}位)" for p in sorted(pf, key=lambda x: x["rank"]))

    return f"""## 考生画像
- 省份：{basic['province']}，科类：{basic['subject_type']}
- 高考分数：{basic['gaokao_score']}分，全省位次：{basic['province_rank']}
- 预算：{profile['constraints']['annual_budget_min']}~{profile['constraints']['annual_budget_max']}元/年
- 偏好城市：{', '.join(profile['constraints']['preferred_cities']) or '不限'}
- 核心看重因素：{pf_str}
- 个人补充：{profile.get('extra_notes', '无')}

## 候选院校专业列表（共{len(slim)}项）
{json.dumps(slim, ensure_ascii=False, indent=2)}

请按要求输出 JSON。"""


# ──────────────────────────────────────────────────────────────────────
# Prompt：阶段4 — 详情页动态 UI 生成
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
    pf    = profile["priority_factors"]
    pf_str = "、".join(f"{p['factor']}" for p in sorted(pf, key=lambda x: x["rank"]))

    return f"""## 考生画像
- 省份：{basic['province']}，科类：{basic['subject_type']}
- 高考分数：{basic['gaokao_score']}分，全省位次：{basic['province_rank']}
- 喜欢的学科：{', '.join(profile['academic_preferences']['liked_subjects'])}
- 核心看重因素（优先级）：{pf_str}
- 预算：{profile['constraints']['annual_budget_min']}~{profile['constraints']['annual_budget_max']}元/年
- 偏好城市：{', '.join(profile['constraints']['preferred_cities']) or '不限'}
- 性格特点：{', '.join(profile['personality']['traits'])}
- 职业方向：{profile['personality']['career_vision']}
- 个人补充：{profile.get('extra_notes', '无')}

## 目标院校数据
- 院校：{university_data['university_name']}（{university_data['city']}，{university_data['university_type']}）
- 专业：{university_data['major_name']}，招生类型：{university_data['enrollment_type']}
- 冲稳保判定：{university_data.get('tier', '稳')}
- 推荐理由：{university_data.get('reason', '')}
- 近三年录取数据：
{json.dumps(university_data['admission_history'], ensure_ascii=False, indent=2)}
- 软性数据：
{json.dumps(university_data.get('enriched_data', {}), ensure_ascii=False, indent=2)}

请根据以上信息，严格按系统要求输出 JSON。"""


# ──────────────────────────────────────────────────────────────────────
# 公共 JSON 解析工具（鲁棒性处理）
# ──────────────────────────────────────────────────────────────────────
def _safe_parse_json(text: str) -> dict:
    """尝试解析 JSON，若带有 markdown 包裹则先清洗"""
    text = text.strip()
    # 移除可能的 ```json ... ``` 包裹
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


# ──────────────────────────────────────────────────────────────────────
# 阶段3：补全 + 分级（异步生成器，供 SSE 使用）
# ──────────────────────────────────────────────────────────────────────
# 每批最多处理的候选项数量，避免单次请求过大导致超时
_BATCH_SIZE = 10


async def enrich_and_classify(profile: dict, candidates: list):
    """
    异步生成器，yield SSE 事件字典。
    事件类型：progress（中间状态） | result（最终结果）| error

    为避免 nginx 504 超时，候选项按批次（每批 _BATCH_SIZE 个）分批调用 LLM。
    每批结果合并回 candidates 列表后，最终统一 yield result 事件。
    """
    total = len(candidates)
    yield {"event": "progress", "data": {
        "stage": "llm_enriching",
        "message": f"大模型正在分析 {total} 个候选项，分批补全学费、宿舍等软性数据..."
    }}

    system_prompt = _ENRICH_SYSTEM

    # 将候选列表切成若干批次
    batches = [candidates[i:i + _BATCH_SIZE] for i in range(0, total, _BATCH_SIZE)]
    num_batches = len(batches)
    offset = 0

    for batch_idx, batch in enumerate(batches):
        batch_num = batch_idx + 1
        print(f"\n[LLM] ── 第 {batch_num}/{num_batches} 批开始 ──────────────────", flush=True)
        print(f"[LLM]   候选项数量: {len(batch)}", flush=True)
        for item in batch:
            print(f"[LLM]     · {item['university_name']} / {item['major_name']}", flush=True)

        yield {"event": "progress", "data": {
            "stage": "llm_enriching",
            "message": f"正在处理第 {batch_num}/{num_batches} 批（共 {len(batch)} 项）..."
        }}

        user_prompt = _build_enrich_user_prompt(profile, batch)
        max_retries = 2
        last_error = None
        success = False

        for attempt in range(max_retries):
            t0 = time.time()
            print(f"[LLM]   → 流式请求发出 (尝试 {attempt+1}/{max_retries})...", flush=True)
            try:
                # ── 使用 stream=True，让 token 边生成边传输 ──────────────
                # 这样 nginx 代理能持续收到数据，不会触发60s超时
                stream = _get_client().chat.completions.create(
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    temperature=0.3,
                    max_tokens=4096,
                    stream=True,
                    extra_body={"enable_search": False},
                )

                # 逐 chunk 收集完整响应文本
                raw_chunks = []
                token_count = 0
                for chunk in stream:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta and delta.content:
                        raw_chunks.append(delta.content)
                        token_count += 1
                        if token_count % 50 == 0:
                            elapsed_so_far = time.time() - t0
                            print(f"[LLM]      ... 已收到 {token_count} tokens ({elapsed_so_far:.1f}s)", flush=True)

                raw = "".join(raw_chunks)
                elapsed = time.time() - t0
                print(f"[LLM]   ✅ 流式响应完成，耗时 {elapsed:.1f}s，共 {token_count} tokens", flush=True)

                parsed = _safe_parse_json(raw)
                enriched_candidates = parsed.get("candidates", [])
                print(f"[LLM]      解析到 {len(enriched_candidates)} 个结果", flush=True)

                # 将 enriched_data / tier / reason 合并回原列表（绝对索引）
                for i, ec in enumerate(enriched_candidates):
                    abs_idx = offset + i
                    if abs_idx < total:
                        candidates[abs_idx]["enriched_data"] = ec.get("enriched_data", {})
                        candidates[abs_idx]["tier"]           = ec.get("tier", "稳")
                        candidates[abs_idx]["reason"]         = ec.get("reason", "")
                        print(f"[LLM]      [{ec.get('tier','?')}] {candidates[abs_idx]['university_name']} "
                              f"— {ec.get('reason','')[:40]}", flush=True)

                success = True
                break

            except Exception as e:
                elapsed = time.time() - t0
                last_error = str(e)
                print(f"[LLM]   ❌ 请求失败（{elapsed:.1f}s）: {last_error[:200]}", flush=True)
                if attempt < max_retries - 1:
                    print(f"[LLM]      重试中...", flush=True)
                    yield {"event": "progress", "data": {
                        "stage": "llm_retry",
                        "message": f"第 {batch_num} 批第 {attempt+1} 次失败，重试中..."
                    }}

        if not success:
            print(f"[LLM]   ⚠️  第 {batch_num} 批全部失败，跳过，使用默认值", flush=True)
            for i in range(len(batch)):
                abs_idx = offset + i
                if abs_idx < total:
                    candidates[abs_idx].setdefault("enriched_data", {})
                    candidates[abs_idx].setdefault("tier", "稳")
                    candidates[abs_idx].setdefault("reason", f"（软性数据补全失败：{last_error[:80]}）")
            yield {"event": "progress", "data": {
                "stage": "llm_retry",
                "message": f"第 {batch_num} 批补全失败，已跳过，继续处理..."
            }}

        offset += len(batch)
        print(f"[LLM] ── 第 {batch_num} 批完成 ────────────────────────", flush=True)

    yield {"event": "progress", "data": {"stage": "llm_classifying", "message": "冲稳保分级完成，正在整理结果..."}}
    yield {"event": "result",   "data": {"candidates": candidates}}


# ──────────────────────────────────────────────────────────────────────
# 阶段4：详情页动态 UI 生成（异步调用）
# ──────────────────────────────────────────────────────────────────────
async def generate_detail_ui(profile: dict, university_data: dict) -> dict:
    """
    返回 {"html": "...", "chartConfig": {...}}
    失败时抛出异常
    """
    system_prompt = _DETAIL_SYSTEM
    user_prompt   = _build_detail_user_prompt(profile, university_data)

    from openai import AsyncOpenAI
    import os
    api_key  = os.environ.get("QWEN_API_KEY")
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
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                # response_format={"type": "json_object"},  # Some streaming endpoints don't support json_object
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
                        
            # 清理可能的 markdown 块
            raw_content = raw_content.strip()
            if raw_content.startswith("```json"):
                raw_content = raw_content[7:]
            if raw_content.startswith("```"):
                raw_content = raw_content[3:]
            if raw_content.endswith("```"):
                raw_content = raw_content[:-3]
                
            result = _safe_parse_json(raw_content)
            if "html" in result and "chartConfig" in result:
                return result
        except Exception as e:
            if attempt == max_retries - 1:
                raise ValueError(f"大模型报错或返回无效 JSON ({type(e).__name__}): {str(e)[:200]}")

    raise ValueError("大模型多次返回无效数据")
