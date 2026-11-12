# 高考志愿规划 Agent

首页 `/` 和 `/chat` 打开志愿研究室，传统填报表单可从 `/form` 进入。两个入口使用同一个账号。
研究室支持多对话、草稿恢复、后台研究、方案版本、来源查看和下载。可以先探索专业，有了省位次后再补考生条件。
历年录取查询、位次排序和候选对照可直接使用数据库。对话研究可配置 Qwen、DeepSeek 或本机 Ollama。

任务恢复、数据迁移和审核机制见 [架构说明](docs/architecture-v3.md)。工具权限和数据口径见 [工具清单](docs/tools.md)。

## 开发运行

使用 Python 3.12。在项目根目录：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock.txt
cd backend
test -f .env || cp .env.example .env
# 本地编辑 .env，配置 QWEN_API_KEY 和 JWT_SECRET_KEY
../.venv/bin/python -m database.import_data
../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

打开 [本地志愿研究室](http://127.0.0.1:8000/)。部署时设置自己的随机 `JWT_SECRET_KEY`，各 API 进程使用同一个值。
未设置密钥的本地进程使用临时随机签名，重启后需要重新登录。

依赖锁记录了 macOS arm64 / Python 3.12 的离线验证环境；其他平台可使用 requirements.txt。
`database.import_data` 导入仓库随附的广东 2021–2025 年历史快照，保留已有院校、录取记录和用户数据，可重复执行。
同一院校、专业、科类和招生类型下的多专业组可能归集成一条历史记录。目标年度的专业组、选科、学费和计划需要官方资料。
`python -m scripts.seed_test_data` 会重建录取、一分一段和院校数据，仅用于隔离测试数据库。

v1 采用单进程任务执行器，请使用单个 Uvicorn worker。v1 服务重启时，活动轮次标记失败并解除锁；
已保存的画像、对话和方案可继续使用，未完成的模型调用不会自动重放。
启动时幂等创建 agent_tasks / agent_turns 新表，不修改现有历史表结构。部署前备份数据库。
v2 默认启动数据库队列工作任务，浏览器断开不取消执行；服务重启后按租约和工具检查点恢复。
可设置 `AGENT_EMBEDDED_WORKER=false`，另开进程运行 `python -m services.run_worker`。

## 使用与接口

1. 注册或登录后，点击「新对话」，从兴趣、院校或专业问题开始。每段对话的草稿保存在当前浏览器。
2. 在「我的考生条件」中填写省份、科类、目标年与已知位次。传统表单结果页也可用「带着画像进入 Agent」创建独立画像对话。
3. 用「查历年录取」按院校或专业搜索。填入省位次可按目标年前最近有位次记录的一年排序，也可对照最多三项。选中候选后可放入对话，或点击「拟研究草案」进入确认流程。
4. 研究执行期间可以切换对话、刷新或停止。报告、审核、来源和方案版本在「研究成果」中查看和下载。
5. 在「对话设置」中改名、复制链接或归档，链接只对该账号可用。已归档的对话可恢复。

### 传统表单接口

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

Qwen 和 DeepSeek 使用各自的 API Key。也可以显式选择本机 Ollama，在运行 Ollama 的终端环境中准备模型：

```bash
ollama pull qwen3:4b
```

在 `backend/.env` 中设置 `LLM_PROVIDER=ollama`，然后重启工作台。默认连接 `http://127.0.0.1:11434/v1`，
本机服务无需配置密钥。可通过 `OLLAMA_MODEL` 选择支持工具调用的模型。若此前设置了通用 `LLM_BASE_URL` 或 `LLM_MODEL`，
请同步更新或清空这些覆盖项。配置的 Tavily 仍用于查询公开关键词。
[Ollama 兼容接口](https://docs.ollama.com/api/openai-compatibility)

## 验证

项目根目录运行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend .venv/bin/python -B -m unittest discover -s backend/tests -v
node --check frontend/chat.js
npm ci
npx playwright install chromium
npm run test:browser
```

已有 Chrome 时可用 `GAOKAO_TEST_BROWSER=chrome npm run test:browser`。浏览器测试使用隔离数据库和脚本模型，覆盖注册登录、
画像、录取查询及对照、断连重试、确认、报告下载、记忆管理、取消、归档、用户隔离和移动端。
配置真实模型后，运行 `npm run test:browser -- --live` 可在隔离库中导入历史快照，并验证真实调查确认、方案来源和下载。
该命令会调用配置的模型网关。
本机真实数据和供应商探测结果见 [工作台验收记录](docs/workbench-acceptance.md)。

页面样式已编译到 frontend/styles.css，不依赖 Tailwind 在线 CDN。
传统结果页使用仓库内的 ECharts。更新 ECharts 版本后执行 `npm run build:vendor` 重新生成静态文件。
修改页面样式类后执行 `npm ci && npm run build:css`，并一并提交生成的 CSS。
任务版本中的详情报告使用对应画像版本，避免调整条件后仍读取初始画像。
