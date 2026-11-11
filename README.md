# 高考志愿规划 Agent

默认旧页面仍运行单 Agent；四角色 v2 已提供独立聊天页面 `/chat`，首页可进入。
v2 已接入持久后台任务、重启恢复、SSE 进度、审核修正、年度资格规则和跨会话记忆。
运行方式、迁移、接口及验收范围见 [六项架构完成记录](docs/architecture-v3.md)。
角色提示词、API 使用方式与明确限制见 [四 Agent 后端原型](docs/multi-agent-v2.md)。
支持配置 Qwen 或 DeepSeek，默认兼容原 Qwen 配置；未自动切换供应商。
详见 [重构基础、配置与边界](docs/architecture-foundation.md)。

保留考生画像表单，新增目标输入、追问回答、候选比较、方案修正与版本回看。
Agent 使用 Qwen 工具调用选择下一步；数据库提供事实，规则引擎计算历史风险，服务端校验方案。

## 开发运行

使用 Python 3.12。在项目根目录：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock.txt
cd backend
cp .env.example .env
# 本地编辑 .env，配置 QWEN_API_KEY 和 JWT_SECRET_KEY
../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

依赖锁记录了 macOS arm64 / Python 3.12 的离线验证环境；其他平台可使用 requirements.txt。
首次需要导入录取数据。开发用 `python -m scripts.seed_test_data` 会清空并重建录取、一分一段和院校数据，
只能用于隔离测试数据库；种子数据为模拟数据，不能用于实际填报。

v1 采用单进程任务执行器，请使用单个 Uvicorn worker。v1 服务重启时，活动轮次标记失败并解除锁；
已保存的画像、对话和方案可继续使用，未完成的模型调用不会自动重放。
启动时幂等创建 agent_tasks / agent_turns 新表，不修改现有历史表结构。部署前备份数据库。
v2 默认启动数据库队列工作任务，浏览器断开不取消执行；服务重启后按租约和工具检查点恢复。
可设置 `AGENT_EMBEDDED_WORKER=false`，另开进程运行 `python -m services.run_worker`。

## 使用与接口

表单的“其他补充需求”作为初始目标。结果页可以继续提问、比较学校、要求调整方案。
需要修改硬约束时，展开“明确修改画像条件”，勾选应用修改后发送；这会创建新的任务画像版本。
刷新页面从服务端恢复任务；推荐历史中新增记录可返回对应任务。

- POST /api/v1/agent/tasks：画像编号 profile_uuid 和目标 goal。
- POST /api/v1/agent/tasks/{id}/messages：request_id、content，可选完整表单 profile。
- GET /api/v1/agent/tasks/{id}：对话、状态、画像版本、方案版本和工具执行摘要。
- POST /api/v1/agent/tasks/{id}/cancel：取消活动轮次。
- 原 /api/v1/recommend 保留为单轮兼容接口，调用同一个 Agent 引擎；多轮使用上述任务接口。

同一任务一次只能有一个活动轮次。相同 request_id 和消息内容重放已有 SSE；
相同编号、不同内容返回 409。SSE 包括 progress、waiting、answer、result、error，
附带 task_id、turn_id。断开浏览器连接不取消任务，可用取消接口停止。

## 工具与边界

工具包括读取画像、规则筛选、条件分页、查询历史详情、追问、提交草稿、校验、比较回答。
推荐必须通过校验；失败反馈给模型修正。工具参数执行服务端 JSON Schema 校验。
学校、专业和风险数据来自候选池，模型不能覆盖。
候选比较输出已查询对象的资料引用；历史方案通过版本保留。
v2 已补齐 16 个工具，包含来源读取、候选详情、历史位次分析、比较、预检和版本化草案；
完整权限、参数、数据口径与测试说明见 [工具清单](docs/tools.md)。
v1 历史年份按省份/科类实际最近三年查询，不再固定为 2023–2025。

每轮最多 24 次模型决策、48 次工具执行、5 分钟；单次模型请求超时 60 秒。
AGENT_MAX_STEPS 和 AGENT_PAGE_SIZE 可配置。超过预算或模型失败时降级为规则备选，
不表示已经满足用户个性化目标。没有候选且无法追问时返回 insufficient_data。
任务终态区分 completed、waiting、degraded、insufficient_data、failed、cancelled。

当前规则只使用现有数据支持的限制与历史位次。预算、具体选科资格、当年招生章程和计划待核实；
risk_score 是历史启发式指标，不是录取概率。v1 无联网检索；v2 可配置 Tavily 搜索。系统不自动填报。

Qwen 工具调用依据：[阿里云 Function Calling 文档](https://www.alibabacloud.com/help/zh/model-studio/qwen-function-calling)。
真实调用需要你自己的 API Key；不把模拟模型测试当作实际模型联调通过。

## 验证

项目根目录运行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend .venv/bin/python -B -m unittest discover -s backend/tests -v
node --check frontend/agent.js
```

离线用例覆盖校验失败后修正、追问与比较的不同路径、非法参数、伪造候选、
缺少密钥与步数耗尽降级、任务持久化、追问恢复、幂等、用户隔离、取消和重启恢复。
真实 Qwen 联调与完整浏览器验收需另行验证，不由离线测试替代。

页面样式已编译到 frontend/styles.css，不依赖 Tailwind 在线 CDN。
修改页面样式类后执行 `npm ci && npm run build:css`，并一并提交生成的 CSS。
任务版本中的详情报告使用对应画像版本，避免调整条件后仍读取初始画像。
