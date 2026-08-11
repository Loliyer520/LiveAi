# P5 模块拆分清单与互斥任务包

> 范围：`core/dev_agent.py`、`core/agent_manager.py`、`core/ai_repository.py`、`core/ai_tools_schema.py`、`core/model_manager.py`、`pack/webui_server.py`、`pack/napcat.py`；`core/ai_runtime.py` 仅用于只读依赖审计。
>
> **硬约束：后续拆分任务不得顺手修改 `core/ai_runtime.py`。** 如果某次拆分需要改变 runtime 的 import、构造参数、调用点或行为，必须停止该文件任务，另开经过确认的 runtime 接线任务；不得在本清单所列任务包中夹带。

## 1. 总览

行数按当前工作树文本行统计。

| 文件 | 行数 | 当前公共入口/稳定调用面 | 主要 owner 状态 | 主要副作用 | 首拆建议 | 风险 |
|---|---:|---|---|---|---|---|
| `core/ai_tools_schema.py` | 672 | `build_tools()`；`LOOP_TOOL_NAMES` | 模块级不可变/准不可变 schema 常量 | 无网络、无持久化 | schema 常量按领域分组，保留 facade | 低 |
| `core/model_manager.py` | 527 | `ModelManager` 的 role/model 查询、切换和 CRUD | `config`、轮询计数器、fallback 索引 | 读写 `data/models_config.json` | 先拆 DTO/校验/迁移纯函数 | 中 |
| `pack/napcat.py` | 510 | `NapcatBot` 注册事件、OneBot 查询/动作、`start()` | WS、回显去重、request cache、executor | WebSocket 长连接、HTTP POST | 先拆事件解析 DTO/纯函数与 OneBot response 校验 | 中高 |
| `core/ai_repository.py` | 876 | `AIRepository` 全部状态仓储 API | `JsonStore`、`ScopedMemoryStore` | JSON/分 scope memory 持久化 | 先拆 key/normalize/scoring/diary DTO 纯逻辑 | 高 |
| `pack/webui_server.py` | 823 | `WebUIService.start()`、查询/动作 service methods | HTTP server/thread、HTML cache、repo/orchestrator 引用 | HTTP server、上游 GET、配置写入、runtime 动作 | 先拆 serializer/request DTO/route table；避免导入 runtime 类型 | 高 |
| `core/dev_agent.py` | 2176 | `run_dev_agent()`；`DevAgentShellManager`；被 `AgentManager` 复用的 legacy helpers | shell jobs/lock；大量模块级策略常量 | 模型/GitHub、文件读写、shell/subprocess、临时目录 | 先拆纯策略、DTO、协议，再拆 adapter | 很高 |
| `core/agent_manager.py` | 1096 | `AgentManager` 生命周期、队列、执行循环、总结 | 持久记录、async queues/tasks/loop、report queue/locks/model | `agents_state.json`、模型调用、shell 生命周期、回调 | 先拆 record/report DTO 与纯摘要/状态规则；运行 owner 后拆 | 很高 |
| `core/ai_runtime.py`（只读） | 6555 | `AIOrchestrator`、代码块辅助函数 | 全局编排 owner | 全部编排副作用 | **禁止在 P5 任务包修改** | 极高 |

建议总体依赖方向：

```text
纯 DTO / Protocol / schema / policy
        ↓
持久化 adapter、HTTP/WS/GitHub/model/shell adapter
        ↓
领域 service / manager
        ↓
AIOrchestrator（composition root，P5 只读）
        ↓
main.py / WebUI 启动接线
```

禁止反向依赖：纯模块不得 import `AIOrchestrator`、`WebUIService`、`NapcatBot`、具体模型 client 或具体 store；repository 不得依赖 runtime；transport/协议解析不得依赖 orchestration。

## 2. 逐文件清单

### 2.1 `core/ai_tools_schema.py`

- **行数**：672。
- **公共入口**：
  - `build_tools(include_qq_request_management=False)`（参数以当前签名为准）。
  - `LOOP_TOOL_NAMES` 被 `core.ai_runtime` 导入；`DIRECTIVE_TOOL_NAMES` 虽未见跨文件引用，也应视作 schema 兼容面。
- **入 imports**：仅 `copy`。
- **出 imports（主要消费者）**：
  - `core/ai_runtime.py`：`LOOP_TOOL_NAMES`, `build_tools`。
  - `test/test_stage0_characterization.py`, `test/test_qq_request_management.py`：`build_tools`。
- **全局状态**：`LOOP_TOOL_NAMES`、`DIRECTIVE_TOOL_NAMES`、`_TOOL_DEFINITIONS`。风险在于嵌套 dict/list 被调用方意外修改；`copy` 表明构建时已有隔离意图。
- **线程/锁/async 边界**：无。
- **持久化路径**：无。
- **网络副作用**：无；这里只描述工具，不执行工具。
- **可先拆**：
  - 按 memory/task/message/admin/QQ request 等领域拆 schema 常量。
  - 工具过滤、名称集合推导、deep-copy 组装为纯函数。
  - `ToolDefinition`/JSON-schema 结构可用 `TypedDict` 或只读 DTO 描述，但不能改变返回的 dict shape。
- **必须留在原 owner**：`build_tools` facade、现有工具名称、参数 schema、工具顺序和 feature flag 语义，直至所有消费者显式迁移。
- **import-cycle 风险**：低；若拆出的 schema 反向 import runtime/toolbox 来“复用实现”，会制造 schema → runtime/toolbox → repository/runtime 环。schema 必须保持叶子层。
- **建议方向**：`ai_tools_schema facade -> tool_schema_* pure constants`，运行实现只依赖工具名，不能被 schema 模块导入。
- **characterization tests**：
  - `test_stage0_characterization.py::test_tool_schema_and_tool_result_shape`
  - `test_qq_request_management.py::test_tools_only_registered_for_master`

### 2.2 `core/model_manager.py`

- **行数**：527。
- **公共入口**：`ModelManager`；重点方法包括 `get_model_for_role`、`notify_failure`、`get_current_model`、`get_role_model`、`get_vision_model`、`switch_model`/`switch_next_model`、配置查询与 upstream/channel/model/role CRUD。
- **入 imports**：stdlib `json`, `random`, `Path`, `Optional`；`pack.console_logger`。
- **出 imports**：`main.py`、`core/ai_runtime.py`、`core/test_command.py`、`test/test_stage0_characterization.py`。
- **全局状态**：`ROLE_LABELS`；实例状态为 `config_path`、`config`、`_rr_counters`、`_fb_indexes`。
- **线程/锁/async 边界**：同步对象，无锁。若从 HTTP thread 与 AI worker 同时做配置 CRUD/模型选择，当前 dict 与计数器没有同步保护；拆分不得假设线程安全已存在。
- **持久化路径**：默认 `data/models_config.json`；初始化迁移和 CRUD 会创建父目录并覆盖写 JSON。
- **网络副作用**：本文件自身无网络请求；返回连接配置供模型 client 使用。
- **可先拆**：
  - `_empty_config`、legacy config migration、字段规范化、channel/upstream 查找与索引解析。
  - model selection 输入/输出 DTO（role、channel、upstream、model id、messages path）。
  - CRUD 参数校验和纯变换：输入 config 副本，输出新 config + message。
- **必须留在原 owner**：
  - `config` 当前值与保存时机。
  - round-robin `_rr_counters`、fallback `_fb_indexes` 及 `notify_failure` 状态转换。
  - 原子性尚未定义的 reload/save 生命周期；不能在两个对象间双持有。
- **import-cycle 风险**：中。`ai_runtime -> ModelManager` 已存在；新模块不得 import runtime 或具体 orchestrator。WebUI 当前另行直接读写同一路径，拆分时要避免形成 `model_manager <-> webui_server`。
- **建议方向**：`model_config_types/policy (pure) <- ModelManager (owner + persistence)`；WebUI 通过已注入的 manager/service 操作，不能让 manager import WebUI。
- **characterization tests**：
  - `test_stage0_characterization.py::test_model_fallback_and_legacy_tasker_role`
  - `test_stage0_characterization.py::test_public_entrypoints_and_signatures_are_importable`
  - WebUI 配置 CRUD/并发保存目前无直接 characterization，应在移动 owner 前补测。

### 2.3 `pack/napcat.py`

- **行数**：510。
- **公共入口**：`NapcatBot`；事件注册 `on_group_message`/`on_private_message`/`on_group_increase`/`on_self_message`，发送与 OneBot 查询/管理方法，`post()`，`start()`。
- **入 imports**：stdlib hash/json/regex/sys/thread/time、`ThreadPoolExecutor`、`Callable`；第三方 `requests`, `websocket`；`core.events.ChatMessage`, `GroupIncreaseEvent`；console logger。
- **出 imports**：`main.py`；`test/test_qq_request_management.py`。`core.transport` 通过传入 bot 实例形成运行时依赖，而非直接 import。
- **全局状态**：无模块级可变全局；实例 owner：WS client、事件 handler 表、单线程 dispatch executor、自发消息 pending/recent 去重缓存及锁、QQ request cache 及锁、连接参数。
- **线程/锁/async 边界**：
  - `websocket.WebSocketApp.run_forever()` 阻塞边界。
  - 事件 handler 经 `ThreadPoolExecutor(max_workers=1)` 串行 dispatch。
  - `_recent_self_sent_lock` 与 `_request_cache_lock` 保护不同缓存；不要合并锁或改变锁覆盖范围。
  - 无 asyncio。
- **持久化路径**：无；request 与去重缓存均仅内存态，重启丢失是当前行为。
- **网络副作用**：
  - WS 长连接收事件。
  - `requests.post(<http_url>/<action>)` 执行所有 OneBot 动作和查询。
  - 发送消息/图片/文件、撤回、审核请求、禁言等均是外部可见副作用。
- **可先拆**：
  - WS payload → `ChatMessage`/`GroupIncreaseEvent` 的纯解析。
  - mention/self-id 判断、message id/content canonicalization、发送 segment 构造。
  - OneBot response success/error 判定与 DTO。
  - `OneBotActionClient` Protocol（`post(action, params)`），供 transport 和测试替身使用。
- **必须留在原 owner**：WS 生命周期、handler 注册/dispatch executor、request cache、回显去重窗口及“HTTP 前占位”竞态处理、连接配置。
- **import-cycle 风险**：中。`core.transport` 已包装 `NapcatBot`；若 `napcat.py` 反向 import transport，会形成 cycle。协议应放在更低层（例如 `core/ports`），两边单向依赖。
- **建议方向**：`event DTO/parser + OneBot protocol <- NapcatBot adapter <- core.transport adapter <- runtime`；不得让 pack adapter import runtime。
- **characterization tests**：
  - `test_qq_request_management.py::{test_request_event_cached_without_persistence,test_unsupported_active_operations_never_call_http,test_onebot_business_error_is_not_success}`
  - `test_transport_contract.py` 全组锁定 legacy action 参数/结果与 inbound registration。
  - `test_stage0_characterization.py::test_event_normalization_and_scope_derivation` 间接覆盖事件契约。

### 2.4 `core/ai_repository.py`

- **行数**：876。
- **公共入口**：`AIRepository`；覆盖 settings、knowledge、identity/relation、agent/memory/diary/note/tool log/turn log、task CRUD、state 查询/reset。
- **入 imports**：`time`, `uuid`, `Path`；`core.ai_types.AgentProfile/PendingTask`；`core.turn_log_slim`；`pack.json_store.JsonStore`；`pack.scoped_memory_store.ScopedMemoryStore`。
- **出 imports**：`main.py`、`core/ai_runtime.py`、`pack/webui_server.py`、`tool/ai_toolbox.py`、`tool/task_tool.py`、`tool/memory_tool.py`、stage 0 tests。
- **全局状态**：无模块级可变状态；实例唯一 owner 为 `store` 与 `memory_store`。大量 mutator 依赖底层 store 的 update 语义。
- **线程/锁/async 边界**：同步 API、文件内无显式锁/async；并发与原子性由 `JsonStore`/`ScopedMemoryStore` 承担。不可把一次 `update(mutator)` 拆成无锁 `load + save`。
- **持久化路径**：
  - 主路径由构造注入的 `JsonStore.file_path` 决定（生产接线通常为 AI state JSON）。
  - 默认 scoped memory 根目录为主 store 同级 `memories/`。
  - diary、messages、notes、tool logs、turn logs 等通过 scoped memory store 持久化；settings/knowledge/tasks/agents/relations 等通过主 store 持久化。
- **网络副作用**：无。
- **可先拆**：
  - `_agent_key`/`_memory_key`、shape ensure/normalize、identity alias/scope 合并、candidate scoring、relation 展示 DTO。
  - diary window/seal/meta-summary 候选计算作为“输入 memory → 输出新 memory/结果”的纯策略。
  - task/agent/profile 的 DTO mapper；保持现有 dict 返回兼容。
- **必须留在原 owner**：
  - 两类 store 的写事务边界与路径选择。
  - diary migration/seal/store 的提交顺序和上限语义。
  - unique task 的查重+创建原子区间。
  - identity/relation/memory 的跨记录更新顺序；在没有事务契约前不得拆成多个 writer。
- **import-cycle 风险**：高。runtime、WebUI、toolbox 均依赖 repository；repository 绝不能反向依赖这些上层。DTO 若放入 `ai_runtime` 或 WebUI 会立刻导致环。
- **建议方向**：`ai_types/repository DTO + pure policies <- AIRepository <- runtime/WebUI/toolbox`；store ports 可下沉，但实际 writer owner 仍唯一。
- **characterization tests**：
  - `test_stage0_characterization.py::{test_public_entrypoints_and_signatures_are_importable,test_old_json_state_is_readable}`
  - `test_stage0_characterization.py::test_runtime_status_contract_is_stable` 间接依赖 state shape。
  - scope/mailbox tests 通过 runtime fixture 间接使用 repository。
  - repository 的 diary、identity、relations、unique task、双 store 事务目前缺直接 characterization，拆 writer 前必须补。

### 2.5 `pack/webui_server.py`

- **行数**：823。
- **公共入口**：`WebUIService(repo, orchestrator, host, port)`、`start()`；HTTP handler 暴露 overview/agents/tasks/settings/knowledge/relations/models/upstream 等查询和动作。
- **入 imports**：`Path`, `json`, `threading`, `time`, `requests`, datetime、stdlib HTTP server/URL parser；`AIRepository`；**`AIOrchestrator`**；console logger。
- **出 imports**：`main.py`、`test/test_stage0_characterization.py`。
- **全局状态**：无模块级可变全局；实例持有 `repo`、`orchestrator`、host/port、`ThreadingHTTPServer`、server thread、HTML cache。
- **线程/锁/async 边界**：
  - `ThreadingHTTPServer` 每请求线程边界。
  - `start()` 启动 daemon thread。
  - service 直接调用 repository/orchestrator/model manager；调用对象是否线程安全未在此层保证。
  - 无 asyncio，但可能跨线程触发 orchestrator 行为。
- **持久化路径**：
  - repository 间接读写 AI state 与 memories。
  - 直接读写仓库内 `data/models_config.json`，并可能触发 orchestrator reload/switch。
  - settings/knowledge/relation 等通过 repository 持久化。
- **网络副作用**：
  - 本地 HTTP server 对外监听。
  - `requests.get` 查询上游余额、模型列表，超时分别约 10/20 秒。
  - admin message、模型切换、agent actions 等通过 orchestrator 触发进一步副作用。
- **可先拆**：
  - `_serialize_*`、`_fmt_ts`、计数、secret masking、response preview、model-id extraction 等纯函数。
  - HTTP request/response DTO、route declaration/dispatch table、JSON response/error mapping。
  - `WebUIRuntimePort` Protocol，列出 WebUI 真正需要的 orchestrator 能力，消除对 `AIOrchestrator` 具体类的 import。
  - upstream inspection client adapter，隔离 `requests.get`。
- **必须留在原 owner**：HTTP server/thread 生命周期、handler 与 service 实例绑定、HTML cache；在 owner 迁移前，配置保存+runtime reload 的顺序也必须保持。
- **import-cycle 风险**：高。当前 `webui_server -> ai_runtime`，而 composition root 又同时构造两者；任何 runtime 对 WebUI 的 import 都会成环。应先用低层 Protocol/duck typing 去除具体 runtime import，但 **P5 不修改 runtime 接线**。
- **建议方向**：`web DTO/serializer + runtime/repository ports <- WebUIService adapter`；`main.py` 注入实现。模型配置应最终只有 `ModelManager` writer，WebUI 不应长期直接写同一路径，但 owner 切换需独立任务。
- **characterization tests**：
  - `test_stage0_characterization.py::test_public_entrypoints_and_signatures_are_importable`
  - 当前无 HTTP route、thread、upstream timeout、models config round-trip 的直接 characterization；实施 route/config 拆分前补黑盒测试。

### 2.6 `core/dev_agent.py`

- **行数**：2176。
- **公共入口/事实兼容面**：
  - 正式入口 `run_dev_agent()`、`DevAgentShellManager`。
  - `AgentManager` 直接复用 `MAX_ITERATIONS`、`MAX_CONTEXT_CHARS`、`RetryableAPIError`、retry/response helpers、shell manager、tool schema builder、ordered executor、project root、context trim 等 underscore legacy helpers；这些已成为事实公共 API。
  - tests 直接导入 retry、budget、parallel executor 等 helpers。
- **入 imports**：asyncio/difflib/inspect/json/os/random/re/signal/subprocess/tempfile/threading/time/datetime/typing；`AnthropicChatModel`、`GitHubService`、console logger、bot logger。
- **出 imports**：`core/agent_manager.py`、`core/ai_runtime.py`、agent tests。
- **全局状态**：大量文件/上下文/retry/shell/并发/result-budget 常量；并行工具名称集合；无模块级 job registry。`DevAgentShellManager` 实例持有 project root、runtime temp dir、jobs、job id、锁。
- **线程/锁/async 边界**：
  - `run_dev_agent` 与 ordered executor 为 async。
  - sync 模型/GitHub/工具调用通过 `asyncio.to_thread`。
  - read-only sub-batch 由 `Semaphore`、`gather` 并发；写/shell/未知工具是串行 barrier。
  - shell manager 以 `threading.Lock` 保护 job registry；subprocess/process-group、signal、timeout、后台 job 生命周期是关键边界。
- **持久化路径**：
  - 默认 prompt `data/prompt/dev_agent.txt`。
  - 文件工具只允许 project root 内，并 deny `data/msgs`、`data/state` 等前缀。
  - shell 输出写临时 runtime directory（`tempfile`），非业务持久化；backup 文件会在目标文件旁生成。
  - 本模块自身不保存 agent/task state。
- **网络副作用**：模型 API；GitHub API 只读/写；工具可执行任意被 schema 允许的网络型 GitHub 动作。另有本地 shell/subprocess 与文件系统副作用。
- **可先拆**：
  - retry classifier/backoff、valid-response 判定。
  - safe-path/deny policy、diff 构建、regex flags、unified-diff path normalize。
  - tool call/result DTO、read/write capability 分类、sub-batch planner、result budgeting/truncation。
  - `ModelCompleter`, `GitHubGateway`, `ShellExecutor`, `LocalFileGateway`, `RunFinishedNotifier` Protocol。
  - 工具 schema 定义与 executor registry；保持原 facade re-export。
- **必须留在原 owner**：
  - shell jobs/lock/temp dir/process cleanup 的单 owner。
  - ordered tool execution barrier 与并发上限。
  - run loop messages、工具结果追加顺序、context trim 时机、finish callback 时机。
  - 安全路径校验与实际写入必须处在同一可信 adapter，不能只把“校验结果”跨层传递后再写。
- **import-cycle 风险**：很高。当前 `agent_manager -> dev_agent`，`runtime -> 两者`。若 dev_agent 拆出模块后 import AgentManager/runtime，会形成直接环；公共 primitives 应下沉到不依赖 manager/runtime 的模块，再由两者共同依赖。
- **建议方向**：
  - `agent_types/policies/protocols`（最底层）
  - `local_file_tools`, `shell_tools`, `github_tools`, `tool_executor` adapters
  - `dev_agent` facade/run owner
  - `agent_manager` 复用低层模块，而不是长期从 `dev_agent` facade 导入 underscore 名称。
- **characterization tests**：
  - `test_agent_model_response_initialization.py` 全组。
  - `test_agent_parallel_tools.py` 全组：并行顺序、barrier、失败隔离、并发上限、GitHub 写串行、预算、retry、prompt 安全规则。
  - `test_agent_manager_integration.py::test_tasker_uses_ordered_executor_and_keeps_finish_callback`
  - `test_stage0_characterization.py::test_tool_schema_and_tool_result_shape` 间接锁定工具结果 shape。

### 2.7 `core/agent_manager.py`

- **行数**：1096。
- **公共入口**：`AgentManager`；创建/销毁/查询/状态/消息，loop/model/notifier/task 注册，report drain/peek/requeue，`send_to_agent`，`run_agent_loop`，`summarize_agent`。
- **入 imports**：asyncio/inspect/json/threading/time/uuid；`JsonStore`；从 `core.dev_agent` 导入 10+ 个事实公共 helper；console logger/bot logger。
- **出 imports**：`core/ai_runtime.py`、agent integration/stage 0 tests。
- **全局状态**：`AGENT_STATUSES`、`DEFAULT_AGENTS_STORAGE_PATH`、`INSTRUCTION_SUMMARY_LIMIT`；实例 owner 为 store、注入 queue map+RLock、pending reports+Lock、notifier、event loop、agent task map、model、flush timer task。
- **线程/锁/async 边界**：
  - 常驻 `run_agent_loop`、destroy/review/summary/flush timer 均为 async。
  - `send_to_agent` 可跨线程，通过 `loop.call_soon_threadsafe` 投递到 queue owner loop。
  - `_inject_queues_lock` 保护 queue check-create；`_pending_reports_lock` 保护 report list drain/requeue。
  - `asyncio.Queue.get()` 是 waiting/idle 挂起边界；task cancel 必须等待 finally 清理 shell。
  - 模型 sync complete 通过 `asyncio.to_thread`。
- **持久化路径**：默认 `data/msgs/agents_state.json`；messages、status、stage/review 字段持久化。queues/tasks/loop/model/reports/flush timer 都是内存态，重启重建或丢失。
- **网络副作用**：经模型 complete；经复用的 dev-agent executor 间接执行 GitHub/file/shell 工具；notifier 回调可触发上层投递。
- **可先拆**：
  - agent record/report/injected-message DTO 与 shape normalization。
  - status transition、done marker、instruction summary、review fallback/rendering等纯策略。
  - `AgentRecordStore`, `AgentModel`, `AgentToolExecutor`, `ReportSink`, `Clock` Protocol。
  - report formatting 与 summary prompt builder（纯函数）。
- **必须留在原 owner**：
  - queue map、event loop、task registry、flush timer 的生命周期。
  - report list+lock 的 drain/requeue 原子性和顺序。
  - status/messages/stage iteration 持久化顺序。
  - cancel → await task → shell finally → summarize → remove record 的销毁顺序。
  - review_required 恢复与上下文保留；waiting/idle 唤醒语义。
- **import-cycle 风险**：很高。当前依赖 dev_agent，runtime 同时依赖二者。第一步必须下沉共同 primitives；不能让 dev_agent import AgentManager，也不能把 DTO 放在 runtime。
- **建议方向**：`agent shared policies/protocols <- dev_agent adapters + AgentManager`；长期可让 manager 注入 executor，而不是导入 dev_agent 私有 helper，但兼容 facade 必须保留到 runtime/测试迁移的独立阶段。
- **characterization tests**：
  - `test_agent_manager_integration.py` 全组：并行工具持久顺序、无工具只读总结、review fallback/恢复、destroy 顺序、waiting 保留、tasker finish callback。
  - `test_stage0_characterization.py::test_agent_report_drain_and_requeue_preserves_order`
  - `test_stage0_characterization.py::test_public_entrypoints_and_signatures_are_importable`
  - `test_agent_parallel_tools.py` 间接锁定 manager 复用的 executor 契约。

### 2.8 `core/ai_runtime.py`（只读依赖参考）

- **行数**：6555。
- **公共入口**：`AIOrchestrator`；模块级 `_extract_code_language`、`has_code_block`、`split_code_block_segments`。
- **与本次目标文件的直接 imports**：
  - `AIRepository`
  - `LOOP_TOOL_NAMES`, `build_tools`
  - `run_dev_agent`, `MAX_ITERATIONS`
  - `AgentManager`
  - `ModelManager`
  - runtime 未直接 import `WebUIService`/`NapcatBot`，而是通过 transport、构造注入和 `main.py` 接线关联。
- **边界**：async event loop/thread、scope queues/locks、模型调用、task/agent/report 编排、transport、repository、toolbox 的 composition owner。
- **P5 规则**：
  1. 不改 imports，不改构造参数，不改调用点，不改任何方法。
  2. 新拆模块必须通过原文件 facade/re-export 保持 runtime 无感。
  3. 若 facade 无法保持兼容，任务包判定失败并回退，不得“顺手修 runtime”。
- **相关 characterization**：`test_stage0_characterization.py`、`test_scope_turn_coordination.py`、`test_pending_owner_equivalence.py`、`test_transport_contract.py`。这些用于发现拆分对 runtime 的间接破坏，不授权修改 runtime。

## 3. 建议依赖方向与 cycle 防线

1. **最低层**：DTO、TypedDict/Protocol、常量、纯 normalize/validate/select/serialize 函数；只依赖 stdlib。
2. **适配层**：JsonStore/ScopedMemoryStore、requests/websocket、GitHub/model、local file/shell；可依赖最低层，不能依赖 runtime/WebUI。
3. **领域 owner**：`AIRepository`、`ModelManager`、`AgentManager`、`DevAgent` run facade、`NapcatBot`、`WebUIService`；持有状态和生命周期。
4. **编排层**：`AIOrchestrator` 只消费 facade/ports；P5 中保持原样。
5. **composition root**：`main.py` 负责实例化和注入；不把实例构造下沉到纯模块。

重点 cycle 防线：

- `ai_repository` ← runtime/WebUI/toolbox，绝不反向。
- shared agent primitives ← `dev_agent` 与 `agent_manager`，二者都不得经 shared 模块互相反向导入。
- event/OneBot protocol ← `napcat` 与 `transport`；`napcat` 不 import transport/runtime。
- runtime port/DTO ← `webui_server`；port 定义不能放进 runtime。
- model config policy ← `model_manager`/WebUI；policy 不 import 两个 owner。

## 4. 互斥文件任务包（按优先级）

“互斥”指同一时刻一个包独占其 **允许写文件**；其他包不得同时修改相同 facade、共享新模块或测试。每个包都必须保持 `core/ai_runtime.py` 只读。优先级采用“先低风险纯层、后单 owner、最后跨线程/多副作用 owner”的既定顺序。

### P0 — characterization 补洞（先决条件，测试专包）

- **允许写**：仅新建/修改该包明确列出的 `test/test_*characterization*.py`（执行前另列精确文件名）。
- **只读**：全部生产代码。
- **内容**：WebUI route/config round-trip，repository diary/identity/unique-task，model config migration/save，NapCat parser/cache race 的当前行为。
- **退出条件**：测试在当前实现上通过；不得为了让测试通过改生产代码。

### P1 — `ai_tools_schema` 纯拆包

- **允许写**：`core/ai_tools_schema.py` + 一个专属新 schema 模块；对应专项测试文件。
- **禁止并行**：任何同时调整 runtime tools、toolbox 或 QQ request 工具的任务。
- **兼容要求**：`build_tools`、`LOOP_TOOL_NAMES` import 路径和返回 shape/顺序不变。

### P2 — `model_manager` 纯 policy/DTO 包

- **允许写**：`core/model_manager.py` + 专属 model config policy/types 新模块；对应测试。
- **禁止并行**：WebUI models config、runtime model fallback/switch、`core/test_command.py` 变更。
- **兼容要求**：`ModelManager` 仍是唯一运行时 config/counter owner；持久化路径和保存时机不变。

### P3 — `napcat` 解析/协议包

- **允许写**：`pack/napcat.py` + 专属 OneBot DTO/parser/protocol 新模块；对应测试。
- **禁止并行**：`core/transport.py`、runtime event handling、QQ request toolbox 变更。
- **兼容要求**：handler 次序、单线程 dispatch、HTTP 前自发消息占位、cache TTL、action 参数和返回值不变。

### P4 — `ai_repository` 纯策略包

- **允许写**：`core/ai_repository.py` + 专属 repository DTO/policy 新模块；对应测试。
- **禁止并行**：WebUI/relation/toolbox/memory/task/runtime state 任务。
- **兼容要求**：store update 原子区间、主 state 与 scoped memories owner、旧 JSON 可读性、dict shape 不变。

### P5 — `webui_server` serializer/port 包

- **允许写**：`pack/webui_server.py` + 专属 WebUI DTO/serializer/port 新模块；对应测试。
- **禁止并行**：ModelManager 配置 owner 迁移、runtime API 变更、main 接线、repository shape 变更。
- **兼容要求**：现有 constructor/start 公共入口、HTTP path/method/status/body、thread 模式不变。只允许定义 port 并让 WebUI 依赖它；不得要求修改 runtime。

### P6 — `dev_agent` 纯策略/DTO/Protocol 包

- **允许写**：`core/dev_agent.py` + 专属 agent policy/types/protocol 新模块；`test_agent_*`。
- **禁止并行**：AgentManager、runtime tasker、GitHub/file/shell 工具功能开发。
- **兼容要求**：所有当前被导入的 underscore helper 从原路径继续可用；工具顺序/barrier、预算、retry、路径安全、finish callback 不变。

### P7 — `dev_agent` adapter 包（文件/GitHub/shell 分包，逐个串行）

- **允许写**：每次只允许 `core/dev_agent.py` + **一个** adapter 新模块 + 对应测试；local-file、GitHub、shell 三个子包彼此也互斥。
- **禁止并行**：AgentManager 运行循环与任何工具 schema/安全策略变更。
- **兼容要求**：shell manager 仍单 owner；安全校验与写入不能被跨越；原 import 路径保留 facade。

### P8 — `agent_manager` 纯策略/DTO 包

- **允许写**：`core/agent_manager.py` + 专属 record/report/review policy 新模块；integration tests。
- **禁止并行**：dev_agent facade/helper 迁移、runtime agent report、持久化 schema 变更。
- **兼容要求**：AgentManager 公共方法、record shape、report FIFO、summary prompt/无工具保证、review marker 不变。

### P9 — `agent_manager` 运行 owner 包（最高风险，最后）

- **允许写**：`core/agent_manager.py` + 一个专属 runtime coordinator 模块 + integration tests。
- **禁止并行**：所有 dev_agent、runtime、report delivery、JsonStore、shutdown/restart 任务。
- **内容限制**：仅在 characterization 足够后移动 queue/task/loop/timer 协调；一次只移动一个 owner，禁止双写/双 task registry。
- **兼容要求**：跨线程投递、cancel/finally/shell cleanup、report drain/requeue、waiting/idle/review/error 状态时序完全一致。

## 5. 每包通用验收与停止条件

### 必验

1. 原文件公共 import 路径可用，stage 0 signature 测试通过。
2. 对应专项 characterization 全通过。
3. `test_stage0_characterization.py` 全通过。
4. 涉及 agent 时运行 `test_agent_model_response_initialization.py`、`test_agent_parallel_tools.py`、`test_agent_manager_integration.py`。
5. 涉及 NapCat/transport 时运行 `test_qq_request_management.py`、`test_transport_contract.py`。
6. 运行 scope/mailbox 回归以确认 facade 拆分未间接改变 runtime：`test_scope_turn_coordination.py`、`test_pending_owner_equivalence.py`。
7. grep/diff 确认 `core/ai_runtime.py` 未修改。

### 立即停止并回退

- 需要修改 `core/ai_runtime.py` 才能导入或运行。
- 出现 shared module 反向 import runtime/WebUI/具体 owner。
- 同一状态出现两个 writer/owner、双写、双 queue、双 task registry 或双配置缓存。
- repository 原子 `store.update` 被拆成非原子 load/save。
- shell 安全校验与实际文件/进程副作用被分到不同信任边界。
- HTTP/WS handler 顺序、线程模型、tool barrier、report FIFO、模型 fallback 索引发生行为变化。

## 6. 当前测试映射摘要

| 行为域 | 已有测试 | 覆盖判断 |
|---|---|---|
| 公共入口/signature | `test_stage0_characterization::test_public_entrypoints_and_signatures_are_importable` | 基线充分，细方法签名仍需专项锁定 |
| tool schema/result shape | stage 0 + QQ request tests | 基线可用 |
| model fallback/legacy tasker role | stage 0 model test | 核心路径有覆盖；migration/CRUD/save 不足 |
| agent retry/response/init | `test_agent_model_response_initialization.py`、parallel retry tests | 较充分 |
| agent 并行/barrier/budget | `test_agent_parallel_tools.py` | 较充分 |
| AgentManager 持久/状态/summary/destroy | `test_agent_manager_integration.py` | 核心路径较充分；跨线程实时竞态仍有限 |
| report FIFO | stage 0 report test | 有明确 characterization |
| old repository JSON | stage 0 old-state test | 仅兼容入口；领域细节不足 |
| NapCat request/action | `test_qq_request_management.py`、`test_transport_contract.py` | 协议主干有覆盖；真实 WS/HTTP 未覆盖 |
| WebUI HTTP | 无直接 route 黑盒测试 | 拆前必须补 |
| runtime scope owner | scope coordination、pending owner equivalence、mailbox/adapter tests | 用于间接回归；**不得据此授权改 runtime** |

---

结论：先抽取无状态叶子层，再移动单一副作用 adapter，最后处理 `dev_agent`/`agent_manager` 的运行 owner。所有拆分均应通过原文件 facade 保持 `core/ai_runtime.py` 无感；任何需要 runtime 接线的变化都不属于这些互斥文件任务包。
