# 多 Agent 重构基础（第二、三步）

本页保留基础重构阶段的设计记录。后续工具已完善，当前实现和数据口径以 [工具清单](tools.md) 为准。

## 基线与范围

单 Agent 基线提交为 `af2a087`，重构分支为 `codex/multi-agent-v2`。
基线自动化测试通过，但真实模型和完整浏览器验收仍未完成。
本阶段不创建专业 Agent、不切换生产模型、不修改数据库表、不改变现有 HTTP/SSE 格式。

## 代码边界

- `backend/providers/client.py`：模型配置、客户端、供应商请求参数、assistant 消息回传。
- `backend/tools/risk.py`：原规则引擎，候选查询与风险计算保持原算法。
- `backend/tools/recommendation.py`：确定性合并、排序和规则降级。
- `backend/tools/candidates.py`：单次执行状态、候选分页/详情、提交、校验和工具 Schema。
- `backend/contracts/artifacts.py`：子任务、证据包、方案草稿、审核报告和交接校验。
- `backend/services/agent_service.py`：现有单 Agent 决策循环，使用上述共享模块。
- `backend/services/llm_service.py`：详情报告，使用同一模型适配层。
- `backend/services/rule_engine.py`：旧路径兼容转发，不保留第二套算法。

工具模块不依赖模型适配器。新旧编排器应使用同一套查询与校验，不能复制规则。
基础阶段共享工具仅包含 v1 集合；后续 v2 已在 `agents/schemas.py` 和执行层落实角色权限。

## 切换供应商

只在本机 `backend/.env` 编辑密钥，不提交或粘贴到聊天。

默认兼容已有 `QWEN_API_KEY`、`QWEN_BASE_URL`、`QWEN_MODEL`。
切换 DeepSeek 时设置：

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=<本机填写>
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
LLM_THINKING=false
LLM_TIMEOUT_SECONDS=60
```

配置优先级：非空的 `LLM_MODEL/BASE_URL/API_KEY` → 所选供应商对应配置 → 模型/地址默认值。
密钥没有默认值，DeepSeek 不读取 Qwen 密钥。切换前清除不再适用的通用覆盖项。
`LLM_THINKING=true` 开启供应商对应思考参数；部分型号不支持该开关，须针对实际型号联调。
SDK 自动重试关闭，单 Agent 原有总时限和工具预算不变；详情最多两次应用层尝试。
内部 assistant 消息保留模型返回字段，不把思考内容写入 SSE 或用户对话摘要。
没有更改本机实际密钥，也没有发起付费模型调用。

参考：
- [DeepSeek 思考模式和工具消息回传](https://api-docs.deepseek.com/guides/thinking_mode/)
- [Qwen Function Calling](https://help.aliyun.com/en/model-studio/qwen-function-calling)

## 产物协议

所有产物含 `schema_version=1`；跨 Agent 产物绑定 `task_id/profile_version/data_version`。
证据包含来源、适用年、省份、候选引用及 verified/unverified/conflicting 状态。
方案只引用候选和证据，不授权模型写入录取事实或覆盖规则风险值。
审核报告引用明确方案，阻断问题不能与通过结论同时出现。
子任务包含角色、依赖、输入输出产物引用、状态和调用/时间预算。

`validate_handoff` 检查版本、证据包、候选授权范围、证据关联及审核方案引用。
Schema/交接检查通过只表示结构一致，不表示来源真实、资格满足或允许发布。
未知/冲突证据允许进入草稿；未来发布器必须单独处理资格和核实状态。
候选整数编号仅在相同数据快照/候选池内使用，不可视作跨版本稳定业务标识。
当前协议是下一阶段的接口基础，未写入现有 task JSON，也未实现调度、依赖环检测或持久恢复。

## 验证与下一阶段

运行 README 中的 unittest 命令。新增测试覆盖双供应商配置隔离、参数、真实 SDK + 模拟 HTTP、
多轮思考字段回传、详情流关闭、严格产物格式、跨任务/版本与证据引用拒绝。
测试不使用真实密钥，不代表线上 API 或业务效果已验证。

下一步：配置实际供应商后进行少量合成画像冒烟测试，再实现规划→审核→定向修正闭环。
等价拆分阶段保留了基线 2023–2025；后续工具完善已改为实际覆盖年份查询，详见工具清单与回归测试。
