# LiveAi 阶段 0：接口与行为基线

> **不可变历史行为快照。** 本文主体及第 15 节的 54 项结果只记录 2026-07-22 当时的工作树与验证事实，不随后续实现或测试集合变化而改写。当前复现入口见第 16 节。
>
> 记录日期：2026-07-22。本文描述当时工作树中的既有行为，供模块化重构前后对照；不是目标架构已经实现的声明。

## 1. 范围与工作树保护

本阶段仅增加文档和 characterization tests，不移动/重命名生产文件，不修改生产配置，不改变运行逻辑。

开始时工作树已有大量未提交修改及未跟踪文件，涉及 `ARCHITECTURE.md`、`README.md`、`config.yaml.example`、`main.py`、`core/`、`pack/`、`tool/`、`test/` 等。它们均视为他人/前序工作，本阶段不覆盖、不回滚、不清理。

测试框架现状：测试主要使用标准库 `unittest`，部分文件使用 pytest 风格；仓库没有发现 requirements/pyproject/pytest.ini/tox.ini/setup.cfg。当前解释器没有安装 pytest（`python -m pytest` 报 `No module named pytest`）。

## 2. 当前目录职责

- `main.py`：唯一生产装配入口。
- `core/`：AI 编排、仓储、模型选择、Agent、事件类型/归一化、运行状态、token 使用等业务逻辑。
- `pack/`：NapCat、模型 HTTP provider、WebUI、JSON/内存存储、日志等封装。
- `tool/`：提供给 AI 的工具实现、工具箱及任务/消息/联系人等适配。
- `data/`：运行数据、模型配置、提示词、下载、图片及持久化状态。
- `group/`、`my/`：群/号主相关数据或兼容目录。
- `test/`：现有测试与辅助产物；当前测试并非全部可离线收集。
- `scripts/`：运维/检查脚本。

## 3. 装配入口与消息入口

`main.py` 顶层装配顺序：

1. 创建 `NapcatBot('config.yaml')`。
2. 创建 `JsonStore(ai.storage_path)`、`ScopedMemoryStore(data/memory)`、`AIRepository`。
3. 创建 `AIConfig`、`AIToolbox`、`AIOrchestrator`，将其注册到 bot。
4. 可选创建并启动 `WebUIService`，WebUI 缺失不阻止 bot 主流程。
5. 调用 `bot.run()`。

NapCat 收到原始 WS event 后，经 `core.event_normalizer.normalize_ws_event(data, self_id)` 形成 `NormalizedEvent`；消息按 `group/private` 转为 `ChatMessage`，再进入 `AIOrchestrator.handle_group_message` / `handle_private_message`。`scope_for` 当前仅接受 `group`、`private`，分别形成 `group:<id>`、`private:<id>`。

## 4. AIOrchestrator 公共接口

当前公共方法（签名以代码为准）：

- `reload_models_config(self) -> dict`
- `start(self)`
- `register(self)`
- `handle_group_message(self, message: ChatMessage)`
- `handle_private_message(self, message: ChatMessage)`
- `handle_command(self, message: ChatMessage, command: str) -> str | None`
- `get_runtime_status(self) -> dict`
- `switch_model_profile(self, requested: str) -> tuple[bool, str]`
- `create_task(self, source_agent: str, kind: str, payload: dict)`

主要调用方：`main.py` 负责构造/注册；`NapcatBot` 通过注册的 message/command handler 调用消息接口；`WebUIService` 调用 runtime status、模型切换及管理接口；内部闹钟、任务与 Agent report 会重新提交到 orchestrator 队列。

`get_runtime_status()` 当前字段契约：`enabled`、`ready`、`active_profile`、`active_model`、`active_label`、`queue_size`、`worker_count`、`scheduled_alarm_count`、`available_models`。`available_models` 每项包含 `index`、`display_name`、`model_id`、`base_url`（当前实际填渠道 strategy）、`active`。

## 5. AIRepository 公共接口

仓储以 `JsonStore` 为主状态，以 `ScopedMemoryStore` 为 scope memory。公共接口分组：

- 设置/知识库：`get_setting`、`set_setting`、`get_knowledge_base`、`add/update/delete_knowledge_entry`。
- 用户身份/事实/解析：`touch_user_identity`、`add_user_fact`、`get_user_profile`、`resolve_user_candidates`、`resolve_scope_by_query`、`find_users_mentioned_in_text`。
- Agent：`get_or_create_master`、`get_or_create_agent`、`list_agents`、`get_agent`。
- 上下文：`append_message`、`list_messages`、`clear_messages`、日记 pending/summary/meta summary、impression/display name。
- notes/memory：`add/list/get/update_note`、`clear_notes`、`get_memory`、`clear_memory`。
- 审计：`add/list_tool_logs`、`add/list/get_turn_log`。
- tasks：`create_task`、`create_unique_task`、`update_task`、`get_task`、`list_tasks`。
- relations：scope/user relation 的 get/update/list。
- 状态：`load_state`、`reset_all`、`count_memory_scopes`。

旧状态读取基线：`JsonStore.load()` 接受正常 JSON；若文件是一个有效 JSON 文档后跟垃圾，会读取首个文档并自愈重写。Repository 的 shape/migration 在访问相关接口时通过 `setdefault` 兼容缺失键。

## 6. AgentManager / tasker 契约

`AgentManager` 公共接口包括：loop/notifier/model/task 注册，Agent CRUD/status/message，`send_to_agent`，以及 report 队列：`on_agent_message`、`has/drain/peek/requeue_pending_reports`。Agent 持久化记录根结构为 `{"agents": {agent_id: record}}`；注入队列、async task 注册表和 pending reports 均仅内存。

Agent report 每项当前为：`agent_id`、`text`、`ts`、`origin_scope`。`on_agent_message` 追加并通知；`drain` 原子取走全部；`requeue` 放回队首并保持原相对顺序。Orchestrator 按 origin scope 合并为 `agent_message` 事件；目标 scope 忙时延后。

一次性 tasker 仍通过任务工具/`create_task` 与 task 状态工作；模型角色公开名为 `tasker`，持久化旧键 `dev_agent` 仍是兼容别名。

## 7. 模型 provider 与选择/retry/fallback

`ModelManager` 配置三级结构：

- `upstreams[]`: `name`、`base_url`、`api_key`、`messages_path`
- `channels[]`: `name`、`strategy` (`fallback|random|roundrobin`)、`models[]` (`upstream`,`model_id`)
- `roles`: `main`、`tiered`、`agent`、`tasker`、`vision`（兼容 `dev_agent`）

`get_model_for_role(role)`：角色缺失时回退 main；渠道缺失时回退首个渠道。`fallback` 使用内存索引，只有 `notify_failure(role)` 才轮到下一模型；索引不持久化。`random` 随机选，`roundrobin` 使用内存计数器。

Provider 输入为 Anthropic 风格 `messages`、system、tools、temperature/max_tokens 等请求参数；输出统一为 `ChatResponse(text, tool_calls, raw_content, usage)`，工具调用统一为 `ToolCall(call_id, name, input)`。网络/API 重试由现有 provider/runtime 负责；重构不得改变尝试次数、可重试错误判定、fallback 通知时机或角色选择。

## 8. 工具 schema 与消息顺序

`core.ai_tools_schema.build_tools(...)` 返回 list；每项至少有 `name`、`description`、`input_schema`，input schema 当前是 JSON Schema object。开关控制 message、memory、remember、notify_master、tasks、search、download、recurring、agent 等工具集合。

模型工具轮当前顺序必须保持：

1. provider 返回 assistant content，其中 tool call 是 Anthropic `tool_use` block；
2. 执行调用并按 call id 形成 `{"type":"tool_result","tool_use_id":...,"content":...}`；
3. 向模型历史先追加 `role=assistant` 的原始/过滤后 content；
4. 再追加 `role=user`、content 为 tool_result blocks；
5. 进入下一次模型调用。

即使 `stay_silent` 也生成配对 tool_result 后终止，不能留下未配对 tool_use。

## 9. scope FIFO 与 pending

当前实现不是目标事件邮箱。已有 `_active_scope_turns`、`_pending_scope_turns`：

- 首个 turn 占用 `scope_key`；同 scope 新消息进入 list 尾部；不同 scope 可由 worker 并行。
- `_release_scope_turn` / `_take_pending_scope_turn` 从 list 头部取出，因此是同 scope FIFO。
- 过期 pending message 会被跳过。
- pending message 当前逐条续轮；部分链路通过 `trigger_messages`/history seed 做上下文携带，但“把当时全部积压事件合成单批”不是已完成能力。
- task 还有独立 pending scope task 协调；不能在阶段 0 改其优先级/提升时机。

测试缺口：完整 pending 合批依赖大型异步 turn、真实消息上下文和多类 task/report 协调，目前没有低侵入纯函数可稳定验证；阶段 0 仅锁定 reserve/FIFO 基础行为，目标邮箱语义留待阶段 1 设计后新增测试。

## 10. 闹钟、循环任务与 Agent report

- 闹钟由 orchestrator 内存调度集合/任务管理，runtime status 暴露 `scheduled_alarm_count`。
- recurring task 持久化在 AI state tasks 中，任务 kind/status/payload 由 Repository 管理；到期后重新提交运行。
- Agent report 通过 AgentManager 内存 pending list 汇集，orchestrator 负责按 scope 投递。
- 本阶段不改变触发时间、循环计算、重启恢复、scope 忙时 requeue 或 report 合并格式。

测试缺口：基于真实 event loop 的闹钟恢复、循环边界与 report→scope 全链路未纳入离线 characterization，避免启动后台任务或修改生产代码。

## 11. WebUI/status 契约

`WebUIService(host, port, repo, orchestrator)` 提供 overview、commands、agents、tasks、memory、settings、relations、models、token usage 等读取/管理方法。`get_overview()` 返回：

- `bot_id`
- `runtime`（见第 4 节）
- `counts`: `agents/groups/privates/masters/tasks/queued_tasks/memories`
- `task_status_counts`、`task_kind_counts`
- `top_agents`
- `recent_tasks`

HTTP API 路径及响应包装仍由 `pack/webui_server.py` handler 定义。阶段 0 测试只验证可隔离的 overview 字段，不启动 HTTP server。

## 12. 持久化文件与 schema
当前主要持久化位置（均不得在阶段 0 改路径）：

- `config.yaml`：NapCat、AI、WebUI 等生产配置（敏感，不作为测试输入）。
- `data/ai_state.json`（由 `ai.storage_path` 决定）：settings、knowledge_base、agents、tasks、user identities/facts、scope/user relations 等。
- `data/memory/<scope>.json`：scope messages/notes/diary/tool logs/turn logs 等，文件名由 `ScopedMemoryStore` 清洗。
- `data/models_config.json`：upstreams/channels/roles。
- `data/agents_state.json`：常驻 Agent 记录。
- `data/token_usage.json`：全局/role/scope/model token 累计。
- `data/tasker_recurring_tasks.json`：tasker 循环任务兼容状态（现有 tasker 路径）。
- `data/download/`、`data/images/`、`data/prompt/`：下载、图片、提示词资源。

Schema 目前以宽松 dict + `setdefault` 演进，没有统一版本号。重构前应避免一次性严格化；需要明确 migration/version 策略。

## 13. 目标设计约束（尚未实现）

长期目标是 `core/` 承载主 AI、分级 AI、Agent 的完整交互/生命周期；`data/` 承载数据与资源；`pack/` 提供可独立升降级的模型、账户、NapCat、图片、搜索、GitHub、存储封装。

目标分级 AI 是每 scope 单消费者事件邮箱：消息、闹钟、Agent report、主 AI 通讯统一投递；生成期间积压；当前轮完整记录 assistant/tool_call/tool_result/turn metadata 后，将当时全部积压合成一批触发下一轮，直到邮箱为空。邮箱仅内存，重启丢弃。**以上是设计约束，不代表当前代码已经实现。**

## 14. 阶段 0 测试覆盖与缺口

新增离线测试覆盖：公共入口/签名、事件归一化与 scope、同 scope reserve/FIFO、fallback 索引与 tasker 旧别名、工具 schema/tool result 格式与追加顺序、Agent report drain/requeue、旧 JSON 状态读取、runtime/WebUI 字段。

未覆盖风险：真实 NapCat 与网络 provider；完整 retry 时间/异常矩阵；完整异步工具轮；pending 全量合批（当前也未实现目标语义）；闹钟/循环任务时间边界和重启恢复；WebUI HTTP 路由；生产数据全量 migration。进入阶段 1 前应由号主确认目标邮箱的事件类型、批次边界、task/report 优先级、旧 pending 兼容窗口及持久化 schema version 策略。

## 15. 测试执行证据

- `python -m pytest --collect-only -q`：未执行测试，当前解释器缺少 pytest，错误为 `No module named pytest`；未安装任何包。
- `python -m unittest discover -s test -p 'test_stage0_characterization.py' -v`：8 passed，0 failed，0 skipped，耗时约 0.013s。
- `python -m unittest discover -s test -p 'test_*.py' -v`：共 54，53 passed，1 import error，0 skipped。错误来自既有 `test/test_switch_preflight.py` 导入不存在的 `core.inbox`；阶段 0 明令禁止创建该目标模块，因此不修复。该全量 discover 还会导入若干现有诊断脚本并触发真实 provider 请求，说明现有 test 目录没有完全隔离网络；后续基线应优先用显式离线模块列表，避免再次执行这些脚本。

运行时另有 `requests` 依赖版本告警（urllib3/chardet/charset_normalizer 组合），不影响新增测试结果，本阶段未调整环境依赖。

## 16. 当前可复现验证入口（不改写历史结果）

以下命令验证当前工作树中仍受阶段 0 锁定的离线契约；它们是新增复现入口，不替代、不修订第 15 节当时的 54 项执行记录：

```bash
python -m unittest discover -s test -p 'test_stage0_characterization.py' -v
```

当前测试集合：`test/test_stage0_characterization.py`，8 项。2026-07-22 本次最终对账复现为 8 passed，0 failed，0 skipped。第 15 节的历史 54 项结果保持不变。阶段 1 当前专项集合已包含对象引用层（mailbox entry/transient identity、adapter 深拷贝与混合 batch）新增测试，共 34 项，详见 `docs/stage1_event_mailbox_shadow.md`；不建议用默认全目录 discover 作为离线基线，因为 `test/` 中仍包含可能访问真实 provider 的诊断脚本。
