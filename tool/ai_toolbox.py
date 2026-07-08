from pack.napcat import NapcatBot
from core.ai_repository import AIRepository
from tool.contact_tool import ContactTool
from tool.memory_tool import MemoryTool
from tool.task_tool import TaskTool


class AIToolbox:
    def __init__(self, bot: NapcatBot, repo: AIRepository):
        self.contact = ContactTool(bot)
        self.memory = MemoryTool(repo)
        self.task = TaskTool(repo)

    def get_group_list(self) -> list[dict]:
        return self.contact.get_group_list()

    def get_friend_list(self) -> list[dict]:
        return self.contact.get_friend_list()

    def remember(self, scope_type: str, scope_id: str, note: str) -> dict | None:
        return self.memory.remember(scope_type, scope_id, note)

    def recall(self, scope_type: str, scope_id: str) -> list[dict]:
        return self.memory.recall(scope_type, scope_id)

    def recall_one(self, scope_type: str, scope_id: str, note_id: str) -> dict | None:
        return self.memory.recall_one(scope_type, scope_id, note_id)

    def rewrite_memory(self, scope_type: str, scope_id: str, note_id: str, content: str) -> dict | None:
        return self.memory.rewrite_memory(scope_type, scope_id, note_id, content)

    def record_tool_use(
        self,
        scope_type: str,
        scope_id: str,
        agent_id: str,
        tool_name: str,
        tool_input: str,
        tool_result: str,
        limit: int = 500,
    ) -> dict:
        return self.memory.record_tool_use(
            scope_type,
            scope_id,
            agent_id,
            tool_name,
            tool_input,
            tool_result,
            limit=limit,
        )

    def list_tool_uses(self, scope_type: str, scope_id: str) -> list[dict]:
        return self.memory.list_tool_uses(scope_type, scope_id)

    def create_task(self, source_agent: str, kind: str, payload: dict):
        return self.task.create_task(source_agent, kind, payload)

    def send_private_message(self, user_id: int, content: str):
        return self.contact.send_private_message(user_id, content)

    def send_chat_message(self, chat_type: str, target_id: int, content: str):
        return self.contact.send_chat_message(chat_type, target_id, content)
