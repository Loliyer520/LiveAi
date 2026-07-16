"""常驻 agent 管理器（新版 agent，与旧版一次性 dev_agent task 并行、互不影响）。

本模块只负责 agent 记录的增删查改、状态机与状态持久化，是多步大功能的第 1 步骨架。
后续步骤才会接入：run 主循环执行、分级 AI 工具调用、双向通信队列、强杀/总结逻辑等。

设计与项目现有持久化风格保持一致：
- 复用 pack.json_store.JsonStore 做原子落盘（临时文件 + replace）。
- 存储结构 payload = {'agents': {agent_id: {...}}}，参考 AIRepository 对 tasks/agents 的存法。
- agent_id 使用短随机 hex（uuid.uuid4().hex[:12]），与 PendingTask.task_id 的生成方式保持一致。
"""

import asyncio
import inspect
import json
import threading
import time
import uuid

from pack.json_store import JsonStore

# 复用旧版 dev_agent 的执行辅助（工具执行、重试、上下文裁剪、工具 schema、shell 管理器等）。
# 只 import、不改动旧版逻辑，保证旧版 run_dev_agent / _run_dev_agent_task 完全不受影响。
from core.dev_agent import (
    MAX_ITERATIONS,
    MAX_CONTEXT_CHARS,
    RetryableAPIError,
    DevAgentShellManager,
    _build_tools_schema,
    _call_with_retry,
    _execute_tool_call,
    _project_root,
    _trim_old_tool_results,
)
from pack.console_logger import error, warn


# 状态机三个合法值：
#   running —— 正在跑工具 / 执行中
#   waiting —— 已输出纯文本，等待对方答复
#   idle    —— 干完待命，不销毁
AGENT_STATUSES = ('running', 'waiting', 'idle')

# agent 状态默认落盘位置。与 ai_state.json 同目录，独立文件，互不干扰。
# data/msgs 在 dev_agent 的文件工具 DENYLIST 里，但 AgentManager 是 bot 进程内代码，
# 通过 JsonStore 直接读写该目录（已确认目录可写），不受 dev_agent 沙箱限制影响。
DEFAULT_AGENTS_STORAGE_PATH = 'data/msgs/agents_state.json'

# instruction 摘要在 list_agents 里的最大长度。
INSTRUCTION_SUMMARY_LIMIT = 80


class AgentManager:
    """管理常驻 agent 的生命周期记录与状态持久化。

    agents 字典结构：
        {
            agent_id: {
                'agent_id': str,
                'status': 'running' | 'waiting' | 'idle',
                'instruction': str,
                'messages': [{'role': ..., 'content': ...}, ...],
                'created_at': float,
                'updated_at': float,
            },
            ...
        }
    """

    def __init__(
        self,
        store: JsonStore | None = None,
        storage_path: str | None = None,
        report_notifier=None,
    ):
        if store is None:
            store = JsonStore(storage_path or DEFAULT_AGENTS_STORAGE_PATH)
        self.store = store
        # 确保存储结构成形；同时相当于一次 load，进程重启后可从磁盘恢复。
        self.store.update(self._ensure_shape)
        # agent 专属的注入队列：{agent_id: asyncio.Queue}。
        # 队列本身是内存态、不落盘（asyncio.Queue 无法序列化），进程重启后重建。
        # 第 3 步的 send_to_agent 会往对应队列 put_nowait 注入消息，
        # 常驻循环每轮开头/挂起时从队列取消息唤醒继续。
        self._inject_queues: dict[str, asyncio.Queue] = {}
        # 全局待上报队列（方向A：agent→AI）。每条形如
        # {'agent_id': str, 'text': str, 'ts': float}。
        # agent 产生纯文本（waiting/汇报）时经 on_agent_message 钩子追加到这里，
        # 由上层 AI（AIOrchestrator）择机取走并投递给会话AI。
        # 用普通 list + 线程锁保护：追加方可能是事件循环线程（run_agent_loop 的
        # _emit_agent_message），取走方是 AI worker 所在的事件循环线程，加锁更稳妥。
        self._pending_reports: list[dict] = []
        self._pending_reports_lock = threading.Lock()
        # 有新待上报内容时通知上层的回调：report_notifier() -> None。
        # 由 AIOrchestrator 注入，用来触发"AI 空闲则立即投递、忙碌则延后"的逻辑。
        self._report_notifier = report_notifier
        # 常驻循环所在事件循环引用。send_to_agent 可能被会话AI线程调用，
        # 而注入队列属于该事件循环，跨线程投递必须走 loop.call_soon_threadsafe。
        # 由 AIOrchestrator 在事件循环起来后通过 set_loop 设置。
        self._loop: asyncio.AbstractEventLoop | None = None
        # 每个 agent 的常驻循环任务注册表：{agent_id: asyncio.Task}。
        # 由启动 run_agent_loop 的一方登记（register_agent_task），destroy_agent
        # 的强杀路径据此 cancel 对应任务。内存态、不落盘，进程重启后重建。
        self._agent_tasks: dict[str, asyncio.Task] = {}
        # 默认模型实例，供无工具权限的总结 AI（summarize_agent）使用。
        # 由 set_model 或 run_agent_loop 启动时登记。
        self._model = None

    def set_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """登记常驻循环所在的事件循环，供 send_to_agent 跨线程安全投递使用。"""
        self._loop = loop

    def set_report_notifier(self, notifier) -> None:
        """登记"有新待上报内容"回调，供 AIOrchestrator 触发投递逻辑。"""
        self._report_notifier = notifier

    def set_model(self, model) -> None:
        """登记一个默认模型实例，供无工具权限的总结 AI（summarize_agent）使用。

        run_agent_loop 启动时也会自动登记它拿到的 model，一般无需手动调用。
        summarize_agent 也可显式传入 model 覆盖。
        """
        self._model = model

    def register_agent_task(self, agent_id: str, task) -> None:
        """登记某个 agent 的常驻循环 asyncio.Task，供强杀（destroy_agent）时 cancel。

        由启动 run_agent_loop 的一方（如 AIOrchestrator.create_task 调 loop.create_task
        后）调用登记。run_agent_loop 内部也会在拿到自身 Task 时自动登记一次。
        """
        agent_id = str(agent_id or '')
        if not agent_id or task is None:
            return
        self._agent_tasks[agent_id] = task

    def get_agent_task(self, agent_id: str):
        """取某个 agent 已登记的常驻循环任务，未登记返回 None。"""
        return self._agent_tasks.get(str(agent_id or ''))

    # ------------------------------------------------------------------
    # 全局待上报队列（方向A：agent→AI）
    # ------------------------------------------------------------------
    def on_agent_message(self, agent_id: str, text: str) -> None:
        """默认的 on_agent_message 钩子实现：把 agent 产生的纯文本追加到全局待上报队列。

        run_agent_loop 在模型产出纯文本（waiting/汇报）时会调用此回调。
        追加完成后触发 report_notifier，让上层 AI 决定"空闲立即投递 / 忙碌延后"。
        """
        agent_id = str(agent_id or '')
        text = str(text or '')
        if not agent_id:
            return
        # 读取该 agent 的 origin_scope（创建它的会话 scope，形如 'group:123'），
        # 一并存进待上报记录，供上层 AI 按 scope 投递回真正创建它的会话。
        # 读不到（agent 记录缺失或未落盘该字段）时存 None，上报时回退 master:0。
        origin_scope = None
        try:
            record = self.get_agent(agent_id)
            if record:
                origin_scope = record.get('origin_scope') or None
        except Exception as exc:
            error(f'[AgentManager] on_agent_message 读取 origin_scope 失败 agent={agent_id}: {exc}')
        with self._pending_reports_lock:
            self._pending_reports.append(
                {'agent_id': agent_id, 'text': text, 'ts': time.time(), 'origin_scope': origin_scope}
            )
        notifier = self._report_notifier
        if notifier is not None:
            try:
                notifier()
            except Exception as exc:
                error(f'[AgentManager] report_notifier 触发失败: {exc}')

    def has_pending_reports(self) -> bool:
        """是否有待上报内容。"""
        with self._pending_reports_lock:
            return bool(self._pending_reports)

    def drain_pending_reports(self) -> list[dict]:
        """取走并清空全部待上报记录，返回 list（每条 {'agent_id','text','ts'}）。

        由上层 AI 在确定要投递（AI 空闲或本轮被触发）时调用，多个 agent 的
        挂起内容会一次性带走，每条都保留各自 agent_id 以便投递时标清来源。
        """
        with self._pending_reports_lock:
            drained = self._pending_reports
            self._pending_reports = []
        return drained

    def peek_pending_reports(self) -> list[dict]:
        """只读查看当前待上报记录副本（不清空）。"""
        with self._pending_reports_lock:
            return [dict(item) for item in self._pending_reports]

    def requeue_pending_reports(self, reports: list[dict]) -> None:
        """把已 drain 出来但暂时无法投递（目标 scope 忙）的待上报记录放回队列。

        由上层 AI 在按 scope 分组投递时使用：忙碌 scope 的 reports 原样放回队列头部，
        保持相对顺序，等该 scope 下次空闲时补投，确保内容不丢失。
        """
        if not reports:
            return
        with self._pending_reports_lock:
            # 放回队列头部：这批本来就是先产生的，补投时应排在后来新增的之前。
            self._pending_reports[0:0] = list(reports)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _ensure_shape(payload: dict):
        payload.setdefault('agents', {})

    @staticmethod
    def _new_agent_id() -> str:
        return uuid.uuid4().hex[:12]

    @staticmethod
    def _summarize(text: str, limit: int = INSTRUCTION_SUMMARY_LIMIT) -> str:
        text = str(text or '').strip().replace('\n', ' ')
        if len(text) <= limit:
            return text
        return text[:limit] + '…'

    # ------------------------------------------------------------------
    # 增 / 删
    # ------------------------------------------------------------------
    def create_agent(self, instruction: str, origin_scope: str | None = None) -> str:
        """创建一条常驻 agent 记录，返回 agent_id。

        初始 status 为 'running'，messages 初始化为一条 user 指令消息。

        origin_scope: 创建该 agent 的会话 scope，格式 'scope_type:scope_id'
                      （如 'group:12345'、'private:67890'、'master:0'）。
                      agent 后续产生的上报内容会按此 scope 投递回真正创建它的会话。
                      为空时不落盘该字段，上报时回退到 master:0。
        """
        instruction = str(instruction or '')
        origin_scope = str(origin_scope or '').strip()
        agent_id = self._new_agent_id()

        def mutator(payload: dict):
            now = time.time()
            record = {
                'agent_id': agent_id,
                'status': 'running',
                'instruction': instruction,
                'messages': [{'role': 'user', 'content': instruction}],
                'created_at': now,
                'updated_at': now,
            }
            if origin_scope:
                record['origin_scope'] = origin_scope
            payload.setdefault('agents', {})[agent_id] = record
            return agent_id

        return self.store.update(mutator)

    def _remove_agent_record(self, agent_id: str) -> bool:
        """仅从持久化字典移除一条 agent 记录（不涉及强杀/总结）。返回是否确实移除。"""
        agent_id = str(agent_id or '')
        if not agent_id:
            return False

        def mutator(payload: dict):
            agents = payload.setdefault('agents', {})
            return agents.pop(agent_id, None) is not None

        return bool(self.store.update(mutator))

    async def destroy_agent(self, agent_id: str, summarize: bool = False) -> dict:
        """强杀并移除一条 agent，可选先做销毁前总结。

        流程：
        1. cancel 该 agent 的常驻循环 Task（若已登记）。cancel 会让 run_agent_loop
           在挂起点（await queue.get() 或 asyncio.to_thread）抛 CancelledError，
           run_agent_loop 的 finally 保证 shell_manager.shutdown() 一定执行，
           后台 shell 任务被安全清理。这里 await 该 Task 直到它真正结束，确保
           finally（含 shell 清理）跑完再继续。
        2. summarize=True：在移除记录【之前】读取 messages 快照，调
           summarize_agent(agent_id, 'destroy') 拿销毁前总结（无工具权限）。
        3. 从持久化字典移除记录，清理 Task/注入队列登记。

        返回 {'removed': bool, 'summary': str|None}。
        summarize=False 时 summary 为 None。
        """
        agent_id = str(agent_id or '')
        result = {'removed': False, 'summary': None}
        if not agent_id:
            return result

        # ---- 1. 强杀：cancel 常驻循环任务并等待其 finally（含 shell 清理）跑完 ----
        task = self._agent_tasks.get(agent_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # 预期内：被我们 cancel 掉。run_agent_loop 的 finally 已执行 shell 清理。
                pass
            except Exception as exc:
                warn(f'[AgentManager] destroy_agent 等待 agent={agent_id} 任务结束异常: {exc}')

        # ---- 2. 销毁前总结（在移除记录之前，需要读到 messages 快照）----
        if summarize:
            try:
                result['summary'] = await self.summarize_agent(agent_id, 'destroy')
            except Exception as exc:
                error(f'[AgentManager] destroy_agent 总结失败 agent={agent_id}: {exc}')
                result['summary'] = f'（销毁前总结生成失败：{exc}）'

        # ---- 3. 移除记录与内存态登记 ----
        result['removed'] = self._remove_agent_record(agent_id)
        self._agent_tasks.pop(agent_id, None)
        self._inject_queues.pop(agent_id, None)
        return result

    # ------------------------------------------------------------------
    # 查
    # ------------------------------------------------------------------
    def get_agent(self, agent_id: str) -> dict | None:
        """返回单条 agent 记录的副本，不存在返回 None。"""
        agent_id = str(agent_id or '')
        if not agent_id:
            return None
        payload = self.store.load()
        data = (payload.get('agents') or {}).get(agent_id)
        return dict(data) if data else None

    def list_agents(self) -> list[dict]:
        """列出所有 agent 概要，含 id/status/instruction 摘要/时间，按更新时间倒序。"""
        payload = self.store.load()
        agents = (payload.get('agents') or {}).values()
        result = []
        for data in agents:
            result.append(
                {
                    'agent_id': data.get('agent_id'),
                    'status': data.get('status'),
                    'instruction_summary': self._summarize(data.get('instruction') or ''),
                    'message_count': len(data.get('messages') or []),
                    'origin_scope': data.get('origin_scope'),
                    'created_at': data.get('created_at'),
                    'updated_at': data.get('updated_at'),
                }
            )
        result.sort(key=lambda item: item.get('updated_at') or 0, reverse=True)
        return result

    # ------------------------------------------------------------------
    # 改：状态与消息
    # ------------------------------------------------------------------
    def set_status(self, agent_id: str, status: str) -> dict | None:
        """更新 agent 状态；status 必须是 AGENT_STATUSES 之一，否则抛 ValueError。

        返回更新后的记录副本，agent 不存在返回 None。
        """
        agent_id = str(agent_id or '')
        if status not in AGENT_STATUSES:
            raise ValueError(f'invalid status: {status!r}, expected one of {AGENT_STATUSES}')

        def mutator(payload: dict):
            data = (payload.get('agents') or {}).get(agent_id)
            if not data:
                return None
            data['status'] = status
            data['updated_at'] = time.time()
            return dict(data)

        return self.store.update(mutator)

    def append_message(self, agent_id: str, message: dict) -> dict | None:
        """向 agent 追加一条消息，返回更新后的记录副本，agent 不存在返回 None。"""
        agent_id = str(agent_id or '')
        if not isinstance(message, dict):
            raise ValueError('message must be a dict')

        def mutator(payload: dict):
            data = (payload.get('agents') or {}).get(agent_id)
            if not data:
                return None
            data.setdefault('messages', []).append(dict(message))
            data['updated_at'] = time.time()
            return dict(data)

        return self.store.update(mutator)

    # ------------------------------------------------------------------
    # 注入队列（双向通信）：结构在本步建好，实际投递逻辑第 3 步接
    # ------------------------------------------------------------------
    def _get_inject_queue(self, agent_id: str) -> asyncio.Queue:
        """取得（或惰性创建）某个 agent 的注入队列。

        asyncio.Queue 需要在有事件循环的上下文里创建，因此本方法应在
        async 调用链（如 run_agent_loop / send_to_agent）内使用。
        队列是内存态，不随 agents_state.json 落盘，进程重启后重建。
        """
        agent_id = str(agent_id or '')
        queue = self._inject_queues.get(agent_id)
        if queue is None:
            queue = asyncio.Queue()
            self._inject_queues[agent_id] = queue
        return queue

    def send_to_agent(self, agent_id: str, message: dict) -> bool:
        """向指定 agent 的注入队列投递一条消息，唤醒挂起的常驻循环。

        message 约定为 {'role': ..., 'content': ...} 形式的消息 dict。
        返回是否成功入队。

        跨线程安全：本方法可能被会话AI线程（AI worker 之外的线程，或同一事件
        循环内的协程）调用，而注入队列（asyncio.Queue）属于 run_agent_loop 所在
        的事件循环，且 asyncio.Queue 不是线程安全的。因此：
        - 若已登记事件循环（self._loop）且当前不在该循环线程，用
          loop.call_soon_threadsafe 把 put_nowait 调度回队列所属事件循环执行，
          既保证入队原子性，也保证唤醒等待者（await queue.get()）的 future 在
          正确的循环里被 set_result。
        - 若就在该事件循环线程内（例如同循环里的协程调用），直接 put_nowait。
        - 若尚未登记事件循环（极少见，循环还没起来），退化为直接 put_nowait，
          队列在此情形下也还没有等待者。
        """
        agent_id = str(agent_id or '')
        if not agent_id:
            return False
        if not isinstance(message, dict):
            raise ValueError('message must be a dict')
        payload = dict(message)
        loop = self._loop
        try:
            if loop is not None and loop.is_running():
                running = None
                try:
                    running = asyncio.get_running_loop()
                except RuntimeError:
                    running = None
                if running is loop:
                    # 已在队列所属事件循环线程内，直接入队。
                    self._get_inject_queue(agent_id).put_nowait(payload)
                else:
                    # 跨线程：调度回队列所属事件循环执行入队 + 唤醒。
                    def _put():
                        try:
                            self._get_inject_queue(agent_id).put_nowait(payload)
                        except Exception as exc:
                            warn(f'[AgentManager] send_to_agent 入队(threadsafe)失败 agent={agent_id}: {exc}')
                    loop.call_soon_threadsafe(_put)
            else:
                # 事件循环尚未登记/未运行，队列此时不会有等待者，直接入队。
                self._get_inject_queue(agent_id).put_nowait(payload)
            return True
        except Exception as exc:
            warn(f'[AgentManager] send_to_agent 入队失败 agent={agent_id}: {exc}')
            return False

    def _drain_inject_queue(self, agent_id: str) -> list[dict]:
        """非阻塞地取出注入队列里当前所有待处理消息（每轮循环开头调用）。"""
        queue = self._get_inject_queue(agent_id)
        drained: list[dict] = []
        while True:
            try:
                drained.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return drained

    # ------------------------------------------------------------------
    # 常驻版执行循环（新版 agent 专用，与旧版 run_dev_agent 并行、互不影响）
    # ------------------------------------------------------------------
    async def run_agent_loop(
        self,
        agent_id: str,
        model,
        github_token: str,
        prompt_path: str = 'data/prompt/dev_agent.txt',
        project_root: str | None = None,
        on_agent_message=None,
        temperature: float = 0.4,
        max_tokens: int = 4096,
    ) -> None:
        """常驻版执行循环。

        与旧版 run_dev_agent 的关键差异：
        - 上下文来自 agent 的持久化 messages（每轮通过 get_agent 读、append_message 写落盘），
          而不是纯局部变量，进程重启后可从磁盘恢复继续。
        - 模型返回纯文本（无 tool_calls）时【不销毁】agent，而是转 waiting，
          通过 on_agent_message 钩子把这段文本交给上层（第 3 步接全局待上报队列），
          然后挂起等待注入队列唤醒。
        - 每轮开头检查注入队列，把外部（send_to_agent）注入的新消息 append 进上下文再继续。
        - waiting/idle 状态用 asyncio.Queue.get() 阻塞等待注入，避免空转占 CPU。

        on_agent_message: 可选回调 (agent_id, text) -> None|awaitable，
                          agent 产生纯文本汇报/提问时触发；本步预留，第 3 步接队列。
        """
        agent_id = str(agent_id or '')
        record = self.get_agent(agent_id)
        if not record:
            warn(f'[AgentManager] run_agent_loop 找不到 agent: {agent_id}')
            return

        # on_agent_message 缺省接到本管理器的全局待上报队列（方向A：agent→AI）。
        # 显式传入则以传入的为准（便于测试或特殊接线）。
        if on_agent_message is None:
            on_agent_message = self.on_agent_message
        # 登记常驻循环所在事件循环，供 send_to_agent 跨线程安全投递使用。
        try:
            self.set_loop(asyncio.get_running_loop())
        except RuntimeError:
            pass
        # 自动登记本循环所用模型，供无工具权限的总结 AI（summarize_agent）复用。
        if model is not None and getattr(self, '_model', None) is None:
            self._model = model
        # 自动登记自身 Task，供 destroy_agent 强杀时 cancel。
        try:
            self_task = asyncio.current_task()
            if self_task is not None:
                self.register_agent_task(agent_id, self_task)
        except RuntimeError:
            pass

        project_root = project_root or _project_root()
        shell_manager = DevAgentShellManager(project_root)
        queue = self._get_inject_queue(agent_id)

        try:
            with open(prompt_path, 'r', encoding='utf-8') as f:
                system_prompt = f.read()
        except OSError:
            system_prompt = (
                '你是这个项目专属的后台代码/资料助手，操作范围限定在本地仓库目录内，'
                '可以只读查阅GitHub任意仓库做参考。'
            )
        instruction = str(record.get('instruction') or '')
        if instruction:
            system_prompt += f'\n\n本次任务原始描述：\n{instruction}'

        tools = _build_tools_schema()

        try:
            # 外层 while True 让 agent 常驻：一轮"跑到需要等待"后挂起，被注入唤醒再进下一轮。
            while True:
                # ---- 每轮开头：检查注入队列，把外部注入的新消息落盘进上下文 ----
                for injected in self._drain_inject_queue(agent_id):
                    self.append_message(agent_id, injected)

                # ---- 从持久化记录读取最新上下文 ----
                record = self.get_agent(agent_id)
                if not record:
                    # agent 已被销毁（例如强杀），安静退出循环。
                    return
                messages = list(record.get('messages') or [])

                # ---- 上下文裁剪：复用旧版逻辑，就地修改后整条回写 ----
                total_chars = sum(
                    len(json.dumps(m.get('content'), ensure_ascii=False)) for m in messages
                )
                if total_chars > MAX_CONTEXT_CHARS:
                    _trim_old_tool_results(messages)
                    self._replace_messages(agent_id, messages)

                # ---- 内层连续工具轮：一直执行工具直到模型给出纯文本 ----
                produced_text = None
                for _ in range(MAX_ITERATIONS):
                    # running 态注入：在连续工具轮里，把 send_to_agent 注入的新消息
                    # 取出、append 到当前上下文（追加在上一轮工具结果之后），
                    # 让本 agent 下一轮模型调用时一并决策。idle/waiting 态的注入
                    # 由挂起处的 await queue.get() 唤醒 + 外层顶部 drain 处理。
                    for injected in self._drain_inject_queue(agent_id):
                        messages.append(injected)
                        self.append_message(agent_id, injected)
                    self.set_status(agent_id, 'running')
                    try:
                        reply = await asyncio.to_thread(
                            _call_with_retry,
                            f'Agent[{agent_id}] 模型调用',
                            lambda: (
                                resp if (resp := model.complete(
                                    system_prompt, messages, tools, None, temperature, max_tokens,
                                )) is not None
                                else (_ for _ in ()).throw(RetryableAPIError('模型没有返回有效响应'))
                            ),
                        )
                    except Exception as exc:
                        error(f'[AgentManager] agent={agent_id} 模型调用异常: {exc}')
                        produced_text = f'（执行异常，已挂起等待指示）模型调用失败: {exc}'
                        break

                    if not reply.tool_calls:
                        # 纯文本：要问 / 要汇报。跳出内层，转 waiting/idle 挂起。
                        produced_text = reply.text or '(模型没有给出文字内容)'
                        break

                    # 有 tool_calls：执行工具，结果 append 进上下文（内存 + 落盘），进入下一轮。
                    assistant_msg = {'role': 'assistant', 'content': reply.raw_content}
                    messages.append(assistant_msg)
                    self.append_message(agent_id, assistant_msg)

                    result_blocks = []
                    for call in reply.tool_calls:
                        result_text = await asyncio.to_thread(
                            _execute_tool_call,
                            call.name, call.input, project_root, github_token, shell_manager,
                        )
                        result_blocks.append({
                            'type': 'tool_result',
                            'tool_use_id': call.call_id,
                            'content': result_text,
                        })
                    tool_result_msg = {'role': 'user', 'content': result_blocks}
                    messages.append(tool_result_msg)
                    self.append_message(agent_id, tool_result_msg)
                else:
                    # 内层跑满 MAX_ITERATIONS 仍未产出纯文本：防跑飞，转挂起等待人工/AI介入。
                    produced_text = (
                        f'已连续执行 {MAX_ITERATIONS} 轮工具仍未收敛，暂时挂起等待进一步指示。'
                    )

                # ---- 内层结束：记录这段文本，判定完成态并挂起 ----
                if produced_text is not None:
                    text_msg = {'role': 'assistant', 'content': produced_text}
                    self.append_message(agent_id, text_msg)
                    await self._emit_agent_message(on_agent_message, agent_id, produced_text)

                # 完成态判定：本步简单处理——识别到完成标记转 idle，否则一律 waiting。
                # 精确的完成判定（第 4 步无权限总结 AI）后续再细化。
                if self._looks_done(produced_text):
                    self.set_status(agent_id, 'idle')
                else:
                    self.set_status(agent_id, 'waiting')

                # ---- 挂起：阻塞等待注入队列唤醒（不空转占 CPU）----
                injected = await queue.get()
                # 被唤醒：把这条注入消息落盘，回到 while 顶部继续。
                if isinstance(injected, dict):
                    self.append_message(agent_id, injected)
        finally:
            stopped_jobs = shell_manager.shutdown()
            if stopped_jobs:
                warn(
                    f'[AgentManager] agent={agent_id} 循环退出，'
                    f'自动停止后台 shell 任务: {", ".join(stopped_jobs)}'
                )
            # 循环退出（正常结束 / 被 cancel）时清理自身 Task 登记，避免悬挂引用。
            if self._agent_tasks.get(agent_id) is not None:
                try:
                    if self._agent_tasks[agent_id] is asyncio.current_task():
                        self._agent_tasks.pop(agent_id, None)
                except RuntimeError:
                    pass

    # ------------------------------------------------------------------
    # 常驻循环用到的内部辅助
    # ------------------------------------------------------------------
    # 约定的"任务完成"标记：模型在纯文本里输出该标记表示干完待命（转 idle）。
    # 本步先提供最简判定，第 4 步再由无权限总结 AI 做精确完成判定。
    AGENT_DONE_MARKER = '[[AGENT_DONE]]'

    @classmethod
    def _looks_done(cls, text: str | None) -> bool:
        return bool(text) and cls.AGENT_DONE_MARKER in str(text)

    def _replace_messages(self, agent_id: str, messages: list[dict]) -> dict | None:
        """整体回写某个 agent 的 messages（用于上下文裁剪后落盘）。"""
        agent_id = str(agent_id or '')

        def mutator(payload: dict):
            data = (payload.get('agents') or {}).get(agent_id)
            if not data:
                return None
            data['messages'] = [dict(m) for m in messages]
            data['updated_at'] = time.time()
            return dict(data)

        return self.store.update(mutator)

    @staticmethod
    async def _emit_agent_message(on_agent_message, agent_id: str, text: str) -> None:
        """触发 on_agent_message 钩子（第 3 步接全局待上报队列）。同步/异步回调均支持。"""
        if on_agent_message is None:
            return
        try:
            result = on_agent_message(agent_id, text)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            error(f'[AgentManager] on_agent_message 回调失败 agent={agent_id}: {exc}')

    # ------------------------------------------------------------------
    # 无工具权限的总结 AI 通道（第 4 步）
    # ------------------------------------------------------------------
    # 总结 AI 的 system prompt 基底：明确其只读、无工具、只做客观总结、绝不操作。
    _SUMMARY_SYSTEM_BASE = (
        '你是一个只读总结助手。你的唯一职责是：基于给定的某个后台 agent 的执行上下文，'
        '客观地总结它已经做了什么、当前进展到哪里、有没有潜在风险或未完成的隐患。'
        '严格约束：你没有任何工具权限，不能也不会执行任何文件、shell、网络或 GitHub 操作；'
        '你不做任何操作、不下达任何指令、不代替 agent 继续任务；你只输出一段客观、简洁、'
        '结构化的中文总结文字。不要编造上下文里没有的信息，看不出来的就如实说“无法判断”。'
    )
    # 渲染给总结 AI 的上下文最大字符数，超出则保留头尾、中间截断，避免超模型上限。
    _SUMMARY_MAX_CHARS = MAX_CONTEXT_CHARS

    @classmethod
    def _render_messages_for_summary(cls, messages: list[dict]) -> str:
        """把 agent 的 messages 上下文渲染成一段可读文本，供无工具总结 AI 阅读。

        只读渲染，不修改任何入参（messages 已经是 get_agent 返回的副本）。
        assistant 的工具调用、user 侧的 tool_result 都被拍平成文字描述。
        """
        lines: list[str] = []
        for msg in messages or []:
            role = str(msg.get('role') or '')
            content = msg.get('content')
            if isinstance(content, str):
                lines.append(f'[{role}] {content}')
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        lines.append(f'[{role}] {block}')
                        continue
                    btype = block.get('type')
                    if btype == 'text':
                        lines.append(f'[{role}] {block.get("text") or ""}')
                    elif btype == 'tool_use':
                        tool_input = json.dumps(block.get('input') or {}, ensure_ascii=False)
                        lines.append(
                            f'[{role} 调用工具] {block.get("name") or ""} 参数={tool_input}'
                        )
                    elif btype == 'tool_result':
                        result_text = block.get('content')
                        if not isinstance(result_text, str):
                            result_text = json.dumps(result_text, ensure_ascii=False)
                        lines.append(f'[工具结果] {result_text}')
                    else:
                        lines.append(f'[{role}] {json.dumps(block, ensure_ascii=False)}')
            elif content is not None:
                lines.append(f'[{role}] {content}')
        rendered = '\n'.join(lines)
        # 超长则保留头尾、中间省略，保证总结 AI 能看到起始任务与最新进展。
        limit = cls._SUMMARY_MAX_CHARS
        if len(rendered) > limit:
            head = rendered[: limit // 2]
            tail = rendered[-(limit // 2):]
            rendered = f'{head}\n\n……（中间上下文过长已省略）……\n\n{tail}'
        return rendered

    async def summarize_agent(self, agent_id: str, purpose: str = 'progress', model=None) -> str:
        """用一个【无任何工具权限】的模型调用，对某 agent 的执行上下文做只读总结。

        供两个场景共用：
        - peek_agent 进度总结：purpose='progress'，agent 还在跑，总结当前进展。
        - destroy_agent 销毁前总结：purpose='destroy'，总结已完成的操作与可能留下的隐患。

        无工具保证：调用 model.complete 时 tools 参数【固定传 None】，所以这个总结 AI
        拿不到任何工具 schema，绝不可能触发文件/shell/GitHub 操作，只会返回纯文本。

        不打断主 agent（peek 场景）：本方法通过 get_agent 拿到的是持久化记录的【副本】，
        只读渲染成文本喂给总结 AI，全程不调用 append_message / set_status / _replace_messages，
        也不往注入队列投递，因此不会修改 agent 的 messages、状态，也不会打断正在跑的循环。

        返回总结文本；agent 不存在或无可用模型时返回说明性文字，不抛异常打断上层。
        """
        agent_id = str(agent_id or '')
        record = self.get_agent(agent_id)  # 副本，只读
        if not record:
            return f'（找不到 agent {agent_id}，无法总结。）'

        use_model = model if model is not None else self._model
        if use_model is None:
            return '（当前没有可用的模型实例，无法生成总结。）'

        messages = list(record.get('messages') or [])
        instruction = str(record.get('instruction') or '')
        status = str(record.get('status') or '')
        rendered = self._render_messages_for_summary(messages)

        if purpose == 'destroy':
            purpose_hint = (
                '当前用途：这个 agent 即将被销毁（强制中断并移除）。请重点总结：'
                '它到目前为止已经完成/执行了哪些实质操作（尤其是对文件、仓库、外部状态的改动），'
                '以及销毁后可能遗留的隐患或未收尾的事项（例如改了一半的文件、未提交/未验证的改动、'
                '仍需人工跟进的点）。'
            )
        else:  # 'progress' 及其它一律按进度总结处理
            purpose_hint = (
                '当前用途：这个 agent 仍在运行，需要一份进度汇报。请重点总结：'
                '它当前进行到哪一步、已经做了什么、正在等待什么或下一步可能要做什么、'
                '是否出现异常或潜在风险。'
            )

        user_prompt = (
            f'{purpose_hint}\n\n'
            f'agent 当前状态：{status}\n'
            f'agent 原始任务指令：\n{instruction}\n\n'
            f'以下是该 agent 的完整执行上下文（含它的思考、工具调用与工具返回结果）：\n'
            f'------8<------\n{rendered}\n------8<------\n\n'
            '请基于以上上下文输出一段客观总结。'
        )

        try:
            reply = await asyncio.to_thread(
                use_model.complete,
                self._SUMMARY_SYSTEM_BASE,  # system：只读总结助手角色约束
                [{'role': 'user', 'content': user_prompt}],
                None,  # tools 固定为 None —— 这是"无工具权限"的关键保证
                None,  # model_name 用实例默认
                0.3,   # 低温，偏客观
                2048,  # max_tokens
            )
        except Exception as exc:
            error(f'[AgentManager] summarize_agent 模型调用失败 agent={agent_id}: {exc}')
            return f'（总结生成失败：{exc}）'

        summary = (reply.text if reply else '').strip()
        return summary or '（总结为空。）'
