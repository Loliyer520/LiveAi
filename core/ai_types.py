from dataclasses import asdict, dataclass, field
import time

from core.prompt_store import default_char_prompt


@dataclass
class AgentProfile:
    agent_id: str
    scope_type: str
    scope_id: str
    role: str
    persona: str = field(default_factory=default_char_prompt)
    trigger_words: list[str] = field(default_factory=lambda: ['ai', '机器人', '冰糖', '砂糖'])
    trigger_rate: float = 0.08
    notes: list[str] = field(default_factory=list)
    impression: str = ''
    impression_updated_at: float = 0.0
    display_name: str = ''
    message_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PendingTask:
    task_id: str
    source_agent: str
    kind: str
    payload: dict
    status: str = 'queued'
    result: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)
