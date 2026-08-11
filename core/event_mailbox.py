from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import threading
from typing import Any, Iterable

from core.event_envelope import EventEnvelope


@dataclass(frozen=True)
class MailboxEntry:
    """An envelope plus optional process-local, identity-sensitive data.

    ``transient`` is retained by reference and is deliberately not part of the
    EventEnvelope serialization contract.
    """

    envelope: EventEnvelope
    transient: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, EventEnvelope):
            raise TypeError('envelope must be an EventEnvelope')


@dataclass(frozen=True)
class EventBatch:
    scope_key: str
    events: tuple[EventEnvelope, ...]
    transients: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if not self.scope_key:
            raise ValueError('scope_key must be non-empty')
        if not self.events:
            raise ValueError('event batch must not be empty')
        if any(event.scope_key != self.scope_key for event in self.events):
            raise ValueError('all batch events must belong to scope_key')
        if not self.transients:
            object.__setattr__(self, 'transients', (None,) * len(self.events))
        elif len(self.transients) != len(self.events):
            raise ValueError('transients must align one-to-one with events')

    @property
    def entries(self) -> tuple[MailboxEntry, ...]:
        return tuple(
            MailboxEntry(envelope=event, transient=transient)
            for event, transient in zip(self.events, self.transients)
        )

    @property
    def first_sequence(self) -> int | None:
        return self.events[0].mailbox_sequence

    @property
    def last_sequence(self) -> int | None:
        return self.events[-1].mailbox_sequence

    def merged_payload(self) -> dict:
        """Neutral batch representation for a future actor adapter.

        Transient objects are intentionally excluded: this representation is
        entirely derived from JSON-serializable EventEnvelope values.
        """
        return {
            'scope_key': self.scope_key,
            'event_count': len(self.events),
            'events': [event.to_dict() for event in self.events],
        }


class InMemoryEventMailbox:
    """Thread-safe, process-local FIFO queues partitioned by scope.

    This class performs no I/O and has no restore method. Creating a new
    instance always starts empty. It is a shadow primitive until an
    orchestrator consumer-owner cutover is explicitly approved.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scope_queues: dict[str, deque[MailboxEntry]] = {}
        self._next_sequence = 1

    def append(self, event: EventEnvelope, transient: Any = None) -> EventEnvelope:
        """Append one event and return its sequence-assigned envelope."""
        return self.append_entry(event, transient=transient).envelope

    def append_entry(self, event: EventEnvelope, transient: Any = None) -> MailboxEntry:
        """Append one event and retain ``transient`` by object identity."""
        if not isinstance(event, EventEnvelope):
            raise TypeError('event must be an EventEnvelope')
        with self._lock:
            queued = replace(event, mailbox_sequence=self._next_sequence)
            entry = MailboxEntry(envelope=queued, transient=transient)
            self._scope_queues.setdefault(queued.scope_key, deque()).append(entry)
            self._next_sequence += 1
            return entry

    def append_many(self, events: Iterable[EventEnvelope]) -> tuple[EventEnvelope, ...]:
        """Validate and append the complete iterable as one atomic commit."""
        pending = tuple(events)
        if any(not isinstance(event, EventEnvelope) for event in pending):
            raise TypeError('all events must be EventEnvelope instances')
        with self._lock:
            entries = tuple(
                MailboxEntry(
                    envelope=replace(event, mailbox_sequence=self._next_sequence + offset)
                )
                for offset, event in enumerate(pending)
            )
            for entry in entries:
                self._scope_queues.setdefault(entry.envelope.scope_key, deque()).append(entry)
            self._next_sequence += len(entries)
        return tuple(entry.envelope for entry in entries)

    def pop_scope(self, scope_key: str) -> EventEnvelope | None:
        """Atomically remove one FIFO event while preserving later arrivals."""
        entry = self.pop_scope_entry(scope_key)
        return None if entry is None else entry.envelope

    def pop_scope_entry(self, scope_key: str) -> MailboxEntry | None:
        """Atomically remove one FIFO entry, including its transient object."""
        scope_key = str(scope_key or '').strip()
        if not scope_key:
            raise ValueError('scope_key must be non-empty')
        with self._lock:
            queue = self._scope_queues.get(scope_key)
            if not queue:
                self._scope_queues.pop(scope_key, None)
                return None
            entry = queue.popleft()
            if not queue:
                self._scope_queues.pop(scope_key, None)
            return entry

    def drain_scope(self, scope_key: str) -> EventBatch | None:
        scope_key = str(scope_key or '').strip()
        if not scope_key:
            raise ValueError('scope_key must be non-empty')
        with self._lock:
            queue = self._scope_queues.pop(scope_key, None)
            if not queue:
                return None
            entries = tuple(queue)
        return EventBatch(
            scope_key=scope_key,
            events=tuple(entry.envelope for entry in entries),
            transients=tuple(entry.transient for entry in entries),
        )

    def pending_count(self, scope_key: str | None = None) -> int:
        with self._lock:
            if scope_key is None:
                return sum(len(queue) for queue in self._scope_queues.values())
            return len(self._scope_queues.get(str(scope_key), ()))

    def pending_scopes(self) -> tuple[str, ...]:
        with self._lock:
            scopes = [
                (queue[0].envelope.mailbox_sequence, scope_key)
                for scope_key, queue in self._scope_queues.items()
                if queue
            ]
        scopes.sort()
        return tuple(scope_key for _sequence, scope_key in scopes)

    def clear(self) -> None:
        with self._lock:
            self._scope_queues.clear()

    def is_empty(self) -> bool:
        return self.pending_count() == 0
