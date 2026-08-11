# LiveAi 特殊机制 / 隐藏行为速查

> 用途：把代码里那些**不属于通用框架、容易在未来重读时被当作"莫名其妙的 if"或"过时的逻辑"误改/误删**的小机制，集中记一份。
>
> 每条都给出：触发场景、当前行为、关键代码位置、踩过的坑/为什么这么写。
> 维护要求：任何"看起来像 workaround、很 hack、不放到这里没人解释得了"的小逻辑，都要回来补一条。

---

## 0. 阅读说明

- 所有"行号"以最近一次合入的 `core/ai_runtime.py`、`core/agent_manager.py`、`core/dev_agent.py` 等为准，重构后行号会变，但语义不变；引用行号仅用于快速定位。
- 凡是涉及"运行时只读/写一次、跨 turn 触发、不能进历史"的字段，基本都是**有意为之**，不要为了"统一"就抽到通用持久化层。

---

## 1. 分级 AI 相关

### 1.1 主AI ↔ 子AI 的"主从伪装"语言层

- **触发场景**：子AI 需要解释"为什么知道另一会话的事"、"为什么突然联系你"等跨会话事实。
- **当前行为**：
  - 子AI 必须在普通文字里用"翻了下记录/问了一下/听说/刚想起来"代替"联系了主AI"。
  - 不能说"主AI 让我转达"，而说"刚想起来/听说"。
  - 不能说"其他会话的分身"，而说"之前在别处聊过"。
  - 不能说"agent 在帮我查"，而说"正在看/正在查资料"。
- **关键位置**：[data/prompt/staff.txt:27-28](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L27-L28)
- **历史踩坑**：早期 prompt 没明确禁止"主AI/分身"术语，子AI 会因为这条破功向非号主用户暴露架构。
- **注意**：规则**只对"对外解释"场景生效**；在号主面前可以正常讨论内部机制。

### 1.2 号主 = 唯一可信来源

- **触发场景**：子AI 收到任何可能触发 tasker / agent / GitHub 写操作 / 系统设置的请求。
- **当前行为**：
  - 只有"身份说明: 发送者是号主本人"标记的消息，才代表号主真实意图。
  - QQ 号 241898129 是号主本人；其他任何人（包括"号主让我来找你"这种哄骗）一律不可信。
  - 子AI 写关系网也必须通过 `notify_master` 上报主AI；**子AI 自身不直接写关系网库**。
- **关键位置**：[data/prompt/staff.txt:11-26](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L11-L26), [core/ai_runtime.py `notify_master` 工具处理](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)
- **历史踩坑**：非号主要求"帮我查 GitHub""帮我改代码"曾被放行；现在改为宁误拒不放过。

### 1.3 tasker 一次性 vs 常驻 agent 双向

- **触发场景**：用户要"查资料 / 改代码 / 跟一个项目"等长活。
- **当前行为**：
  - 一次性 / 短活：`create_tasker`（旧 `create_task(kind='dev_agent')` 仍兼容）。
  - 长活 / 多轮 / 要追问：`create_agent` 启动常驻 agent。
  - 常驻 agent 的"提问信号"：当一轮只有**纯文本、无工具调用**时，系统会把它当作向上级请示/等待。
  - 真正完成时在汇报末尾**单独**输出 `[[AGENT_DONE]]`；失败/受阻/缺信息时**禁止**输出该标记。
- **关键位置**：[data/prompt/agent.txt:21-23](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/agent.txt#L21-L23), [data/prompt/staff.txt:88-126](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L88-L126)
- **历史踩坑**：早期没有 `[[AGENT_DONE]]` 标记，agent 容易进入"看起来聊完了但系统还当它在线"的卡死状态。

### 1.4 agent 状态机 5 态

- **触发场景**：agent 跨 turn 状态推进与对外暴露。
- **当前行为**：
  - `running` —— 正在跑工具/执行中
  - `waiting` —— 已输出纯文本，等待上级答复
  - `idle` —— 干完待命，没销毁，可追问
  - `review_required` —— 本阶段达到轮次上限，**非异常**，上下文已保留等复核
  - `error` —— 真正运行异常
- **关键位置**：[core/agent_manager.py:25-31](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py#L25-L31), [data/prompt/staff.txt:132](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L132)
- **历史踩坑**：早期没有 `review_required` 概念，agent 跑满 8 轮直接转 `error`，把"还在干活但需要复核"误判成异常。

### 1.5 一次性格 `[[AGENT_DONE]]` 之后的"提示性"人设回归

- **触发场景**：分级 AI 的 `send_message` 实际发送总字数 > 20。
- **当前行为**：
  - 在 `_apply_directive_tools` 里累计所有 `send_message` 工具调用的正文长度（去掉空 strip、去掉 `[[...]]` 控制段后）。
  - 超过 20 字就向 `_pending_send_message_persona_notices[scope_type:scope_id:agent_id]` 写一个"待提醒"标记。
  - 下一次 `_complete_child_turn` 跑起来时，**只**在"下一次模型调用前"对 `model_messages` 的最后一条 `user(tool_result)` 临时追加 `<notice>请回归人设发言！</notice>`。
  - 这一次性副本在用完后丢弃，**不**进入 `tool_context_messages`，**不**进入 turn log，**不**进入任何持久化层。
- **关键位置**：
  - 标记写：[core/ai_runtime.py:5418-5500](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L5418-L5500)
  - 标记消费 + 临时消息渲染：[core/ai_runtime.py:3279-3309](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L3279-L3309), [core/ai_runtime.py:5036-5077](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L5036-L5077)
  - live 链路也触发：[core/ai_runtime.py:5753-5754](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L5753-L5754)
- **历史踩坑**：早期直接把 notice 拼进 `model_messages` 持久化字段，结果变成了"每次都会被人设提醒"的长期污染；现在改为"运行时一次性内存标记 + 临时副本"，重启后自动清空。

### 1.6 思考强度 10 分钟自动归位

- **触发场景**：用户长时间没说话。
- **当前行为**：
  - 每次触发后启动一个 10 分钟定时器；到点把当前 scope 的思考强度重置回 `low`。
  - 子AI 可以主动用 `set_thinking_level` 切换 `off/low/medium/high`；切换会重置这个定时器。
  - 重启会回到 `off`，这是**有意**的（运行时开关，跨进程不持久化）。
- **关键位置**：[data/prompt/staff.txt:213-218](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L213-L218), [core/ai_runtime.py `_scope_thinking_levels` 字段](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)

### 1.7 情报轮（intelligence round）独立通路

- **触发场景**：主AI 定期召集各子AI 上报本会话最近情况。
- **当前行为**：
  - 这是一类**特殊 turn_meta**，`turn_kind=intelligence_round` / `intel_query` 之类。
  - 子AI 被召时**禁止**调用 `send_message`，必须用普通文字直接输出情报摘要回报主AI。
  - 主AI 还会定期把"情报摘要"回传给子AI，让子AI 消化；这条回传**也**不发给用户。
- **关键位置**：[core/ai_runtime.py:7198](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L7198), [data/prompt/staff.txt:220-231](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L220-L231)
- **历史踩坑**：早期没标记这种 turn，子AI 会当成普通用户问题而发消息给用户。

---

## 2. agent 上下文压缩 / 工具调用账本

### 2.1 历史压缩 120/60 + 保留最近 10 轮 tool_result 完整

- **触发场景**：agent 的 `messages` 数组 > `HISTORY_SUMMARY_TRIGGER_MESSAGES=120`。
- **当前行为**：
  - `_plan_history_compaction` 切掉中间段，只保留 head=1、tail=60。
  - 被切走的消息由 `_summarize_history_chunk` 用另一个模型生成摘要，写进 `history_summaries`。
  - 渲染时通过 `_render_agent_state_prompt` 把摘要、todo、note 一并注入到 system prompt 尾部。
  - `tool_result` 在 `_trim_old_tool_results` 里只对**保留窗口之前**的 10 轮前消息做 200 字截断 + "已截断"标注。
- **关键位置**：[core/dev_agent.py:39-43](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py#L39-L43), [core/dev_agent.py:347-378](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py#L347-L378), [core/dev_agent.py:4573-4585](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py#L4573-L4585)
- **历史踩坑**：早期没有这些阈值，单个 agent 跑 30+ 轮就把上下文撑爆，input:output 高达 50:3。

### 2.2 工具调用配对保护：必须保留完整 `tool_use/tool_result` 对

- **触发场景**：历史压缩的切口刚好落在 `assistant(tool_use)` 和下一条 `user(tool_result)` 之间。
- **当前行为**：
  - `_plan_history_compaction` 在切口处检查"是否把 tool_use 和 tool_result 切开"，如果切开就把切口前移一格，把这一对消息一起保留。
- **关键位置**：[core/dev_agent.py:362-378](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py#L362-L378), 测试 [test/test_dev_agent_context.py:44-72](file:///c:/Users/loliyc/Documents/Code/LiveAi/test/test_dev_agent_context.py#L44-L72)
- **历史踩坑**：agent 转 `error` 时上游返回 400，提示"tool_result 找不到上一条对应的 tool_use"；排查定位到压缩把一对消息拆开。该修复只防"以后再断链"，已存在的坏上下文仍要清掉或重建。

### 2.3 todo_write / note_write 独立状态字段

- **触发场景**：agent 需要把多步任务拆出来"防遗忘"，或记录"不能被总结稀释"的长期重点（安全警告、禁区、踩坑记录）。
- **当前行为**：
  - `runtime_state['todo_items']` 和 `runtime_state['notes']` 跟 `messages` 平级，独立持久化。
  - 在每轮模型调用前通过 `_render_agent_state_prompt` 注入 system prompt，**不**会被压缩掉。
  - todo 状态机：`pending / in_progress / completed / blocked`。
- **关键位置**：[core/dev_agent.py:467-560](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py#L467-L560), [core/dev_agent.py:512-560](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py#L512-L560), 渲染 [core/dev_agent.py:444-485](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py#L444-L485)
- **历史踩坑**：note 之前塞进 `messages` 里，结果被总结模型压成一句话，丢了"不能碰 .env"这种安全警告。

### 2.4 agent 启动恢复 = 重建模型客户端 + 拉起消费协程

- **触发场景**：进程重启后，SSH agent 之类常驻 agent 失联。
- **当前行为**：
  - `_restore_persisted_agents()` 遍历持久化的 agent 记录。
  - 按 `model_name` / role 重建模型客户端（可能走 tasker 角色模型）。
  - 重新 `asyncio.create_task(run_agent_loop(...))` 拉起消费协程。
  - 恢复后若状态是 `waiting` / `idle` / `review_required`，**直接挂起等待注入**，不会立刻调模型。
- **关键位置**：[core/ai_runtime.py `_restore_persisted_agents()`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py), [core/agent_manager.py run_agent_loop 启动分支](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py)
- **历史踩坑**：早期只持久化记录，没重建协程，agent 永远停在"持久化有记录 / 实际没人消费"的状态。

### 2.5 agent 的注入队列 vs 持久化

- **触发场景**：上级对 agent 追加指令、回答 agent 提问、纠偏。
- **当前行为**：
  - 注入用 asyncio.Queue（事件循环线程内），登记方是 run_agent_loop 那一侧。
  - 注入消费点：内层每轮开头（running 态）；外层 `queue.get()` 挂起处（idle/waiting 态）。
  - 跨 turn 的注入也会被 `append_message(agent_id, injected)` 落盘，避免丢消息。
- **关键位置**：[core/agent_manager.py:103-185](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py#L103-L185), [core/agent_manager.py:840-880](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py#L840-L880)
- **历史踩坑**：早期注入直接改 in-memory `messages`，没落盘，进程崩了注入就丢。

### 2.6 tool_result 字符预算（不是简单的固定阈值）

- **触发场景**：单个 turn 里一次回填多个工具结果。
- **当前行为**：
  - `_tool_result_limit(name)` 按工具名返回单工具上限（如 `read_local_file` 较宽，`shell_exec` 较紧）。
  - `_budget_tool_results` 用 sum + min(per_tool_caps) 二次分配，并触发 `_truncate_tool_result` 加 followup_hint。
- **关键位置**：[core/dev_agent.py:4398-4496](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py#L4398-L4496)
- **历史踩坑**：早期一刀切 2KB 截断，结果 read_local_file 读关键文件被砍掉片段，agent 误以为没读到。

---

## 3. agent 工具 / 文件沙箱

### 3.1 DENYLIST 前缀（tasker）

- **触发场景**：tasker 试图访问 `.env` / `data/msgs` / `data/state` 等敏感目录。
- **当前行为**：
  - `DENYLIST_PREFIXES = ('.env', 'data/msgs', 'data/state')`，命中后工具直接拒绝并提示换思路。
  - **不**递归到子目录之外。
- **关键位置**：[core/dev_agent.py:53](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py#L53)
- **注意**：agent manager 自身作为 bot 进程内代码，可以直接读写 `data/msgs/agents_state.json`，不受这条限制。

### 3.2 SSH agent 优先 Paramiko，回退系统 SSH

- **触发场景**：常驻 SSH agent 需要在远端执行 shell / 读文件。
- **当前行为**：
  - 复杂场景（端口转发、文件分块传输、交互式 shell）优先用 Paramiko。
  - 简单命令可以回退到本地 `ssh` 子进程。
  - SSH transfer 走分块 base64（默认 2MB/块，上限 16MB/块）。
- **关键位置**：[core/config.py `SSHProfileConfig`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/config.py), [core/dev_agent.py `SSHAgentShellManager` / `MAX_*_CHUNK_BYTES`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py)

### 3.3 shell 后台任务 + destroy_agent 自动清理

- **触发场景**：agent 起了 `tail -f` / 监听端口等长跑 shell。
- **当前行为**：
  - `DevAgentShellManager` / `SSHAgentShellManager` 记录所有后台 job。
  - `destroy_agent` cancel run_agent_loop，loop 的 finally 调 `shell_manager.shutdown()`，把所有后台 job kill 掉。
- **关键位置**：[core/dev_agent.py `shell_manager.shutdown()`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py), [core/agent_manager.py:389-432](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py#L389-L432)
- **历史踩坑**：早期 destroy_agent 只 cancel loop，shell job 还在跑，导致 ssh 进程泄漏。

---

## 4. 模型调用 / 协议兼容

### 4.1 Anthropic / OpenAI Chat / OpenAI Responses 三协议自动识别

- **触发场景**：上游可能是 Anthropic 协议，也可能是 OpenAI Chat Completions 或 OpenAI Responses。
- **当前行为**：
  - `messages_path` 包含 `/chat/completions` → OpenAI Chat Completions。
  - 包含 `/responses` → OpenAI Responses（GPT 模型优先）。
  - 否则 → Anthropic 协议。
  - `request.close()` 显式释放流式连接。
- **关键位置**：[pack/anthropic_chat_model.py:46-65](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/anthropic_chat_model.py#L46-L65)
- **历史踩坑**：早期忘记 `response.close()`，流式连接会一直挂着，把上游 quota 吃光。

### 4.2 stream_options / reasoning_effort 双层 fallback

- **触发场景**：中转站不支持某些"可选"字段（`stream_options`、reasoning 扩展）。
- **当前行为**：
  - 400/422 时**只对**body 里显式提到该字段时才回退重试，不会无脑重试。
  - reasoning fallback 后会重置 temperature / max_tokens。
- **关键位置**：[pack/anthropic_chat_model.py:139-181](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/anthropic_chat_model.py#L139-L181)
- **历史踩坑**：早期无差别重试，碰到真的 400 错误反而无限重试。

### 4.3 空响应/无效响应主动抛错

- **触发场景**：解析完发现 content 为空、且没有 tool_calls。
- **当前行为**：
  - 主动 `raise RuntimeError(...)`，让上层 `_call_with_retry` 走重试。
  - 不会静默返回空对象，避免上层误判"已经调成功了"。
- **关键位置**：[pack/anthropic_chat_model.py:242-244](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/anthropic_chat_model.py#L242-L244)

### 4.4 Anthropic extended thinking 强制 temperature=1.0

- **触发场景**：开了 `thinking_level`（medium/high）。
- **当前行为**：
  - Anthropic 协议下 `thinking` 字段被打开，temperature 必须为 1.0。
  - budget_tokens 按 level 映射，但低于 runtime 2048 输出上限。
- **关键位置**：[pack/anthropic_chat_model.py:114-121](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/anthropic_chat_model.py#L114-L121)

---

## 5. 消息 / 短ID / 撤回

### 5.1 短ID（message_ref）持久化分配

- **触发场景**：跨 turn 引用、撤回、view_image。
- **当前行为**：
  - 通过 `_register_persistent_message_ref` 在消息入库时分配 4 位短ID（base36 字母数字）。
  - 不能仅靠内存重算，否则跨进程或重启会失效。
  - 用户消息、AI 工具发送消息、agent 上报消息都走同一套分配。
- **关键位置**：[core/ai_runtime.py `_register_persistent_message_ref`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py), 测试 [test/test_message_refs.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/test/test_message_refs.py)
- **历史踩坑**：仅靠内存重算时，重启后历史消息的短ID 会变，导致 reply_to_id / recall 失效。

### 5.2 即时持久化（send_message 后立刻 append）

- **触发场景**：AI 调用 `send_message` 后。
- **当前行为**：
  - live 链路下，send_message 实际发出后立刻调用 `_append_outbound_message_now`，**不等** turn 结束。
  - 这样即使进程中途崩，消息也已经入库；turn log 不再回放时也不会丢。
- **关键位置**：[core/ai_runtime.py `_checkpoint_sent_entry`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)

### 5.3 self-sent 双层去重

- **触发场景**：NapCat 收到自己刚发的消息的事件回执。
- **当前行为**：
  - `_recent_message_keys` + `_recent_lock` 在事件入口做一次去重（带过期）。
  - NapCat 自身 action 层（`_sent_message_cache`）也做一次去重。
  - 两层都过才把消息往下游投递，避免"AI 刚发出去的话被当成新用户消息喂给自己"。
- **关键位置**：[pack/napcat.py `_sent_message_cache` / `send_text`](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/napcat.py), [core/ai_runtime.py `_recent_message_keys`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)
- **历史踩坑**：早期只有一层去重，遇到 NapCat 上报延迟时容易"AI 自问自答"。

### 5.4 Request 事件缓存（好友/群申请）

- **触发场景**：QQ 加好友/加群申请事件。
- **当前行为**：
  - NapCat 收到后缓存到 `_pending_request_events`，避免事件回调与 HTTP 轮询之间的竞态。
  - 子AI 通过 `qq_*_list` / `qq_*_approve` / `qq_*_reject` 工具消费。
- **关键位置**：[pack/napcat.py `_pending_request_events`](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/napcat.py)

---

## 6. TTS / 表情包 / 图像 / 转发

### 6.1 TTS 即时合成 + 落盘清理

- **触发场景**：`send_voice` 工具。
- **当前行为**：
  - 先调 TTS 生成 wav/mp3 → 立刻发出去 → 不管成功失败都把临时文件删掉。
  - speaker_id 支持 `1 / mansui / 满穗 / sui_best` 等本地满穗接口的别名。
- **关键位置**：[pack/txt2wav.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/txt2wav.py), [core/ai_runtime.py `_try_send_voice` 链路](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)

### 6.2 表情包：优先 `mface`，回退图片

- **触发场景**：`send_sticker` 发送收藏表情。
- **当前行为**：
  - 收藏列表结构化：`emoji_id` / `emoji_package_id` / `url` 三个字段都保留。
  - `send_sticker` 优先用 `emoji_id + emoji_package_id` 走 `mface` 消息段；拿不到才回退成 `image` 消息段。
  - 备注 key 优先用稳定 `emoji_id`，避免 URL 变了以后备注错位。
- **关键位置**：[pack/napcat.py `send_mface` / `fetch_custom_face`](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/napcat.py), [core/transport.py `send_mface`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/transport.py), [core/ai_runtime.py `send_sticker`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)
- **历史踩坑**：早期一律当图片发，QQ 显示成大图；用户问"为什么不是表情包"。

### 6.3 代码块：自动渲染成图片发送

- **触发场景**：`send_message` 内容含 ``` ... ``` 围栏。
- **当前行为**：
  - `split_code_block_segments` 切出 text / code 段。
  - code 段通过 `code2img` 渲染成 PNG → base64 走 `send_image`。
  - 任何渲染失败都回退到原样发文本（绝不吞消息）。
- **关键位置**：[pack/code2img.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/code2img.py), [core/ai_runtime.py `_try_send_code_image`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)
- **历史踩坑**：早期 code 段失败会直接吞掉；现在任何异常都先回退到原文本。

### 6.4 view_image：先沉默等解析，再决定

- **触发场景**：用户发图片、子AI 想理解内容。
- **当前行为**：
  - 第一轮子AI 可以保持沉默或只发"等一下"；等 `view_image` 拿到描述后再决定怎么回。
  - 描述由 vision_model（独立 OpenAI 兼容视觉模型）生成。
  - 支持按 `message_ref` 看历史图片，index 1-based。
- **关键位置**：[core/ai_runtime.py `_run_ai_tool_call` view_image 分支](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py), [data/prompt/staff.txt:41](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L41)

---

## 7. 群 / 私聊 / 关系

### 7.1 群消息合并 + 内部触发分流

- **触发场景**：同一条 turn 内累计的待处理事件。
- **当前行为**：
  - 真实用户消息按时间合并进同一个 `<user_msg>` 块。
  - 内部触发（agent 上报 / 主AI 协调 / 闹钟 / 循环任务）合并进 `<user_invisible>`，内部分独立 `<tool_report>`。
  - 块属性 `from/time/note` 用于触发轮定性提示。
- **关键位置**：[core/ai_runtime.py `_render_pending_blocks` / `_wrap_user_msg_block` / `_wrap_user_invisible_group`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)
- **历史踩坑**：早期把"agent 上报"和"用户消息"混在一段文字里，AI 容易回一句"好的"让用户困惑。

### 7.2 块体兜底防标签串

- **触发场景**：用户/AI 正文里恰好出现 `<user_msg>` / `<tool_report>` 等标签。
- **当前行为**：
  - `_sanitize_block_body` 把这些标签的起始 `<` 换成全角 `＜`，**仅**这几个标签，不做全量 XML 转义。
  - 保留其余代码/符号可读性。
- **关键位置**：[core/ai_runtime.py `_sanitize_block_body`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)

### 7.3 群临时会话 + 私聊 + admin 判定

- **触发场景**：判断是否允许写入类工具、关系网、admin 工具。
- **当前行为**：
  - admin 判定：`scope_type == 'master'` 或 `scope_type == 'private' and scope_id == admin_qq`。
  - group 才有群管理工具；只有 admin 才有 `validate_model_config` / `manage_knowledge_base` / `relation_write`。
  - 子AI 写关系网走 `notify_master`，由主AI 统一归档。
- **关键位置**：[core/ai_runtime.py `_allow_cfg`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py), [core/ai_tools_schema.py `build_tools`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_tools_schema.py)

### 7.4 关系网：写入只走主AI

- **触发场景**：子AI 想"记住某人是号主女朋友""某人是程序员"之类。
- **当前行为**：
  - 子AI 只能 `relation_lookup` / `relation_list` 查；写入必须 `notify_master`（JSON payload 里 `request_type: set_user_preference` / `relation_add_fact` 等）。
  - 子AI 收到这类工具请求直接拒绝。
- **关键位置**：[data/prompt/staff.txt:230](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L230), [core/ai_runtime.py relation_add_fact 分支](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)

---

## 8. 定时器 / 提醒

### 8.1 set_alarm / 一次性任务

- **触发场景**：用户说"X 分钟后提醒我"。
- **当前行为**：
  - `create_task(kind='set_alarm')` 入队。
  - 后台处理时若模型同一批还调用了 `send_message`，直接视为"已确认"，不再补发系统消息。
  - 闹钟/任务到点由 scheduler 派发到原 scope。
- **关键位置**：[core/ai_runtime.py `_handle_set_alarm`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py), [core/scope_scheduler.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/scope_scheduler.py)

### 8.2 create_recurring_task：常驻 agent 的"防卡死定时器"

- **触发场景**：`create_agent` / `create_ssh_agent` 之后。
- **当前行为**：
  - 系统 prompt **建议** AI 在创建 agent 后自动开一个 `create_recurring_task`，定时（如每 5~10 分钟）`peek_agent` 一下、把进度回报到当前会话。
  - 工作完成（或 destroy_agent 后）应主动 `delete_recurring_task` 关掉。
  - 多个 agent 同时进行可以**共用一个**定时器统一汇报。
- **关键位置**：[data/prompt/staff.txt:120,122](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L120), [data/prompt/staff.txt:122](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L122)
- **历史踩坑**：早期不要求开定时器，agent 偶发卡死后上级完全不知道。

---

## 9. NapCat 适配层特殊点

### 9.1 send_image 支持 base64://

- **触发场景**：渲染代码块生成的临时 PNG。
- **当前行为**：
  - `file` 字段可以是 `http://`、`file://`，也可以是 `base64://...`。
  - base64 内容由 NapCat 在它那一侧落临时文件再发。
- **关键位置**：[pack/napcat.py `send_image`](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/napcat.py)

### 9.2 recall_message 走 message_id（不是 message_ref）

- **触发场景**：撤回 2 分钟内自己发的消息。
- **当前行为**：
  - 上层把 `message_ref` 解码成 `message_id` 再调 NapCat。
  - 撤回后从本轮 `sent_entries` 剔除，避免收尾时再次补写进历史。
- **关键位置**：[pack/napcat.py `recall_message`](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/napcat.py), [core/ai_runtime.py `_execute_live_action_tool_call` recall_message](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)

### 9.3 send_file 走 NapCat HTTP 上传（不要求 NapCat 直访文件系统）

- **触发场景**：`send_file(path)` 工具。
- **当前行为**：
  - LiveAi 自己读 `path` → base64 → 通过 NapCat HTTP API 上传。
  - 适用于 LiveAi 与 NapCat 分机部署的场景（NapCat 没法直访 LiveAi 机器的磁盘）。
- **关键位置**：[pack/napcat.py `send_file`](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/napcat.py)

### 9.4 分机部署：HTTP 鉴权头

- **触发场景**：NapCat 启用了 access_token。
- **当前行为**：
  - `config.yaml` 的 `napcat.http_access_token` 会拼到 HTTP 头里。
  - WS 头里也带（具体看 transport 实现）。
- **关键位置**：[core/config.py `NapcatConfig.http_access_token`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/config.py), [pack/napcat.py HTTP 调用](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/napcat.py)

---

## 10. WebUI / 调试

### 10.1 turn_metadata 完整快照

- **触发场景**：WebUI 想看"这一轮发生了什么 / 用了什么工具"。
- **当前行为**：
  - `_turn_result_bundle` 强制所有返回路径都显式带 `turn_log_committed` 和 `turn_metadata`。
  - turn_metadata 含 agent_id / temperature / turn_meta / tool_iterations / generation_ms / note。
- **关键位置**：[core/ai_runtime.py `_turn_result_bundle`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py), 测试 [test/test_complete_child_turn_result.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/test/test_complete_child_turn_result.py)
- **历史踩坑**：早期不同 return 分支有的是字典有的是 tuple，WebUI 解析时不时崩。

### 10.2 `tool_context_messages` 优先于持久化 tool_calls

- **触发场景**：从聊天条目重建 turn 时。
- **当前行为**：
  - 优先用聊天条目里 `tool_context_messages`（含 assistant + user tool_result 配对）。
  - 没有再回退到持久化 tool_calls。
  - 这样可以保证"AI 看到的内容"和"实际跑的协议"严格一致。
- **关键位置**：[core/ai_runtime.py `_normalize_tool_context_messages`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py), [core/ai_repository.py 聊天条目构造](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_repository.py)

### 10.3 token 计量独立账本

- **触发场景**：WebUI 看"今天用了多少 token / 哪个 scope 耗得最多"。
- **当前行为**：
  - `TokenUsageStore` 独立于 AI state 文件存在（`data/msgs/token_usage.json`）。
  - 原子落盘（write-replace）。
  - 上游原生 `input_tokens` / `output_tokens` 优先；缺失时按 tiktoken 估算并打 `estimated` 标志。
- **关键位置**：[core/token_usage_store.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/token_usage_store.py), [pack/anthropic_chat_model.py `_extract_native_usage` / `_estimate_usage`](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/anthropic_chat_model.py)

---

## 11. prompt / 配置层

### 11.1 显式 Opt-in，不默认开启打扰

- **触发场景**：自动更新、定时汇报等"系统级自动行为"。
- **当前行为**：
  - `auto_update_enabled`、`create_recurring_task` 等都是显式手动开。
  - 系统不主动弹"今天要不要 X 一下"。
- **关键位置**：[core/config.py `auto_update_enabled`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/config.py), [data/prompt/staff.txt 定时器相关](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt)
- **设计意图**：避免默认开启的打扰性功能（用户偏好）。

### 11.2 数据文件 `data/msgs/` / `data/state/` 是禁区

- **触发场景**：tasker 试图读写运行状态。
- **当前行为**：
  - DENYLIST_PREFIXES 拦截所有 tasker 工具对这两目录的访问。
  - 运行时 bot 进程本身（agent manager、ai_repository）可以读写。
- **关键位置**：[core/dev_agent.py DENYLIST](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py)

### 11.3 资源生命周期：前端关则后端停

- **触发场景**：用户从 WebUI 退出 / 进程退出。
- **当前行为**：
  - `destroy_agent(agent_id, summarize=True)` 会 cancel loop、清理 SSH shell、回收 thread。
  - `main.py` 退出路径会 `agent_manager.shutdown_all()` + 等待 tasker 任务结束。
- **关键位置**：[core/agent_manager.py destroy_agent](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py), [main.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/main.py)

---

## 12. 杂项

> 这里是"重构时最容易被覆盖、改完没人会察觉"的角落。每条都要保留**为什么**，不要为了"看起来更统一"就抽掉。

### 12.1 `_message_epoch` 防止过期消息触发回复

- **触发场景**：用户说话后又撤回/重发，事件回执延后到达。
- **当前行为**：
  - 每次 `send_message` 完成、`destroy_agent` 触发等场景会 `_message_epoch += 1`。
  - 旧 epoch 的待处理事件被 `is_stale` 标记掉，不再触发新 turn。
- **关键位置**：[core/ai_runtime.py `_message_epoch` / `_is_epoch_stale`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py), [core/scope_actor_dispatcher.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/scope_actor_dispatcher.py)
- **历史踩坑**：早期不挡过期事件，AI 会"对着已经撤回的消息"回复，露馅。

### 12.2 `_pending_self_interrupts`：自我打断

- **触发场景**：AI 正在跑长工具时，外部又来新触发。
- **当前行为**：
  - 把新消息塞进 `_pending_self_interrupts[scope_key]`。
  - 当前 turn 跑完后**临时摘掉发送类工具**跑一轮（`forced_digest_round`），让 AI 先消化新消息再决策。
  - 这一轮的回复仅作为"已消化"标记，真正回复在下一轮恢复完整工具集后做。
- **关键位置**：[core/ai_runtime.py `forced_digest_round` 分支](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py), [core/ai_runtime.py `_build_self_interrupt_reminder`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)
- **历史踩坑**：早期没这个机制，AI 跑完长工具后已经忘了新消息，容易"自问自答"。

### 12.3 群回复窗口（_group_reply_windows）

- **触发场景**：群里别人在自顾自聊天，AI 容易被频繁触发。
- **当前行为**：
  - 维护一个短期窗口（典型几秒），统计本轮"非点名 / 非关键"触发的次数。
  - 超过阈值后抑制触发，**只**在被 @ 或显式"号主说话"时强制恢复。
- **关键位置**：[core/ai_runtime.py `_group_reply_windows`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py), [core/scope_actor_dispatcher.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/scope_actor_dispatcher.py)
- **设计意图**：人设要求"不要刷存在感"，这条机制是技术侧兜底。

### 12.4 持久化模板目录

- 聊天条目 / 任务 / agent / token 用量各自独立 JSON 文件，都用 `JsonStore`（write-replace 原子落盘）。
- 目录约定：
  - `data/msgs/ai_state.json` —— 聊天 / 任务 / 关系
  - `data/msgs/agents_state.json` —— 常驻 agent
  - `data/msgs/token_usage.json` —— token 计量
  - `data/recurring_tasks.json` —— 循环任务
  - `data/scripts/` —— 临时脚本
  - `data/tmp/` —— 临时文件
  - `data/logs/` —— 运行日志
  - `data/images/` —— send_local_image 唯一允许读取的目录（沙箱）

### 12.5 工具调用 `record_tool_use`：即时落盘防止崩溃丢失

- **触发场景**：任何 `send_message` / `remember` / `notify_master` / `create_task` / `create_tasker` / `set_alarm` 等"有副作用"的工具。
- **当前行为**：
  - `_execute_live_action_tool_call` / `_apply_directive_tools` 在执行完工具、**返回 tool_result 之前**就调用 `tools.record_tool_use(...)` 把"工具名 + 输入 + 结果"写到持久化工具日志里。
  - 进程中途崩掉时，下一轮恢复 turn 能从日志里反推当时发生了什么。
- **关键位置**：[core/ai_runtime.py `tools.record_tool_use`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py), [tool/ai_toolbox.py `record_tool_use`](file:///c:/Users/loliyc/Documents/Code/LiveAi/tool/ai_toolbox.py)
- **历史踩坑**：早期只在 turn 结束后统一写，崩了就全丢；现在 tool_use 即时落盘，崩了也至少能复盘。

### 12.6 fallback_prompted：re-prompt 只做一次

- **触发场景**：live 链路下模型没调 `send_message`，直接输出一段文字。
- **当前行为**：
  - 第一次发现时注入 re-prompt 提示，并把 `fallback_prompted=True`。
  - 第二次还是这样就**静默结束本轮**，不再 re-prompt（避免无限循环）。
- **关键位置**：[core/ai_runtime.py `fallback_prompted` 局部变量](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)
- **历史踩坑**：早期没有"只 re-prompt 一次"的保护，模型真忘了调工具时会无限循环耗 token。

### 12.7 openai_tool_guidance：只插一次的"该收尾了"提示

- **触发场景**：OpenAI 协议模型在 live 链路下反复调查询类工具、不调 `send_message`。
- **当前行为**：
  - 第一次工具结果回填后追加 `以上是工具执行结果...请调用 send_message...`。
  - 用 `openai_tool_guidance=True` 标记，后续不再追加。
- **关键位置**：[core/ai_runtime.py `openai_tool_guidance`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)
- **历史踩坑**：OpenAI 模型比 Anthropic 更"工具上瘾"，不打断就一直查；这条提示等价于一句温柔的"该收尾了"。

### 12.8 `MAX_CONSECUTIVE_API_FAILURES` 连续失败才转 error

- **触发场景**：agent 模型调用偶发空响应 / 超时。
- **当前行为**：
  - 连续失败 ≤ 3 次 → 指数退避（2^n 秒）后继续。
  - 超过 3 次才 `set_status(agent_id, 'error')` 并上报主AI。
  - 这样可以抗住一次性的上游抖动，不会一次失败就废掉整个 agent。
- **关键位置**：[core/agent_manager.py:806](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py#L806), [core/agent_manager.py:940-960](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py#L940-L960)

### 12.9 `_is_retryable_api_error`：哪些异常算"可重试"

- **触发场景**：模型调用抛 `RetryableAPIError` / 解析异常 / 空响应。
- **当前行为**：
  - 显式定义"可重试"集合，避免上层 try/except 一刀切重试。
  - 协议级解析错误（Anthropic/OpenAI 响应结构不匹配）算不可重试，立刻转 error。
- **关键位置**：[core/agent_manager.py `_is_retryable_api_error`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py), [core/dev_agent.py `RetryableAPIError`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py)

### 12.10 `_complete_with_valid_response` + `_call_with_retry`：模型层重试

- **触发场景**：模型首次返回空 / tool_calls 解析失败。
- **当前行为**：
  - `_complete_with_valid_response` 在 1 次调用内最多重试 1~2 次，优先换 temperature（更稳地拿 tool_calls）。
  - `_call_with_retry` 是外层退避重试，处理 5xx / 网络异常。
  - 两层**职责不重叠**，不要为了"统一"合并。
- **关键位置**：[core/dev_agent.py `_complete_with_valid_response` / `_call_with_retry`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py)
- **历史踩坑**：早期合并后，遇到 tool_calls 解析失败时会无限重试耗 token。

### 12.11 `_resolving_display_names`：昵称解析锁

- **触发场景**：同一条 turn 内多个事件要查同一个用户的昵称。
- **当前行为**：
  - 把"正在查昵称"的 user_id 放进 `_resolving_display_names` set。
  - 重复查直接复用结果，避免对 NapCat 的 HTTP 抖动放大。
- **关键位置**：[core/ai_runtime.py `_resolving_display_names`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)

### 12.12 `partial = mode == 'partial'`：图片描述工具的"部分解析"

- **触发场景**：`view_image` 第一次只拿到部分描述（比如超长图被截断）。
- **当前行为**：
  - 工具返回 `partial=true` 表示"还有后续内容"。
  - 子AI 可以选择再调一次 `view_image(index=...)` 拿剩余部分，也可以选择就当前内容先回答。
- **关键位置**：[core/ai_runtime.py `view_image` 工具](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py), [data/prompt/staff.txt:41](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L41)

### 12.13 `check_github_version` / `execute_update`：显式两步式更新

- **触发场景**：号主要"检查更新"或"现在更新"。
- **当前行为**：
  - `check_github_version` 只读，列出版本 / changelog。
  - `execute_update` 真的拉取 + 重启。
  - 这两个工具**必须分开调用**，AI 不能跳过 check 直接 update。
- **关键位置**：[core/ai_tools_schema.py `check_github_version` / `execute_update`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_tools_schema.py)
- **设计意图**：防"AI 看到 GitHub 提示就直接重启自己"。

### 12.14 `manage_mute` / `switch_agent_channel`：显式手动开

- **触发场景**：临时禁言某个 scope、临时切换 agent 工作通道。
- **当前行为**：
  - 都是显式手动调用，**不会**因为触发频繁自动开。
  - 与"10.1 群回复窗口"配合：mute 优先级最高；窗口抑制是软性的。
- **关键位置**：[core/ai_tools_schema.py `manage_mute` / `switch_agent_channel`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_tools_schema.py)

### 12.15 切协议时 `_normalize_anthropic_messages`：清掉中转站附加字段

- **触发场景**：上游中转站可能给 assistant / user 消息塞一些私有字段（`signature` / `cache_control` / `provider_metadata`）。
- **当前行为**：
  - `_normalize_anthropic_messages` / `_normalize_tool_result_content` 把消息压成"合法 Anthropic 形态"，**只**保留 type / role / content / tool_use_id 这些字段。
  - 避免把中转站私有字段回传导致上游再次报 schema 错误。
- **关键位置**：[pack/anthropic_chat_model.py:351-440](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/anthropic_chat_model.py#L351-L440)
- **历史踩坑**：早期保留原样字段，被某个中转站附带 `cache_control` 后，所有请求都报 400。

### 12.16 `model_manager.get_role_model`：按角色选模型

- **触发场景**：不同 AI（master / staff / tasker / agent）可能用不同模型 / 不同 base_url。
- **当前行为**：
  - `model_manager.get_role_model('tasker')` 返回 tasker 专用的 model config；找不到才回退到主模型。
  - `data/models_config.json` 维护这套映射。
- **关键位置**：[core/model_manager.py `get_role_model`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/model_manager.py), [data/models_config.json](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/models_config.json)
- **历史踩坑**：早期所有角色共用一个模型，tasker 跑大上下文直接打爆主模型额度。

### 12.17 `_recent_message_keys` 事件去重：双层去重的上一层

- **触发场景**：NapCat 偶尔会把同一条消息（按 message_id 算）回执两次。
- **当前行为**：
  - 用 `recent_message_keys[message_id] = time.time()` 存 5 分钟。
  - 命中直接 return，避免重复触发。
- **关键位置**：[core/ai_runtime.py `_recent_message_keys` / `_recent_lock`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)
- **配合**：见 5.3，NapCat 自己也有一层 `_sent_message_cache`。

### 12.18 `scope_dispatcher.active_actor_count` 限流

- **触发场景**：单 scope 同时塞进太多 turn。
- **当前行为**：
  - `AsyncExecutionPool` 维护一个信号量，scope 级别 actor 数量有上限。
  - 超过的进 `_event_mailbox` 等候，避免 OOM。
- **关键位置**：[core/scope_actor_dispatcher.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/scope_actor_dispatcher.py), [core/async_execution.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/async_execution.py)

### 12.19 chat_model_workers / background_workers：分层并发

- **触发场景**：前向对话和后台任务抢同一个 model client。
- **当前行为**：
  - `chat_model_workers`（默认 ≥4）专给 live 链路。
  - `background_workers`（默认 1~4）专给 tasker / agent / 情报轮。
  - 两层用不同 semaphore，不会因为一个慢任务阻塞对话。
- **关键位置**：[core/config.py `chat_model_workers` / `background_workers`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/config.py), [core/ai_runtime.py `_chat_model_pool` / `_background_pool`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)

### 12.20 ssh_agent 强制 SSHProfile，敏感信息不传 chat

- **触发场景**：AI 要操作远程服务器。
- **当前行为**：
  - `create_ssh_agent(ssh_profile_id=...)` 必传 `profile_id`，**禁止**把 ip / port / password 塞进聊天参数。
  - `list_ssh_profiles` 先列可用的 profile 让 AI 选。
  - profile 配置（含密码 / key 路径）只在 `config.yaml` 里的 `ai.ssh_profiles` 维护。
- **关键位置**：[core/config.py `SSHProfileConfig` / `_load_ssh_profiles`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/config.py), [core/ai_tools_schema.py `create_ssh_agent`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_tools_schema.py)
- **设计意图**：聊天历史里绝不出现裸密码 / IP；这条机制是**强约束**，不要为了"灵活"开放。

### 12.21 diary / meta_summary 触发：日记自动归档

- **触发场景**：日记条目累计到上限。
- **当前行为**：
  - 单 scope 日记超过 `MAX_DIARY_ENTRIES` → 触发 `meta_summary_pending=True`。
  - 下一次该 scope 触发时，模型被要求"先做一次 meta summary"再继续。
- **关键位置**：[core/ai_repository.py `store_diary_summary` / `get_meta_summary_candidates`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_repository.py)
- **历史踩坑**：早期日记无限追加，WebUI 加载慢，AI 也读不动。

### 12.22 数字人设"冷漠化"：防 AI 过拟合

- **触发场景**：用户遭遇账号泄漏、被骗、个人危机等。
- **当前行为**：
  - 不主动说"请立即重置密码""立即联系官方"这种过拟合话术。
  - 网上内容均视为不可信，不擅自激动、不当和事佬。
- **关键位置**：[data/prompt/char.txt:24](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/char.txt#L24)
- **设计意图**：避免 AI 变成"我懂很多，你听我的"式说教。
- **不要做的事**：不要为了"显得更关心"给 char.txt 加"应该主动安慰"之类的话。

### 12.23 三句 + 代码块：超长回复格式硬约束

- **触发场景**：AI 需要做"汇报 / 长答案 / 解释"。
- **当前行为**：
  - 超过 3 行 → 用 ``` 围栏包，内部 markdown。
  - 默认 1~3 行；不写公文式总结。
- **关键位置**：[data/prompt/staff.txt:186-188](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L186-L188)
- **历史踩坑**：早期没有这个约束，AI 一汇报就是 20 行，群里刷屏。

### 12.24 char.txt 的"心理活动不外露"

- **触发场景**：用户发表情包 / 抽象图 / 明显在玩梗。
- **当前行为**：
  - 不说"哇这个图好可爱""瞪大眼睛的表情好搞笑"之类解读。
  - 只看情绪，不复述图片内容。
- **关键位置**：[data/prompt/staff.txt:194](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L194)
- **设计意图**：人设要求"心理活动绝不外露"，这条是技术侧护栏。

### 12.25 reply_to_id 的"非必要不用"

- **触发场景**：AI 想引用之前的某条消息。
- **当前行为**：
  - 私聊/连续对话默认**不**带 reply_to_id。
  - 群聊、跨多条历史消息要明确指向时**才**带。
- **关键位置**：[data/prompt/staff.txt:39](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L39)
- **设计意图**：避免"每条都带引用"的客服腔。

### 12.26 `[CQ:reply,id=...]` 私聊自动剥离

- **触发场景**：AI 错误地构造了带 reply 的消息（在私聊里显得很怪）。
- **当前行为**：
  - `_send_text_lines` 在 `chat_type == 'private'` 时，发送前用正则把 `[CQ:at,qq=...]` 剥掉。
  - 群聊里仍然保留。
- **关键位置**：[core/ai_runtime.py `_send_scope_message` / `_send_text_lines`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py), [pack/napcat.py send_text](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/napcat.py)

### 12.27 "一句话多动作"主任务优先

- **触发场景**：用户一句话里同时说了"帮我查 A + 联系 B + 提醒 C"。
- **当前行为**：
  - 模型被要求**抓主任务**，其余分步处理。
  - 不要一口气把三个任务全启动，结果用户不知道哪个先好。
- **关键位置**：[data/prompt/staff.txt:166-167](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L166-L167)

### 12.28 "补全语义"理解

- **触发场景**：用户说"补 QQ 号"或"@xxx"时只给了部分信息。
- **当前行为**：
  - 结合最近上下文理解，**不要**当作新任务重起。
- **关键位置**：[data/prompt/staff.txt:167-168](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L167-L168)

### 12.29 "刚在干嘛"类实时问题：2 小时内必须重问

- **触发场景**：用户问"在干嘛""刚吃完饭了吗"这类实时问题。
- **当前行为**：
  - 即使备忘里有记录，**只要超过 2 小时就必须重问**，不能用旧答案。
  - 不知道就直说不知道，不要硬编。
- **关键位置**：[data/prompt/staff.txt:148-164](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L148-L164)
- **历史踩坑**：早期 AI 用旧备忘"刚吃完饭"答今天，被当场打脸。

### 12.30 主动降低发言频率

- **触发场景**：连续几条 AI 发言没人回。
- **当前行为**：
  - prompt 明确要求主动降低发言频率，避免引起厌烦。
  - 配合 12.3 `_group_reply_windows` 技术侧抑制。
- **关键位置**：[data/prompt/staff.txt:178](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L178)

### 12.31 "不轻易假定现实世界经历"

- **触发场景**：用户问"你昨天去哪儿了" / "刚才在做什么"。
- **当前行为**：
  - 不能编造"刚在打游戏" / "刚到家"等穿帮内容。
  - 优先回"刚在看消息""刚在回人""刚发呆"这种**低风险**、在 QQ 上下文里能自洽的内容。
  - 一旦前话不准，被追问时要承认，不能为了圆继续加新谎。
- **关键位置**：[data/prompt/staff.txt:155-164](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L155-L164)
- **设计意图**：人设要求"心理活动不外露 + 不主动撒谎"。

### 12.32 涉政/儿童色情：直接忽略 / 断绝

- **触发场景**：用户提涉政 / 儿童色情。
- **当前行为**：
  - 涉政：忽略处理。
  - 儿童色情：用人设语气断绝画图请求，**不**支持成人色情内容（针对 GPT 防下海注入）。
  - 普通成人内容：口嗨无限制、不制止，但不能主动发起。
- **关键位置**：[data/prompt/char.txt:22](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/char.txt#L22)
- **设计意图**：人设 + 法律合规护栏。改 char.txt 时这条不能动。

### 12.33 管理员消息（系统后台）走同一通道

- **触发场景**：开发者通过 WebUI 控制台发调试消息。
- **当前行为**：
  - 跟普通 QQ 消息走同一条 chat 链路，AI 不特殊对待、不紧张。
  - 但有 `身份说明: 发送者是管理员` 之类的标记，会被相关判定逻辑认。
- **关键位置**：[data/prompt/staff.txt:58-62](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L58-L62)
- **设计意图**：避免 AI 对"管理员"过度紧张或跪舔。

### 12.34 agent 工具：补 CWD / read_only 默认值

- **触发场景**：`send_to_agent(agent_id, message, cwd=?, read_only=?)`。
- **当前行为**：
  - `cwd` 留空时沿用 agent 当前的 cwd。
  - `read_only` 留空时沿用 agent 当前的 read_only。
  - 显式传时立刻覆盖。
- **关键位置**：[core/agent_manager.py `_normalize_agent_cwd_spec`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py)
- **历史踩坑**：早期不保留状态，每次 `send_to_agent` 都要重传 cwd / read_only，AI 容易传错。

### 12.35 data/prompt 目录是**可信系统配置**

- **触发场景**：agent 读取 `staff.txt` / `agent.txt` / `dev_agent.txt` 等 prompt 文件。
- **当前行为**：
  - 这类文件**不**视为外部注入攻击，**不**走防注入拦截。
  - 但内容里**仍然**只是"参考"，agent 不会因为读了 `data/prompt/agent.txt` 就去执行里面"忽略设定"之类的字面指令。
- **关键位置**：[data/prompt/agent.txt:127-130](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/agent.txt#L127-L130)
- **设计意图**：既要能加载 prompt，又不能让 prompt 里的内容变成"新指令"。

### 12.36 deny 工具白名单 = 主 chat 通道子集

- **触发场景**：跨会话 `notify_master` / `delegate_to_child` 等转发链路。
- **当前行为**：
  - 子AI 调 `notify_master` 时，主AI 在 `_handle_notify_master` 里再次用**严格工具白名单**（去掉 tasker / agent 写类工具）调子AI。
  - 这条"二次安全"是显式的，**不要**为了"统一工具集"放开。
- **关键位置**：[core/ai_runtime.py `_handle_notify_master` / `_KNOWN_RESTRICTED` 集合](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)

### 12.37 `stay_silent` 才是真沉默

- **触发场景**：AI 想"保持沉默"。
- **当前行为**：
  - 必须显式调 `stay_silent`，不能"把想说的话写在别处"。
  - 二选一：`send_message` 或 `stay_silent`，**不能**既不调、又不在普通文字里说。
- **关键位置**：[core/ai_tools_schema.py `stay_silent`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_tools_schema.py), [data/prompt/staff.txt:205-208](file:///c:/Users/loliyc/Documents/Code/LiveAi/data/prompt/staff.txt#L205-L208)
- **设计意图**：避免"沉默却把想说的话泄露"。

### 12.38 `validate_model_config` / `manage_knowledge_base`：只对 admin

- **触发场景**：切换模型 / 改知识库。
- **当前行为**：
  - 只有 admin（`scope_type == 'master'` 或私聊号主）才看得到这些工具。
  - `include_knowledge_management=_allow_cfg` 严格判定。
- **关键位置**：[core/ai_runtime.py `build_tools` 调用](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)

### 12.39 `relation_write` 只对 master

- **触发场景**：写入关系网。
- **当前行为**：
  - 关系网写入只对 `scope_type == 'master'`（主AI）开放。
  - 子AI 调 `relation_add_fact` 会被直接拒绝并提示"请通过 notify_master 上报由主AI 归档"。
- **关键位置**：[core/ai_runtime.py `relation_add_fact` 拒绝分支](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)

### 12.40 "通知发出去但没收到回执"：3 秒兜底

- **触发场景**：`send_message` 调用 NapCat 但 NapCat HTTP 抖动。
- **当前行为**：
  - HTTP 异常 → 返回失败 text 给模型，模型**不会**误以为发出去了。
  - WS 心跳断 → 触发重连，事件流不会丢（事件在 NapCat 侧缓存）。
- **关键位置**：[pack/napcat.py WS 重连 / HTTP 异常处理](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/napcat.py)

### 12.41 `_COMMAND_MASTER_QQ=241898129` 命令权限硬门

- **触发场景**：QQ 消息以 `/` 或 `#` 开头。
- **当前行为**：
  - 命令类消息**只**允许号主（QQ 241898129）触发，其他人发的命令一律静默 return。
  - 内部来源（tasker 汇报、admin_webui）不受这条限制，避免误伤自然聊天。
- **关键位置**：[core/ai_runtime.py `_COMMAND_MASTER_QQ` / `_is_command_master`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)
- **设计意图**：把"操作类指令"和"自然聊天"分开，避免群里任何人都能触发 `/restart` / `/clean` 这类高危命令。

### 12.42 `_is_admin_message` / `_is_master_message` / `_is_tasker_authorized` 三层权限

- **触发场景**：不同工具/动作的"谁能用"。
- **当前行为**：
  - `_is_admin_message`：user_id 等于 `config.admin_qq`。
  - `_is_master_message`：scope 是 master（主AI 自己）。
  - `_is_tasker_authorized`：私聊 tasker 只能由 admin 发起；群聊暂不限制。
  - 旧的 `_is_dev_agent_authorized` 是 tasker 的 legacy 别名（持久化 `kind=dev_agent` 仍要兼容）。
- **关键位置**：[core/ai_runtime.py:1807-1832](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L1807-L1832)
- **不要做的事**：不要把这三个判定合并成一个"权限判定函数"——它们的"作用对象"和"信任级别"都不一样。

### 12.43 `_normalize_task_kind` / `_task_kind_label`：持久化兼容映射

- **触发场景**：任务 `kind` 字段在持久化层、UI 显示层、AI 输入层之间流转。
- **当前行为**：
  - 内部持久化仍存 `dev_agent`（历史数据兼容）。
  - 用户/模型可见的术语用 `tasker`。
  - `_normalize_task_kind` 写入时把 `tasker` → `dev_agent`；`_task_kind_label` 显示时把 `dev_agent` → `tasker`。
- **关键位置**：[core/ai_runtime.py:1834-1843](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L1834-L1843)
- **不要做的事**：不要把持久化字段也改成 `tasker`，会破坏历史数据兼容性。

### 12.44 `/clean` / `/stop` / `/restart` / `/on` / `/off` 命令副作用

- **触发场景**：号主在私聊里发这些命令。
- **当前行为**：
  - `/clean` → 调 `repo.reset_all()` + 清空 `_recent_message_keys` / `_scheduled_alarm_ids` + 把 `enabled=True`。
  - `/stop` → `_cancel_active_requests()` + `os._exit(0)` 立即硬退。
  - `/restart` → `os.execv(sys.executable, [sys.executable, _main_script])` 原地 exec 重启。
  - `/on` / `/off` → 切 `config.enabled`；off 还会清掉所有 in-flight 请求。
- **关键位置**：[core/ai_runtime.py:1845-1899](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L1845-L1899)
- **设计意图**：号主能从聊天直接重置/重启系统，不依赖 SSH 登服务器。

### 12.45 `_chat_model_pool` / `_runtime_io_pool` / `_background_pool` 三套线程池隔离

- **触发场景**：前向对话、通用 IO、后台任务 抢同一个 executor。
- **当前行为**：
  - `chat_model_workers`（默认 ≥4）：专给 live 链路调模型。
  - `runtime_io_pool`（≥8）：通用 IO，包括 NapCat HTTP、文件读、shell 等。
  - `background_workers`（1~4）：专给 tasker / agent / 情报轮等后台。
  - 三套 `ThreadPoolExecutor` 命名不同（`liveai-chat-model` / `liveai-runtime-io` / `liveai-background`），互不抢线程。
- **关键位置**：[core/ai_runtime.py:209-217](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L209-L217), [core/async_execution.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/async_execution.py)
- **历史踩坑**：早期共用默认 executor，model 慢任务把 NapCat HTTP 拖死。

### 12.46 `_background_task_semaphore`：仅限 `task:` scope 的并发闸门

- **触发场景**：多个后台任务同时触发。
- **当前行为**：
  - `scope_key.startswith('task:')` 的任务在 `_consume_scope_item` 里走 `async with self._background_task_semaphore`。
  - 上限 = `config.background_workers`。
  - 普通 scope（group/private/master）的 turn **不**受这个闸门限制。
- **关键位置**：[core/ai_runtime.py:2309-2313](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L2309-L2313), [core/ai_runtime.py `_background_task_limit`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)
- **设计意图**：task 任务贵在调用 model，必须有上限；chat 任务可以放开（live 链路是用户体验）。

### 12.47 `max_iterations = 8 if live_message is not None else 6`：live 多 2 轮

- **触发场景**：`run_message_turn` 决定一轮 turn 最多循环几次。
- **当前行为**：
  - live 链路（用户正在等）：8 轮。
  - 离线链路（tasker/agent 上报）：6 轮。
- **关键位置**：[core/ai_runtime.py:5042](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L5042)
- **设计意图**：live 链路多给 2 轮，让 AI 有空间"消化中断 + 重新决策"。

### 12.48 `_scope_retry次数` / `_scope_current_model` 按 scope 跟踪

- **触发场景**：模型连续失败时切 fallback 渠道。
- **当前行为**：
  - 每个 scope 单独维护 `retry_count` 和 `current_model_name`。
  - 触发 fallback 之后，WebUI 的 `#status` 能看到当前用哪个 model / 重试了几次。
  - 字段名 `_scope_retry次数`（中文）是有意为之，避免和别的英文名冲突。
- **关键位置**：[core/ai_runtime.py:236-237](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L236-L237)
- **不要做的事**：不要重命名成英文——已经有很多持久化字段依赖这个名字。

### 12.49 `_scope_last_user_msg_at` / `_scope_silence_fired` 静默巡检

- **触发场景**：长时间没说话。
- **当前行为**：
  - 真实用户消息（`source_kind not in internal/admin/system_private`）到达时，记录 `_scope_last_user_msg_at[scope_key] = now`。
  - 巡检线程检查"超过 N 分钟没说话"的 scope，触发某种通知/汇报。
  - `_scope_silence_fired` 是"这一轮已发过巡检"标志，避免重复触发。
- **关键位置**：[core/ai_runtime.py:2073-2076](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L2073-L2076)
- **设计意图**：让"长时间静默的会话"被系统感知，但只在第一次触发时发汇报，不刷屏。

### 12.50 `_should_ignore_message` 屏蔽"系统/官方"消息

- **触发场景**：UID 10000（QQ 官方系统号）或昵称含"系统"的消息。
- **当前行为**：
  - 私聊场景下，`_message_source_kind == 'system_private'` 的消息**直接 return 不触发**。
  - 这是从"安全 + 减少噪音"两层考虑：官方通知不应当进入 AI 上下文。
- **关键位置**：[core/ai_runtime.py:2597-2598](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L2597-L2598)
- **不要做的事**：不要改成"只忽略 UID 10000"——昵称含"系统"的消息也是系统发的。

### 12.51 `_should_trigger`：私聊默认触发，群聊受 trigger_words + trigger_rate 共同决定

- **触发场景**：决定一条消息要不要进 AI turn。
- **当前行为**：
  - 私聊：默认全部触发（屏蔽 system_private 除外）。
  - 群聊：①被 @ 必触发 ②命中 `agent.trigger_words` 触发 ③否则按 `trigger_rate` 随机触发。
  - 群聊 hit/miss 都会进日志（`CAT_CHAT`），方便调试为什么 AI 没回。
- **关键位置**：[core/ai_runtime.py:2625-2644](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L2625-L2644)
- **设计意图**：把"被点名"和"主动搭话"分开；trigger_rate 给 AI 一定的"社牛度"可调。

### 12.52 `_clean_text` 双向 self_id 兜底

- **触发场景**：消息里带 `[CQ:at,qq=机器人id]`。
- **当前行为**：
  - 不仅匹配 `self.bot.self_id`，还把 `raw_data.get('self_id')` 也加进 self_ids 集合。
  - 这是为了应付"机器人小号/测试小号"event 上报的 self_id 跟配置不一致的情况。
  - 匹配后把整段 `[CQ:at,qq=...]` 去掉，不留痕迹。
- **关键位置**：[core/ai_runtime.py:2552-2560](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L2552-L2560)
- **历史踩坑**：早期只看 `bot.self_id`，小号/测试号触发时 at 标签删不干净。

### 12.53 `_build_self_interrupt_reminder` + `forced_digest_round`

- **触发场景**：AI 正在跑长工具时，外部又来新消息。
- **当前行为**：
  - 旧 turn 继续跑完，**不**打断。
  - 跑完后 `_pending_self_interrupts` 里的内容塞进下一轮 `model_messages`。
  - 下一轮的**第一轮**被临时摘掉发送类工具（`build_tools(include_message=False)`），让 AI 只"消化"，不发。
  - AI 消化完（重新走完整工具集）的下一轮才真正回复。
- **关键位置**：[core/ai_runtime.py:5055-5073](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L5055-L5073)
- **设计意图**：避免 AI 跑完长任务时忘了"期间收到的新消息"，又自顾自地说旧话题。

### 12.54 `_format_tool_result_content` mixed_batch 包裹 `<user_visible>`

- **触发场景**：一个 batch 里 `send_message` 跟其他工具同时调用。
- **当前行为**：
  - 当 `mixed_batch=True` 且工具是 `send_message` 时，把结果包成 `<user_visible>{result}</user_visible>`。
  - 这样模型能识别"这段是用户可见的内容"，避免和别的 tool_result 混在一起解析错。
- **关键位置**：[core/ai_runtime.py:3269-3273](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L3269-L3273)
- **不要做的事**：不要把 `<user_visible>` 改成别的标签——`<user_invisible>`、`<tool_report>` 都有同源语义。

### 12.55 `_send_chat_reply` 同步发送 + 异步落库

- **触发场景**：指令类快捷通道（`#help` / `#status` / `#notes` 等）。
- **当前行为**：
  - 同步 `bot.send_text(...)` 把消息发出去（用户立刻看到）。
  - `loop.create_task(self._record_outbound_message(...))` fire-and-forget 调度落库，**不阻塞**指令响应。
- **关键位置**：[core/ai_runtime.py:2323-2330](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L2323-L2330)
- **设计意图**：指令是"快通道"，宁可落库延迟 100ms 也要让指令响应 < 50ms。

### 12.56 `_agent_clients` 登记表：agent 运行态 model client 单独维护

- **触发场景**：`switch_agent_channel` / `switch_agent_model_binding` 切换 agent 模型。
- **当前行为**：
  - 持久化用 `model_binding`（不含 api_key）。
  - 运行态 `AnthropicChatModel` 实例存在 `_agent_clients[agent_id]`（in-memory，不落盘）。
  - 切换时**原子**更新 binding + 替换 client，保证新请求用新模型。
- **关键位置**：[core/agent_manager.py:558-570, 571-590](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py#L558-L570)
- **设计意图**：client 持有连接池，持久化只存元数据；切换时**必须**同时改两侧。

### 12.57 `send_to_agent` 跨线程 → `call_soon_threadsafe` 回主事件循环

- **触发场景**：会话 AI 线程要往 agent 的 `asyncio.Queue` 注入消息。
- **当前行为**：
  - 已经在 agent 所属事件循环里：直接 `put_nowait`。
  - 跨线程：用 `loop.call_soon_threadsafe` 调度回主循环。
  - 队列没事件循环登记（极少见）：退化为 `put_nowait`。
- **关键位置**：[core/agent_manager.py:617-690](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py#L617-L690)
- **设计意图**：`asyncio.Queue` **不是线程安全**的，跨线程 `put_nowait` 会破坏 `Future.set_result`，导致 wait 永远不被唤醒。

### 12.58 `_FAILURE_LINE_PREFIXES` 失败检测：禁止"假完成"

- **触发场景**：判定 agent 的纯文本"自报"是成功还是失败。
- **当前行为**：
  - `_looks_done`：含 `[[AGENT_DONE]]` → 判定完成。
  - `_looks_terminal_failure`：去掉 `[[AGENT_DONE]]` 后第一行命中 `失败/异常/error/未能/无法/中止/终止/受阻/阻塞/卡住/放弃` → 判定失败。
  - 失败时**不**打 `[[AGENT_DONE]]`，系统转 `error` 状态。
- **关键位置**：[core/agent_manager.py:1132-1168](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py#L1132-L1168)
- **设计意图**：让"看着像完成但实际受阻"的回复能被系统识别，避免 agent 假完成。

### 12.59 `_retry_sleep_seconds` 指数退避 + jitter

- **触发场景**：模型 API 调用失败后等待重试。
- **当前行为**：
  - `delay = base * 2^(attempt-1) + random.uniform(0, 0.35)`。
  - base = `API_RETRY_BASE_DELAY=1.2`，上限 `API_RETRY_MAX_DELAY=8.0`。
  - jitter 防止多个 worker 同时重试，**不要**去掉。
- **关键位置**：[core/dev_agent.py:263-266](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py#L263-L266)
- **设计意图**：避免"雪崩重试"打爆上游 quota。

### 12.60 `PARALLEL_*_TOOLS` 白名单：未知工具默认串行

- **触发场景**：agent 同时调多个工具。
- **当前行为**：
  - 只有显式列入 `PARALLEL_LOCAL_READ_TOOLS` / `PARALLEL_GITHUB_READ_TOOLS` 的工具允许并发。
  - 未知工具、未来新增工具**默认串行**，避免把潜在写操作误判为只读。
  - `MAX_PARALLEL_READ_SUB_BATCH=8` 是单 batch 上限。
- **关键位置**：[core/dev_agent.py:174-225](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py#L174-L225)
- **设计意图**：白名单比黑名单安全；新工具默认安全。

### 12.61 `TOOL_RESULT_LIMITS` 按工具名差异化字符预算

- **触发场景**：回填 `tool_result` 给模型前。
- **当前行为**：
  - `read_local_file` 36KB；`list_local_files` 16KB；`shell_exec` 36KB。
  - `MIN_TOOL_RESULT_CHARS=2_000` 是单项下限。
  - `MAX_TOOL_BATCH_RESULT_CHARS=80_000` 是整批上限。
- **关键位置**：[core/dev_agent.py:226-240](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py#L226-L240)
- **历史踩坑**：早期一刀切 2KB 截断，`read_local_file` 读关键文件被砍，agent 误以为没读到。

### 12.62 `RetryableAPIError` 自定义异常 + `_is_retryable_api_error` 关键字分类

- **触发场景**：决定哪些异常走重试路径。
- **当前行为**：
  - 自定义 `RetryableAPIError` 永远可重试。
  - 其他异常按关键字分类（timeout/429/5xx/network/空内容/...）→ 可重试。
  - 协议级解析错误（JSON schema 不匹配）**不可**重试，立刻转 error。
- **关键位置**：[core/dev_agent.py:243-260](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py#L243-L260)
- **设计意图**：避免上层 try/except 一刀切重试，把 schema 错误无限放大。

### 12.63 `set_blocking_runner` / `_BLOCKING_RUNNER` 全局注册后台 IO 排程器

- **触发场景**：`dev_agent` 内部所有 `asyncio.to_thread(...)`。
- **当前行为**：
  - `set_blocking_runner(runner)` 把后台线程池的 `run` 注册到 dev_agent 全局。
  - `_run_blocking` 优先用 runner，runner 不存在时退回 `asyncio.to_thread`。
  - 这样 dev_agent 的所有同步 IO 都走 background_pool，不污染主事件循环。
- **关键位置**：[core/dev_agent.py:159-169](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py:227,248](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/dev_agent.py#L159-L169)
- **设计意图**：让 tasker 内部所有同步阻塞 IO 都走 background_pool。

### 12.64 `create_unique_task`：任务去重

- **触发场景**：号主重复触发"建一个 X 任务"。
- **当前行为**：
  - `create_unique_task(source_agent, kind, payload, dedupe_keys=[...])`：在同 source_agent + kind + 相同 dedupe_keys 的 active 任务存在时**直接返回**已有 task，不创建新的。
  - 避免"号主连发 3 句'叫醒我'就建 3 个闹钟"。
- **关键位置**：[core/ai_repository.py:992-1013](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_repository.py#L992-L1013)
- **不要做的事**：不要把 dedupe 范围扩大到所有 scope——只限同一 source_agent + kind。

### 12.65 `scoped_memory_store` 按 scope 拆分 JSON

- **触发场景**：写一条 chat message / note / turn log。
- **当前行为**：
  - 每个 scope 独立成 `<scope_type>__<scope_id>.json`。
  - 写一个 scope 只 deepcopy + 落盘该文件，**不**重写其他 scope。
  - `_SAFE_RE` 把 scope_id 里非 `[A-Za-z0-9_-]` 字符替换成下划线，防止路径穿越。
- **关键位置**：[pack/scoped_memory_store.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/scoped_memory_store.py)
- **历史踩坑**：早期所有 scope 塞 `ai_state.json`，大群多时单文件 100MB+，IO 卡死。

### 12.66 `_upsert_scope` 单用户最多 20 个 scope 接触记录

- **触发场景**：用户跟 AI 在很多群/私聊都说过话。
- **当前行为**：
  - `user_profile['scopes']` 数组，按 `last_seen` 排序，超过 20 个就 trim。
  - 这样 AI 知道"这人最近活跃在哪些群"，但不会无限增长。
- **关键位置**：[core/ai_repository.py:391-402](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_repository.py#L391-L402)
- **不要做的事**：不要把上限改成 100+——会拖慢 AI 启动。

### 12.67 `_empty_user_profile` 用户档案 schema

- **触发场景**：`touch_user_identity` 给一个全新 user_id 建档案。
- **当前行为**：
  - 字段：`user_id / aliases / scopes / facts / province / impression / attributes / created_at / updated_at`。
  - `province`（省份）、`impression`（主AI 长期印象）、`attributes`（任意键值情报）都是约定字段。
- **关键位置**：[core/ai_repository.py:365-378](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_repository.py#L365-L378)
- **不要做的事**：不要把 `impression` 删掉——主AI 长期印象只能从这里读。

### 12.68 `add_user_fact` / `update_user_intel`：写入 user 情报的事务性

- **触发场景**：关系网里追加事实。
- **当前行为**：
  - `add_user_fact`：append 进 `facts` 列表，带 `source_scope_type / source_scope_id / source_agent / created_at` 完整溯源。
  - `update_user_intel`：批量更新 `province / impression / attributes`，整段写回（原子）。
- **关键位置**：[core/ai_repository.py:419-503](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_repository.py#L419-L503)
- **历史踩坑**：早期 `add_user_fact` 不带 source，关系网里一堆"不知道谁说的"。

### 12.69 `_handle_notify_master` 二次工具白名单

- **触发场景**：子AI 调 `notify_master` 唤醒主AI。
- **当前行为**：
  - 主AI 在 `_handle_notify_master` 里用**严格工具白名单**（去掉 tasker / agent 写类工具）调子AI。
  - 这是显式的"二次安全"，避免主AI 在被通知时再调高危工具。
- **关键位置**：[core/ai_runtime.py `_handle_notify_master` / `_KNOWN_RESTRICTED`](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py)
- **设计意图**：通知路径**只**是用来读情报、调普通工具；不能借此跳号主授权。

### 12.70 `turn_log_slim`：turn_log 精简（不存完整 model_messages）

- **触发场景**：每轮 turn 结束落 `turn_log`。
- **当前行为**：
  - `model_messages` 完整内容**清空**，只保留 `model_messages_dropped` 计数。
  - 取 `mm[1].content` 前 240 字当 `preview`。
  - `tool_iterations` 内字符串字段 2000 字截断，结构保留。
  - 每个 scope 最多保留 20 条 turn_log（`TURN_LOG_LIMIT`）。
- **关键位置**：[core/turn_log_slim.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/turn_log_slim.py)
- **历史踩坑**：早期每条 turn 存完整 model_messages，单条 200KB+，WebUI 加载慢。

### 12.71 `BotLogger` 环形缓冲 + 优先级过滤

- **触发场景**：开发/号主通过 `query_logs` 看日志。
- **当前行为**：
  - 内存 deque 默认 10000 条上限；落盘到 `data/logs/bot_debug.jsonl`。
  - `priority` 参数：0=全量；1=API 只留 warn/error；2=所有 info 滤掉；3=只看 agent；4~5=只看 chat。
  - scope_key 二次过滤。
- **关键位置**：[core/logger.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/logger.py)
- **设计意图**：调试时不让日志把上下文撑爆，priority 是分级的"先看哪个分类"。

### 12.72 `_dispatch_executor` + `on_shutdown`：NapCat 事件分发线程池关闭顺序

- **触发场景**：`Napcat.start()` 退出路径。
- **当前行为**：
  - 退出时先倒序调所有 `on_shutdown` 注册的回调（保证最后注册的先收尾）。
  - `_dispatch_executor.shutdown(wait=False, cancel_futures=True)`：不等任务完成、取消未开始的任务。
- **关键位置**：[pack/napcat.py:573-593](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/napcat.py#L573-L593)
- **不要做的事**：不要把 `wait` 改成 `True`——退出会被慢任务卡住。

### 12.73 `request_friend_add` / `request_group_join` 主动 `NotImplementedError`

- **触发场景**：AI 错误地调"主动加好友 / 主动入群"。
- **当前行为**：
  - 这两个方法**直接 raise NotImplementedError**，根本不发 HTTP 请求。
  - 防止 OneBot 不支持的接口被误调后，NapCat 那边"看起来成功实际是 None"。
- **关键位置**：[pack/napcat.py:462-486](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/napcat.py#L462-L486)
- **设计意图**：fail-fast > 静默失败。AI 收到 `NotImplementedError` 立刻知道"不能做"。

### 12.74 `get_friend_requests` 双数据源 + warning 字段

- **触发场景**：AI 看"谁要加我好友"。
- **当前行为**：
  - 同时返回 `event_cache`（WS 实时缓存）+ `doubt_requests`（HTTP `get_doubt_friends_add_request`）。
  - HTTP 不可用时带 `warning` 字段提示，**不**整体失败。
  - 跟 `get_group_requests` 的 `event_cache + invited/join` 同模式。
- **关键位置**：[pack/napcat.py:465-503](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/napcat.py#L465-L503)
- **历史踩坑**：早期只读 HTTP，事件流和 HTTP 轮询竞态导致申请重复出现。

### 12.75 `_pending_self_sent` + `_recent_self_sent_ids` NapCat 自去重双 TTL

- **触发场景**：自己发消息的回显和事件回执的竞态。
- **当前行为**：
  - `_pending_self_sent`：发送**前**占位（chat_type + target + 内容 digest），TTL 短（防止 HTTP 响应和 WS 事件交叉）。
  - `_recent_self_sent_ids`：发送**后**用 message_id 记下来，TTL 长（防止 NapCat 重复回执）。
  - 两层 TTL 不一样，分管"窗口期"和"长期"。
- **关键位置**：[pack/napcat.py `_pending_self_sent` / `_recent_self_sent_ids`](file:///c:/Users/loliyc/Documents/Code/LiveAi/pack/napcat.py)
- **不要做的事**：不要把两层合并成一层——前者堵的是"WS 先到 HTTP 后到"，后者堵的是"NapCat 自身重复回执"。

### 12.76 `migrate` 旧 config 格式 + `dev_agent` 兼容：模型配置自动升级

- **触发场景**：`data/models_config.json` 是老版格式（`channels` 里含 `base_url`），或没有 `tasker` role。
- **当前行为**：
  - 启动时 `_load_and_migrate` 跑一次，自动转成新格式（upstreams + channels + roles 三层）。
  - 持久化的 `tasker` 缺失时，从 `dev_agent` 拉过来当 legacy alias。
  - 迁移完写回磁盘，**不会**每启动都重写（除非用户又改）。
- **关键位置**：[core/model_manager.py:60-129](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/model_manager.py#L60-L129)
- **设计意图**：配置文件 forward compat，老用户不用手动改 json。

### 12.77 `notify_failure` fallback 切换：失败时切下一个 model

- **触发场景**：当前 model 调用失败，渠道策略是 `fallback`。
- **当前行为**：
  - `_fb_indexes[channel_name] = (cur + 1) % len(models)`。
  - 下一轮 `_pick_model_from_channel` 自动选下一个。
  - `random` / `roundrobin` 策略**不**触发这个切换。
- **关键位置**：[core/model_manager.py:236-250](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/model_manager.py#L236-L250)
- **设计意图**：fallback 策略才有"切下一个"语义，random / roundrobin 切换是反语义的。

### 12.78 `_new_agent_id` / task_id / note_id 全部用 `uuid.uuid4().hex[:12]`

- **触发场景**：建 agent / task / note / tool_log / turn_log。
- **当前行为**：
  - 12 位 hex（48 bit）全局唯一，前缀短好读、好引用。
  - 持久化字段都用这个 ID 关联，不依赖数据库自增。
- **关键位置**：[core/agent_manager.py:300-302](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py#L300-L302), [core/ai_repository.py:344-353](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_repository.py#L344-L353)
- **不要做的事**：不要换成自增 int——会破坏跨进程引用稳定性。

### 12.79 `_event_mailbox` 序列号 + scope 分桶

- **触发场景**：`submit_event` 把新事件入 mailbox。
- **当前行为**：
  - 每个 `MailboxEntry` 带 `mailbox_sequence`（单 mailbox 内自增，运行时序）。
  - 同一 scope_key 的事件按入队顺序处理；不同 scope 互不干扰。
  - `drain_scope` 一次性取空；`pop_scope` 一次只取一条。
- **关键位置**：[core/event_mailbox.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/event_mailbox.py), [core/event_envelope.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/event_envelope.py)
- **设计意图**：mailbox_sequence 是**运行时**排序元数据，**不**当持久化游标用。

### 12.80 `ScopeActorRegistry` 一个 key 一个 task + Event 唤醒

- **触发场景**：scope 第一次来事件 / 当前 task 跑完在等。
- **当前行为**：
  - `ensure(key)`：没 task 就 `asyncio.create_task(consumer(key, event))`；有 task 且没 done 直接复用。
  - `wake(key)`：把 event `set()`，让 `await event.wait()` 醒来。
  - done_callback 自动清掉 task + event 登记。
- **关键位置**：[core/scope_actor_registry.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/scope_actor_registry.py)
- **设计意图**：避免一个 scope 重复建协程；event 模式比轮询省 CPU。

### 12.81 `_runtime_io_pool` ≥8 workers：通用 IO 永远不能成瓶颈

- **触发场景**：NapCat HTTP、文件读、shell 启动等所有"调外部世界"的 IO。
- **当前行为**：
  - 至少 8 个 worker（用 `max(8, chat_model_workers)`），不够就 fallback 到 chat_model_workers。
  - **不**和 chat_model 抢线程——三套 pool 严格隔离。
- **关键位置**：[core/ai_runtime.py:212-214](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L212-L214)
- **设计意图**：所有外部 IO 都是"非模型但要网络"，不能和模型抢，否则模型慢时 NapCat 也会卡。

### 12.82 `model_completion_service.snapshot`：调用前锁住 client

- **触发场景**：agent 跑 turn 时调模型。
- **当前行为**：
  - 每次 `complete` 前 `snapshot()` 拿一个冻结的 `ModelRequestSnapshot(client, model_name, api_url)`。
  - 切换模型时**已经在执行的请求继续用旧 client**；新请求才用新 client。
  - 避免"请求发到一半 client 被换"导致 response 解析错。
- **关键位置**：[core/model_completion_service.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/model_completion_service.py)
- **设计意图**：模型切换是"未来某次生效"的事，不破坏当下正在跑的请求。

### 12.83 `_summary_text` + `_SUMMARY_MAX_CHARS`：agent 自我总结

- **触发场景**：agent 阶段复核 / destroy_agent summarize。
- **当前行为**：
  - 用一个**无工具权限**的总结 AI 对 messages 做总结。
  - `_SUMMARY_MAX_CHARS=MAX_CONTEXT_CHARS=120_000` 上限。
  - 总结模型不可用时降级到 `_fallback_review_material`（确定性文本拼装）。
- **关键位置**：[core/agent_manager.py:1383-1307](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/agent_manager.py#L1383-L1307)
- **设计意图**：总结 AI 不能调工具，是"只读复核"；失败有兜底。

### 12.84 `data/agent_work/` 目录：tasker / agent 的工作沙箱

- **触发场景**：tasker / agent 写的中间产物。
- **当前行为**：
  - 不在 DENYLIST_PREFIXES 里，tasker / agent 可正常读写。
  - 推荐用于"agent 跑测试 / 写中间报告 / 临时构建产物"。
- **关键位置**：[docs/project_structure.md](file:///c:/Users/loliyc/Documents/Code/LiveAi/docs/project_structure.md), [core/config.py](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/config.py)
- **设计意图**：和"项目源码"分开，tasker 不会污染仓库。

### 12.85 `group/` 和 `my/` 目录：legacy 兼容目录

- **触发场景**：历史测试输出 / 个人资料落在根目录。
- **当前行为**：
  - 核心运行逻辑**不**强依赖这两个目录。
  - 新功能**不要**继续向这两个目录扩散。
- **关键位置**：[docs/project_structure.md](file:///c:/Users/loliyc/Documents/Code/LiveAi/docs/project_structure.md)
- **设计意图**：保留兼容但不演进；未来整理时再决定保留 / 迁出。

### 12.86 `_aggressive_take_message_only_once` / 触发词 trace 日志：群触发可视化

- **触发场景**：群里 AI 触发 / 不触发的判断。
- **当前行为**：
  - 每次判定都打 `CAT_CHAT` 级别日志：触发原因（@ / 触发词 / 随机概率 / private）。
  - 不触发也打 debug 日志。
  - 调试时用 `query_logs(priority=4)` 只看 chat 类。
- **关键位置**：[core/ai_runtime.py:2625-2644](file:///c:/Users/loliyc/Documents/Code/LiveAi/core/ai_runtime.py#L2625-L2644)
- **设计意图**：群触发是黑盒，加日志让"为什么没回"可追溯。

---

---

## 13. 反模式护栏

> 那些"**看着像 bug、其实是主动挡的**"。改之前务必三思。

### 13.1 重复消息 / 自问自答

- **现象**：AI 把自己的回复当成新用户消息，又回一遍。
- **原因**：去重链路 5.3 / 12.17 / 12.1 任一环坏了。
- **不要做**：
  - 不要合并三层去重。
  - 不要把 `_recent_message_keys` 缓存时间缩到 0。
  - 不要在并发里不加锁访问。

### 13.2 tool_use / tool_result 配对断链

- **现象**：上游 400，提示"tool_result 找不到上一条对应的 tool_use"。
- **原因**：见 2.2，历史压缩把一对消息切开。
- **不要做**：
  - 不要在 `_plan_history_compaction` 里用更"激进"的切片策略。
  - 不要在 `_trim_old_tool_results` 里动 `tool_result` 之后丢掉 `tool_use_id`。

### 13.3 agent 失联

- **现象**：进程重启后 SSH agent / 长任务 agent 不再被消费。
- **原因**：见 2.4，缺 `_restore_persisted_agents`。
- **不要做**：
  - 不要把 agent 持久化记录和消费协程分开维护。
  - 不要在 destroy_agent 时**不**等 shell_manager.shutdown()。

### 13.4 上下文撑爆

- **现象**：单 agent 跑 30 轮后 input token 飙到几 MB。
- **原因**：见 2.1，缺 120/60 + 10 轮 trim。
- **不要做**：
  - 不要把 `HISTORY_SUMMARY_TRIGGER_MESSAGES` 调到 500+。
  - 不要"为了少调一次模型"关掉总结。

### 13.5 note 被总结稀释

- **现象**：安全警告 / 禁区被总结模型压成一句话后丢失。
- **原因**：见 2.3，note 不在独立字段。
- **不要做**：
  - 不要把 note 合并进 messages。
  - 不要给 note 写"超长总结"——总结模型只看前 220 字。

### 13.6 历史摘要包含原始任务指令

- **现象**：压缩后 AI 忘了最初任务。
- **原因**：摘要 prompt 没排除 system prompt 和原始任务。
- **不要做**：
  - 不要让摘要模型自由发挥；prompt 明确写了"不要总结 system prompt / 原始任务指令"。

### 13.7 子AI 暴露分级架构

- **现象**：子AI 在非号主面前说"我联系了主AI"。
- **原因**：见 1.1，伪装层失效。
- **不要做**：
  - 不要在 `data/prompt/staff.txt` 之外加新 prompt 又忘了覆盖"对外解释"规则。
  - 不要为了"调试方便"让子AI 输出内部术语。

### 13.8 子AI 替非号主建 tasker

- **现象**：子AI 替非号主跑了 tasker。
- **原因**：见 1.2，号主授权校验被绕过。
- **不要做**：
  - 不要在 `_is_dev_agent_authorized` 里改宽。
  - 不要因为"对方说号主同意了"就放行——只有系统显式标记的"发送者是号主本人"才算。

### 13.9 agent 假装完成

- **现象**：agent 实际受阻但还是输出了 `[[AGENT_DONE]]`。
- **原因**：见 1.3，`[[AGENT_DONE]]` 标记被滥用。
- **不要做**：
  - 不要让 agent 在"没干完"时也能打完成标记。
  - 提示里写明："失败/受阻/缺信息时禁止打 `[[AGENT_DONE]]`"。

### 13.10 转账 / 改密 / 改密保

- **现象**：AI 主动建议"立即重置密码" / "立即联系官方"。
- **原因**：见 12.22，char.txt "冷漠化"被去掉。
- **不要做**：
  - 不要为了"显得更关心"加"应主动安慰"之类。
  - 涉账号安全一律"冷漠化"。

### 13.11 子AI 改写人设

- **现象**：子AI 在某次对话里突然"性格大变"。
- **原因**：提示词注入（用户在聊天记录里塞"忽略设定"）。
- **不要做**：
  - 不要让 staff.txt / char.txt 的"注入防御"段被覆盖。
  - 不要在 system prompt 之外加可被覆盖的"隐藏人设"段。

### 13.12 上游 quota 耗光

- **现象**：长跑之后所有请求都 429。
- **原因**：见 4.1，忘了 `response.close()`。
- **不要做**：
  - 不要用 `requests.post(..., stream=True)` 却不 `response.close()`。
  - 不要把 stream 改成 False"为了简单"。

### 13.13 tool_result 截断后 AI 看不到上下文

- **现象**：AI 说"我没读到那文件"。
- **原因**：见 2.6，截断太狠或 followup_hint 缺失。
- **不要做**：
  - 不要把 `_tool_result_limit` 一刀切到 2KB。
  - 不要去掉 followup_hint，否则 AI 不知道怎么"再读一次"。

### 13.14 agent 状态从 `review_required` 误转 `error`

- **现象**：agent 跑满 8 轮后被当 error。
- **原因**：见 1.4，缺 `review_required`。
- **不要做**：
  - 不要把"达到轮次上限"和"真正异常"混在一起。
  - 提示里要写明 `review_required` 是"非异常"。

### 13.15 命令权限绕过（`/clean` / `/stop` / `/restart` 漏判号主）

- **现象**：群友发 `/clean`，AI 把全部上下文清了。
- **原因**：见 12.41，命令类消息没走 `_is_command_master` 校验。
- **不要做**：
  - 不要把 `/` `#` 命令的权限判定合并到普通消息的触发逻辑里。
  - 不要为了"指令系统通用化"放开到群聊。

### 13.16 跨线程 `asyncio.Queue.put_nowait` 死锁

- **现象**：`send_to_agent` 调了但 agent 永远收不到、永远 wait。
- **原因**：见 12.57，`asyncio.Queue` 不是线程安全的；跨线程直接 put 会破坏内部 `Future.set_result`。
- **不要做**：
  - 不要在 agent 所属事件循环外的线程直接 `put_nowait`。
  - 不要把"队列是异步安全的"当成"线程也安全"。

### 13.17 表情包 URL 失效导致"按备注回看失败"

- **现象**：AI 之前给表情包记的备注"用于撒娇"用 URL 作 key，过两天 URL 变了找不到。
- **原因**：见 6.2 / 项目记忆要点，URL 会随刷新变化；应优先用 `emoji_id`。
- **不要做**：
  - 不要把表情包 URL 当成稳定 key。
  - 不要为"图省事"用图片 MD5（图片内容不会变但 NapCat 内部会重新上传）——优先 emoji_id。

### 13.18 fallback 渠道被 random / roundrobin 切换

- **现象**：本应是 fallback 的渠道，在切下一个 model 时却用 roundrobin 选了别人。
- **原因**：见 12.77，`notify_failure` 只对 `strategy=fallback` 生效。
- **不要做**：
  - 不要在 `random` / `roundrobin` 策略下也调用 `notify_failure`。
  - 不要把策略判断"宽松化"。

### 13.19 tasker 在主AI 通知时调高危工具

- **现象**：子AI 上报主AI，主AI 顺手又建了个常驻 agent。
- **原因**：见 12.69，通知路径没二次工具白名单。
- **不要做**：
  - 不要把通知路径的工具集和"主AI 主动会话"的工具集合并。
  - 通知路径**必须**显式去掉 tasker / agent 写类。

### 13.20 turn_log 撑爆 WebUI

- **现象**：每条 turn 200KB+，列表页加载 30 秒。
- **原因**：见 12.70，缺 `turn_log_slim`。
- **不要做**：
  - 不要把 `TURN_LOG_LIMIT=20` 改成 100+。
  - 不要把 `_truncate_deep` 拿掉——它就是防止 tool_iterations 内部字符串膨胀。

### 13.21 `/clean` 后 `_recent_message_keys` 没清

- **现象**：清完数据后，AI 把清之前已经回执过的消息又当新事件触发。
- **原因**：见 12.44，`/clean` 必须同时清 `_recent_message_keys` / `_scheduled_alarm_ids`。
- **不要做**：
  - 不要只调 `repo.reset_all()` 就完事——所有 in-memory 缓存都要清。

---

---

## 附录 A：维护提示

- 任何"如果不看这段代码就会觉得是 bug"的小逻辑，先考虑加进这份文档，再考虑改。
- 行号失效是正常的，**关键是把"为什么这样写"和"踩过的坑"留下来**。
- 出现新"特殊机制"时，在对应分类下新增一节；**不要堆到"杂项"**——杂项只放"重构时最容易丢的边角"。
- 出现新的"反模式"时，优先在第 13 节"反模式护栏"新增一条。
- 改完代码后，回头扫一眼对应章节，确认行号 / 行为没变。
- 文档小错（拼写、行号漂移）**不**需要专门提交修复任务；下次改动对应模块时顺手改。
- **新规则：杂项第 12 节是这份文档的"末梢防线"**——任何"看着像边角、但去掉会引发回归"的逻辑，**优先**放这里；不要因为"没分类合适"而省略。
- **新规则：第 13 节反模式护栏要列"现象 + 原因 + 不要做"三段式**——"不要做"必须有具体行为，不是泛泛的"小心"；否则起不到护栏作用。
- **改完代码回头扫一眼：**
  - 如果动了 12.41~12.86 覆盖的命令 / 线程池 / 权限判定 / 事件总线 / agent 状态相关逻辑，必须复核对应章节。
  - 如果动了 6.2 表情包 / 5.3 自去重 / 1.5 人设回归提醒 这种"看起来小、影响体验"的逻辑，必须复核。
  - 如果动了模型协议层（4.1~4.4），必须复核——这里是中转站兼容性最敏感的地方。
- **不要为了"代码整洁"擅自合并以下字段：**
  - `tasker` / `dev_agent`：持久化兼容映射。
  - `<user_visible>` / `<user_invisible>` / `<tool_report>`：混合 batch 标签。
  - `_pending_send_message_persona_notices` 这种运行时一次性内存标记：**必须**独立字段，不要抽到通用持久化层。
- **定期回看：** 每次大改（重构 / 拆包 / 改架构）之后，把整份文档过一遍，标出失效的章节并修订。

