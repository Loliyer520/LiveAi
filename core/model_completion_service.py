from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from core.async_execution import AsyncExecutionPool
from pack.anthropic_chat_model import AnthropicChatModel, AnthropicReply


ModelProvider = Callable[[], AnthropicChatModel]


@dataclass(frozen=True)
class ModelRequestSnapshot:
    client: AnthropicChatModel
    model_name: str
    api_url: str


class ModelCompletionService:
    """Execute one model request against a stable client snapshot."""

    def __init__(
        self,
        *,
        get_client: ModelProvider,
        default_pool: AsyncExecutionPool,
    ) -> None:
        if not callable(get_client):
            raise TypeError('get_client must be callable')
        if not isinstance(default_pool, AsyncExecutionPool):
            raise TypeError('default_pool must be an AsyncExecutionPool')
        self._get_client = get_client
        self._default_pool = default_pool

    def snapshot(self) -> ModelRequestSnapshot:
        client = self._get_client()
        if not isinstance(client, AnthropicChatModel):
            raise TypeError('model provider must return an AnthropicChatModel')
        return ModelRequestSnapshot(
            client=client,
            model_name=client.model_name,
            api_url=f'{client.base_url}{client.messages_path}',
        )

    async def complete(
        self,
        snapshot: ModelRequestSnapshot,
        system_blocks: list[dict],
        messages: list[dict],
        tools: list[dict] | None,
        temperature: float,
        *,
        thinking: str = 'off',
        execution_pool: AsyncExecutionPool | None = None,
    ) -> AnthropicReply | None:
        if not isinstance(snapshot, ModelRequestSnapshot):
            raise TypeError('snapshot must be a ModelRequestSnapshot')
        pool = execution_pool or self._default_pool
        return await pool.run(
            snapshot.client.complete,
            system_blocks,
            messages,
            tools,
            snapshot.model_name,
            temperature,
            thinking=thinking,
        )
