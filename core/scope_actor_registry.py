from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


ActorConsumer = Callable[[str, asyncio.Event], Awaitable[None]]


class ScopeActorRegistry:
    """Process-local registry that owns exactly one consumer task per key."""

    def __init__(self, consumer: ActorConsumer) -> None:
        if not callable(consumer):
            raise TypeError('consumer must be callable')
        self._consumer = consumer
        self._tasks: dict[str, asyncio.Task] = {}
        self._events: dict[str, asyncio.Event] = {}

    def ensure(self, key: str) -> asyncio.Task:
        key = str(key or '').strip()
        if not key:
            raise ValueError('actor key must be non-empty')
        task = self._tasks.get(key)
        if task is not None and not task.done():
            return task
        event = asyncio.Event()
        self._events[key] = event
        task = asyncio.create_task(
            self._consumer(key, event),
            name=f'scope-actor:{key}',
        )
        self._tasks[key] = task
        task.add_done_callback(lambda finished, actor_key=key: self._discard(actor_key, finished))
        return task

    def wake(self, key: str) -> None:
        event = self._events.get(str(key or ''))
        if event is not None:
            event.set()

    def active_count(self) -> int:
        return sum(not task.done() for task in self._tasks.values())

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(key for key, task in self._tasks.items() if not task.done()))

    async def close(self) -> None:
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._events.clear()

    def _discard(self, key: str, task: asyncio.Task) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
            self._events.pop(key, None)
