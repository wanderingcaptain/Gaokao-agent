# 四 Agent 后端原型

本页记录早期原型。后续六项架构已接入，当前运行、异步 API、恢复机制、前端和资格规则请以
[六项架构完成记录](architecture-v3.md) 为准；下文关于长请求、无前端和重启失败的描述属于旧阶段。

## 入口与角色

这是新增的 v2 后端试验入口，不替换现有 v1 表单和推荐页面。

- `backend/agents/prompts/coordinator.md`：主对话、兴趣探索与委派。
- `backend/agents/prompts/major_research.md`：专业课程、能力及发展研究。
- `backend/agents/prompts/admissions.md`：广东院校与招生核实。
- `backend/agents/prompts/planner.md`：综合排序的测试志愿表。
- `backend/agents/runtime.py`：四角色工具循环、LangGraph 动态路由、授权和产物交接。
- `backend/agents/schemas.py`：工具参数及服务端角色权限。
- `backend/agents/research.py`：真实数据库查询与可选 Tavily 搜索。
- `backend/tools/research_session.py`：16 个会话工具、来源版本、交接和方案预检/提交。
- `backend/tools/planning.py`：有效画像、硬约束差异与历史位次分析。
- `backend/routers/multi_agent.py`：认证、会话持久化、用户确认和幂等 API。

每个角色有独立的模型上下文和工具集合。主 Agent 可以直接回答，也可以委派一个专家，
专家最多 8 次模型调用，提交产物后回到主 Agent。不会强制四角色按固定顺序运行。
当前委派串行，尚未实现专家并行和独立队列。

## 本地运行

按 README 安装锁定依赖、配置模型凭据并启动后端。新增依赖 LangGraph，未引入 LangChain 高层 Agent。
搜索连接器默认使用 `TAVILY_API_KEY`，不配置就返回 `search_not_configured`。
只实现受控搜索 API，不开放应用服务器任意 URL 抓取，避免把网页地址变成内网访问工具。
搜索会向第三方发送查询关键词，执行前拦截常见电话号码、证件号、邮箱和显式身份字段；这不是完整个人信息识别器。
不自动创建账户、充值或发起真实付费测试。

官方域名由 `RESEARCH_OFFICIAL_DOMAINS` 配置，默认只有广东考试院。
学校官方站点需维护者确认后加入。域名可信不等于内容适用于该候选，搜索摘要也不是全文核实。
网页来源包含内容哈希，补查后的不同内容不会静默覆盖旧证据。

## API 操作

以下端点均要求既有 Bearer 登录令牌。可在本地 `/docs` 中使用。

1. `POST /api/v2/agent/sessions`，请求 `{}`，或传入自己拥有的 `profile_uuid`。
   返回会话 `id` 与开场问题。画像可选，不要求先完成旧表单。
2. `POST /api/v2/agent/sessions/{id}/messages`：

```json
{"request_id":"唯一请求编号","text":"我想了解计算机专业","confirmed_ids":[]}
```

3. 如返回 `status=waiting` 和 `confirmation_id`，由用户明确确认后，在下一条消息的
   `confirmed_ids` 中传入该编号。硬约束提案的编号可从会话 pending 中读取。
   模型不能生成授权。当前授权只对本轮调查和表格变更有效。
4. `GET /api/v2/agent/sessions/{id}` 读取记忆、待确认项、产物、来源、表格版本与工具摘要。
   专家交回 artifact_id，主 Agent 用 read_artifact 按需读取完整产物；不重新运行专家来取结果。

相同请求编号与内容返回原结果；相同编号内容不同或已有活动请求返回 409；跨用户访问返回 404。
目前是 JSON 长请求而非流式聊天。旧前端尚未接入这个入口。

## 执行、记忆与安全边界

- 每轮最多 24 次模型调用、48 次工具调用、8 次搜索、总时限 300 秒；不是精确人民币费用计量。
- 软偏好必须引用本轮用户原话，自动保存并由主 Agent 提示。引用原话不代表程序能完全判断语义，仍需模型评测。
- 硬约束字段不可作为 soft 写入；用户确认后才进入已确认记忆。
- 首版支持兴趣、职业目标、学习偏好、排除城市和预算提案，不是完整考生画像编辑器。
- 记忆仍局限当前会话，尚无跨会话用户级记忆检索、删除/更正界面。
- 画像版本变化会令旧研究产物失效，主 Agent 必须重新调查或提交新产物。
- 角色权限、来源编号、候选引用、版本和授权由程序校验，不只依赖提示词。
- 每次工具执行后将产物和业务状态持久保存，不保存模型内部思考。
- 当前 LangGraph 节点执行状态在进程内；数据库业务快照不等于 LangGraph checkpointer。
  服务重启会标记活动请求失败、解除锁，保留已保存产物，但不能从中断的模型调用继续。
- 暂限单个后端工作进程。客户端断开/取消不保证后台任务独立继续；后台队列和显式取消端点待实现。

## 志愿表与数据

数据库查询覆盖广东实际存在的所有年份，不沿用旧规则硬编码年份。
历史记录按学校、专业、招生类型、科类聚合，候选引用绑定返回的数据哈希。
没有目标年度资格规则时一律保留 unknown，表格带 `test_only=true`，不可直接正式填报。
已确认排除城市、导入画像科类不符或不接受中外合作者单独置于 excluded，不进入推荐顺序。
学费预算和真实选科条件未具备完整确定性检查，始终列为待核实，不能声称满足。
排序由规划 Agent 综合解释，不展示固定冲稳保标签，也不承诺录取概率。
新规划工具不使用 v1 固定风险阈值或强制层级覆盖要求。`analyze_historical_risk` 使用最近三个记录年份的有效位次中位数，
返回范围、波动、趋势和与考生位次差额；缺失数据、年份不连续与科类不符有明确结果，可按目标年截断。
`validate_plan` 与 `submit_plan` 使用同一套确定性校验，后者保存新版本，不覆盖旧方案。
完整 16 个工具、角色权限、来源快照、修订操作与数据口径见 [工具清单](tools.md)。

## 验收状态

新增自动化测试使用明确的模拟模型/搜索与临时数据库，覆盖四角色真实图执行、产物交回与读取、
确认门禁、工具越权、伪造引用、旧版本拒绝、条件不符隔离、预算、API 用户隔离、幂等和重启解除锁。
真实 Qwen/DeepSeek、真实搜索质量和浏览器聊天体验尚未验证；不能将离线通过当作生产完成。

后续优先：真实少量合成案例联调 → 聊天前端与确认按钮 → 记忆编辑 → 持久图恢复与并行调查。

参考：[LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、
[Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)。
