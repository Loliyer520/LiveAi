from __future__ import annotations

import time
from typing import Any, Mapping

from core.events import ChatMessage


class TimedEventMessageFactory:
    def build_alarm_message(
        self,
        payload: Mapping[str, Any],
        *,
        task_id: str,
        text: str,
        occurred_at: float | None = None,
    ) -> ChatMessage | None:
        scope = self._scope_from_parts(
            payload.get('scope_type'),
            payload.get('scope_id'),
        )
        if scope is None:
            return None
        scope_type, chat_id = scope
        return ChatMessage(
            chat_type=scope_type,
            chat_id=chat_id,
            user_id=0,
            text=str(text),
            raw_message=str(text),
            sender={'nickname': '闹钟系统', 'user_id': 0},
            message_id=None,
            mentions_self=True,
            timestamp=time.time() if occurred_at is None else float(occurred_at),
            raw_data={
                'source': 'alarm',
                'system_event': 'alarm',
                'task_id': str(task_id),
            },
        )

    def build_recurring_message(
        self,
        task: Mapping[str, Any],
        *,
        occurred_at: float | None = None,
    ) -> ChatMessage | None:
        target_scope = str(
            task.get('target_scope')
            or task.get('creator_scope')
            or ''
        ).strip()
        scope_type, separator, scope_id = target_scope.partition(':')
        if not separator:
            return None
        scope = self._scope_from_parts(scope_type, scope_id)
        if scope is None:
            return None
        scope_type, chat_id = scope
        instruction = str(task.get('instruction') or '').strip()
        return ChatMessage(
            chat_type=scope_type,
            chat_id=chat_id,
            user_id=0,
            text=f'[循环任务触发] {instruction}',
            raw_message='',
            sender={'nickname': '循环任务', 'user_id': 0},
            message_id=None,
            mentions_self=True,
            timestamp=time.time() if occurred_at is None else float(occurred_at),
            raw_data={
                'source': 'recurring_task',
                'system_event': 'recurring_task',
                'task_id': str(task.get('id') or ''),
            },
        )

    def build_silence_report_message(
        self,
        scope_type: str,
        scope_id: str,
        *,
        text: str,
        occurred_at: float | None = None,
    ) -> ChatMessage | None:
        scope = self._scope_from_parts(scope_type, scope_id)
        if scope is None:
            return None
        chat_type, chat_id = scope
        return ChatMessage(
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=0,
            text=str(text),
            raw_message=str(text),
            sender={'nickname': '情报巡检', 'user_id': 0},
            message_id=None,
            mentions_self=True,
            timestamp=time.time() if occurred_at is None else float(occurred_at),
            raw_data={
                'source': 'silence_report',
                'system_event': 'silence_report',
            },
        )

    @staticmethod
    def _scope_from_parts(
        scope_type: Any,
        scope_id: Any,
    ) -> tuple[str, int] | None:
        normalized_type = str(scope_type or '').strip()
        normalized_id = (
            '' if scope_id is None else str(scope_id).strip()
        )
        if not normalized_type or not normalized_id:
            return None
        if normalized_type == 'master':
            return normalized_type, 0
        try:
            return normalized_type, int(normalized_id)
        except (TypeError, ValueError):
            return None
