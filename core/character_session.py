from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
from typing import Any

from core.event_envelope import EventEnvelope
from core.event_mailbox import EventBatch, InMemoryEventMailbox, MailboxEntry


@dataclass(frozen=True)
class ScopeIdentity:
    scope_type: str
    scope_id: str

    def __post_init__(self) -> None:
        if not str(self.scope_type or '').strip() or ':' in str(self.scope_type):
            raise ValueError('scope_type must be non-empty and contain no colon')
        if not str(self.scope_id or '').strip():
            raise ValueError('scope_id must be non-empty')

    @property
    def scope_key(self) -> str:
        return f'{self.scope_type}:{self.scope_id}'

    @classmethod
    def from_scope_key(cls, scope_key: str) -> 'ScopeIdentity':
        scope_type, separator, scope_id = str(scope_key or '').partition(':')
        if not separator:
            raise ValueError('scope_key must be <scope_type>:<scope_id>')
        return cls(scope_type=scope_type, scope_id=scope_id)



@dataclass(frozen=True)
class CharacterSessionSnapshot:
    scope_type: str
    scope_id: str
    scope_key: str
    active: bool
    pending_event_count: int
    pending_task_count: int
    retired: bool

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
            'retired': self.retired,
            'busy': self.busy,
        }

class CharacterSession:
    """Single-scope, process-local ownership boundary.

    This class owns no model/provider behavior. It only centralizes the state a
    future single-consumer scope actor needs: active ownership, its mailbox
    partition and message-before-task promotion. It is not wired into the
    orchestrator yet.
    """

    def __init__(
        self,
        identity: ScopeIdentity,
        *,
        mailbox: InMemoryEventMailbox | None = None,
    ) -> None:
        if not isinstance(identity, ScopeIdentity):
            raise TypeError('identity must be a ScopeIdentity')
        self.identity = identity
        self.mailbox = mailbox or InMemoryEventMailbox()
        self._lock = threading.Lock()
        self._active = False
        self._retired = False
        self._pending_tasks: deque[dict[str, Any]] = deque()

    def snapshot(self) -> CharacterSessionSnapshot:
        """Return an immutable observation without changing session state."""
        with self._lock:
            return CharacterSessionSnapshot(
                scope_type=self.identity.scope_type,
                scope_id=self.identity.scope_id,
                scope_key=self.scope_key,
                active=self._active,
                pending_event_count=self.mailbox.pending_count(self.scope_key),
                pending_task_count=len(self._pending_tasks),
                retired=self._retired,
            )

    @property
    def scope_key(self) -> str:
        return self.identity.scope_key

    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def is_busy(self) -> bool:
        return self.is_active() or self.pending_event_count() > 0

    def activate(self) -> bool:
        with self._lock:
            if self._retired:
                raise RuntimeError('character session is retired')
            if self._active:
                return False
            self._active = True
            return True

    def deactivate(self) -> None:
        with self._lock:
            self._active = False

    def append_event(self, envelope: EventEnvelope, *, transient: Any = None) -> EventEnvelope:
        if envelope.scope_key != self.scope_key:
            raise ValueError(f'event scope mismatch: {envelope.scope_key} != {self.scope_key}')
        # Session lock is always acquired before the shared mailbox lock.
        with self._lock:
            if self._retired:
                raise RuntimeError('character session is retired')
            return self.mailbox.append(envelope, transient=transient)

    def pop_raw_entry(self) -> MailboxEntry | None:
        return self.mailbox.pop_scope_entry(self.scope_key)

    def drain_event_batch(self) -> EventBatch | None:
        return self.mailbox.drain_scope(self.scope_key)

    def pending_event_count(self) -> int:
        return self.mailbox.pending_count(self.scope_key)

    def append_task(self, task: dict[str, Any]) -> int:
        if not isinstance(task, dict):
            raise TypeError('task must be a dict')
        with self._lock:
            if self._retired:
                raise RuntimeError('character session is retired')
            count_before = len(self._pending_tasks)
            self._pending_tasks.append(task)
            return count_before

    def pending_task_count(self) -> int:
        with self._lock:
            return len(self._pending_tasks)

    def promote_task_if_mailbox_empty(self) -> dict[str, Any] | None:
        # Lock order is session -> mailbox throughout this class.
        with self._lock:
            if self.mailbox.pending_count(self.scope_key) > 0:
                return None
            if not self._pending_tasks:
                return None
            return self._pending_tasks.popleft()

    def clear_pending_tasks(self) -> None:
        with self._lock:
            if self._retired:
                raise RuntimeError('character session is retired')
            self._pending_tasks.clear()

    def retire_if_idle(self) -> bool:
        """Retire only when no accepted event/task/active turn remains.

        Once this returns True, stale holders cannot activate or append work.
        """
        with self._lock:
            if (
                self._active
                or self._pending_tasks
                or self.mailbox.pending_count(self.scope_key) > 0
            ):
                return False
            self._retired = True
            return True

    def is_retired(self) -> bool:
        with self._lock:
            return self._retired

    def clear_runtime_state(self) -> None:
        # Block session writers while draining this scope.
        with self._lock:
            self.mailbox.drain_scope(self.scope_key)
            self._active = False
            self._pending_tasks.clear()
class CharacterSessionRegistry:
    """Thread-safe identity map for one CharacterSession per scope."""

    def __init__(self, *, mailbox: InMemoryEventMailbox | None = None) -> None:
        self._mailbox = mailbox or InMemoryEventMailbox()
        self._lock = threading.Lock()
        self._sessions: dict[str, CharacterSession] = {}

    @property
    def mailbox(self) -> InMemoryEventMailbox:
        return self._mailbox

    def get_or_create(self, scope_type: str, scope_id: str) -> CharacterSession:
        identity = ScopeIdentity(str(scope_type), str(scope_id))
        scope_key = identity.scope_key
        with self._lock:
            session = self._sessions.get(scope_key)
            if session is None:
                session = CharacterSession(identity, mailbox=self._mailbox)
                self._sessions[scope_key] = session
            return session

    def snapshots(self) -> tuple[CharacterSessionSnapshot, ...]:
        """Observe current sessions in stable scope-key order."""
        with self._lock:
            sessions = tuple(
                self._sessions[key] for key in sorted(self._sessions)
            )
        return tuple(session.snapshot() for session in sessions)

    def get(self, scope_key: str) -> CharacterSession | None:
        with self._lock:
            return self._sessions.get(str(scope_key))

    def list_scope_keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._sessions))

    def is_active(self, scope_key: str) -> bool:
        session = self.get(str(scope_key))
        return False if session is None else session.is_active()

    def pending_task_count(self, scope_key: str) -> int:
        session = self.get(str(scope_key))
        return 0 if session is None else session.pending_task_count()

    def pop_tool_raw(self, scope_key: str) -> Any:
        session = self.get(str(scope_key))
        if session is None:
            return None
        entry = session.pop_raw_entry()
        if entry is None:
            return None
        if entry.transient is None:
            raise RuntimeError(
                f'pending mailbox entry missing transient item: {scope_key}'
            )
        return entry.transient

    def append_pending_task(self, scope_key: str, task: dict[str, Any]) -> int:
        identity = ScopeIdentity.from_scope_key(scope_key)
        session = self.get_or_create(identity.scope_type, identity.scope_id)
        return session.append_task(task)

    def promote_pending_task_if_mailbox_empty(
        self, scope_key: str,
    ) -> dict[str, Any] | None:
        session = self.get(str(scope_key))
        return None if session is None else session.promote_task_if_mailbox_empty()

    def clear_pending_tasks(self, scope_key: str | None = None) -> None:
        if scope_key is not None:
            session = self.get(str(scope_key))
            if session is not None:
                session.clear_pending_tasks()
            return
        for key in self.list_scope_keys():
            self.clear_pending_tasks(key)

    def activate(self, scope_key: str) -> bool:
        identity = ScopeIdentity.from_scope_key(scope_key)
        return self.get_or_create(identity.scope_type, identity.scope_id).activate()

    def deactivate(self, scope_key: str) -> None:
        session = self.get(str(scope_key))
        if session is not None:
            session.deactivate()

    def clear_active(self) -> None:
        """Clear active bits only; mailbox and task owners remain untouched."""
        with self._lock:
            sessions = tuple(self._sessions.values())
        for session in sessions:
            session.deactivate()

    def discard_if_idle(self, scope_key: str) -> bool:
        with self._lock:
            session = self._sessions.get(str(scope_key))
            if session is None:
                return False
            if not session.retire_if_idle():
                return False
            del self._sessions[str(scope_key)]
            return True

    def clear_runtime_state(self) -> None:
        with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.clear_runtime_state()
        self._mailbox.clear()
