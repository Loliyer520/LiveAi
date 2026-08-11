from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from core.events import ChatMessage


ScopeActivePredicate = Callable[[str], bool]
ReportDelivery = Callable[[str, str, list[dict[str, Any]]], None]


class AgentReportDeliveryService:
    """Group pending agent reports and prepare scoped system messages."""

    def __init__(self, fallback_scope_type: str = 'master', fallback_scope_id: str = '0') -> None:
        self.fallback_scope_type = str(fallback_scope_type)
        self.fallback_scope_id = str(fallback_scope_id)

    def flush(
        self,
        manager,
        *,
        is_scope_active: ScopeActivePredicate,
        deliver: ReportDelivery,
        only_if_idle: bool = True,
    ) -> None:
        if manager is None or not manager.has_pending_reports():
            return
        reports = manager.drain_pending_reports()
        if not reports:
            return

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in reports:
            scope = self.parse_scope(item.get('origin_scope'))
            grouped.setdefault(scope, []).append(item)

        deferred: list[dict[str, Any]] = []
        for (scope_type, scope_id), items in grouped.items():
            scope_key = f'{scope_type}:{scope_id}'
            urgent_items = [item for item in items if item.get('urgent')]
            normal_items = [item for item in items if not item.get('urgent')]
            if urgent_items:
                deliver(scope_type, scope_id, urgent_items)
            if normal_items:
                if only_if_idle and is_scope_active(scope_key):
                    deferred.extend(normal_items)
                else:
                    deliver(scope_type, scope_id, normal_items)

        if deferred:
            manager.requeue_pending_reports(deferred)

    def parse_scope(self, origin_scope: Any) -> tuple[str, str]:
        raw = str(origin_scope or '').strip()
        if not raw or ':' not in raw:
            return self.fallback_scope_type, self.fallback_scope_id
        scope_type, _, scope_id = raw.partition(':')
        scope_type = scope_type.strip()
        scope_id = scope_id.strip()
        if not scope_type or not scope_id:
            return self.fallback_scope_type, self.fallback_scope_id
        return scope_type, scope_id

    def build_message(
        self,
        scope_type: str,
        scope_id: str,
        items: list[dict[str, Any]],
    ) -> ChatMessage | None:
        if not items:
            return None
        lines = [
            f"【agent#{str(item.get('agent_id') or '?')}】\n{str(item.get('text') or '').strip()}"
            for item in items
        ]
        body = '\n\n'.join(lines)
        wrapped = (
            '【内部系统通知：以下是后台常驻 agent 的挂起内容（提问/汇报/进度/阶段复核），不是任何人直接对你说的话，'
            '仅供你参考决策。若内容标记“阶段复核请求”，请检查其任务方向、自检和未完成项；确认正常可通过 '
            'send_to_agent 给对应 agent 追加“继续”，需要纠偏则直接追加纠偏指令。每段以【agent#编号】标注来源。】\n\n'
            f'{body}'
        )
        try:
            chat_id = 0 if scope_type == 'master' else int(scope_id)
        except (TypeError, ValueError):
            scope_type = self.fallback_scope_type
            chat_id = int(self.fallback_scope_id)
        return ChatMessage(
            chat_type=scope_type,
            chat_id=chat_id,
            user_id=0,
            text=wrapped,
            raw_message=wrapped,
            sender={'nickname': '常驻agent系统', 'user_id': 0},
            message_id=None,
            mentions_self=True,
            timestamp=time.time(),
            raw_data={
                'source': 'agent_message',
                'system_event': 'agent_message',
                'agent_count': len(items),
            },
        )
