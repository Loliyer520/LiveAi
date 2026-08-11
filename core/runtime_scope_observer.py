from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from core.character_session import ScopeIdentity
from core.event_mailbox import InMemoryEventMailbox


@dataclass(frozen=True)
class RuntimeScopeObservation:
    """Immutable, request-time view of the current orchestrator owners.

    ``consistent`` only means two consecutive best-effort samples matched. The
    orchestrator currently has no single lock spanning active scopes, mailbox,
    pending tasks and asyncio.Queue, so this interface deliberately does not
    claim a transactional snapshot.
    """

    scope_type: str
    scope_id: str
    scope_key: str
    active: bool
    pending_event_count: int
    pending_task_count: int
    runtime_queue_size: int | None
    consistent: bool

    @property
    def busy(self) -> bool:
        return self.active or self.pending_event_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            'scope_type': self.scope_type,
            'scope_id': self.scope_id,
            'scope_key': self.scope_key,
            'active': self.active,
            'pending_event_count': self.pending_event_count,
            'pending_task_count': self.pending_task_count,
            'runtime_queue_size': self.runtime_queue_size,
            'consistent': self.consistent,
            'busy': self.busy,
        }


class RuntimeScopeObserver:
    """One-way adapter from runtime owners to immutable observations.

    It never mutates CharacterSession or runtime state and performs no shadow
    writes. Counts are sampled twice to expose concurrent changes rather than
    pretending cross-owner atomicity.
    """

    def __init__(
        self,
        *,
        is_active: Callable[[str], bool],
        mailbox: InMemoryEventMailbox,
        pending_task_count: Callable[[str], int],
        queue_size: Callable[[], int] | None = None,
    ) -> None:
        if not callable(is_active):
            raise TypeError('is_active must be callable')
        self._is_active = is_active
        self._mailbox = mailbox
        self._pending_task_count = pending_task_count
        self._queue_size = queue_size

    def _sample(self, scope_key: str) -> tuple[bool, int, int, int | None]:
        active = bool(self._is_active(scope_key))
        pending_events = self._mailbox.pending_count(scope_key)
        pending_tasks = int(self._pending_task_count(scope_key))
        queue_size = None if self._queue_size is None else int(self._queue_size())
        return active, pending_events, pending_tasks, queue_size

    def observe(self, scope_type: str, scope_id: str) -> RuntimeScopeObservation:
        identity = ScopeIdentity(str(scope_type), str(scope_id))
        first = self._sample(identity.scope_key)
        second = self._sample(identity.scope_key)
        active, pending_events, pending_tasks, queue_size = second
        return RuntimeScopeObservation(
            scope_type=identity.scope_type,
            scope_id=identity.scope_id,
            scope_key=identity.scope_key,
            active=active,
            pending_event_count=pending_events,
            pending_task_count=pending_tasks,
            runtime_queue_size=queue_size,
            consistent=first == second,
        )

    def observe_key(self, scope_key: str) -> RuntimeScopeObservation:
        identity = ScopeIdentity.from_scope_key(scope_key)
        return self.observe(identity.scope_type, identity.scope_id)
