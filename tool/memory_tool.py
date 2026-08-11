from core.ai_repository import AIRepository


class MemoryTool:
    def __init__(self, repo: AIRepository):
        self.repo = repo

    def remember(self, scope_type: str, scope_id: str, note: str) -> dict | None:
        return self.repo.add_note(scope_type, scope_id, note)

    def recall(self, scope_type: str, scope_id: str) -> list[dict]:
        return self.repo.list_notes(scope_type, scope_id)

    def recall_one(self, scope_type: str, scope_id: str, note_id: str) -> dict | None:
        return self.repo.get_note(scope_type, scope_id, note_id)

    def rewrite_memory(self, scope_type: str, scope_id: str, note_id: str, content: str) -> dict | None:
        return self.repo.update_note(scope_type, scope_id, note_id, content)

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
        return self.repo.add_tool_log(
            scope_type,
            scope_id,
            agent_id,
            tool_name,
            tool_input,
            tool_result,
            limit=limit,
        )

    def list_tool_uses(self, scope_type: str, scope_id: str) -> list[dict]:
        return self.repo.list_tool_logs(scope_type, scope_id)
