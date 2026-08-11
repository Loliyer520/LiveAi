# 阶段 1：Mailbox owner 一次性切换计划

> 本文是**后续目标计划**，不是当前实现说明。当前代码仍处于旧 `_pending_scope_turns` 单 owner 的稳定状态；`InMemoryEventMailbox` 尚未接管生产 pending 流量。历史影子层与当前事实见 `docs/stage1_event_mailbox_shadow.md`。

## 1. 当前前提（切换前事实）

1. `core/ai_runtime.py` 初始化并直接访问 `_pending_scope_turns: dict[scope, list]`；append/count/pop/clear、debounce busy、task promotion、Agent report 和工具轮路径仍依赖旧结构。
2. `core/ai_runtime.py` 当前未 import 或实例化 `InMemoryEventMailbox`，因此没有 mailbox/旧 pending 双写，也没有 mailbox consumer owner。
3. `test/test_scope_turn_coordination.py` 的 fixture 仍显式创建 `_pending_scope_turns = {}`，锁定当前旧 owner 协调语义。
4. `test/test_pending_owner_equivalence.py` 只提供旧 pending 与候选 mailbox 的等价性证据，不表示切换已发生。

## 2. 目标状态与风险清单（尚未实现）

1. **单 owner**：初始化后只保留 `InMemoryEventMailbox`；停止写入和读取 `_pending_scope_turns`，不得双写。
2. **FIFO 与 pop 粒度**：旧协调接口的 raw pop 每次只取一个；普通 follow-up 如何使用原子 batch 必须显式实现且通过批准的验收。
3. **stale 语义**：live pop 跳过 stale message；raw pop 仍可观察 stale。
4. **优先级**：pending message 先于 pending task；同 scope 的 Agent report 仅在 active 时进入 pending。
5. **history seed**：follow-up turn 仍携带首个 pending message 作为 history seed；若批准合批，后续事件进入 trigger metadata。
6. **clear/status**：`_clear_scope_state`、停止流程、debounce busy 与 pending count/status 必须只读 mailbox。
7. **fixture**：切换完成后，专项测试不得再靠手工创建 `_pending_scope_turns` 才能运行。
8. **回滚**：由于禁止双写，回滚只能回代码，不能迁移运行中队列；重启后 mailbox 按既定契约为空。

## 3. 建议的一次性切换边界

后续获批实施时，应在同一提交完成：

1. `AIOrchestrator.__init__` 将旧 dict 初始化替换为 `InMemoryEventMailbox`。
2. `_reserve_scope_turn`、`_take_pending_scope_turn`、`_release_scope_turn`、pending count/has/clear 全部改为 mailbox API。
3. stop/reset 清理、debounce busy、pending task promotion、Agent report busy/requeue、工具轮 follow-up/history seed 全部停止直接访问旧 dict/list shape。
4. 普通 follow-up 批处理按批准规则实现；raw pop 保持单项语义。
5. 更新专项 fixture 和断言，删除 `_pending_scope_turns` 的生产与测试依赖。
6. 做全仓文本审计，确认不存在 `_pending_scope_turns` 生产引用及并行 owner。

本节仅描述目标切换边界；这些动作在当前工作树中尚未执行。

## 4. 当前测试集合与数量

当前阶段 1 专项集合共 **34 项**：

| 测试文件 | 数量 | 当前证明范围 |
|---|---:|---|
| `test/test_event_mailbox.py` | 14 | mailbox schema/FIFO/pop/drain/隔离/不恢复/不去重；对象引用 transient identity 与 payload 隔离；批量提交/并发 drain 原子性；clear/sequence 和 pending-scope head 顺序 |
| `test/test_event_adapters.py` | 9 | normalized、Agent report、通用 scoped event、scope-turn batch；输入 alias 隔离、字段 round-trip、显式失败路径及混合 batch 无损 |
| `test/test_scope_turn_coordination.py` | 8 | 当前旧 `_pending_scope_turns` owner 的协调契约 |
| `test/test_pending_owner_equivalence.py` | 3 | 当前对象引用层下的 FIFO batch 合并、transient identity、不泄漏 payload及 task promotion 重封装 |

另有阶段 0 独立集合 `test/test_stage0_characterization.py`：8 项，不计入上述 34 项。

旧文档中的“限定离线 28/28”和上一轮“29 项”均已随对象引用层测试调整而漂移。本次最终对账基于当前工作树逐文件实际执行：上述 34 项阶段 1 专项全部 passed，阶段 0 的 8 项也全部 passed；均为 0 failed、0 skipped。阶段 0 的历史 54 项结果不作改写。

## 5. 当前可复现命令

```bash
python -m unittest discover -s test -p 'test_event_mailbox.py' -v
python -m unittest discover -s test -p 'test_event_adapters.py' -v
python -m unittest discover -s test -p 'test_scope_turn_coordination.py' -v
python -m unittest discover -s test -p 'test_pending_owner_equivalence.py' -v
python -m unittest discover -s test -p 'test_stage0_characterization.py' -v
```

不要以默认全目录 discover 作为离线验收入口，避免现有网络诊断脚本访问真实 provider。

## 6. 后续切换验收门槛

### 6.1 静态门槛

切换实现后再执行：

```bash
rg -n "_pending_scope_turns" core test
```

目标是生产代码零命中；测试不得再通过构造旧 dict 维持 fixture。文档中的历史引用不纳入该生产门槛。

### 6.2 行为门槛

切换后的测试必须继续证明：

- 同 scope FIFO、跨 scope 隔离、pop/drain 原子性；
- stale live/raw 差异；
- pending message 优先于 pending task；
- Agent report active-only 入队；
- history seed、trigger metadata 和批准的 batch snapshot；
- clear、stop/reset、debounce busy/status 只依赖 mailbox；
- 新 runtime/mailbox 实例不恢复旧内存队列。

### 6.3 回滚门槛

若任一门槛失败，回滚整个 owner 切换提交并重启恢复旧实现；不得通过临时恢复双写或同时保留两个 owner 修补中间态。
