from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from core.scope_actor_dispatcher import ScopeActorDispatcher


TaskLoader = Callable[[str], Awaitable[Mapping[str, Any] | None]]
ScopeResolver = Callable[[Mapping[str, Any]], str | None]
StalePredicate = Callable[[dict[str, Any]], bool]
ErrorCallback = Callable[[Exception], None]


class TaskIngressRouter:
    """Route task ingress items to their single-consumer scope actor."""

    # run() 并发加载任务的并发上限；同一 scope_key 仍由 actor 串行消费。
    _route_concurrency = 8

    def __init__(
        self,
        *,
        queue: asyncio.Queue,
        dispatcher: ScopeActorDispatcher,
        load_task: TaskLoader,
        resolve_scope: ScopeResolver,
        is_stale: StalePredicate | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        if not isinstance(queue, asyncio.Queue):
            raise TypeError('queue must be an asyncio.Queue')
        if not isinstance(dispatcher, ScopeActorDispatcher):
            raise TypeError('dispatcher must be a ScopeActorDispatcher')
        if not callable(load_task):
            raise TypeError('load_task must be callable')
        if not callable(resolve_scope):
            raise TypeError('resolve_scope must be callable')
        self.queue = queue
        self.dispatcher = dispatcher
        self._load_task = load_task
        self._resolve_scope = resolve_scope
        self._is_stale = is_stale or (lambda _item: False)
        self._on_error = on_error

    async def run(self) -> None:
        # 主 AI 任务的入口是单一 queue + 单一消费者；串行 await load_task
        # 会让一个慢任务阻塞后面所有任务入队分发。这里改为受限并发加载：
        # 队列顺序保证取回顺序，route 以最多 `_route_concurrency` 个并发执行，
        # 同一 scope_key 的任务仍由 actor 按提交顺序串行消费，跨 scope 不再互相阻塞。
        pending: set[asyncio.Task] = set()
        semaphore = asyncio.Semaphore(self._route_concurrency)

        async def _process(item):
            async with semaphore:
                try:
                    await self.route(item)
                except Exception as exc:
                    # 失败任务落失败状态（item 为 dict 时原地标注），避免静默丢失；
                    # 后续仍由 task_done 保证队列 join 语义。
                    try:
                        item['status'] = 'failed'
                        item['error'] = str(exc)
                    except Exception:
                        pass
                    if self._on_error is not None:
                        self._on_error(exc)
                finally:
                    self.queue.task_done()

        while True:
            item = await self.queue.get()
            task = asyncio.create_task(_process(item))
            pending.add(task)
            task.add_done_callback(pending.discard)

    async def route(self, item: dict[str, Any]) -> str | None:
        if not isinstance(item, dict):
            raise TypeError('task ingress item must be a dict')
        if self._is_stale(item) or item.get('kind') != 'task':
            return None

        task_id = str(item.get('task_id') or '').strip()
        if not task_id:
            raise ValueError('task ingress item must include task_id')
        task = await self._load_task(task_id)
        target_scope = self._resolve_scope(task or {})
        scope_key = str(target_scope or f'task:{task_id}')

        if target_scope:
            item['scope_prereserved'] = True
        item['scope_key'] = scope_key
        self.dispatcher.submit_task(scope_key, item)
        return scope_key
