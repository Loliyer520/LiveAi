from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from core.event_envelope import EventEnvelope, EventType
from core.event_normalizer import NormalizedEvent
from core.event_mailbox import EventBatch
from core.events import ChatMessage


_SUPPORTED_TURN_KINDS = {'message', 'task', 'report'}
_TRANSIENT_TURN_FIELDS = {
    'scope_prereserved',
    'mailbox_event_ids',
    'mailbox_sequences',
}


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f'{label} must be a mapping')
    return value


def _scope_parts(scope_key: Any, label: str = 'scope_key') -> tuple[str, str]:
    scope_type, separator, scope_id = str(scope_key or '').strip().partition(':')
    if not separator or not scope_type or not scope_id:
        raise ValueError(f'{label} must be <scope_type>:<scope_id>')
    return scope_type, scope_id


def _turn_kind(item: Mapping[str, Any]) -> str:
    if 'kind' not in item:
        raise ValueError('scope turn item must include kind')
    kind = str(item['kind'])
    if kind not in _SUPPORTED_TURN_KINDS:
        raise ValueError(f'unsupported scope turn item kind: {kind!r}')
    return kind


def _safe_occurred_at(value: Any, label: str) -> float:
    if value is None or value == '':
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label} must be numeric, empty, or None') from exc


def envelope_from_normalized_event(event: NormalizedEvent) -> EventEnvelope:
    """Adapt an existing normalized NapCat event without changing dispatch."""
    if not isinstance(event, NormalizedEvent):
        raise TypeError('event must be a NormalizedEvent')
    if not event.scope_type or not event.scope_id:
        raise ValueError('normalized event has no AI scope')
    return EventEnvelope(
        event_type=EventType.MESSAGE,
        scope_type=event.scope_type,
        scope_id=event.scope_id,
        source='napcat',
        event_id=event.source_key,
        occurred_at=float(event.payload.get('_normalized_at') or event.payload.get('time') or 0.0),
        payload=deepcopy(event.payload),
    )


def envelope_from_agent_report(report: Mapping[str, Any]) -> EventEnvelope:
    """Adapt an AgentManager report while retaining its complete report data."""
    report = _require_mapping(report, 'report')
    if 'origin_scope' not in report:
        raise ValueError('agent report must include origin_scope')
    scope_type, scope_id = _scope_parts(report['origin_scope'], 'agent report origin_scope')
    agent_id = str(report.get('agent_id') or '').strip()
    if not agent_id:
        raise ValueError('agent report must include agent_id')
    if 'ts' not in report:
        raise ValueError('agent report must include ts')
    occurred_at = _safe_occurred_at(report['ts'], 'agent report ts')
    event_id_value = report.get('event_id')
    event_id = str(event_id_value) if event_id_value not in (None, '') else f'agent-report:{agent_id}:{occurred_at}'
    return EventEnvelope(
        event_type=EventType.AGENT_REPORT,
        scope_type=scope_type,
        scope_id=scope_id,
        source=f'agent:{agent_id}',
        event_id=event_id,
        occurred_at=occurred_at,
        payload=deepcopy(dict(report)),
    )


def create_scoped_event(
    event_type: EventType,
    scope_key: str,
    payload: Mapping[str, Any],
    *,
    source: str,
    event_id: str,
    occurred_at: float,
) -> EventEnvelope:
    """Create alarm/task/main-AI envelopes without defining delivery policy."""
    scope_type, scope_id = _scope_parts(scope_key)
    payload = _require_mapping(payload, 'payload')
    return EventEnvelope(
        event_type=event_type,
        scope_type=scope_type,
        scope_id=scope_id,
        payload=deepcopy(dict(payload)),
        source=source,
        event_id=event_id,
        occurred_at=occurred_at,
    )


def envelope_from_scope_turn_item(item: Mapping[str, Any]) -> EventEnvelope:
    """Serialize a message, promoted task, or report turn for mailbox ownership."""
    item = _require_mapping(item, 'scope turn item')
    kind = _turn_kind(item)
    message = item.get('message')

    if message is not None:
        if not isinstance(message, ChatMessage):
            raise TypeError('scope turn item message must be a ChatMessage')
        scope_type = str(message.chat_type)
        scope_id = str(message.chat_id)
        raw_data = deepcopy(dict(message.raw_data or {}))
        system_event = str(raw_data.get('system_event') or '')
        event_type = {
            'agent_message': EventType.AGENT_REPORT,
            'alarm': EventType.ALARM,
            'recurring_task': EventType.RECURRING_TASK,
            'main_ai_message': EventType.MAIN_AI_MESSAGE,
        }.get(system_event, {
            'message': EventType.MESSAGE,
            'task': EventType.RECURRING_TASK,
            'report': EventType.AGENT_REPORT,
        }[kind])
        payload = {
            'message': {
                'chat_type': message.chat_type,
                'chat_id': message.chat_id,
                'user_id': message.user_id,
                'text': message.text,
                'raw_message': message.raw_message,
                'sender': deepcopy(dict(message.sender or {})),
                'message_id': deepcopy(message.message_id),
                'mentions_self': bool(message.mentions_self),
                'timestamp': deepcopy(message.timestamp),
                'raw_data': raw_data,
            },
            'kind': kind,
            'cleaned': deepcopy(item.get('cleaned', '')),
            'agent_id': deepcopy(item.get('agent_id', '')),
            'deferred_count': deepcopy(item.get('deferred_count', 0)),
            'trigger_messages': deepcopy(item.get('trigger_messages', [])),
            'message_epoch': deepcopy(item.get('message_epoch')),
            'history_seed': deepcopy(item.get('history_seed')),
            'metadata': deepcopy(item.get('metadata')),
            'silent_event': bool(item.get('silent_event', False)),
        }
        source = str(raw_data.get('source') or raw_data.get('system_event') or kind)
        message_id = message.message_id
        event_id = str(message_id) if message_id not in (None, '') else str(raw_data.get('event_id') or '')
        if not event_id:
            event_id = f'{message.chat_type}:{message.chat_id}:{message.user_id}:{message.timestamp}'
        occurred_at = _safe_occurred_at(message.timestamp, 'message timestamp')
    else:
        if kind == 'message':
            raise ValueError('message scope turn item must include message')
        if 'scope_key' not in item:
            raise ValueError(f'{kind} scope turn item must include scope_key')
        scope_type, scope_id = _scope_parts(item['scope_key'])
        item_data = {
            key: deepcopy(value)
            for key, value in item.items()
            if key not in {'kind', 'scope_key'} | _TRANSIENT_TURN_FIELDS
        }
        payload = {'kind': kind, 'item': item_data}
        event_type = EventType.RECURRING_TASK if kind == 'task' else EventType.AGENT_REPORT
        source = str(item.get('source') or kind)
        event_id_value = item.get('event_id') or item.get('task_id') or item.get('report_id')
        event_id = str(event_id_value) if event_id_value not in (None, '') else f'{kind}:{scope_type}:{scope_id}'
        occurred_at = _safe_occurred_at(item.get('timestamp', item.get('ts')), f'{kind} timestamp')

    return EventEnvelope(
        event_type=event_type,
        scope_type=scope_type,
        scope_id=scope_id,
        payload=payload,
        source=source,
        event_id=event_id,
        occurred_at=occurred_at,
    )


def scope_turn_item_from_envelope(event: EventEnvelope) -> dict[str, Any]:
    """Restore one existing pending turn item from its mailbox envelope."""
    if not isinstance(event, EventEnvelope):
        raise TypeError('event must be an EventEnvelope')
    data = _require_mapping(event.payload, 'event payload')
    kind = _turn_kind(data)
    message_data = data.get('message')
    if message_data is None:
        item_data = _require_mapping(data.get('item'), 'event payload item')
        restored = {'kind': kind, **deepcopy(dict(item_data)), 'scope_key': event.scope_key}
    else:
        message_data = _require_mapping(message_data, 'event payload message')
        message = ChatMessage(**deepcopy(dict(message_data)))
        restored = {
            'kind': kind,
            'message': message,
            'cleaned': deepcopy(data.get('cleaned', '')),
            'agent_id': deepcopy(data.get('agent_id', '')),
            'deferred_count': deepcopy(data.get('deferred_count', 0)),
            'trigger_messages': deepcopy(data.get('trigger_messages', [])),
            'message_epoch': deepcopy(data.get('message_epoch')),
            'history_seed': deepcopy(data.get('history_seed')),
            'metadata': deepcopy(data.get('metadata')),
            'silent_event': bool(data.get('silent_event', False)),
            'scope_key': event.scope_key,
        }
    restored['mailbox_event_ids'] = [event.event_id]
    restored['mailbox_sequences'] = [event.mailbox_sequence]
    return restored


def scope_turn_item_from_batch(batch: EventBatch) -> dict[str, Any]:
    """Merge a drain while exposing exact FIFO-decoded items for mixed batches."""
    if not isinstance(batch, EventBatch):
        raise TypeError('batch must be an EventBatch')
    items = [scope_turn_item_from_envelope(event) for event in batch.events]
    result = deepcopy(items[-1])
    trigger_messages = []
    deferred_count = 0
    history_seed = None
    for item in items:
        entries = item.get('trigger_messages') or []
        if entries:
            trigger_messages.extend(deepcopy(entries))
        elif isinstance(item.get('message'), ChatMessage):
            message = item['message']
            trigger_messages.append({
                'text': deepcopy(item.get('cleaned') or message.text),
                'raw_message': deepcopy(message.raw_message),
            })
        deferred_count += max(1, int(item.get('deferred_count') or 0))
        if history_seed is None and item.get('history_seed') is not None:
            history_seed = deepcopy(item['history_seed'])
    result.update({
        'deferred_count': deferred_count,
        'trigger_messages': trigger_messages,
        'history_seed': history_seed,
        'scope_key': batch.scope_key,
        'mailbox_event_ids': [event.event_id for event in batch.events],
        'mailbox_sequences': [event.mailbox_sequence for event in batch.events],
        'batch_items': items,
    })
    return result
