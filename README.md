# LiveAi

> 单进程、分级 AI、scope 串行的 QQ 机器人运行时。
> 核心入口：[AIOrchestrator](core/ai_runtime.py)。

---

## 目录

1. [运行方式](#1-运行方式)
2. [整体架构](#2-整体架构)
3. [分级 AI 逻辑](#3-分级-ai-逻辑)
4. [消息接收 / 触发](#4-消息接收--触发)
5. [Turn 串行与 Mailbox](#5-turn-串行与-mailbox)
6. [工具调用协议](#6-工具调用协议)
7. [跨级 AI 协作](#7-跨级-ai-协作)
8. [工具循环并流（Live Drain）](#8-工具循环并流live-drain)
9. [消息回执与 Short ID](#9-消息回执与-short-id)
10. [TTS 引擎切换](#10-tts-引擎切换)
11. [SSH / 远程执行](#11-ssh--远程执行)
12. [已知 Bug / 性能问题](#12-已知-bug--性能问题)

---

## 1. 运行方式

```bash
python main.py
# 或
/my/bot.sh start
```

- `main.py` 是唯一生产入口
- `NapcatBot` 拥有 WebSocket 接收循环与主动 HTTP 发送
- `SatangyunModule`、`AIOrchestrator` 共享同一个 `NapcatBot` 实例
- **不要**再额外启动 receiver/sender 进程

仓库结构见 [docs/project_structure.md](./docs/project_structure.md)。

---

## 2. 整体架构

```
┌────────────────────────────────────────────────────────────┐
│  NapcatBot (WebSocket 接收 / HTTP 主动发送)              │
│      ↓ 事件入 enqueue                                      │
│  AIOrchestrator                                            │
│    ├── 1. 会话子 AI (per-scope)                            │
│    │     ├── 私聊/群聊/管理员 WebUI                         │
│    │     └── 接收 mailbox 事件                             │
│    │                                                        │
│    ├── 2. 主 AI (master scope)                             │
│    │     ├── 处理 notify_master                            │
│    │     └── 派发 delegate_to_child / followup_to_child   │
│    │                                                        │
│    └── 3. 后台任务层                                        │
│          ├── tasker (一次性任务)                            │
│          ├── dev_agent (常驻 agent)                        │
│          ├── ssh agent (远程 Shell)                        │
│          ├── message_scope / set_alarm / recurring         │
│          └── notify_master / delegate / child_report        │
└────────────────────────────────────────────────────────────┘
```

---

## 3. 分级 AI 逻辑

| 层级 | 名称 | 入口 | 职责 |
|---|---|---|---|
| L1 | 会话子 AI | [_process_message](core/ai_runtime.py) | 每个 scope 一份独立人格，处理日常聊天 |
| L2 | 主 AI | [_handle_notify_master](core/ai_runtime.py) | 跨 scope 协调，全局关系网，知识库 |
| L3 | 执行器 | `tasker` / `dev_agent` / `ssh agent` | 后台长任务、远程 Shell、一次性任务 |

**协作原则**：

- 子 AI 遇到跨 scope / 全局问题 → `notify_master`
- 主 AI 决策后可能 `delegate_to_child` 让别的子 AI 主动联系
- 子 AI 的执行结果通过 `child_report` 回到主 AI，主 AI 再决定是否回写原会话

---

## 4. 消息接收 / 触发

### 接收链路

```
NapcatBot WS 事件
    ↓
AIOrchestrator.register / handle_group_message / handle_private_message
    ↓
submit_message (去重)
    ↓
enqueue_message (清洗 @、过期消息过滤)
    ↓
should_trigger (触发判断)
    ↓
reserve_scope_turn (占 scope)
```

### 触发规则（[_should_trigger](core/ai_runtime.py)）

- **私聊**：默认触发
- **群聊**：必须 `@自己` 或命中 `trigger_words`
- 否则按 `trigger_rate` 随机触发
- 群聊命中后，5 秒内用户继续说会触发"自然续聊"

### 来源判定

[_message_source_kind](core/ai_runtime.py) 把消息分类为：

- `admin_webui`：管理员 Web 控制台
- `internal_task`：tasker / agent 内部上报
- `group` / `friend_private` / `group_temp_private` / `system_private`

`system_private` 一律忽略；其余进入触发判断。

---

## 5. Turn 串行与 Mailbox

### Scope 与 Turn

- `scope_key = "scope_type:scope_id"`，例如 `group:941124102`、`private:qq`、`master:0`
- 同一 scope **最多一个 active turn**（[_reserve_scope_turn](core/ai_runtime.py)）
- 忙时新事件进 mailbox，turn 结束 `_pop_next_live_pending_scope_turn`

### Mailbox 与 Pending Task

| 通道 | 来源 | 优先级 |
|---|---|---|
| mailbox | 用户消息、agent report、tasker 报告 | 消息级别 |
| pending task | `message_scope` / `delegate_to_child` / `followup_to_child` | mailbox 空时被 promote |

入口：[ScopeActorDispatcher](core/scope_actor_dispatcher.py)、[EventMailbox](core/event_mailbox.py)。

---

## 6. 工具调用协议

### 两类工具

| 类别 | 示例 | 入口 |
|---|---|---|
| 查询类 | `memory_search`、`web_search`、`view_image`、`list_agents` | [_run_ai_tool_call](core/ai_runtime.py) |
| 即时动作类 | `send_message` / `recall_message` / `notify_master` / `create_task` | [_execute_live_action_tool_call](core/ai_runtime.py) |

### 发送协议

- **普通文字不会发出去**
- **只有 `send_message` 工具的 `content` 真的发给用户**
- **不想说话时显式调用 `stay_silent`**
- 上述规则在 [_static_system_blocks](core/ai_runtime.py) 强制写入 system prompt

### 工具回执

每次工具执行的结果（成功或失败）都作为 `tool_result` 回填到 `model_messages`，模型立刻看到。

`send_message` 的回执格式：

```
已发送 N 条消息，message_id: xxx，短ID: #A1B2。发送内容：
<sent_text>
```

---

## 7. 跨级 AI 协作

```
子 AI 决策
    ↓ notify_master (工具调用)
主 AI 决策 (master scope 一次性 turn)
    ↓ delegate_to_child / followup_to_child
目标子 AI 决策
    ↓ child_report (回主 AI)
主 AI 综合 → 选择性回写原会话
```

**入口**：
- [_handle_notify_master](core/ai_runtime.py)
- [_handle_delegate_to_child](core/ai_runtime.py)
- [_handle_followup_to_child](core/ai_runtime.py)
- [_handle_child_report](core/ai_runtime.py)
- [_handle_message_scope](core/ai_runtime.py)
- [_handle_set_alarm](core/ai_runtime.py)

**主 AI 拿到的 child_report 是字符串**（不是结构化数据），主 AI 自行解析。

---

## 8. 工具循环并流（Live Drain）

### 核心问题

AI 在多轮工具调用期间可能继续收到新事件（用户消息、agent report、tasker 报告）。早期实现只在工具轮结束才把新事件并入下一轮，导致 AI"信息过期"。

### 现在的做法

每轮工具执行后，调用 [_drain_live_tool_scope_turn](core/ai_runtime.py)：

1. **drain 整个 mailbox**（一次性取出当前 scope 的所有新事件）
2. **去重**（[_dedupe_trigger_message_entries](core/ai_runtime.py)）
3. **拼接**到当前 `model_messages`，作为新 user segment 喂回 AI
4. **可见性区分**（[_render_pending_user_segment](core/ai_runtime.py)）：用户消息可见，agent report / tasker 报告包成 `user_invisible/tool_report`

### 收益

- ✅ AI 在长任务期间能看到 agent report
- ✅ 同一消息不会重复出现（live batch / pending fold reminder / rerun trigger 三处都去重）
- ✅ 真正实现"流式决策"而不是"批次决策"

---

## 9. 消息回执与 Short ID

### Short ID 机制

每条 outbound 消息生成 4 位 short ID（`#A1B2`），AI 用它来：

- `reply_to_id`：回复指定消息
- `recall_message`：撤回指定消息
- `view_image`：定位图片

入口：[annotate_message_refs](core/ai_runtime.py)、[lookup_message_ref](core/ai_runtime.py)。

### 跨进程限制

⚠️ short ID 存在内存 map，**进程重启后整张表重置**。重启前 turn 里 AI 引用的 `#A1B2` 在新进程里可能指代别的消息。

---

## 10. TTS 引擎切换

### 当前默认

`config.yaml` 里 `tts_provider: "cosyvoice"`（满穗 TTS 统一网关，默认 speaker `Sui_Full`）。

### 可用引擎

| provider | 后端 | 用途 |
|---|---|---|
| `cosyvoice` / `mansui_unified` | [MansuiUnifiedTxt2WavProvider](pack/txt2wav.py) | **默认**走 23458 网关 |
| `bert_vits2` / `vits_api` / `mansui_vits` | [BertVits2Txt2WavProvider](pack/txt2wav.py) | 老 vits-api 23456 |
| `fish_audio` | [FishAudioTxt2WavProvider](pack/txt2wav.py) | 鱼音云 |
| `tiax` | [TiaxTxt2WavProvider](pack/txt2wav.py) | tiax 在线 |

### 高级用法

```python
# 情绪控制
provider_options={'instruct': 'joyful and excited'}

# 零样本克隆
provider_options={'ref_audio_path': 'D:/ref.wav', 'ref_text': '参考文本'}

# 临时切回老引擎
provider_options={'engine': 'bert-vits2', 'id': 0, 'lang': 'zh'}
```

### 切换方法

只改 [config.yaml](config.yaml)：

```yaml
tts_provider: "cosyvoice"   # ← 改这一行
tts_reference_id: "Sui_Full"
tts_base_url: "http://127.0.0.1:23458/tts"
tts_model: "cosyvoice"
```

---

## 11. SSH / 远程执行

[core/dev_agent.py](core/dev_agent.py) 现在 **Paramiko-first**：

- 优先 Paramiko（密码 / 密钥）
- 失败回退系统 `ssh`（`shutil.which('ssh')`）
- 前台 `shell_exec` 统一走 `_run_ssh_command`，不再绕回旧路径

**实测保护**：
- `paramiko rc=0` 之后系统 ssh 不会再被打到
- `~` 家目录展开、绝对路径识别都已在路径解析层处理

---

## 12. 已知 Bug / 性能问题

### 已修复（最近轮次）

- ✅ 普通 agent report 不再因 scope busy 延后到 idle
- ✅ 工具循环批量 drain mailbox（不再只 pop 一条）
- ✅ 触发消息三处去重（live batch / pending fold reminder / rerun trigger）
- ✅ SSH 前台 `shell_exec` 走 Paramiko-first
- ✅ agent report 等待注入心跳噪音节流
- ✅ 流式响应显式 `response.close()`

### ❗ 仍存在

| 编号 | 类型 | 描述 | 风险 |
|---|---|---|---|
| Bug-A | 调度 | `set_alarm` / `message_scope` 任务和 mailbox 优先级策略不显式 | 任务可能被多轮消息压住抖动 |
| Bug-B | 幂等 | `send_message` 无动作级幂等（无 `recent-success ledger`） | 重复发送（已观察到） |
| Bug-C | 一致性 | short ID 进程重启后失效 | 跨进程恢复指错消息 |
| Bug-D | 死代码 | `_flush_agent_reports(only_if_idle=...)` 仍存在 | 代码债，未拆除老路径 |
| 性能-E | 计算 | mailbox `drain_scope` 每轮工具都重算 | 高 QPS 场景 O(N) 重算 |
| 性能-F | 工具 | 无已完成 tool_call 短路 | AI 误调重复工具浪费 token |
| 可靠性-G | 数据 | master 拿到的 child_report 是字符串 | 主 AI 误读风险 |

### 监控信号

如果出现以下情况，对照上表排查：

- 重复发消息 → 看 Bug-B
- 撤回失败/回复错对象 → 看 Bug-C
- 5 分钟任务很久不响应 → 看 Bug-A
- agent report 不再实时到达 → 看 Bug-D 是否被回滚

---

## 附录：关键文件索引

| 文件 | 角色 |
|---|---|
| [core/ai_runtime.py](core/ai_runtime.py) | 总编排器，AIOrchestrator |
| [core/agent_manager.py](core/agent_manager.py) | 常驻 agent 生命周期 |
| [core/agent_report_delivery.py](core/agent_report_delivery.py) | agent 上报投递服务 |
| [core/character_session.py](core/character_session.py) | 会话状态机 |
| [core/scope_actor_dispatcher.py](core/scope_actor_dispatcher.py) | scope actor 调度 |
| [core/scope_actor_registry.py](core/scope_actor_registry.py) | actor 注册表 |
| [core/scope_scheduler.py](core/scope_scheduler.py) | scope 调度器 |
| [core/event_mailbox.py](core/event_mailbox.py) | 事件 mailbox |
| [core/event_batch_coordinator.py](core/event_batch_coordinator.py) | 批量事件协调 |
| [core/event_envelope.py](core/event_envelope.py) | 事件信封 |
| [core/event_normalizer.py](core/event_normalizer.py) | 事件归一化 |
| [core/ai_repository.py](core/ai_repository.py) | 持久化 repo |
| [core/dev_agent.py](core/dev_agent.py) | dev_agent + SSH |
| [pack/napcat.py](pack/napcat.py) | NapCat 适配 |
| [pack/txt2wav.py](pack/txt2wav.py) | TTS 引擎 |
| [pack/anthropic_chat_model.py](pack/anthropic_chat_model.py) | Anthropic 协议模型 |
| [pack/chat_model.py](pack/chat_model.py) | 通用 chat 模型 |
| [data/prompt/agent.txt](data/prompt/agent.txt) | 子 AI 人格 prompt |
| [data/prompt/main.txt](data/prompt/main.txt) | 主 AI prompt |
