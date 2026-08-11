# LiveAi 阶段 1：统一事件与内存 Mailbox 影子层

> 本文记录当前已经落盘的低风险影子能力。消费 owner 尚未切换，目标 actor/mailbox 语义尚未接管生产流量。

## 1. 旧 durable inbox 测试处置

已删除未被 Git 跟踪的 `test/test_switch_preflight.py`。证据：

- 文件唯一生产依赖是已不存在的 `from core.inbox import InboxStore`。
- 测试构造 `receiver.mode='external'`、`inbox_path`、`transport.mode='outbox'`，验证的是旧外置 durable inbox/outbox receiver 切换预检。
- 当前 `main.py` 直接装配单进程 `NapcatBot`，NapCat WS 事件经 `normalize_ws_event` 后直接 dispatch；没有 `core.inbox` import，也没有 receiver/outbox 消费 owner。
- `core.inbox.py` 本身不存在且未被 Git 跟踪；未恢复它。

`core/switch_checks.py` 仍是未跟踪文件且包含上述旧 receiver/inbox 预检逻辑，但本阶段没有生产引用、也不阻塞新影子层。按号主要求暂不扩大清理，保留现状。

## 2. 状态对账：历史影子层、当前实现、后续目标

### 2.1 历史影子层状态

阶段 1 最初落盘的是不接生产流量的事件 schema、纯内存 mailbox 和 adapters。该历史阶段的约束是：生产路径不 import/实例化 mailbox，不双写，不改变 `AIOrchestrator` 的旧 pending owner。本文旧版“12 passed”等数字仅对应当时更小的测试集合，不再作为当前数量。

### 2.2 当前实现状态（以代码与测试为准）

- `core/event_envelope.py`
  - `EventType` 包含 `message`、`alarm`、`recurring_task`、`agent_report`、`main_ai_message`、`system`。
  - `EventEnvelope` 校验 scope、event type、JSON payload、时间和 sequence，支持 `to_dict/from_dict`。
- `core/event_mailbox.py`
  - `InMemoryEventMailbox` 是线程安全、按 scope 隔离的进程内 FIFO。
  - `append/append_many` 分配单调 sequence；`pop_scope` 原子取一个；`drain_scope` 原子取走当时 scope 快照。
  - `pending_count/pending_scopes/clear/is_empty` 只操作内存；新实例为空，无 store/path/load/restore。
- `core/event_adapters.py`
  - 支持 normalized message、Agent report、通用 scoped event 到 envelope 的映射。
  - 支持现有 scope-turn item 与 envelope 的转换，以及 `EventBatch` 到 follow-up item/trigger metadata 的合并。
- `core/ai_runtime.py`
  - 当前仍由 `_pending_scope_turns: dict[scope, list]` 持有 pending message；初始化、append/count/pop/clear 均引用该 dict。
  - 当前生产 runtime 未 import 或实例化 `InMemoryEventMailbox`，因此不存在 mailbox 与旧 pending 双 owner。
  - scope active、旧 pending FIFO、pending task、Agent report、工具轮、debounce/status 的现有协调语义仍由旧实现负责。

### 2.3 后续目标（尚未实现）

后续目标才是把生产 pending owner 一次性切到 `InMemoryEventMailbox`，并保持既有 stale/raw-pop/task/report/history/clear/status 契约；完成完整 turn 提交后，再按批准的批次规则 drain。目标状态不得写成当前已接线事实。

## 3. 当前 owner 边界

当前稳定状态是**旧 `_pending_scope_turns` 单 owner**：

- `AIOrchestrator` 使用 `_pending_scope_turns` 保存与弹出 pending turn。
- Mailbox、envelope 和 adapters 是可独立测试的候选实现，没有挂入 NapCat dispatch、Agent report drain、alarm 提交或 runtime pending 路径。
- `_process_message`、scope active lock、pending FIFO/task 协调、工具轮和完整 turn 提交顺序未因影子层改变。
- retry/fallback、模型选择、Agent report、闹钟/循环任务、WebUI/status、NapCat 发送未因影子层改变。
- Mailbox 没有持久化和重启恢复。

因此，任何“当前 mailbox 已是 consumer owner”“当前普通 follow-up 已批量 drain”或“fixture 已不再创建 `_pending_scope_turns`”的现在时描述都不成立，只能列为后续切换目标。

## 4. Mailbox 当前契约

- 同 scope 按 append sequence FIFO。
- `pop_scope` 在线程锁内原子取一个，后续事件保留。
- `drain_scope` 在线程锁内取走当时全部事件；drain 后到达的事件进入下一批。
- 不同 scope 队列隔离；`pending_scopes()` 按各 scope 首个待处理事件的 sequence 排序，只提供确定性观察。
- Mailbox 不隐式去重；相同 event id 仍保留。
- `EventBatch` 不合成自然语言，只保留有序 envelope list。

以上是 mailbox 类本身的当前契约，不表示生产 runtime 已使用这些能力。

## 5. 当前离线测试集合与数量

逐文件实际执行，当前共 **34 项阶段 1 专项测试**，另有 **8 项阶段 0 characterization**：

1. `test/test_event_mailbox.py`：14 项。
   - 原有 envelope schema、FIFO、跨 scope 隔离、单项 pop、快照 drain、新实例不恢复与不去重契约；
   - 对象引用层新增：`MailboxEntry.transient` 保留运行时对象 identity 且不进入序列化 payload、单项/FIFO 引用保持；
   - 原子性新增：`append_many` 中途非法时不部分提交、并发 drain 不观察半批；
   - 状态细节新增：clear 后 sequence 仍单调、部分 pop 后按剩余 head 重排 pending scopes。
2. `test/test_event_adapters.py`：9 项。
   - normalized message、Agent report、通用 scoped event、scope-turn batch 适配；
   - 对象引用/隔离层新增：输入 dict 不被修改且嵌套 alias 被断开；
   - round-trip 与失败路径新增：message 标量与 trigger/history/metadata 保留、Agent report 来源字段与 extra metadata 保留、必填/未知 kind/非 mapping 显式失败、混合 message/task/report batch FIFO 无损。
3. `test/test_scope_turn_coordination.py`：8 项。
   - 直接以 `_pending_scope_turns = {}` 构造当前旧 owner fixture；锁定 FIFO/count/busy、reserve/take/release/history seed、stale/raw pop、message-before-task、clear、Agent report active-only、工具轮单项 raw pop。
4. `test/test_pending_owner_equivalence.py`：3 项。
   - 当前对象引用层切换前证据：FIFO batch 与 legacy latest-message 合并、mailbox entry transient 保留运行时 identity 且不泄漏 payload、task promotion 在当前 FIFO batch 后重新 envelope。
5. `test/test_stage0_characterization.py`：8 项（阶段 0 独立集合，不计入上述 34 项）。

本次最终对账基于当前工作树逐文件实际执行：14 + 9 + 8 + 3 = **34 项阶段 1 专项**全部 passed；阶段 0 的 8 项也全部 passed；均为 0 failed、0 skipped。旧文档中的“event 12 项”“限定离线 28/28”以及上一轮的“29 项”都只对应更早测试集合，不再作为当前数量。阶段 0 第 15 节的历史 54 项执行记录保持不变。

可复现命令：

```bash
python -m unittest discover -s test -p 'test_event_mailbox.py' -v
python -m unittest discover -s test -p 'test_event_adapters.py' -v
python -m unittest discover -s test -p 'test_scope_turn_coordination.py' -v
python -m unittest discover -s test -p 'test_pending_owner_equivalence.py' -v
python -m unittest discover -s test -p 'test_stage0_characterization.py' -v
```

不使用默认全目录 discover 作为离线验收入口，避免 `test/` 中现有网络诊断脚本访问真实 provider。

## 6. 后续切换前仍需确认/实现

- 将 runtime 初始化及 pending 协调接口一次性切到 mailbox，不能双写。
- 保持 raw pop 每次一个、live pop 跳过 stale、message-before-task、Agent report active-only、history seed、clear/status 语义。
- 明确普通 follow-up 是否按当前积压快照合批，以及批内消息、闹钟、Agent report、主 AI 通讯、循环任务的排序/优先级。
- 明确多事件如何转成模型输入、turn metadata 和审计记录。
- 保持 mailbox 仅内存、重启不恢复，并补齐切换后的生产 owner 验收。

## 7. 接线预检结论

历史上曾尝试直接替换 `_pending_scope_turns`，但因旧 pending 还被清理、debounce busy、task promotion、Agent report、工具轮等路径依赖而回退。当前代码事实是旧 `_pending_scope_turns` 单 owner 稳定运行，mailbox/adapters 仅作为候选实现和等价性测试对象。

正式切换应先保证所有访问通过内部协调接口表达，再一次性替换 owner；切换完成前，不把候选 mailbox 的 batch 能力描述成当前生产行为。