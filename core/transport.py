from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
import uuid

from core.events import ChatMessage


@dataclass(frozen=True)
class ActionEnvelope:
    """Transport-neutral NapCat action request.

    The legacy adapter executes this synchronously.  A later durable outbox can
    persist the same envelope without changing callers in the business layer.
    """

    action_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    scope_type: str | None = None
    scope_id: int | None = None
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class NapcatActionTransport(Protocol):
    """Business-facing NapCat action/query boundary."""

    @property
    def self_id(self) -> int: ...

    def execute(self, action: ActionEnvelope) -> Any: ...
    def send_text(self, chat_type: str, target_id: int, message: str): ...
    def send_group_text(self, group_id: int, message: str): ...
    def send_private_text(self, user_id: int, message: str): ...
    def send_image(self, chat_type: str, target_id: int, file: str, text: str | None = None): ...
    def send_mface(
        self,
        chat_type: str,
        target_id: int,
        emoji_id: str,
        emoji_package_id: str = '',
        key: str = '',
        summary: str | None = None,
    ): ...
    def send_record(self, chat_type: str, target_id: int, file: str): ...
    def send_file(self, chat_type: str, target_id: int, file: str, name: str | None = None): ...
    def recall_message(self, message_id, scope_type: str | None = None, scope_id: int | None = None): ...
    def get_file(self, file_id: str) -> dict: ...
    def fetch_custom_face(self, count: int = 48) -> list[dict[str, str]]: ...
    def get_group_list(self) -> list[dict]: ...
    def get_friend_list(self) -> list[dict]: ...
    def get_stranger_info(self, user_id: int) -> dict: ...
    def get_group_info(self, group_id: int) -> dict: ...
    def get_group_member_list(self, group_id: int) -> list[dict]: ...
    def get_group_member_info(self, group_id: int, user_id: int, no_cache: bool = False) -> dict: ...
    def set_group_ban(self, group_id: int, user_id: int, duration: int) -> dict: ...
    def set_group_whole_ban(self, group_id: int, enable: bool) -> dict: ...
    def request_friend_add(self, user_id: int, comment: str = '') -> dict: ...
    def get_friend_requests(self, count: int = 50) -> dict: ...
    def set_friend_add_request(self, flag: str, approve: bool, remark: str = '') -> dict: ...
    def request_group_join(self, group_id: int, comment: str = '') -> dict: ...
    def get_group_requests(self, count: int = 50) -> dict: ...
    def set_group_add_request(self, flag: str, sub_type: str, approve: bool, reason: str = '') -> dict: ...
    def at(self, user_id: int) -> str: ...


class NapcatEventSource(Protocol):
    """Inbound callback registration boundary; kept separate from actions."""

    def on_group_message(self, callback: Callable[[ChatMessage], None]): ...
    def on_private_message(self, callback: Callable[[ChatMessage], None]): ...
    def on_self_message(self, callback: Callable[[ChatMessage], None]): ...
    def on_group_increase(self, callback: Callable[[Any], None]): ...


class LegacyNapcatTransport:
    """Compatibility adapter around the current in-process ``NapcatBot``.

    It intentionally preserves synchronous return values and exceptions.  The
    only new behavior is representing every outbound/query operation as an
    ``ActionEnvelope`` before immediately dispatching it to the legacy bot.
    """

    def __init__(self, bot: Any):
        self._bot = bot

    @property
    def self_id(self) -> int:
        return self._bot.self_id

    def execute(self, action: ActionEnvelope) -> Any:
        method = getattr(self._bot, action.action_type)
        return method(**dict(action.payload))

    def _execute(self, action_type: str, payload: dict[str, Any], scope_type: str | None = None, scope_id: int | None = None):
        return self.execute(ActionEnvelope(action_type, payload, scope_type, scope_id))

    def send_text(self, chat_type: str, target_id: int, message: str):
        return self._execute('send_text', {'chat_type': chat_type, 'target_id': target_id, 'message': message}, chat_type, target_id)

    def send_group_text(self, group_id: int, message: str):
        return self.send_text('group', group_id, message)

    def send_private_text(self, user_id: int, message: str):
        return self.send_text('private', user_id, message)

    def send_image(self, chat_type: str, target_id: int, file: str, text: str | None = None):
        return self._execute('send_image', {'chat_type': chat_type, 'target_id': target_id, 'file': file, 'text': text}, chat_type, target_id)

    def send_mface(
        self,
        chat_type: str,
        target_id: int,
        emoji_id: str,
        emoji_package_id: str = '',
        key: str = '',
        summary: str | None = None,
    ):
        return self._execute(
            'send_mface',
            {
                'chat_type': chat_type,
                'target_id': target_id,
                'emoji_id': emoji_id,
                'emoji_package_id': emoji_package_id,
                'key': key,
                'summary': summary,
            },
            chat_type,
            target_id,
        )

    def send_record(self, chat_type: str, target_id: int, file: str):
        return self._execute('send_record', {'chat_type': chat_type, 'target_id': target_id, 'file': file}, chat_type, target_id)

    def send_file(self, chat_type: str, target_id: int, file: str, name: str | None = None):
        return self._execute('send_file', {'chat_type': chat_type, 'target_id': target_id, 'file': file, 'name': name}, chat_type, target_id)

    def recall_message(self, message_id, scope_type: str | None = None, scope_id: int | None = None):
        return self._execute('recall_message', {'message_id': message_id})

    def get_file(self, file_id: str) -> dict:
        return self._execute('get_file', {'file_id': file_id})

    def fetch_custom_face(self, count: int = 48) -> list[dict[str, str]]:
        return self._execute('fetch_custom_face', {'count': count})

    def get_group_list(self) -> list[dict]:
        return self._execute('get_group_list', {})

    def get_friend_list(self) -> list[dict]:
        return self._execute('get_friend_list', {})

    def get_stranger_info(self, user_id: int) -> dict:
        return self._execute('get_stranger_info', {'user_id': user_id}, 'private', user_id)

    def get_group_info(self, group_id: int) -> dict:
        return self._execute('get_group_info', {'group_id': group_id}, 'group', group_id)

    def get_group_member_list(self, group_id: int) -> list[dict]:
        return self._execute('get_group_member_list', {'group_id': group_id}, 'group', group_id)

    def get_group_member_info(self, group_id: int, user_id: int, no_cache: bool = False) -> dict:
        return self._execute('get_group_member_info', {'group_id': group_id, 'user_id': user_id, 'no_cache': no_cache}, 'group', group_id)

    def set_group_ban(self, group_id: int, user_id: int, duration: int) -> dict:
        return self._execute('set_group_ban', {'group_id': group_id, 'user_id': user_id, 'duration': duration}, 'group', group_id)

    def set_group_whole_ban(self, group_id: int, enable: bool) -> dict:
        return self._execute('set_group_whole_ban', {'group_id': group_id, 'enable': enable}, 'group', group_id)

    def request_friend_add(self, user_id: int, comment: str = '') -> dict:
        return self._execute('request_friend_add', {'user_id': user_id, 'comment': comment}, 'private', user_id)

    def get_friend_requests(self, count: int = 50) -> dict:
        return self._execute('get_friend_requests', {'count': count})

    def set_friend_add_request(self, flag: str, approve: bool, remark: str = '') -> dict:
        return self._execute('set_friend_add_request', {'flag': flag, 'approve': approve, 'remark': remark})

    def request_group_join(self, group_id: int, comment: str = '') -> dict:
        return self._execute('request_group_join', {'group_id': group_id, 'comment': comment}, 'group', group_id)

    def get_group_requests(self, count: int = 50) -> dict:
        return self._execute('get_group_requests', {'count': count})

    def set_group_add_request(self, flag: str, sub_type: str, approve: bool, reason: str = '') -> dict:
        return self._execute('set_group_add_request', {'flag': flag, 'sub_type': sub_type, 'approve': approve, 'reason': reason})

    @staticmethod
    def at(user_id: int) -> str:
        return f'[CQ:at,qq={user_id}]'

    # Inbound registration remains delegated during the legacy single-process stage.
    def on_group_message(self, callback):
        return self._bot.on_group_message(callback)

    def on_private_message(self, callback):
        return self._bot.on_private_message(callback)

    def on_self_message(self, callback):
        return self._bot.on_self_message(callback)

    def on_group_increase(self, callback):
        return self._bot.on_group_increase(callback)

