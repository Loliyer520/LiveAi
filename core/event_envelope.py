from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import time
from typing import Any, Mapping
import uuid


class EventType(str, Enum):
    """Events that may eventually enter a scope actor mailbox."""

    MESSAGE = 'message'
    ALARM = 'alarm'
    RECURRING_TASK = 'recurring_task'
    AGENT_REPORT = 'agent_report'
    MAIN_AI_MESSAGE = 'main_ai_message'
    SYSTEM = 'system'


@dataclass(frozen=True)
class EventEnvelope:
    """Transport-neutral, JSON-serializable event for one AI scope.

    ``mailbox_sequence`` is assigned by an in-memory mailbox. It is runtime
    ordering metadata, not a persistence cursor.
    """

    event_type: EventType
    scope_type: str
    scope_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = 'unknown'
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    occurred_at: float = field(default_factory=time.time)
    mailbox_sequence: int | None = None

    def __post_init__(self) -> None:
        event_type = self.event_type
        if not isinstance(event_type, EventType):
            try:
                event_type = EventType(str(event_type))
            except ValueError as exc:
                raise ValueError(f'unsupported event_type: {self.event_type!r}') from exc
            object.__setattr__(self, 'event_type', event_type)
        scope_type = str(self.scope_type or '').strip()
        scope_id = str(self.scope_id or '').strip()
        source = str(self.source or '').strip()
        event_id = str(self.event_id or '').strip()
        if not scope_type or ':' in scope_type:
            raise ValueError('scope_type must be non-empty and must not contain colon')
        if not scope_id:
            raise ValueError('scope_id must be non-empty')
        if not source:
            raise ValueError('source must be non-empty')
        if not event_id:
            raise ValueError('event_id must be non-empty')
        if not isinstance(self.payload, dict):
            raise TypeError('payload must be a dict')
        try:
            json.dumps(self.payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError('payload must be JSON-serializable') from exc
        occurred_at = float(self.occurred_at)
        if occurred_at < 0:
            raise ValueError('occurred_at must be non-negative')
        sequence = self.mailbox_sequence
        if sequence is not None and (not isinstance(sequence, int) or sequence < 1):
            raise ValueError('mailbox_sequence must be a positive integer or None')
        object.__setattr__(self, 'scope_type', scope_type)
        object.__setattr__(self, 'scope_id', scope_id)
        object.__setattr__(self, 'source', source)
        object.__setattr__(self, 'event_id', event_id)
        object.__setattr__(self, 'occurred_at', occurred_at)
        object.__setattr__(self, 'payload', dict(self.payload))

    @property
    def scope_key(self) -> str:
        return f'{self.scope_type}:{self.scope_id}'

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data['event_type'] = self.event_type.value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'EventEnvelope':
        if not isinstance(data, Mapping):
            raise TypeError('event envelope data must be a mapping')
        required = {'event_type', 'scope_type', 'scope_id', 'payload', 'source', 'event_id', 'occurred_at'}
        missing = sorted(required - set(data))
        if missing:
            raise ValueError(f'missing event envelope fields: {", ".join(missing)}')
        return cls(
            event_type=EventType(str(data['event_type'])),
            scope_type=str(data['scope_type']),
            scope_id=str(data['scope_id']),
            payload=dict(data['payload']),
            source=str(data['source']),
            event_id=str(data['event_id']),
            occurred_at=float(data['occurred_at']),
            mailbox_sequence=data.get('mailbox_sequence'),
        )
