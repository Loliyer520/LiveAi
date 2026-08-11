from dataclasses import dataclass, field
from typing import Any
import time


@dataclass
class ChatMessage:
    chat_type: str
    chat_id: int
    user_id: int
    text: str
    raw_message: str
    sender: dict[str, Any]
    message_id: int | None = None
    mentions_self: bool = False
    timestamp: float = field(default_factory=time.time)
    raw_data: dict[str, Any] = field(default_factory=dict)

    @property
    def nickname(self) -> str:
        return str(self.sender.get('nickname') or self.sender.get('card') or self.user_id)


@dataclass
class GroupIncreaseEvent:
    group_id: int
    user_id: int
    sub_type: str | None
    raw_data: dict[str, Any]
