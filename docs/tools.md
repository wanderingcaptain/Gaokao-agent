# Agent 工具清单与验证

工具数量仍为 v1 8 个、v2 16 个。后续增加了持久执行、审核产物、年度资格规则和跨会话记忆；
当前架构与新增参数请同时参阅 [六项架构完成记录](architecture-v3.md)。

本次完善范围为应用内模型可调用的工具：v1 保留 8 个工具，v2 从 9 个补齐为 16 个。
本页记录工具完善阶段；后续架构阶段已经接入聊天前端。系统不自动提交官方志愿。
工具权限、输入结构、候选事实、来源版本和方案校验均由服务端执行。

## v2：16 个工具

角色简称：主对话 C、专业研究 M、招生研究 A、方案规划 P。

| 工具 | 角色 | 行为与边界 |
| --- | --- | --- |
| `get_profile` | C/M/A/P | 返回当前有效画像、已确认记忆、版本、授权和缺失字段；已确认预算/排除城市合并到有效约束 |
| `list_artifacts` | C/M/A/P | 分页列出当前画像与候选数据仍有效的产物，可按角色过滤 |
| `read_artifact` | C/M/A/P | 读取报告或草案，同时交接报告关联的数据库候选事实与来源目录 |
| `read_sources` | C/M/A/P | 按编号读取已有网页摘要或数据库历史快照；拒绝不存在或过期来源 |
| `get_candidate_details` | C/A/P | 读取本会话已查询候选、完整历史、约束差异及待核实项 |
| `analyze_historical_risk` | C/A/P | 最近三个记录年份的有效位次中位数、范围、波动、趋势及与考生位次的差额 |
| `compare_candidates` | C/A/P | 对比 2–10 个已查询候选，保持输入顺序；不自动确定推荐顺序 |
| `search_sources` | M/A | Tavily 公开检索；招生调查需授权。没有密钥、限流和网络错误有明确结果 |
| `query_candidates` | A | 查询广东本地历史；支持专业、学校、城市、科类、招生类型、年份及分页 |
| `submit_report` | M/A/P | 保存研究结论和已读来源；无来源必须说明缺失；数据库来源自动绑定候选 |
| `validate_plan` | P | 无副作用预检候选、来源、版本、重复项、约束差异与修订引用 |
| `submit_plan` | P | 独立重做校验，保存有序草案、排除项和新版本；失败允许模型修正 |
| `delegate_task` | C | 校验目标角色与输入产物后委派；A/P 需要当前轮次的明确授权 |
| `propose_memory` | C | 必须引用本轮用户原话；软偏好自动记录，硬约束创建确认提案；相同记忆不重复升版 |
| `request_confirmation` | C | 创建绑定画像版本的确认项并暂停；只有认证 API 输入可以确认 |
| `respond` | C | 结束本轮并返回用户可读回答 |

输入拒绝额外字段、类型不符、空白文本、重复编号和越界分页。角色权限在执行层再次检查。
一批工具调用一旦结束或交接，后续调用只返回拒绝结果，不继续修改业务状态。

## 查询、来源与历史风险口径

`query_candidates` 返回 `total/offset/returned/next_offset/remaining/covered_years`。
学校、专业采用字面子串匹配，城市、科类、招生类型精确匹配；年份筛选决定哪些候选匹配，
每个候选仍携带完整历史，避免同一候选被局部年份结果覆盖。缺少院校基础资料的孤立记录不进入候选分页。

候选按院校、专业、招生类型、科类聚合，不把同专业不同年份重复入表。
数据库内容、院校属性或网页摘要变化时生成不同的来源哈希；旧快照保留。
同一来源编号不能覆盖为不同内容。画像变化后必须重新查询来源；候选数据变化后旧报告不得用于新方案。
旧会话缺少 `source_versions` 时可以读取会话历史，但需重新调查才能将旧事实用于新产物。

历史风险使用最近三个**记录年份**内大于零的位次；缺失位次不由更旧年份悄悄补齐。
可传 `target_year`，排除该年及之后的数据；不传时使用画像目标年，否则仅做现有历史描述。
位次差额 = 历史中位位次 − 考生位次，正数表示考生位次数值更小。
只有考生位次、省份、科类完整且匹配才计算差额；否则返回 `insufficient_data`。
少于三年、年份不连续、缺少目标前一年记录会返回说明。该指标不是录取概率，不证明报考资格。

搜索仅调用固定 Tavily 地址，不抓取模型指定 URL。发送前拦截常见手机号、证件号、邮箱和显式身份字段；
这不是完整的个人信息识别器。只接收正常 HTTPS 链接，并按官方域名白名单分类；所有搜索摘要仍为 `unverified`。

## 规划和修订

建议调用链：`query_candidates → submit_report → read_artifact → read_sources（网页）→ compare_candidates / analyze_historical_risk → validate_plan → submit_plan`。
规划前至少读取一份有效招生报告；每行引用当前候选自己的数据库来源，引用网页前需读取其摘要。
只有学校、专业等事实来自服务器，模型只填写排序、理由、取舍和来源编号。

已确认排除城市、招生省份/科类不符、拒绝中外合作（含院校属性）进入 `excluded`，不进入有序 `rows`。
预算、具体选科、目标年度计划等未知项仍保留。所有行均被排除时返回空排序和明确缺失说明。
每份草案都保留 `test_only=true`、`ready_for_submission=false`。

已有有效方案的调整必须先读取最新版，并通过 `base_plan_id` 明确引用。
新版本记录 `plan_version/supersedes_plan_id`，保留旧方案；数据或画像变化使旧方案失效时，允许基于新研究重建。

## v1：8 个兼容工具

`get_profile`、`run_rule_screening`、`get_candidate_page`、`get_candidate_details`、`ask_user`、
`submit_recommendation`、`validate_recommendation`、`answer_comparison`。
保留原接口和冲稳保算法，历史年份改为当前省份/科类实际最近三年，权重按新到旧 0.5/0.3/0.2。
补上院校本身的中外合作标记过滤、事实篡改检查和失效比较引用检查；规则降级不再修改原候选池，也保留有效的零匹配分。

## 验证方法

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend .venv/bin/python -B -m unittest discover -s backend/tests -v
node --check frontend/agent.js
```

`test_research_tools.py` 对照实际 Schema 集合逐一执行全部 16 个 v2 工具成功路径，
并覆盖全部角色权限与非法参数、来源不可覆盖、过期证据、分页、实际年份、目标年截断、搜索 HTTP 错误、
硬约束隔离、失败修正、方案版本保留和记忆确认。
`test_multi_agent.py` 经真实 LangGraph 和认证路由执行确认→SQL 查询→研究交接→校验失败→修正提交→持久化与幂等回放。
模型为脚本化替身，搜索 HTTP 为 MockTransport，数据库为隔离 SQLite；未调用真实模型或付费搜索。
这些结果验证工具逻辑与接口，不代表真实模型的研究质量、搜索质量或完整浏览器体验已通过。
