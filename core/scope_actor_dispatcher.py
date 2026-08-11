from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from core.character_session import CharacterSessionRegistry
from core.event_envelope import EventEnvelope
from core.event_mailbox import InMemoryEventMailbox
from core.scope_actor_registry import ScopeActorRegistry


ItemConsumer = Callable[[str, dict[str, Any]], Awaitable[None]]
IdleCallback = Callable[[str], None]
StalePredicate = Callable[[dict[str, Any]], bool]


class ScopeActorDispatcher:
    """In-memory single-consumer actor dispatcher partitioned by scope key."""

    def __init__(
        self,
        *,
        mailbox: InMemoryEventMailbox,
        sessions: CharacterSessionRegistry,
        consume: ItemConsumer,
        is_stale: StalePredicate | None = None,
        on_idle: IdleCallback | None = None,
    ) -> None:
        if not isinstance(mailbox, InMemoryEventMailbox):
            raise TypeError('mailbox must be an InMemoryEventMailbox')
        if not isinstance(sessions, CharacterSessionRegistry):
            raise TypeError('sessions must be a CharacterSessionRegistry')
        if not callable(consume):
            raise TypeError('consume must be callable')
        self.mailbox = mailbox
        self.sessions = sessions
        self._consume = consume
        self._is_stale = is_stale or (lambda _item: False)
        self._on_idle = on_idle
        self._actors = ScopeActorRegistry(self._run_actor)

    def submit_event(self, envelope: EventEnvelope, transient: dict[str, Any]) -> None:
        if not isinstance(envelope, EventEnvelope):
            raise TypeError('envelope must be an EventEnvelope')
        if not isinstance(transient, dict):
            raise TypeError('transient must be a dict')
        self.mailbox.append(envelope, transient=transient)
        self._actors.ensure(envelope.scope_key)
        self._actors.wake(envelope.scope_key)

    def submit_task(self, scope_key: str, item: dict[str, Any]) -> None:
        scope_key = str(scope_key or '').strip()
        if not scope_key:
            raise ValueError('scope_key must be non-empty')
        if not isinstance(item, dict):
            raise TypeError('item must be a dict')
        self.sessions.append_pending_task(scope_key, item)
        self._actors.ensure(scope_key)
        self._actors.wake(scope_key)

    def wake(self, scope_key: str) -> None:
        self._actors.wake(scope_key)

    def active_actor_count(self) -> int:
        return self._actors.active_count()

    def actor_keys(self) -> tuple[str, ...]:
        return self._actors.keys()

    def clear_runtime_state(self) -> None:
        self.mailbox.clear()
        self.sessions.clear_pending_tasks()
        self.sessions.clear_active()

    async def close(self) -> None:
        await self._actors.close()
        self.clear_runtime_state()

    async def _run_actor(self, scope_key: str, wakeup: asyncio.Event) -> None:
        session = self.sessions.get_or_create(*scope_key.split(':', 1))
        try:
            while True:
                await wakeup.wait()
                wakeup.clear()
                while True:
                    item = self._next_item(scope_key)
                    if item is None:
                        session.deactivate()
                        if self._on_idle is not None:
                            self._on_idle(scope_key)
                        break
                    if self._is_stale(item):
                        continue
                    if not session.is_active():
                        session.activate()
                    await self._consume(scope_key, item)
        finally:
            session.deactivate()

    def _next_item(self, scope_key: str) -> dict[str, Any] | None:
        entry = self.mailbox.pop_scope_entry(scope_key)
        if entry is not None:
            if not isinstance(entry.transient, dict):
                raise RuntimeError(f'mailbox transient must be a dict: {scope_key}')
            return entry.transient
        return self.sessions.promote_pending_task_if_mailbox_empty(scope_key)
