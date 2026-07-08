from core.ai_repository import AIRepository


class TaskTool:
    def __init__(self, repo: AIRepository):
        self.repo = repo

    def create_task(self, source_agent: str, kind: str, payload: dict):
        return self.repo.create_task(source_agent, kind, payload)
