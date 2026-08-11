from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import time
from typing import Any


@dataclass(frozen=True)
class NormalizedEvent:
    source_key: str
    event_type: str
    lane: str
    scope_key: str | None
    scope_type: str | None
    scope_id: str | None
    payload: dict[str, Any]
    ignored: bool = False
    ignore_reason: str | None = None


def normalize_message_id(message_id: Any) -> int | str | None:
    if message_id is None:
        return None
    try:
        return int(message_id)
    except (TypeError, ValueError):
        return str(message_id)


def canonical_content_key(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for segment in message:
            if not isinstance(segment, dict):
                parts.append(str(segment))
                continue
            seg_type = str(segment.get('type') or '')
            data = segment.get('data') or {}
            if seg_type == 'text':
                parts.append(f"text:{data.get('text', '')}")
            elif seg_type == 'image':
                parts.append(f"image:{data.get('file', '')}")
            elif seg_type == 'reply':
                parts.append(f"reply:{data.get('id', '')}")
            elif seg_type == 'at':
                parts.append(f"at:{data.get('qq', '')}")
            else:
                parts.append(f'{seg_type}:{json.dumps(data, sort_keys=True, ensure_ascii=False)}')
        return '|'.join(parts)
    return str(message) if message is not None else ''


def content_digest(message: Any) -> str:
    return hashlib.sha1(canonical_content_key(message).encode('utf-8', 'ignore')).hexdigest()


def scope_for(chat_type: str | None, target_id: Any) -> tuple[str | None, str | None, str | None]:
    if chat_type not in {'group', 'private'} or target_id in (None, ''):
        return None, None, None
    scope_id = str(target_id)
    return f'{chat_type}:{scope_id}', chat_type, scope_id


def message_mentions_self(data: dict[str, Any], self_ids: set[str]) -> bool:
    raw_message = str(data.get('raw_message') or '')
    for qq in re.findall(r'\[CQ:at,qq=(\d+)(?:,[^\]]*)?\]', raw_message):
        if qq in self_ids:
            return True
    segments = data.get('message') or []
    if isinstance(segments, list):
        return any(
            isinstance(segment, dict)
            and str(segment.get('type') or '') == 'at'
            and str((segment.get('data') or {}).get('qq') or '') in self_ids
            for segment in segments
        )
    return False


def event_self_ids(data: dict[str, Any], self_id: int | str) -> set[str]:
    ids = {str(self_id)}
    event_self_id = data.get('self_id')
    if event_self_id not in {None, ''}:
        ids.add(str(event_self_id))
    return {value for value in ids if value}


def derive_source_key(data: dict[str, Any]) -> str:
    for key in ('message_id', 'event_id', 'notice_id', 'request_id'):
        value = data.get(key)
        if value not in (None, ''):
            return f'{data.get("post_type", "event")}:{key}:{value}'
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'sha256:' + hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def normalize_ws_event(data: dict[str, Any], self_id: int | str) -> NormalizedEvent | None:
    if not isinstance(data, dict):
        return None
    if data.get('meta_event_type') in {'heartbeat', 'lifecycle'}:
        return None

    post_type = data.get('post_type')
    source_key = derive_source_key(data)
    now = time.time()
    payload = dict(data)
    payload['_normalized_at'] = now
    self_ids = event_self_ids(data, self_id)

    if post_type == 'message':
        message_type = data.get('message_type')
        scope, scope_type, scope_id = scope_for(
            message_type, data.get('group_id') if message_type == 'group' else data.get('user_id')
        )
        if message_type not in {'group', 'private'}:
            return None
        return NormalizedEvent(source_key, f'{message_type}_message', 'user', scope,
                               scope_type, scope_id, payload,
                               ignored=message_mentions_self(data, self_ids),
                               ignore_reason='mentions_self' if message_mentions_self(data, self_ids) else None)

    if post_type == 'message_sent':
        message_type = data.get('message_type')
        target_id = data.get('group_id') if message_type == 'group' else data.get('target_id') or data.get('user_id')
        scope, scope_type, scope_id = scope_for(message_type, target_id)
        if message_type not in {'group', 'private'}:
            return None
        return NormalizedEvent(source_key, 'self_message', 'self', scope, scope_type, scope_id, payload)

    if post_type == 'notice' and data.get('notice_type') == 'group_increase':
        scope, scope_type, scope_id = scope_for('group', data.get('group_id'))
        return NormalizedEvent(source_key, 'group_increase', 'system', scope, scope_type, scope_id, payload)

    if post_type == 'request':
        request_type = str(data.get('request_type') or 'unknown')
        scope, scope_type, scope_id = scope_for(
            'group' if request_type == 'group' else 'private',
            data.get('group_id') if request_type == 'group' else data.get('user_id'),
        )
        return NormalizedEvent(source_key, 'request', 'request', scope, scope_type, scope_id, payload)
    return None
